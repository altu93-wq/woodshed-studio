#!/usr/bin/env python3
"""Woodshed Studio: one web app over a PDF library index.
Search-first homepage + Topic Graph + Drop folder + OCR queue + Health.
Stdlib HTTP server. Production rules:
  - store.py owns the paths (library root / index.db / inbox / trash, all
    overridable by WOOD_* env vars or config.json) and the single-writer
    connection to it (WAL + busy_timeout keep the index readable while we write);
  - readers use mode=ro;
  - the inbox is the drop folder — drop/copy PDFs there or drag-and-drop in the
    UI; "Scan library" additionally walks the whole tree, so an archive that
    was never reorganised indexes as-is. The book list spans every indexed
    folder in the library.
  py -3 server.py  ->  http://localhost:8766
"""
import os, re, json, time, sqlite3, threading, socket, shutil, base64, hmac
import queue as Queue
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

import store, ingest, ocr_worker, search_api, vectors, titles

HERE = os.path.dirname(os.path.abspath(__file__))
INBOX = store.INBOX                           # real drop folder
HTML = os.path.join(HERE, "studio.html")
VIEWER = os.path.join(HERE, "viewer.html")
VENDOR = os.path.join(HERE, "vendor")
LOGO = os.path.join(HERE, "logo.png")         # wordmark + favicon

# One version string for the whole app. The banner prints it, the HTTP server
# answers with it, and studio.html carries the placeholder __VERSION__, which
# _page() swaps in as the file is served - so bumping the release is this line.
VERSION = "2.0"
LOGDIR = os.path.join(HERE, "logs")
PORT = 8766

EVENTS = []          # (seq, line) ring buffer for SSE, newest 300 kept
EV_SEQ = 0           # monotonic; never reused, so truncation cannot lose a reader
EV_COND = threading.Condition()
writer = None
ocr = None

def lib_audit():
    """Info line about the library DB we serve (single source of truth)."""
    try:
        st = os.stat(store.DB)
        return {"lib_size": st.st_size,
                "lib_mtime": time.strftime("%Y-%m-%d %H:%M:%S",
                                           time.localtime(st.st_mtime))}
    except OSError as e:
        return {"lib_size": -1, "lib_mtime": str(e)}

AUDIT0 = None

def emit(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(os.path.join(LOGDIR, "studio.log"), "a",
                  encoding="utf-8", errors="replace") as f:
            f.write(line + "\n")
    except OSError:
        pass
    global EV_SEQ
    with EV_COND:
        EV_SEQ += 1
        EVENTS.append((EV_SEQ, line))
        del EVENTS[:-300]
        EV_COND.notify_all()

def normpath(p):
    r"""Canonical key for a filesystem path.

    The library can contain the same book twice for `D:\Books\x.pdf` and
    `d:\Books\x.pdf`; without this the scanner thinks the file is new and
    indexes it a second time, which also duplicates search results.
    """
    try:
        return os.path.normcase(os.path.abspath(p)) if p else ""
    except (OSError, ValueError):
        return (p or "").lower()

def on_event(status, title, detail, path=None):
    emit(f"{status:12} {title[:50]}  {detail[:80]}")
    if status == "idle":
        # the OCR queue's "nothing running" heartbeat: a UI event, not a job.
        # Persisting it planted a '-' sentinel row that Health then counted as
        # an active job forever.
        return
    def _up(c):
        if path:
            # match on the path first — title-only matching created a second row
            # (path = title) whenever an event arrived before the folder scan
            r = c.execute("SELECT id FROM jobs WHERE path=? COLLATE NOCASE",
                          (path,)).fetchone()
            if not r:
                r = c.execute("SELECT id FROM jobs WHERE title=? AND path=? "
                              "COLLATE NOCASE", (title, title)).fetchone()
            if r:
                c.execute("UPDATE jobs SET status=?, detail=?, updated=? WHERE id=?",
                          (status, detail, time.time(), r[0]))
                return
            c.execute("INSERT INTO jobs(path,title,status,detail,updated)"
                      " VALUES(?,?,?,?,?)", (path, title, status, detail, time.time()))
            return
        r = c.execute("SELECT id FROM jobs WHERE title=?", (title,)).fetchone()
        if r:
            c.execute("UPDATE jobs SET status=?, detail=?, updated=? WHERE id=?",
                      (status, detail, time.time(), r[0]))
        # no path and no existing row: don't invent one (used to store `title`
        # as `path`, which is what produced the bogus duplicate rows)
    try:
        writer.submit(_up)
    except Exception:
        pass

def indexed_paths():
    try:
        c = store.ro()
        rows = {normpath(r[0]) for r in c.execute("SELECT path FROM sources")}
        c.close()
        return rows
    except Exception:
        return set()

def dup_key(path):
    """Group key for "book.pdf" and "book (1).pdf" — the same file twice.

    The folder is part of the key: once the list spans the whole library, two
    different folders can legitimately hold books with the same title.
    """
    d, base = os.path.split(path or "")
    base = os.path.splitext(base)[0]
    return (normpath(d) + os.sep +
            re.sub(r"\s*\(\d+\)$", "", base).strip().casefold())

def ignored_paths():
    """Files the user merged away: extra copies that rescan must not re-add."""
    try:
        c = store.ro()
        rows = {normpath(r[0]) for r in c.execute(
            "SELECT path FROM jobs WHERE status='duplicate' AND path")}
        c.close()
        return rows
    except Exception:
        return set()

def _new_pdfs(paths):
    """Filter a candidate list down to the PDFs not indexed and not merged away."""
    done, skip = indexed_paths(), ignored_paths()
    return [p for p in paths
            if not ingest.ignorable(p) and normpath(p) not in done
            and normpath(p) not in skip]


def _queue_paths(fresh, parallel=True, done=None):
    """Register a 'queued' job row per file, then hand each to a worker.

    `parallel=False` feeds the single background queue instead of one thread per
    file: a whole-library scan can be hundreds of books, and hundreds of
    concurrent PyMuPDF extractions would thrash the disk and the FTS writer.

    `done` is called once that queue has drained, which is what releases the
    scan lock - it must not be released when this function returns.
    """
    fresh = sorted(fresh, key=lambda p: os.path.getsize(p) if os.path.exists(p) else 0)
    for p in fresh:
        emit(f"queued       {os.path.basename(p)[:50]}")
        def _q(c, _p=p):
            title = os.path.splitext(os.path.basename(_p))[0]
            c.execute("INSERT OR IGNORE INTO jobs(path,title,status,detail,updated)"
                      " VALUES(?,?,?,?,?)", (_p, title, "queued", "waiting", time.time()))
        writer.submit(_q)
    if parallel:
        for p in fresh:
            threading.Thread(target=process_one, args=(p,), daemon=True).start()
    else:
        threading.Thread(target=_index_queue, args=(fresh, done),
                         daemon=True).start()
    return fresh


def scan_inbox():
    """Every new PDF in the drop folder."""
    found = []
    for dirpath, _, files in os.walk(INBOX):
        for f in files:
            found.append(os.path.abspath(os.path.join(dirpath, f)))
    return _queue_paths(_new_pdfs(found))


def scan_library(root=None):
    """Every new PDF in the whole library tree.

    This is what makes the app archive-agnostic: books never have to be moved
    into the drop folder first. The walk skips the app folder, the database
    folder and the trash (store.SKIP_DIRS) so it cannot index its own files.
    """
    root = root or store.ROOT
    emit(f"scan library {root}")
    allp = ingest.walk_library(root)
    fresh = _new_pdfs(allp)
    emit(f"scan library {len(allp)} pdf(s) found, {len(fresh)} not indexed yet")
    return _queue_paths(fresh, parallel=False)


SCAN = {"running": False, "total": 0, "queued": 0, "root": ""}


def scan_library_bg(root=None):
    """Walk + queue on a background thread so the UI stays responsive."""
    if SCAN["running"]:
        return False
    SCAN.update(running=True, total=0, queued=0, root=root or store.ROOT)

    def _run():
        try:
            root0 = SCAN["root"]
            allp = ingest.walk_library(root0)
            SCAN["total"] = len(allp)
            fresh = _new_pdfs(allp)
            SCAN["queued"] = len(fresh)
            emit(f"scan library {len(allp)} pdf(s) in {root0}, "
                 f"{len(fresh)} not indexed yet")
            _queue_paths(fresh, parallel=False, done=lambda: _finish_scan())
        except Exception as e:
            emit(f"scan FAILED  {e}")
            _finish_scan()
    threading.Thread(target=_run, daemon=True).start()
    return True


def _finish_scan():
    """Release the scan lock - but only when the queue has actually drained.

    `SCAN['running']` used to be cleared as soon as the tree walk returned,
    while hundreds of books were still being indexed. A second
    "Scan whole library" then passed the guard, built its own list from the
    same not-yet-indexed files, and two `_index_queue` threads worked the
    same paths: every book in the overlap was indexed twice, giving duplicate
    `sources` rows and duplicate `pages` rows (239 books on one 440-book
    import). The flag has to mean "a scan is in flight", not "a walk is".
    """
    SCAN["running"] = False

def _index_queue(paths, done=None):
    """Index paths one after another on a single background thread."""
    try:
        for p in paths:
            try:
                process_one(p)
            except Exception as e:        # one bad file must not stop the queue
                emit(f"bulk index failed  {os.path.basename(p)[:50]}  {e}")
    finally:
        if done:
            done()


_CLAIMED = set()
_CLAIM_LOCK = threading.Lock()


def _claim(path):
    """Take exclusive ownership of a path for this process.

    `process_one` checks `indexed_paths()` and then does the work, with a
    long gap in between. Two threads - a scan queue and a re-index click, or
    two scans started before the lock existed - both pass that check before
    either writes, and both insert. The claim is in-process and short-lived:
    taken before the first check, released when the book is written.
    """
    k = normpath(os.path.abspath(path))
    with _CLAIM_LOCK:
        if k in _CLAIMED:
            return None
        _CLAIMED.add(k)
    return k


def _release(key):
    if key:
        with _CLAIM_LOCK:
            _CLAIMED.discard(key)


def process_one(path):
    if ingest.ignorable(path) or not os.path.exists(path):
        return
    if normpath(os.path.abspath(path)) in ignored_paths():
        return
    _key = _claim(path)
    if _key is None:
        return                      # another worker already has this file
    try:
        _process_claimed(path)
    finally:
        _release(_key)


def _process_claimed(path):
    if normpath(os.path.abspath(path)) in indexed_paths():
        return
    if not ingest.wait_stable(path):
        emit(f"unsettled    {os.path.basename(path)[:50]}  (still copying?)")
        return
    if os.path.abspath(path) in indexed_paths():
        return
    try:
        ingest.index_file(path, writer, on_event)
    except Exception as e:
        emit(f"ERROR        {os.path.basename(path)[:50]}  {e}")

# ---- book list / duplicate handling (Add & OCR tab) ------------------------
def inbox_files():
    """normcase(path) -> real path, for every PDF in the drop folder."""
    out = {}
    for dirpath, _, names in os.walk(INBOX):
        for n in names:
            p = os.path.abspath(os.path.join(dirpath, n))
            if ingest.ignorable(p):
                continue
            out[normpath(p)] = p
    return out


_WALK_CACHE = {"t": 0.0, "files": {}}
WALK_TTL = 20.0        # seconds


def library_files(force=False, max_age=WALK_TTL):
    """normcase(path) -> real path, for every PDF in the whole library tree.

    Cached, because the Add & OCR list re-reads it on every SSE tick and a tree
    walk is far more expensive than a stat(). `force=True` refreshes it, which
    the scan endpoints do so the UI updates right after a scan.
    """
    now = time.time()
    if not force and _WALK_CACHE["files"] and now - _WALK_CACHE["t"] < max_age:
        return _WALK_CACHE["files"]
    out = {}
    for p in ingest.walk_library():
        out[normpath(p)] = p
    _WALK_CACHE.update(t=now, files=out)
    return out

def src_groups():
    """normcase(path) -> [source rows]. More than one row = double-indexed."""
    c = store.ro()
    rows = c.execute("SELECT collection,title,path,pages,indexed_pages,chars,status "
                     "FROM sources").fetchall()
    c.close()
    g = {}
    for r in rows:
        g.setdefault(normpath(r[2]), []).append(r)
    return g

def books_payload():
    # The not-indexed rows come from the WHOLE tree, not just the drop folder:
    # a book anywhere in the archive shows up as "not indexed" until it is.
    files, src = library_files(), src_groups()
    c = store.ro()
    years_by_path = book_years()
    # normcase path -> (pub, pub_src), same cache search results read from.
    pm = search_api.pub_map()
    jp, jt, marked = {}, {}, set()
    for path, title, status, detail, updated in c.execute(
            "SELECT path,title,status,detail,updated FROM jobs ORDER BY updated DESC"):
        if path:
            jp.setdefault(normpath(path), (status, detail))
            if status == "duplicate":
                marked.add(normpath(path))
        if title:
            jt.setdefault(title, (status, detail))
    c.close()
    rows, seen = [], set()
    # every indexed source in the library, not just the drop folder
    for nk, entries in src.items():
        if not entries:
            continue
        canon = entries[0]
        extras = entries[1:]
        job = jp.get(nk)
        hit = pm.get(nk) or (None, None)
        st = canon[6]
        if nk in marked:
            st = "duplicate"          # merged away: extra copy, ignored by rescan
        elif job and job[0] in ("indexing", "ocr_running", "queued", "failed"):
            st = job[0]
        try:
            s = os.stat(canon[2])
            size, mtime, exists = s.st_size, s.st_mtime, True
        except OSError:
            size, mtime, exists = 0, 0, False
        if not exists and st != "duplicate":
            st = "missing"           # index row survives, the PDF does not
        # `title` in this payload is a DISPLAY name: it goes through
        # titles.display_name so the list reads like the search results. The
        # exact stored title is not needed by anything downstream - `file` and
        # `pdf_name` carry the real name for tracing back to the PDF - and the
        # job lookup above matches on the raw title, before this.
        rows.append({"title": titles.display_name(canon[1]) or canon[1],
                     "file": canon[2], "exists": exists,
                     "pages": canon[3], "indexed_pages": canon[4],
                     "status": st, "detail": job[1] if job else "",
                     "chars": canon[5], "size": size, "mtime": mtime,
                     "year": years_by_path.get(nk),
                     "dup": len(extras), "dup_paths": [e[2] for e in extras],
                     # `pub` is the publication name resolved at ingest time
                     # (a magazine issue stored as `7.pdf`); it is None when
                     # the filename already named the book, and the Book
                     # column then shows `title`. Both are display names by
                     # the time they leave here - see the note on `title`.
                     "pub": titles.display_name(hit[0]) or hit[0],
                     "pub_src": hit[1],
                     # `file` is the full path and is used by the delete /
                     # re-index endpoints; the filename for display is its own key.
                     "pdf_name": os.path.basename(canon[2] or ""),
                     "collection": canon[0] or "", "in_inbox":
                         nk.startswith(normpath(INBOX) + os.sep)})
        seen.add(nk)
    # PDFs anywhere in the tree that are not indexed yet
    for nk, fpath in files.items():
        if nk in seen:
            continue
        seen.add(nk)
        title = os.path.splitext(os.path.basename(fpath))[0]
        job = jp.get(nk) or jt.get(title)
        st = "not-indexed"
        if nk in marked:
            st = "duplicate"
        elif job and job[0] in ("indexing", "queued", "extracting", "failed"):
            st = job[0]
        try:
            s = os.stat(fpath)
            size, mtime = s.st_size, s.st_mtime
        except OSError:
            size, mtime = 0, 0
        rows.append({"title": titles.display_name(title) or title,
                     "file": fpath, "exists": True,
                     "pages": 0, "indexed_pages": 0,
                     "status": st, "detail": job[1] if job else "",
                     "chars": 0, "size": size, "mtime": mtime, "year": None,
                     "dup": 0, "dup_paths": [],
                     # Not indexed yet, so there is no stored pub to read.
                     # The folder rule is cheap and exact enough to preview
                     # what the name will be once it is.
                     "pub": titles.display_name(titles.from_folder(
                         ingest.collection_for(fpath))),
                     "pub_src": "folder",
                     "pdf_name": os.path.basename(fpath),
                     "collection": ingest.collection_for(fpath),
                     "in_inbox": nk.startswith(normpath(INBOX) + os.sep)})
    # same book present as "x.pdf" AND "x (1).pdf" in the folder
    groups = {}
    for r in rows:
        groups.setdefault(dup_key(r["file"]), []).append(r)
    copy_rows = 0
    for g in groups.values():
        if len(g) < 2:
            continue
        g.sort(key=lambda r: (r["status"] == "duplicate",
                              "(" in os.path.basename(r["file"])))
        keep = g[0]
        keep["copies"] = len(g)
        keep["dup_files"] = [x["file"] for x in g[1:]]
        for x in g[1:]:
            x["dup_of"] = keep["title"]
            copy_rows += 1
    rows.sort(key=lambda r: (r["status"] == "missing", (r["collection"] or "").casefold(),
                             dup_key(r["file"])))
    colls = {}
    for r in rows:
        colls[r["collection"] or "(none)"] = colls.get(r["collection"] or "(none)", 0) + 1
    # searchable page count, so the header can show a live number that follows
    # every add / index / OCR / delete instead of a hard-coded one
    c = store.ro()
    try:
        npages = c.execute("SELECT COALESCE(sum(indexed_pages),0) FROM sources").fetchone()[0]
    finally:
        c.close()
    return {"books": rows, "count": len(rows), "file_copies": copy_rows,
            "collections": [{"name": k, "n": v} for k, v in
                            sorted(colls.items(), key=lambda kv: -kv[1])],
            "pages": npages,
            "inbox": normpath(INBOX)}

def _targets_for(path):
    """Every stored path that means this same file (E:\\ vs e:\\,
    and single- vs double-backslash on disk)."""
    want = normpath(path)
    want_raw = normpath(os.path.abspath(path))
    c = store.ro()
    allp = [r[0] for r in c.execute("SELECT path FROM sources")]
    c.close()
    out = []
    for p in allp:
        if normpath(p) == want or normpath(p.replace("\\\\", "\\")) == want:
            out.append(p)
        elif normpath(p) == want_raw or normpath(p.replace("\\\\", "\\")) == want_raw:
            out.append(p)
    return out

def trash_dir():
    """Where a deleted PDF is parked instead of being erased.

    The UI asks whether to delete the file too. Removing it outright is not
    recoverable and index.db has no undo, so the file is moved here and the UI
    says where it went.
    """
    d = store.TRASH
    os.makedirs(d, exist_ok=True)
    return d


def delete_pdf(path):
    """Move one PDF to _trash. Returns the new location, or an error dict."""
    p = os.path.abspath(path)
    root = os.path.abspath(store.ROOT)
    if not p.lower().startswith(root.lower()):
        return {"error": "outside the library"}
    if not os.path.exists(p):
        return {"error": "file missing"}
    if normpath(os.path.dirname(p)) == normpath(trash_dir()):
        return {"error": "already in trash"}
    base = os.path.basename(p)
    dest = os.path.join(trash_dir(), base)
    i = 1
    while os.path.exists(dest):
        stem, ext = os.path.splitext(base)
        dest = os.path.join(trash_dir(), f"{stem} ({i}){ext}")
        i += 1
    try:
        shutil.move(p, dest)
    except Exception as e:
        return {"error": str(e)}
    return {"moved_to": dest}


_YEARS_CACHE = {}


def _semantic_dirty():
    """Mark the semantic layer stale after page rows changed.

    Deleting or re-indexing a book removes page rowids, so the vector store has
    entries pointing at rows that no longer exist. Pruning is cheap (an
    anti-join on 99k rows) and is done in the background so the delete stays
    instant.
    """
    search_api.hybrid_reset()
    def _prune():
        try:
            n = vectors.prune()
            vectors.invalidate()
            if n:
                emit(f"semantic     {n} stale vector(s) pruned")
        except Exception as e:
            emit(f"semantic     prune failed: {e}")
    threading.Thread(target=_prune, daemon=True).start()


def book_years():
    """Most common non-zero year per book, keyed by normpath(book path).

    pyear is per page; a book that spans decades should not show an arbitrary
    one, so the mode is used and ties fall to the earliest year.

    `pages.path` is already the book's path, so there is no join to `sources`.
    (Joining on `lower(s.path)=lower(p.path)` kills both indexes and turned
    this into a 99k x 422 cartesian scan -- 10 s per /api/books call.)

    The 99k-row scan costs ~500 ms, and /api/books is re-fetched on every SSE
    tick, so the result is memoised. The cache is keyed on a cheap fingerprint
    of `sources` (count + indexed_pages + pages + chars + max(rowid)) plus one
    on `pyear` itself; those move on every index or delete,
    whenever a book is indexed or deleted, which is exactly when pyear can
    change. Cheaper and more robust than remembering to invalidate by hand.
    """
    c = store.ro()
    fp = (c.execute("SELECT count(*), sum(indexed_pages), sum(pages), "
                    "sum(chars), max(rowid) FROM sources").fetchone(),
          c.execute("SELECT count(*), max(rowid), sum(year) "
                    "FROM pyear").fetchone())
    if _YEARS_CACHE.get("v") is not None and _YEARS_CACHE.get("fp") == fp:
        c.close()
        return _YEARS_CACHE["v"]
    try:
        counts = {}
        for path, year in c.execute(
                "SELECT p.path, py.year FROM pages p "
                "LEFT JOIN pyear py ON py.rowid=p.rowid"):
            if not year or not path:
                continue
            per = counts.setdefault(normpath(path), {})
            per[year] = per.get(year, 0) + 1
    finally:
        c.close()
    out = {}
    for k, per in counts.items():
        best = max(per.items(), key=lambda kv: (kv[1], -kv[0]))
        out[k] = best[0]
    _YEARS_CACHE["fp"] = fp
    _YEARS_CACHE["v"] = out
    return out


def delete_book(path, keep=None):
    """Remove index rows for one book (sources + pages + pyear + fwwmap + jobs).
    The PDF on disk is never touched.  `keep` = a stored path to preserve,
    used when de-duplicating: only the extra rows go away.
    """
    _semantic_dirty()
    targets = _targets_for(path)
    if keep:
        keep_n = normpath(keep)
        targets = [p for p in targets if normpath(p) != keep_n]
    if not targets:
        targets = [os.path.abspath(path)]
    def _del(c):
        out = {"pages": 0, "sources": 0, "jobs": 0}
        for p in targets:
            rids = [r[0] for r in c.execute(
                "SELECT rowid FROM pages WHERE path=? COLLATE NOCASE", (p,))]
            if rids:
                c.executemany("DELETE FROM pyear WHERE rowid=?", [(r,) for r in rids])
                c.executemany("DELETE FROM fwwmap WHERE rowid=?", [(r,) for r in rids])
            out["pages"] += max(0, c.execute(
                "DELETE FROM pages WHERE path=? COLLATE NOCASE", (p,)).rowcount)
            out["sources"] += max(0, c.execute(
                "DELETE FROM sources WHERE path=? COLLATE NOCASE", (p,)).rowcount)
            out["jobs"] += max(0, c.execute(
                "DELETE FROM jobs WHERE path=? COLLATE NOCASE", (p,)).rowcount)
        return out
    try:
        return writer.submit(_del)
    except Exception as e:
        return {"error": str(e)}

def fix_dupes():
    """Collapse every double-indexed book: keep the row whose path matches the
    file on disk, delete the rest (index rows only, never the PDF)."""
    files, src = library_files(), src_groups()
    out = {"groups": 0, "pages": 0, "sources": 0}
    for nk, entries in src.items():
        if len(entries) < 2:
            continue
        fpath = files.get(nk)
        # keep a row whose stored path matches the file on disk (case-insensitive
        # is fine for choosing, the actual delete compares exactly)
        keep = next((e[2] for e in entries if fpath and normpath(e[2]) == normpath(fpath)),
                    entries[0][2])
        out["groups"] += 1
        r = delete_book_from(entries, keep)
        for k in ("pages", "sources"):
            out[k] += r.get(k, 0)
    return out

def delete_book_from(entries, keep):
    """Drop every row of this book EXCEPT `keep`.

    The comparison must be exact (COLLATE BINARY): the duplicate rows differ
    only by drive-letter case (E:\\ vs e:\\), so NOCASE would match the row we
    are keeping and delete it too.
    """
    _semantic_dirty()
    drop = [e[2] for e in entries if e[2] != keep]
    def _del(c):
        out = {"pages": 0, "sources": 0, "jobs": 0}
        for p in drop:
            rids = [r[0] for r in c.execute(
                "SELECT rowid FROM pages WHERE path=? COLLATE BINARY", (p,))]
            if rids:
                c.executemany("DELETE FROM pyear WHERE rowid=?", [(r,) for r in rids])
                c.executemany("DELETE FROM fwwmap WHERE rowid=?", [(r,) for r in rids])
            out["pages"] += max(0, c.execute(
                "DELETE FROM pages WHERE path=? COLLATE BINARY", (p,)).rowcount)
            out["sources"] += max(0, c.execute(
                "DELETE FROM sources WHERE path=? COLLATE BINARY", (p,)).rowcount)
            out["jobs"] += max(0, c.execute(
                "DELETE FROM jobs WHERE path=? COLLATE BINARY", (p,)).rowcount)
        return out
    try:
        return writer.submit(_del)
    except Exception as e:
        return {"error": str(e)}

def tidy_jobs():
    """Drop the stale duplicate job rows (path stored as the bare title)."""
    def _t(c):
        rows = c.execute("SELECT id,path,title FROM jobs").fetchall()
        by_title = {}
        for i, p, t in rows:
            by_title.setdefault(t, []).append((i, p))
        dead = []
        for lst in by_title.values():
            if len(lst) < 2:
                continue
            good = {i for i, p in lst if p and os.path.exists(p)}
            dead += [(i,) for i, p in lst if i not in good]
        if dead:
            c.executemany("DELETE FROM jobs WHERE id=?", dead)
        return len(dead)
    try:
        return writer.submit(_t)
    except Exception:
        return 0

def fts_query(raw):
    parts = ['"' + m.group(1).replace('"', '') + '"'
             for m in re.finditer(r'"([^"]+)"', raw or "")]
    stripped = re.sub(r'"[^"]+"', ' ', raw or "")
    parts += ['"' + w + '"' for w in re.findall(r"[0-9A-Za-z][0-9A-Za-z'\-]*", stripped)]
    return " ".join(parts) if parts else None

def _inline_pdf_name(path):
    """Content-Disposition for a PDF, safe for any filename.

    HTTP headers are latin-1, and library files contain names like
    "No. 2116.pdf" (U+2116) which raise UnicodeEncodeError and kill the
    response mid-handshake. Emit an ASCII fallback plus RFC 6266 filename*.
    """
    name = os.path.basename(path)
    ascii_name = name.encode("ascii", "replace").decode("ascii").replace('"', "'")
    quoted = quote(name, safe="")
    return 'inline; filename="%s"; filename*=UTF-8\'\'%s' % (ascii_name, quoted)


class H(BaseHTTPRequestHandler):
    server_version = "WoodshedStudio/" + VERSION
    def log_message(self, *a):
        pass
    def _send(self, obj, code=200, ctype="application/json"):
        if isinstance(obj, (dict, list)):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        elif isinstance(obj, str):
            body = obj.encode("utf-8")
        else:
            body = bytes(obj)
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8" if "text" in ctype or "json" in ctype else ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
    def _static(self, path, ctype):
        try:
            body = open(path, "rb").read()
        except OSError:
            return self._send({"error": "missing " + os.path.basename(path)}, 500)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # HTML with __VERSION__ resolved. Read per request, like _static, so an
    # edited page shows up after a restart; the substitution is a single
    # bytes.replace on a marker that cannot appear in real content.
    def _page(self, path):
        try:
            body = open(path, "rb").read()
        except OSError:
            return self._send({"error": "missing " + os.path.basename(path)}, 500)
        body = body.replace(b"__VERSION__", VERSION.encode("ascii"))
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # --- optional shared-secret auth ------------------------------------
    # Basic auth on purpose: the browser remembers it for the origin, so the
    # SSE stream, the reader's Range requests and every fetch after the first
    # page load go out with the header already attached. No token in a URL,
    # no login form, no cookie to expire mid-session.
    #
    # It guards everything, including /api/health, so the page cannot be
    # probed before it is opened. When no password is configured this is a
    # single dict lookup that returns "allowed" - the local default.
    def _auth_ok(self):
        want = store.password()
        if not want:
            return True
        got = self.headers.get("Authorization") or ""
        if not got.lower().startswith("basic "):
            return False
        try:
            raw = base64.b64decode(got.split(None, 1)[1]).decode("utf-8", "replace")
        except (ValueError, IndexError, TypeError):
            return False
        # "user:password"; the username is ignored, so one shared secret is
        # enough and nobody has to invent an account name.
        given = raw.split(":", 1)[1] if ":" in raw else ""
        return hmac.compare_digest(given.encode("utf-8"), want.encode("utf-8"))

    def _deny(self):
        # Not every client shows a login prompt - an embedded webview, a
        # script, a curl - and those land on whatever body we send. A browser
        # navigating gets a readable page; everything else keeps JSON, because
        # studio.html parses every response as JSON and an HTML error body
        # would throw inside fetch() instead of showing a message.
        if "text/html" in (self.headers.get("Accept") or ""):
            body = ("""<!doctype html><meta charset="utf-8">"""
                    """<title>Woodshed Studio</title><style>"""
                    """body{background:#16191d;color:#e8eaed;margin:0;height:100vh;display:grid;"""
                    """place-items:center;font:16px/1.6 system-ui,'Segoe UI',sans-serif;text-align:center}"""
                    """div{max-width:32rem;padding:2rem}h1{font-weight:600;letter-spacing:.3px}"""
                    """p{color:#9aa4b2}code{color:#7fa8d0}</style>"""
                    """<div><h1>&#129720; Woodshed Studio</h1>"""
                    """<p>This library is password protected.</p>"""
                    """<p>Your browser should ask for the password &mdash; enter it with an empty
                    username. If no prompt appeared, reload, or ask whoever set this up.</p></div>"""
                    ).encode("utf-8")
            ctype = "text/html; charset=utf-8"
        else:
            body = b'{"error":"password required"}'
            ctype = "application/json"
        self.send_response(401)
        self.send_header("WWW-Authenticate",
                         'Basic realm="Woodshed Studio", charset="UTF-8"')
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if not self._auth_ok():
            return self._deny()
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        g = lambda k, d="": qs.get(k, [d])[0]
        if u.path in ("/", "/index.html"):
            return self._page(HTML)
        if u.path == "/viewer.html":
            return self._static(VIEWER, "text/html; charset=utf-8")
        if u.path in ("/logo.png", "/favicon.ico"):
            return self._static(LOGO, "image/png")
        if u.path.startswith("/vendor/"):
            name = os.path.basename(u.path)
            if name not in ("pdf.min.js", "pdf.worker.min.js"):
                return self._send({"error": "not found"}, 404)
            return self._static(os.path.join(VENDOR, name),
                                "application/javascript; charset=utf-8")
        if u.path == "/api/topicgraph":
            return self._send(search_api.graph())
        if u.path == "/api/subgraph":
            return self._send(search_api.api_subgraph(g("q")))
        if u.path == "/api/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            # Resume by sequence number, not by list index. The buffer is
            # capped at 300, so an index cursor goes stale the moment the cap
            # bites: once a reader had seen 300 lines, idx == len(EVENTS) ==
            # 300 forever after, `EVENTS[idx:]` was permanently empty, and
            # the stream died silently - a busy index run produced nothing
            # after the first 300 lines. EV_SEQ only increases, so truncation
            # can drop lines from the buffer without ever confusing a client
            # about which ones it already has. EventSource replays
            # `Last-Event-ID` for us on reconnect.
            try:
                last = int(self.headers.get("Last-Event-ID") or g("after") or 0)
            except (TypeError, ValueError):
                last = 0
            try:
                while True:
                    with EV_COND:
                        EV_COND.wait(timeout=15)
                        batch = [(s, ln) for s, ln in EVENTS if s > last]
                        if batch:
                            last = batch[-1][0]
                    for seq, line in batch:
                        self.wfile.write(
                            f"id: {seq}\ndata: {line}\n\n".encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            return
        if u.path == "/api/stats":
            # tiny counter endpoint: the header page count follows every
            # add / index / OCR / delete without shipping the whole book list
            c = store.ro()
            try:
                np = c.execute("SELECT COALESCE(sum(indexed_pages),0) FROM sources").fetchone()[0]
                ns = c.execute("SELECT count(*) FROM sources").fetchone()[0]
            finally:
                c.close()
            return self._send({"pages": np, "sources": ns})
        if u.path == "/api/books":
            return self._send(books_payload())
        if u.path == "/api/paths":
            # where the app believes the library lives - shown in Health so a
            # fresh install can be checked without reading the console
            d = store.describe()
            d["scan"] = dict(SCAN)
            on_disk = len(library_files())
            d["library_pdfs"] = on_disk
            d["sources"] = len(indexed_paths())
            d["not_indexed"] = max(0, on_disk - d["sources"])
            return self._send(d)
        if u.path == "/api/embed_status":
            return self._send(vectors.status())
        if u.path == "/api/jobs":
            c = store.ro()
            out = []
            for p, t, s, d in c.execute(
                    "SELECT path,title,status,detail FROM jobs ORDER BY updated DESC"):
                pg = c.execute("SELECT pages FROM sources WHERE path=?", (p,)).fetchone()
                out.append({"path": p, "title": t, "status": s, "detail": d,
                            "pages": pg[0] if pg else None})
            c.close()
            return self._send({"jobs": out})
        if u.path == "/api/search":
            return self._send(search_api.api_search(
                q=g("q"), allw=g("all"), phrase=g("phrase"), anyw=g("any"),
                none=g("none"), coll=g("coll"), ymin=g("ymin") or None,
                ymax=g("ymax") or None, sort=g("sort", "relevance"),
                offset=int(g("offset", "0") or 0),
                limit=int(g("limit", "60") or 60),
                hybrid=g("hybrid", "1") not in ("0", "false", "off")))
        if u.path == "/api/related":
            return self._send(search_api.api_related(g("q")))
        if u.path == "/api/text":
            return self._send(search_api.api_text(int(g("id", "0") or 0)))
        if u.path == "/api/terms":
            words = [w for w in re.findall(r"[0-9A-Za-z][0-9A-Za-z'\-]*",
                                            g("terms"))][:12]
            return self._send(search_api.api_terms(int(g("id", "0") or 0), words))
        if u.path == "/pdf":
            try:
                return self._pdf(int(g("id", "0") or 0))
            except Exception as e:
                return self._send({"error": str(e)}, 500)
        if u.path == "/rawfile":
            return self._raw(g("path", ""))
        if u.path == "/api/health":
            a = lib_audit()
            try:
                c = store.ro()
                # sum(indexed_pages) equals COUNT(*) FROM pages but reads 425
                # source rows instead of scanning ~99k page rows (0.8ms vs 350ms)
                pages = c.execute("SELECT COALESCE(sum(indexed_pages),0) FROM sources").fetchone()[0]
                sources = c.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
                img = c.execute("SELECT COUNT(*) FROM sources WHERE status='image-only'").fetchone()[0]
                act = c.execute("SELECT COUNT(*) FROM jobs WHERE status NOT IN "
                                "('done','failed','idle')").fetchone()[0]
                pgs = c.execute("SELECT COALESCE(SUM(pages),0), COALESCE(SUM(indexed_pages),0) "
                                "FROM sources").fetchone()
                dfiles = c.execute("SELECT COUNT(*) FROM jobs WHERE status='duplicate'").fetchone()[0]
                c.close()
            except Exception:
                pages = sources = img = act = pgs = dfiles = -1
            # duplicated books (same file indexed under E:\\ and e:\\)
            try:
                dup = sum(1 for g in src_groups().values() if len(g) > 1)
            except Exception:
                dup = -1
            # Concept-layer freshness. Built per library and stored beside
            # index.db, so this reports THIS library's tag layer; the old
            # app-folder copy is only a read-time fallback in search_api and
            # must not be what the age is measured from.
            c_age, c_where = -1, None
            for cand in (vectors.concept_path(),
                         os.path.join(HERE, "concepts.npz")):
                try:
                    c_age = int((time.time() - os.path.getmtime(cand)) / 60)
                    c_where = cand
                    break
                except OSError:
                    continue
            return self._send({"pages": pages, "sources": sources, "image_only": img,
                               "jobs_active": act,
                               "src_pages": pgs[0] if pgs != -1 else -1,
                               "src_indexed": pgs[1] if pgs != -1 else -1,
                               "dup_groups": dup,
                               "dup_files": dfiles,
                               "ocr_running": bool(ocr and ocr.running()),
                               "ocr_mode": ("GPU" if ocr and ocr.cuda else "CPU") if ocr else "CPU",
                               "concept_age_min": c_age, "concept_path": c_where,
                               "password_set": bool(store.password()),
                               "password_source": store.password_source(),
                               "config_path": store.CONFIG_PATH,
                               **a})
        return self._send({"error": "not found"}, 404)

    def _pdf(self, rid, head=False):
        c = store.ro()
        try:
            r = c.execute("SELECT path FROM pages WHERE rowid=?", (rid,)).fetchone()
            fm = c.execute("SELECT pdf_path FROM fwwmap WHERE rowid=?", (rid,)).fetchone()
        finally:
            c.close()
        path = store.resolve_path(r[0]) if r and r[0] else None
        if not (path and path.lower().endswith(".pdf") and os.path.exists(path)):
            path = store.resolve_path(fm[0]) if fm and fm[0] else None
        if not path or not os.path.exists(path):
            return self._send({"error": "no pdf for this page"}, 404)
        return self._serve_pdf(path, head)

    def _raw(self, path, head=False):
        """Serve a whole book by path, so the browser's own PDF viewer gets it."""
        p = os.path.abspath(os.path.join(store.ROOT, path.lstrip("/\\"))) \
            if not os.path.isabs(path) else os.path.abspath(path)
        if not p.lower().endswith(".pdf"):
            return self._send({"error": "not a pdf"}, 400)
        # Only books that are actually in the index may be opened this way.
        if normpath(p) not in indexed_paths():
            return self._send({"error": "not indexed"}, 404)
        if not os.path.exists(p):
            return self._send({"error": "file missing"}, 404)
        return self._serve_pdf(p, head)

    def _serve_pdf(self, path, head=False):
        path = os.path.abspath(path)
        # Whole library tree is fair game; nothing outside the configured
        # library root may be served. Compared on the resolved root, not on a
        # hard-coded path, so a library on another drive works the same.
        if not path.lower().startswith(os.path.abspath(store.ROOT).lower()):
            return self._send({"error": "forbidden"}, 403)
        size = os.path.getsize(path)
        start, end, partial = 0, size - 1, False
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng.strip())
            if m:
                s, e = m.group(1), m.group(2)
                if s == "" and e:
                    start, end = max(0, size - int(e)), size - 1
                else:
                    start = int(s)
                    end = int(e) if e else size - 1
                end = min(end, size - 1)
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers(); return
                partial = True
        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition", _inline_pdf_name(path))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(length))
        self.end_headers()
        if head or self.command == "HEAD":
            return
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(262144, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    break
                remaining -= len(chunk)

    def do_HEAD(self):
        if not self._auth_ok():
            return self._deny()
        u = urlparse(self.path)
        if u.path == "/pdf":
            try:
                return self._pdf(int(parse_qs(u.query).get("id", ["0"])[0]), head=True)
            except Exception as e:
                return self._send({"error": str(e)}, 500)
        if u.path == "/rawfile":
            try:
                return self._raw(parse_qs(u.query).get("path", [""])[0], head=True)
            except Exception as e:
                return self._send({"error": str(e)}, 500)
        self.send_response(200); self.send_header("Content-Length", "0"); self.end_headers()

    def do_POST(self):
        if not self._auth_ok():
            return self._deny()
        u = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        if u.path == "/api/upload":
            ctype = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in ctype or "boundary=" not in ctype:
                return self._send({"error": "need multipart pdf upload"}, 400)
            boundary = ctype.split("boundary=")[1].strip().strip('"').encode()
            body = self.rfile.read(length) if length else b""
            saved = []
            for chunk in body.split(b"--" + boundary):
                hi, _, data = chunk.partition(b"\r\n\r\n")
                if b'filename="' not in hi or not data:
                    continue
                name = os.path.basename(
                    hi.split(b'filename="')[1].split(b'"')[0].decode("utf-8", "replace"))
                if not name.lower().endswith(".pdf"):
                    continue
                if data.endswith(b"\r\n"):
                    data = data[:-2]
                dest = os.path.join(INBOX, name)
                k = 1
                while os.path.exists(dest):
                    dest = os.path.join(INBOX, os.path.splitext(name)[0] + f" ({k}).pdf")
                    k += 1
                with open(dest, "wb") as f:
                    f.write(data)
                saved.append(dest)
                threading.Thread(target=process_one, args=(dest,), daemon=True).start()
            emit(f"uploaded     {len(saved)} file(s) via browser")
            return self._send({"saved": saved})
        body = self.rfile.read(length) if length else b""
        try:
            data = json.loads(body or b"{}")
        except Exception:
            data = {}
        if u.path == "/api/password":
            # Set, change or remove the shared secret. Guarded by _auth_ok()
            # above, so whoever reaches this has already proved they know the
            # current one - or there is none set and the app is open anyway.
            new = data.get("password")
            if new is None:
                return self._send({"error": "password required"}, 400)
            new = str(new)
            if new and len(new) < 6:
                return self._send({"error": "use at least 6 characters, "
                                             "or send an empty one to remove it"}, 400)
            try:
                r = store.set_password(new)
            except OSError as e:
                return self._send({"error": "could not write config.json: " + str(e)}, 500)
            r["env_override"] = store.password_source() == "env"
            emit(("password set" if r["password_set"] else "password removed")
                 + f"  (from {self.client_address[0]})")
            return self._send(r)
        if u.path == "/api/rescan":
            n = scan_inbox()
            emit(f"rescan      {len(n)} new file(s) in the drop folder")
            return self._send({"queued": n, "count": len(n), "inbox": INBOX})
        if u.path == "/api/scan_library":
            root = (data.get("root") or "").strip() or None
            if root and not os.path.isdir(root):
                return self._send({"error": "not a folder: " + root}, 400)
            started = scan_library_bg(root)
            library_files(force=True)
            return self._send({"started": started, "root": root or store.ROOT,
                               "running": SCAN["running"],
                               "note": "" if started else "a scan is already running"})
        if u.path == "/api/book_delete":
            p = os.path.abspath(data.get("path") or "")
            if not p or not data.get("path"):
                return self._send({"error": "path required"}, 400)
            r = delete_book(p)
            if isinstance(r, dict) and r.get("error"):
                return self._send(r, 500)
            moved = None
            if data.get("file"):          # the user chose to delete the PDF too
                m = delete_pdf(p)
                if m.get("error"):
                    return self._send({"deleted": r, "path": p, "pdf": m}, 500)
                moved = m["moved_to"]
            emit(f"deleted     {os.path.basename(p)[:50]}  "
                 + (f"index + pdf -> {moved}" if moved else "index only (pdf kept)"))
            return self._send({"deleted": r, "path": p, "moved_to": moved})
        if u.path == "/api/book_index":
            # Add / Re-index: index a fresh file, or drop the old rows first
            # and index it again so the button can do both jobs.
            p = os.path.abspath(data.get("path") or "")
            if not data.get("path") or not p.lower().endswith(".pdf"):
                return self._send({"error": "pdf path required"}, 400)
            if not os.path.exists(p):
                return self._send({"error": "file not found: " + p}, 404)
            mode, removed = "index", None
            if normpath(p) in indexed_paths():
                removed = delete_book(p)
                if isinstance(removed, dict) and removed.get("error"):
                    return self._send(removed, 500)
                mode = "re-index"
            emit(f"{mode}      {os.path.basename(p)[:50]}  queued for ingest")
            threading.Thread(target=process_one, args=(p,), daemon=True).start()
            return self._send({"queued": p, "mode": mode, "deleted": removed})
        if u.path == "/api/bulk_delete":
            # remove many books from the index at once; the PDFs only go when
            # the dialog was answered with "delete the PDFs too"
            paths = [p for p in (data.get("paths") or []) if p]
            if not paths:
                return self._send({"error": "paths required"}, 400)
            tot_s = tot_p = n_files = 0
            failed = []
            for p in paths:
                p = os.path.abspath(p)
                r = delete_book(p)
                if isinstance(r, dict) and r.get("error"):
                    failed.append({"path": p, "error": r["error"]})
                    continue
                tot_s += (r or {}).get("sources", 0)
                tot_p += (r or {}).get("pages", 0)
                if data.get("file"):
                    m = delete_pdf(p)
                    if m.get("error"):
                        failed.append({"path": p, "pdf": m["error"]})
                    else:
                        n_files += 1
            emit(f"bulk delete {len(paths)} book(s)  "
                 f"-{tot_s} source/-{tot_p} page rows"
                 + (f" · {n_files} pdf moved to _trash" if n_files else " (pdfs kept)"))
            return self._send({"sources": tot_s, "pages": tot_p, "files": n_files,
                               "requested": len(paths), "failed": failed})
        if u.path == "/api/bulk_reindex":
            # one background worker, sequential: a thread per book would flood
            # the single writer queue and hold hundreds of open connections
            paths = [p for p in (data.get("paths") or []) if p]
            if not paths:
                return self._send({"error": "paths required"}, 400)
            indexed = indexed_paths()
            todo, skipped, deleted_p = [], 0, 0
            for p in paths:
                p = os.path.abspath(p)
                if not p.lower().endswith(".pdf") or not os.path.exists(p):
                    skipped += 1
                    continue
                if normpath(p) in indexed:
                    r = delete_book(p)
                    if isinstance(r, dict) and r.get("error"):
                        skipped += 1
                        continue
                    deleted_p += (r or {}).get("pages", 0)
                todo.append(p)
            if todo:
                threading.Thread(target=_index_queue, args=(todo,),
                                 daemon=True).start()
            emit(f"bulk re-index {len(todo)} book(s) queued"
                 + (f", {skipped} skipped" if skipped else "")
                 + f", -{deleted_p} old page rows")
            return self._send({"queued": len(todo), "skipped": skipped,
                               "deleted": deleted_p})
        if u.path == "/api/bulk_ocr":
            paths = {normpath(p) for p in (data.get("paths") or []) if p}
            if not paths:
                return self._send({"error": "paths required"}, 400)
            groups = src_groups()
            rows, skipped = [], 0
            for want in paths:
                row = next((e for e in groups.get(want, []) if e[6] == "image-only"),
                           None)
                if row:
                    rows.append((row[2], row[1]))       # (path, title)
                else:
                    skipped += 1
            if rows and ocr.running():
                return self._send({"error": "OCR already running"}, 409)
            if rows:
                ocr.start(rows)
            emit(f"bulk OCR {len(rows)} book(s) queued"
                 + (f", {skipped} skipped (not image-only)" if skipped else ""))
            return self._send({"queued": len(rows), "skipped": skipped,
                               "started": [t for _, t in rows]})
        if u.path == "/api/book_merge":
            # two real files, same book: index rows go away for the extra copy
            # and it is marked so rescan does not index it again
            p = os.path.abspath(data.get("path") or "")
            if not data.get("path"):
                return self._send({"error": "path required"}, 400)
            r = delete_book(p) or {}
            title = os.path.splitext(os.path.basename(p))[0]
            def _mk(c):
                row = c.execute("SELECT id FROM jobs WHERE path=? COLLATE NOCASE",
                                (p,)).fetchone()
                if row:
                    c.execute("UPDATE jobs SET status='duplicate', detail=?, updated=? "
                              "WHERE id=?", ("extra copy · ignored by rescan",
                                             time.time(), row[0]))
                else:
                    c.execute("INSERT INTO jobs(path,title,status,detail,updated)"
                              " VALUES(?,?,?,?,?)",
                              (p, title, "duplicate",
                               "extra copy · ignored by rescan", time.time()))
            try:
                writer.submit(_mk)
            except Exception as e:
                return self._send({"error": str(e)}, 500)
            emit(f"merged      {os.path.basename(p)[:50]}  "
                 f"-{r.get('sources', 0)} source/-{r.get('pages', 0)} page rows, copy ignored")
            return self._send({"merged": p, **r})
        if u.path == "/api/book_restore":
            # undo a merge: forget the marker and index the file again
            p = os.path.abspath(data.get("path") or "")
            if not data.get("path"):
                return self._send({"error": "path required"}, 400)
            if not os.path.exists(p):
                return self._send({"error": "file not found: " + p}, 404)
            def _un(c):
                c.execute("DELETE FROM jobs WHERE path=? COLLATE NOCASE AND status='duplicate'",
                          (p,))
            try:
                writer.submit(_un)
            except Exception as e:
                return self._send({"error": str(e)}, 500)
            emit(f"restored    {os.path.basename(p)[:50]}  copy is being indexed again")
            threading.Thread(target=process_one, args=(p,), daemon=True).start()
            return self._send({"restored": p})
        if u.path == "/api/book_dedup":
            # keep the row that matches the file on disk, drop the E:/e: twin
            want = normpath(data.get("path") or "")
            group = src_groups().get(want, [])
            if len(group) < 2:
                return self._send({"groups": 0, "note": "not a duplicate"})
            files = library_files(force=True)
            on_disk = files.get(want)
            keep = next((e[2] for e in group
                         if on_disk and normpath(e[2]) == normpath(on_disk)),
                        group[0][2])
            r = delete_book_from(group, keep)
            tj = tidy_jobs()
            if isinstance(r, dict) and r.get("error"):
                return self._send(r, 500)
            emit(f"dedup       {os.path.basename(keep)[:50]}  "
                 f"-{r.get('sources', 0)} source/-{r.get('pages', 0)} page row(s)")
            return self._send({"groups": 1, "kept": keep, "jobs_removed": tj, **r})
        if u.path == "/api/dup_fix":
            r = fix_dupes()
            tj = tidy_jobs()
            r["jobs_removed"] = tj
            emit(f"dup fix     {r.get('groups', 0)} book(s) collapsed, "
                 f"{r.get('sources', 0)} extra source row(s) dropped")
            return self._send(r)
        if u.path == "/api/ocr_one":
            want = normpath(data.get("path", ""))
            # match through normpath: the stored path may differ by drive-letter
            # case or backslash count from the path the browser sent us
            row = next((e for e in src_groups().get(want, [])
                        if e[6] == "image-only"), None)
            if not row:
                return self._send({"error": "not image-only"}, 400)
            if ocr.running():
                return self._send({"error": "OCR already running"}, 409)
            ocr.start([(row[2], row[1])])   # (path, title)
            return self._send({"started": [row[1]]})
        if u.path == "/api/ocr_all":
            c = store.ro()
            rows = c.execute("SELECT path,title FROM sources WHERE status='image-only'"
                             " ORDER BY pages").fetchall()
            c.close()
            if not rows:
                return self._send({"started": []})
            if ocr.running():
                return self._send({"error": "OCR already running"}, 409)
            ocr.start(rows)
            return self._send({"started": [t for _, t in rows]})
        if u.path == "/api/ocr_stop":
            ocr.request_stop()
            return self._send({"stopping": True})
        if u.path == "/api/embed_build":
            n = int(data.get("limit") or 0) or None
            if vectors.BUILD["running"]:
                return self._send({"error": "already building"}, 409)
            t = vectors.build_background(n, on_event=lambda s, ti, de: emit(
                f"{s:12} {ti[:50]}  {de[:80]}"))
            return self._send({"started": bool(t), "limit": n})
        if u.path == "/api/embed_stop":
            vectors.BUILD["stop"] = True
            return self._send({"stopping": True})
        if u.path == "/api/embed_reset":
            # drops every vector; the next build starts from scratch
            if vectors.BUILD["running"]:
                return self._send({"error": "stop the build first"}, 409)
            vectors._reset()
            search_api.hybrid_reset()
            emit("semantic     all page vectors dropped")
            return self._send({"reset": True})
        if u.path == "/api/open":
            rid = int(data.get("id", 0) or 0)
            c = store.ro()
            try:
                row = c.execute("SELECT path FROM pages WHERE rowid=?", (rid,)).fetchone()
                fm = c.execute("SELECT pdf_path FROM fwwmap WHERE rowid=?", (rid,)).fetchone()
            finally:
                c.close()
            path = store.resolve_path(row[0]) if row and row[0] else None
            if not (path and path.lower().endswith(".pdf") and os.path.exists(path)):
                path = store.resolve_path(fm[0]) if fm and fm[0] else None
            if not (path and path.lower().endswith(".pdf") and os.path.exists(path)):
                return self._send({"error": "no pdf for this page"}, 404)
            ap = os.path.abspath(path)
            if not ap.lower().startswith(os.path.abspath(store.ROOT).lower()):
                return self._send({"error": "forbidden"}, 403)
            try:
                os.startfile(ap)
            except OSError as e:
                return self._send({"error": str(e)}, 500)
            emit(f"open         {os.path.basename(ap)[:60]}  external reader")
            return self._send({"ok": True, "path": ap})
        return self._send({"error": "not found"}, 404)

def start_inbox_watch():
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    class Ih(FileSystemEventHandler):
        def _m(self, src):
            if src and src.lower().endswith(".pdf"):
                time.sleep(2)
                process_one(os.path.abspath(src))
        def on_created(self, e):
            if not e.is_directory:
                self._m(e.src_path)
        def on_moved(self, e):
            if not e.is_directory:
                self._m(getattr(e, "dest_path", None) or e.src_path)
    os.makedirs(INBOX, exist_ok=True)
    ob = Observer()
    ob.schedule(Ih(), INBOX, recursive=True)
    ob.start()
    return ob

def _start_semantic():
    """Warm the vector matrix and top up whatever has no vector yet."""
    try:
        st = vectors.status()
        if not st["dim"]:
            emit("semantic     no vectors yet - use Health ▸ Build vectors "
                 "or py -3 studio.py embed")
            return
        vectors.warm()
        search_api.hybrid_reset()
        emit(f"semantic     {st['embedded']}/{st['pages']} pages embedded "
             f"({st['pct']}%)")
        if os.environ.get("WOOD_EMBED_AUTOBUILD", "1") in ("0", "false", "off"):
            return
        if st["embedded"] >= st["pages"] > 0:
            emit("semantic     up to date")
            return
        vectors.build(on_event=lambda s, ti, de: emit(
            f"{s:12} {ti[:50]}  {de[:80]}"))
        search_api.hybrid_reset()
    except Exception as e:
        emit(f"semantic     startup failed: {e}")


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()

def main():
    global writer, ocr
    if not os.path.isdir(INBOX):
        os.makedirs(INBOX, exist_ok=True)
    os.makedirs(LOGDIR, exist_ok=True)
    lib0 = lib_audit()
    store.init_db()
    writer = store.Writer()
    ocr = ocr_worker.OcrJob(writer, on_event, dpi=150)
    emit(f"studio up  (library {lib0['lib_size']} bytes, mtime {lib0['lib_mtime']})")
    emit(f"library root {store.ROOT}")
    emit(f"index db     {store.DB}")
    # OCR uses the GPU whenever a CUDA session can be created (~3.5x faster on
    # an RTX 3080); set WOOD_OCR_PROVIDERS=CPU to force the CPU.
    if ocr_worker.cuda_available():
        emit(f"ocr         GPU available ({ocr_worker.ocr_providers() or 'CUDA'})")
    elif ocr_worker.ocr_providers():
        emit(f"ocr         GPU requested but unavailable -> CPU "
             f"(missing CUDA runtime? see README)")
    else:
        emit("ocr         CPU (no CUDA session)")
    # Drop queue sentinels left by older builds: they are not jobs and used to
    # keep "active jobs" pinned at 1 forever.
    try:
        dropped = writer.submit(lambda c: c.execute(
            "DELETE FROM jobs WHERE trim(path) IN ('-','') "
            "AND trim(title) IN ('-','')").rowcount)
        if dropped:
            emit(f"cleanup     removed {dropped} stale job sentinel row(s)")
    except Exception:
        pass
    scan_inbox()
    ob = start_inbox_watch()
    ip = lan_ip()
    print("====================================================")
    print("Woodshed Studio Lab v" + VERSION)
    print(f"  > On this PC               : http://localhost:{PORT}")
    print(f"  > On your phone (same Wi-Fi): http://{ip}:{PORT}")
    print("  > Password                 : "
          + ("required" if store.password() else "not set (open to anyone who can reach this port)"))
    print(f"  > Library root             : {store.ROOT}")
    print(f"  > Library DB               : {store.DB}")
    print(f"  > Inbox (drop folder)      : {INBOX}")
    print("====================================================")
    try:
        search_api.graph()  # pre-warm topic graph
    except Exception:
        pass
    # Step 2: load the concept vectors + ONNX model off the request path, so
    # the first semantic query is not paying for a model load.
    threading.Thread(target=search_api.warm_concepts, daemon=True).start()
    # Semantic page vectors: warm the matrix, then embed whatever is missing in
    # the background. It is incremental, so a restart resumes instead of
    # redoing the library; on this machine the GPU builds all 99k pages in
    # about 70 s, the CPU in ~1.8 h. Set WOOD_EMBED_AUTOBUILD=0 to skip it.
    threading.Thread(target=_start_semantic, daemon=True).start()
    try:
        ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        ob.stop()

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Ingest: stability wait -> PyMuPDF extract -> classify -> index.
Rules: MIN_PAGE_CHARS=40, IMG_ONLY_RATIO=0.15,
collection = first folder under the library root (e.g. 'new books'),
title=filename.  Writes via store.Writer into the index.db.

`walk_library()` finds every PDF in an arbitrary archive tree, so a library
does not have to be assembled inside one drop folder.
"""
import os, time
import years, titles, store

ROOT = store.ROOT
MIN_PAGE_CHARS = 40
IMG_ONLY_RATIO = 0.15
STABLE_SECS = 2.0
STABLE_TIMEOUT = 120
IGNORE_SUFFIX = (".tmp", ".part", ".crdownload", ".bak", ".log")
IGNORE_PREFIX = ("~$", "._", "_tmp_")


def collection_for(path):
    """Top-level folder under the library root - the book list's grouping.

    A book dropped straight into the root (or living outside it) falls back to
    the drop folder's name, so the value is never empty.
    """
    d = os.path.dirname(os.path.abspath(path))
    try:
        rel = os.path.relpath(d, ROOT)
    except ValueError:                    # different drive -> not under ROOT
        rel = "."
    if rel and not rel.startswith("..") and rel != ".":
        return rel.split(os.sep)[0]
    return os.path.basename(store.INBOX) or "library"


def walk_library(root=None, skip_dirs=None, follow_symlinks=False):
    """Every PDF under `root`, recursively. Sorted by size so a scan indexes
    the small books first and the user sees results come in quickly.
    """
    root = root or store.ROOT
    skip = {d.lower() for d in (skip_dirs if skip_dirs is not None
                                else store.SKIP_DIRS)}
    found = []
    for dirpath, dirnames, files in os.walk(root, followlinks=follow_symlinks):
        dirnames[:] = [d for d in dirnames
                       if d.lower() not in skip and not d.startswith(".")]
        for f in files:
            p = os.path.abspath(os.path.join(dirpath, f))
            if ignorable(p):
                continue
            found.append(p)
    found.sort(key=lambda p: (os.path.getsize(p) if os.path.exists(p) else 0, p))
    return found

def ignorable(path):
    b = os.path.basename(path)
    return (not b.lower().endswith(".pdf") or b.startswith(IGNORE_PREFIX)
            or b.lower().endswith(IGNORE_SUFFIX))

def wait_stable(path, timeout=STABLE_TIMEOUT):
    t0, last, steady = time.time(), -1, None
    while time.time() - t0 < timeout:
        try:
            size = os.path.getsize(path)
        except OSError:
            time.sleep(1.0); continue
        now = time.time()
        if size == last and size > 0:
            if steady is None:
                steady = now
            if now - steady >= STABLE_SECS:
                return True
        else:
            steady = None
        last = size; time.sleep(1.0)
    return os.path.exists(path)

def extract_pdf(path):
    import fitz
    try:
        doc = fitz.open(path)
    except Exception as e:
        return None, f"openfail:{e}"
    try:
        pages = [(p.get_text("text") or "") for p in doc]
        doc.close()
        return pages, None
    except Exception as e:
        try:
            doc.close()
        except Exception:
            pass
        return None, f"extract-error:{e}"

def _meta_title(path):
    """The PDF's own Title field, or '' if it cannot be read.

    Best-effort by design: a PDF with an unreadable xref still indexes its
    text, and a missing Title just means the folder rule decides the name.
    """
    try:
        import fitz
        d = fitz.open(path)
        try:
            return (d.metadata or {}).get("title") or ""
        finally:
            d.close()
    except Exception:
        return ""


def index_file(path, writer, on_event):
    """Full ingest of one PDF. Returns (status, detail)."""
    abspath = os.path.abspath(path)
    title = os.path.splitext(os.path.basename(path))[0]
    collection = collection_for(abspath)
    on_event("extracting", title, "reading pages with PyMuPDF")
    pages, err = extract_pdf(abspath)
    if pages is None:
        # A failed PDF gets a `jobs` row but NO `sources` row, on purpose.
        # `server.indexed_paths()` treats every `sources` path as already
        # indexed, and both the watcher and `_new_pdfs()` skip those. Writing
        # a 0-page `sources` row here therefore marked the file as done for
        # good: a PDF caught half-copied during a bulk import stayed a silent
        # 0-page book and was never retried, even after the copy finished.
        # Staying out of `sources` leaves it visible as "not indexed", so the
        # next scan tries it again - which is what you want for a truncated
        # file, and harmless for one that is genuinely broken (the `jobs` row
        # still records why it failed).
        def _fail(c):
            c.execute("INSERT OR IGNORE INTO jobs(path,title,status,detail,updated)"
                      " VALUES(?,?,?,?,?)", (abspath, title, "failed", err, time.time()))
        writer.submit(_fail)
        return "failed", err
    real = [(i + 1, p) for i, p in enumerate(pages) if len(p.strip()) >= MIN_PAGE_CHARS]
    npages, nreal = len(pages), len(real)
    ratio = (nreal / npages) if npages else 0
    status = "image-only" if ratio < IMG_ONLY_RATIO else "indexed"
    on_event("indexing", title, f"{nreal}/{npages} text pages")
    ordered = sorted(real, key=lambda r: r[0])
    head = " ".join(p for _, p in ordered[:6])
    tail = " ".join(p for _, p in ordered[-4:])
    y = years.book_year(title, head, tail)
    # Display title, resolved once for both branches below. A magazine issue
    # stored as `7.pdf` gets its publication name here; a book whose filename
    # already names it comes back as None and keeps the plain filename title.
    pub, pub_src = titles.detect(
        abspath, collection=collection, meta_title=_meta_title(abspath),
        head_text=ordered[0][1] if ordered else "", year=y)
    if status == "indexed":
        def _ok(c):
            c.executemany("INSERT INTO pages(text,collection,title,page,path)"
                          " VALUES(?,?,?,?,?)",
                          [(t, collection, title, str(n), abspath) for n, t in real])
            rids = [r[0] for r in c.execute(
                "SELECT rowid FROM pages WHERE path=?", (abspath,))]
            c.executemany("INSERT OR REPLACE INTO pyear VALUES(?,?)",
                          [(r, y) for r in rids])
            chars = sum(len(p) for _, p in real)
            c.execute("INSERT INTO sources"
                      "(collection,title,path,pages,indexed_pages,chars,status,"
                      "pub,pub_src) VALUES(?,?,?,?,?,?,?,?,?)",
                      (collection, title, abspath, npages, nreal, chars, status,
                       pub, pub_src))
            c.execute("INSERT OR REPLACE INTO jobs(path,title,status,detail,updated)"
                      " VALUES(?,?,?,?,?)",
                      (abspath, title, "done", f"{nreal}/{npages}p year={y}", time.time()))
        writer.submit(_ok)
        return "done", f"{nreal}/{npages}p year={y}"
    def _img(c):
        c.execute("INSERT INTO sources"
                  "(collection,title,path,pages,indexed_pages,chars,status,"
                  "pub,pub_src) VALUES(?,?,?,?,?,?,?,?,?)",
                  (collection, title, abspath, npages, nreal, 0, status,
                   pub, pub_src))
        c.execute("INSERT OR REPLACE INTO jobs(path,title,status,detail,updated)"
                  " VALUES(?,?,?,?,?)",
                  (abspath, title, "image-only",
                   f"scan: only {nreal}/{npages}p have text - OCR to unlock", time.time()))
    writer.submit(_img)
    return "image-only", f"scan: only {nreal}/{npages}p have text"

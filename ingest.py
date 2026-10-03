#!/usr/bin/env python3
"""Ingest: stability wait -> PyMuPDF extract -> classify -> index.
Rules: MIN_PAGE_CHARS=40, IMG_ONLY_RATIO=0.15,
collection = first folder under the library root (e.g. 'new books'),
title=filename.  Writes via store.Writer into the index.db.

`walk_library()` finds every PDF in an arbitrary archive tree, so a library
does not have to be assembled inside one drop folder.
"""
import os, time
import years, store

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

def index_file(path, writer, on_event):
    """Full ingest of one PDF. Returns (status, detail)."""
    abspath = os.path.abspath(path)
    title = os.path.splitext(os.path.basename(path))[0]
    collection = collection_for(abspath)
    on_event("extracting", title, "reading pages with PyMuPDF")
    pages, err = extract_pdf(abspath)
    if pages is None:
        def _fail(c):
            c.execute("INSERT OR IGNORE INTO jobs(path,title,status,detail,updated)"
                      " VALUES(?,?,?,?,?)", (abspath, title, "failed", err, time.time()))
            c.execute("INSERT INTO sources VALUES(?,?,?,?,?,?,?)",
                      (collection, title, abspath, 0, 0, 0, err))
        writer.submit(_fail)
        return "failed", err
    real = [(i + 1, p) for i, p in enumerate(pages) if len(p.strip()) >= MIN_PAGE_CHARS]
    npages, nreal = len(pages), len(real)
    ratio = (nreal / npages) if npages else 0
    status = "image-only" if ratio < IMG_ONLY_RATIO else "indexed"
    on_event("indexing", title, f"{nreal}/{npages} text pages")
    if status == "indexed":
        ordered = sorted(real, key=lambda r: r[0])
        head = " ".join(p for _, p in ordered[:6])
        tail = " ".join(p for _, p in ordered[-4:])
        y = years.book_year(title, head, tail)
        def _ok(c):
            c.executemany("INSERT INTO pages(text,collection,title,page,path)"
                          " VALUES(?,?,?,?,?)",
                          [(t, collection, title, str(n), abspath) for n, t in real])
            rids = [r[0] for r in c.execute(
                "SELECT rowid FROM pages WHERE path=?", (abspath,))]
            c.executemany("INSERT OR REPLACE INTO pyear VALUES(?,?)",
                          [(r, y) for r in rids])
            chars = sum(len(p) for _, p in real)
            c.execute("INSERT INTO sources VALUES(?,?,?,?,?,?,?)",
                      (collection, title, abspath, npages, nreal, chars, status))
            c.execute("INSERT OR REPLACE INTO jobs(path,title,status,detail,updated)"
                      " VALUES(?,?,?,?,?)",
                      (abspath, title, "done", f"{nreal}/{npages}p year={y}", time.time()))
        writer.submit(_ok)
        return "done", f"{nreal}/{npages}p year={y}"
    def _img(c):
        c.execute("INSERT INTO sources VALUES(?,?,?,?,?,?,?)",
                  (collection, title, abspath, npages, nreal, 0, status))
        c.execute("INSERT OR REPLACE INTO jobs(path,title,status,detail,updated)"
                  " VALUES(?,?,?,?,?)",
                  (abspath, title, "image-only",
                   f"scan: only {nreal}/{npages}p have text - OCR to unlock", time.time()))
    writer.submit(_img)
    return "image-only", f"scan: only {nreal}/{npages}p have text"

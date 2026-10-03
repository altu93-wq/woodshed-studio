#!/usr/bin/env python3
"""Semantic layer: one embedding vector per indexed page, plus a cosine search.

Why a plain file and not sqlite-vec: there is no `sqlite_vec` here and none is
needed. 99k pages x 384 float32 = ~152 MB, which fits in RAM easily, so the
whole matrix is a numpy array and a query is one matrix-vector product
(~15 ms) — far cheaper than maintaining a vector index.

Layout (all under `_index/vectors/`, next to index.db):
    pages.f32   raw float32 matrix, row `i` = vector for `rowids[i]`
    map.db      (rowid INTEGER PRIMARY KEY, slot INT) — which vector is where
    meta.json   model name, dim, chunk settings, build progress

Why a map table and not "slot == rowid": page rowids are not contiguous (a
delete leaves holes) and pages get re-indexed, so the mapping has to be
explicit. It is also what makes a build resumable: the map holds exactly the
slots that are on disk, and the file is append-only.

The build is incremental and interruptible. Re-running it embeds only the
pages that have no vector yet, which is also what happens after you add or
OCR a book, so the semantic layer never goes stale.
"""
import json
import os
import sqlite3
import threading
import time

import store

VECDIR = os.path.join(os.path.dirname(store.DB), "vectors")
VECFILE = os.path.join(VECDIR, "pages.f32")
MAPDB = os.path.join(VECDIR, "map.db")
META = os.path.join(VECDIR, "meta.json")


def concept_path(name="concepts.npz"):
    """Per-library concept layer (tag embeddings), beside index.db.

    Not in the app folder: the tags come from one library's `articles` table,
    so a file shared between libraries would offer one library's vocabulary as
    another's. `search_api` reads a pre-move copy from the app folder once, for
    installs that predate this.
    """
    return os.path.join(VECDIR, name)

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
MAX_CHARS = 1200          # truncate a page to this many characters before
                          # embedding; measured page average is ~1150 and the
                          # model's limit is 512 tokens, so more only costs time
MIN_CHARS = 40            # a page with less text than this carries no meaning
                          # worth a vector (same floor as ingest)

BATCH = 256

_S = {"mat": None, "n": 0, "err": None, "lock": threading.Lock(),
      "model": None, "model_err": None, "meta": None, "meta_mtime": None}


# ---- metadata ---------------------------------------------------------------

def _meta():
    """The build metadata, re-read whenever the file changed on disk.

    Another process can build the vectors (`py -3 studio.py embed`) while the
    server is running. A cache that never looked again would keep reporting
    "no vectors yet" for a store that is complete, and the server would refuse
    to blend in results that do exist.
    """
    try:
        m = os.path.getmtime(META)
    except OSError:
        m = 0
    if _S["meta"] is None or m != _S["meta_mtime"]:
        try:
            with open(META, encoding="utf-8") as f:
                _S["meta"] = json.load(f)
        except (OSError, ValueError):
            if _S["meta"] is None:
                _S["meta"] = {"model": os.environ.get("WOOD_EMBED_MODEL",
                                                      DEFAULT_MODEL),
                              "dim": 0, "built": 0, "chars": MAX_CHARS,
                              "last_run": 0, "runs": 0}
        _S["meta_mtime"] = m
    return _S["meta"]


def _save_meta():
    with open(META, "w", encoding="utf-8") as f:
        json.dump(_S["meta"], f, indent=2)
    try:
        _S["meta_mtime"] = os.path.getmtime(META)
    except OSError:
        _S["meta_mtime"] = 0


def map_conn(create=False):
    if not os.path.isdir(VECDIR):
        if not create:
            raise sqlite3.OperationalError("no vector store yet")
        os.makedirs(VECDIR, exist_ok=True)
    c = sqlite3.connect(MAPDB, timeout=60)
    c.execute("PRAGMA busy_timeout=60000")
    c.execute("CREATE TABLE IF NOT EXISTS vecmap("
              "rowid INTEGER PRIMARY KEY, slot INTEGER UNIQUE)")
    return c


BUILD = {"running": False, "done": 0, "total": 0, "started": 0, "stop": False,
         "last_ms_per_page": 0.0}


_ST = {"fp": None, "pages": 0, "skipped": 0}


def _page_stats():
    """(pages, skipped) — memoised, because both are full scans of `pages`.

    `pages` is an FTS5 virtual table, so `COUNT(*)` reads every row: 378 ms
    here, and `/api/health` polls this every couple of seconds. The fingerprint
    is a handful of aggregates over `sources` (a few hundred plain rows), which
    moves whenever a book is indexed, re-indexed or deleted — exactly when the
    page table changes. Same trick as `server.book_years()`.
    """
    try:
        c = store.ro()
    except Exception:
        return _ST["pages"], _ST["skipped"]
    try:
        fp = c.execute("SELECT count(*), sum(indexed_pages), sum(pages), "
                       "max(rowid) FROM sources").fetchone()
        if fp != _ST["fp"]:
            _ST["pages"] = c.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
            _ST["skipped"] = c.execute(
                "SELECT COUNT(*) FROM pages WHERE length(text)<?",
                (MIN_CHARS,)).fetchone()[0]
            _ST["fp"] = fp
        return _ST["pages"], _ST["skipped"]
    except Exception:
        return _ST["pages"], _ST["skipped"]
    finally:
        c.close()


def status():
    """What the Health tab shows. Never raises - a missing store is normal."""
    m = _meta()
    out = {"enabled": True, "model": m.get("model"), "dim": m.get("dim", 0),
           "embedded": 0, "pages": 0, "skipped": 0, "embeddable": 0, "pct": 0,
           "age_min": -1, "last_run": m.get("last_run") or 0,
           "runs": m.get("runs", 0), "dir": VECDIR,
           "bytes": 0, "building": BUILD["running"],
           "model_ready": model_ready(),
           "model_error": _S["model_err"], "error": _S["err"]}
    try:
        out["embedded"] = _count()
        out["bytes"] = os.path.getsize(VECFILE) if os.path.exists(VECFILE) else 0
    except Exception as e:
        out["error"] = str(e)
    try:
        # `skipped` = pages too short to embed. Reporting the gap matters:
        # without it the UI says "98,373 / 99,112 - top up" for a store that
        # is already complete, and offers a build that can only find nothing.
        out["pages"], out["skipped"] = _page_stats()
    except Exception:
        out["skipped"] = 0
    out["embeddable"] = max(0, out["pages"] - out["skipped"])
    if out["embeddable"]:
        out["pct"] = round(100.0 * min(out["embedded"], out["embeddable"])
                           / out["embeddable"])
    if m.get("last_run"):
        out["age_min"] = int((time.time() - m["last_run"]) / 60)
    return out


def _count():
    if not os.path.exists(MAPDB):
        return 0
    c = map_conn()
    try:
        return c.execute("SELECT COUNT(*) FROM vecmap").fetchone()[0]
    finally:
        c.close()


# ---- the matrix -------------------------------------------------------------

def model_ready():
    """Is `fastembed` importable here?

    Deliberately does NOT construct the model: `TextEmbedding(...)` downloads
    it on a cold cache, and /api/health is polled every couple of seconds, so
    probing with the real constructor would turn a status poll into a download.
    An already-built model in this process counts as ready without the import.
    """
    if _S["model"] is not None:
        return True
    try:
        import importlib.util
        return importlib.util.find_spec("fastembed") is not None
    except Exception:
        return False


def _vecfile_size():
    try:
        return os.path.getsize(VECFILE)
    except OSError:
        return -1


def load():
    """(matrix, n) with the matrix in RAM, or (None, 0) when not built.

    Loaded once per process - the second query must not pay for 152 MB of I/O -
    but re-read when the file grew underneath us, so a build run from another
    process (or a delete) is picked up instead of serving stale vectors.
    """
    m = _meta()
    size = _vecfile_size()
    if _S["mat"] is not None and _S["size"] != size:
        invalidate()
    if _S["mat"] is not None or _S["err"] is not None:
        return _S["mat"], _S["n"]
    with _S["lock"]:
        if _S["mat"] is not None or _S["err"] is not None:
            return _S["mat"], _S["n"]
        dim, n = m.get("dim") or 0, 0
        try:
            import numpy as np
            if dim and size > 0:
                n = _count()
                _S["mat"] = np.fromfile(VECFILE, dtype="float32",
                                        count=n * dim).reshape(n, dim)
                _S["n"] = n
                _S["size"] = _vecfile_size()
        except Exception as e:
            _S["err"] = str(e)
            _S["mat"] = None
    return _S["mat"], _S["n"]


def invalidate():
    """Drop the cached matrix - the build changed it underneath us."""
    with _S["lock"]:
        _S["mat"] = None
        _S["n"] = 0
        _S["size"] = None


def _model():
    """The ONNX embedding model, created once. Providers follow the GPU."""
    if _S["model"] is not None or _S["model_err"] is not None:
        return _S["model"]
    with _S["lock"]:
        if _S["model"] is not None or _S["model_err"] is not None:
            return _S["model"]
        try:
            from fastembed import TextEmbedding
            import search_api
            _S["model"] = TextEmbedding(model_name=_meta().get("model")
                                        or DEFAULT_MODEL,
                                        providers=search_api.embed_providers())
        except Exception as e:
            _S["model_err"] = str(e)
    return _S["model"]


def embed_texts(texts):
    """[(texts)] -> (n, dim) float32, L2-normalised, or None."""
    model = _model()
    if model is None or not texts:
        return None
    try:
        import numpy as np
        v = np.asarray(list(model.embed(texts)), dtype="float32")
        n = np.linalg.norm(v, axis=1, keepdims=True)
        np.maximum(n, 1e-12, out=n)
        return v / n
    except Exception as e:
        _S["model_err"] = str(e)
        return None


# ---- build ------------------------------------------------------------------


def pending(limit=None):
    """Page rowids that still need a vector, oldest first.

    The length filter lives in SQL: on this library the average page is ~3.8k
    characters, so pulling every row into Python just to discard the short ones
    would read hundreds of MB for nothing. Note the filter is MIN_CHARS (is
    there a page worth embedding), NOT MAX_CHARS (how much of it to keep) -
    confusing the two silently skips every page shorter than the cut-off.
    """
    c = store.ro()
    try:
        if os.path.exists(MAPDB):
            # ATTACH lets SQLite do the anti-join; 99k rowids never cross into
            # Python just to be filtered there.
            c.execute("ATTACH DATABASE ? AS map", (MAPDB,))
            q = ("SELECT p.rowid FROM pages p LEFT JOIN map.vecmap m "
                 "ON m.rowid=p.rowid WHERE m.rowid IS NULL "
                 "AND length(p.text)>=? ORDER BY p.rowid")
        else:
            q = "SELECT rowid FROM pages WHERE length(text)>=? ORDER BY rowid"
        p = [MIN_CHARS] + ([limit] if limit else [])
        return [r[0] for r in c.execute(q + (" LIMIT ?" if limit else ""), p)]
    finally:
        try:
            c.execute("DETACH DATABASE map")
        except sqlite3.Error:
            pass
        c.close()


def prune():
    """Forget vectors whose page rows are gone (a deleted or re-indexed book).

    Without this the matrix keeps rows that can never be returned and the
    coverage percentage in Health drifts downwards forever. Slots are not
    reclaimed: the file is append-only, so a pruned slot is simply unused.
    """
    if not os.path.exists(MAPDB):
        return 0
    m = map_conn()
    try:
        m.execute("ATTACH DATABASE ? AS lib", ("file:" + store.DB + "?mode=ro",))
        m.execute("ATTACH DATABASE ? AS main", (MAPDB,))
    except sqlite3.Error:
        m.close()
        return 0
    try:
        cur = m.execute(
            "DELETE FROM main.vecmap WHERE rowid NOT IN "
            "(SELECT rowid FROM lib.pages)")
        m.commit()
        return cur.rowcount
    finally:
        m.close()


def build(limit=None, on_event=None, stop_flag=None):
    """Embed every page that has no vector yet. Incremental and resumable.

    Appends to the matrix file and commits the map rows for that batch only, so
    killing the process at any point loses at most one batch of work.
    """
    if BUILD["running"]:
        return {"error": "a build is already running"}
    probe = embed_texts(["dimension probe"])
    if probe is None:
        return {"error": "embedding model unavailable: " + str(_S["model_err"])}
    dim = int(probe.shape[1])
    m = _meta()
    want = os.environ.get("WOOD_EMBED_MODEL", m.get("model") or DEFAULT_MODEL)
    if (m.get("dim") or 0) != dim or (m.get("model") != want):
        # a different model or width invalidates every stored vector: cosine
        # space is not comparable across models, so mixing them would silently
        # return nonsense instead of failing.
        _reset()
        with _S["lock"]:
            _S["model"] = None            # the cached model is the wrong one
            _S["model_err"] = None
        m = _meta()
    todo = pending(limit)
    BUILD.update(running=True, done=0, total=len(todo), stop=False)
    if not todo:
        BUILD["running"] = False
        if on_event:
            on_event("semantic", "nothing to do", "every page already has a vector")
        return {"embedded": 0, "total": 0, "note": "already up to date"}
    say = on_event or (lambda *a: None)
    t0 = time.time()
    try:
        import numpy as np
        os.makedirs(VECDIR, exist_ok=True)
        m.update(model=want, dim=dim)
        _save_meta()
        base = _count()
        say("semantic", f"{len(todo)} page(s) to embed",
            f"{m['model']} dim={dim}, {base} already stored")
        reader = store.ro()
        conn = map_conn(create=True)
        done = 0
        try:
            for i in range(0, len(todo), BATCH):
                if BUILD["stop"] or (stop_flag and stop_flag()):
                    say("semantic", "stopped", f"{done} embedded this run")
                    break
                ids = todo[i:i + BATCH]
                # dict, not order: an `IN (...)` list does not promise the rows
                # come back in the order they were asked for, and a mismatch
                # here would attach every vector to the wrong page silently.
                pairs = dict(reader.execute(
                    "SELECT rowid,text FROM pages WHERE rowid IN "
                    f"({','.join('?' * len(ids))})", ids))
                cap = m.get("chars") or MAX_CHARS
                vecs = embed_texts([(pairs.get(r) or " ")[:cap] for r in ids])
                if vecs is None:
                    say("semantic", "aborted", str(_S["model_err"]))
                    break
                with open(VECFILE, "ab") as f:
                    vecs.astype("float32").tofile(f)
                conn.executemany("INSERT OR REPLACE INTO vecmap(rowid,slot) "
                                 "VALUES(?,?)",
                                 [(r, base + i + j) for j, r in enumerate(ids)])
                conn.commit()
                done += len(ids)
                BUILD["done"] = done
                if i and i % (BATCH * 10) == 0:
                    el = time.time() - t0
                    per = el / done
                    BUILD["last_ms_per_page"] = per * 1000
                    say("semantic", f"{done}/{len(todo)} pages",
                        f"{per * 1000:.1f} ms/page, "
                        f"{el / max(1, done) * (len(todo) - done):.0f}s left")
            conn.commit()
        finally:
            conn.close()
            reader.close()
    except Exception as e:
        say("semantic", "FAILED", str(e))
        BUILD["running"] = False
        return {"error": str(e)}
    m = _meta()
    m.update(built=_count(), last_run=time.time(),
             runs=m.get("runs", 0) + 1)
    _save_meta()
    invalidate()
    el = time.time() - t0
    say("semantic", f"{done} page(s) embedded",
        f"{el:.0f}s total, {el / max(1, done) * 1000:.1f} ms/page")
    BUILD["running"] = False
    BUILD["last_ms_per_page"] = el / max(1, done) * 1000
    return {"embedded": done, "total": len(todo), "seconds": round(el, 1),
            "ms_per_page": round(BUILD["last_ms_per_page"], 2)}


def _reset():
    """Start the vector store over (model change or a corrupted file)."""
    invalidate()
    for p in (MAPDB, VECFILE, META):
        try:
            os.remove(p)
        except OSError:
            pass
    # go through _meta() so the dict exists even when nothing has read it yet
    _S["meta"] = None
    _S["meta_mtime"] = None
    S = _meta()
    S.clear()
    S.update({"model": os.environ.get("WOOD_EMBED_MODEL", DEFAULT_MODEL),
              "dim": 0, "built": 0, "chars": MAX_CHARS,
              "last_run": 0, "runs": 0})


def build_background(limit=None, on_event=None):
    """Start build() on its own thread. Returns the thread, or None if busy."""
    if BUILD["running"]:
        return None
    t = threading.Thread(target=build, kwargs={"limit": limit,
                                               "on_event": on_event},
                         daemon=True)
    t.start()
    return t


# ---- query ------------------------------------------------------------------

def search(q, k=200, rows=None):
    """[(rowid, cosine)] - the k pages closest to q.

    `rows`, when given, restricts the search to those rowids (used to blend the
    semantic side with the keyword side instead of scanning the whole matrix).
    Without it this is a full scan: one 152 MB matrix-vector product, ~15 ms.
    """
    mat, n = load()
    if mat is None or n == 0 or not q:
        return []
    import numpy as np
    v = embed_texts([q])
    if v is None:
        return []
    v = v[0]
    if rows is None:
        sims = mat @ v
        idx = np.argpartition(-sims, min(k, n - 1))[:k]
        idx = idx[np.argsort(-sims[idx])]
        return [(int(i), float(sims[i])) for i in idx]
    pos = {r: i for i, r in enumerate(rows)}
    if not pos:
        return []
    c = map_conn()
    try:
        slots = dict(c.execute(
            "SELECT rowid,slot FROM vecmap WHERE rowid IN "
            f"({','.join('?' * len(pos))})", list(pos)))
    finally:
        c.close()
    pairs = [(pos[r], s) for r, s in slots.items() if s < n]
    if not pairs:
        return []
    p = np.asarray([i for i, _ in pairs], dtype="int64")
    s = np.asarray([sl for _, sl in pairs], dtype="int64")
    sims = mat[s] @ v
    order = np.argsort(-sims)[:k]
    return [(int(p[i]), float(sims[i])) for i in order]


def warm():
    """Pre-load the matrix at server start so the first hybrid query is fast."""
    load()
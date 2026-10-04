#!/usr/bin/env python3
"""DB layer: single-writer queue over SQLite FTS5 (the library index).

Everything about *where* things live is resolved here, once, from three
sources in this order:

  1. environment variables  WOOD_ROOT / WOOD_INDEX_DB / WOOD_INBOX / WOOD_TRASH
  2. `config.json` next to this file   {"library_root": ..., "inbox": ...}
  3. auto-detection: the app folder's parent, if it looks like a library
     (it holds an `_index` folder)

So the app works unchanged over any PDF archive: point WOOD_ROOT at the folder
that holds the books and everything else follows.

init_db() creates the FULL schema (sources, pages FTS5, pyear, fwwmap, jobs,
articles, meta) with IF NOT EXISTS, so a brand-new empty archive gets a
complete index on first run and an existing index is left alone.

All writes go through one connection owned by the writer thread; readers
use mode=ro. WAL + busy_timeout keep the index readable while we write.
"""
import os, json, sqlite3, threading, queue

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")


def load_config():
    """config.json is optional; a malformed one must not stop the app."""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}


CFG = load_config()
_CFG_MTIME = None


def _cfg_fresh():
    """Pick up a hand-edited config.json without a restart.

    A stat per call is cheap next to the request it rides on, and it is the
    difference between "edit the file, restart the server" and "edit the file".
    Without it, deleting config.json to remove a password would look like it
    worked while the running server carried on demanding it.
    """
    global CFG, _CFG_MTIME
    try:
        m = os.path.getmtime(CONFIG_PATH)
    except OSError:
        m = None
    if m != _CFG_MTIME:
        CFG = load_config()
        _CFG_MTIME = m
    return CFG


def _setting(env, key, default):
    """env var wins over config.json, which wins over the built-in default."""
    v = os.environ.get(env)
    if v:
        return v
    v = CFG.get(key)
    if v:
        return v
    return default


def password():
    """Optional shared secret required on every request. Empty = no auth.

    Empty is the right default for a laptop nobody else can reach. Set it the
    moment the app is reachable from another machine: every /api endpoint can
    delete from the index, start OCR or write files, so being able to open the
    page has to mean being allowed to do that.
    """
    _cfg_fresh()
    return _setting("WOOD_PASSWORD", "password", "")


def password_source():
    """Which of the three sources is actually deciding the password.

    "env" means config.json is being ignored, so a password saved from the
    Health tab would look saved and do nothing - the UI has to be able to say
    that out loud instead of quietly pretending it worked.
    """
    if os.environ.get("WOOD_PASSWORD"):
        return "env"
    if _cfg_fresh().get("password"):
        return "config"
    return "none"


def set_password(value):
    """Write or clear the password in config.json, effective immediately.

    Re-reads the file first so it merges into whatever is already in there
    (library_root, inbox, ...) instead of replacing it, and writes through a
    temp file + os.replace so a crash mid-write cannot leave a half-written
    config that fails to parse on the next start.
    """
    global CFG, _CFG_MTIME
    value = str(value or "").strip()
    cfg = load_config()
    cfg["password"] = value
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, CONFIG_PATH)
    CFG = cfg            # no restart: the next request already sees this
    _CFG_MTIME = os.path.getmtime(CONFIG_PATH)
    return {"ok": True, "password_set": bool(value),
            "source": password_source(), "stored_in": CONFIG_PATH}


def _auto_root():
    """The library root, when the app sits inside one.

    The shipped layout is `<library>/_index/<app>/server.py`, so we walk up a
    couple of levels looking for a parent that owns an `_index` folder. A
    standalone copy of the app (books simply beside it) matches nothing and
    falls back to the app's own parent. Set WOOD_ROOT or config.json for any
    other arrangement - the guess is only a default.
    """
    d = HERE
    for _ in range(3):
        parent = os.path.dirname(d)
        if not parent or parent == d:
            break
        if os.path.isdir(os.path.join(parent, "_index")):
            return parent
        d = parent
    return os.path.dirname(HERE)


ROOT = os.path.abspath(_setting("WOOD_ROOT", "library_root", _auto_root()))
DB = os.path.abspath(_setting("WOOD_INDEX_DB", "index_db",
                               os.path.join(ROOT, "_index", "index.db")))
INBOX = os.path.abspath(_setting("WOOD_INBOX", "inbox",
                                 os.path.join(ROOT, "new books")))
TRASH = os.path.abspath(_setting("WOOD_TRASH", "trash",
                                 os.path.join(ROOT, "_trash")))

# never walked, never indexed: our own folder, the database folder, the trash
SKIP_DIRS = {"_index", "_trash", "__pycache__", "vendor", "node_modules",
             ".git", ".svn"}

ROOTKEY = os.sep + os.path.basename(ROOT) + os.sep


def describe():
    """One place that knows the layout - printed by the CLI and the server."""
    return {
        "app": HERE,
        "library_root": ROOT,
        "index_db": DB,
        "inbox": INBOX,
        "trash": TRASH,
        "db_exists": os.path.exists(DB),
        "db_size": os.path.getsize(DB) if os.path.exists(DB) else -1,
    }


def resolve_path(p):
    """Make a stored absolute path work even if the drive letter changed."""
    if not p:
        return p
    if os.path.exists(p):
        return p
    q = p.replace("/", os.sep)
    i = q.lower().rfind(ROOTKEY)
    if i >= 0:
        cand = os.path.join(ROOT, q[i + len(ROOTKEY):])
        if os.path.exists(cand):
            return cand
    return p


SCHEMA = """
CREATE TABLE IF NOT EXISTS sources(
    collection TEXT, title TEXT, path TEXT,
    pages INT, indexed_pages INT, chars INT, status TEXT,
    pub TEXT, pub_src TEXT);

CREATE TABLE IF NOT EXISTS fwwmap(rowid INTEGER PRIMARY KEY, pdf_path TEXT, pdf_page INT);

CREATE TABLE IF NOT EXISTS pyear(rowid INTEGER PRIMARY KEY, year INT);
CREATE INDEX IF NOT EXISTS ix_pyear ON pyear(year);

CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT UNIQUE,
  title TEXT, status TEXT, detail TEXT, updated REAL);

CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v);

CREATE VIRTUAL TABLE IF NOT EXISTS pages USING fts5(
        text,
        collection UNINDEXED,
        title      UNINDEXED,
        page       UNINDEXED,
        path       UNINDEXED,
        tokenize = 'porter unicode61'
    );

CREATE VIRTUAL TABLE IF NOT EXISTS articles USING fts5(
        headline, subhead, abstract, taxonomy,
        author  UNINDEXED,
        pageref UNINDEXED,
        issue   UNINDEXED,
        tokenize = 'porter unicode61'
    );
"""


def init_db():
    """Create the whole schema if it is missing; safe to call on every start."""
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c = sqlite3.connect(DB, timeout=60)
    try:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        # `pub` is the display title shown in search results and the book list.
        # It is kept next to `title` rather than overwriting it: `title` stays
        # the honest filename-derived name (and what `fwwmap`/`pages` agree
        # on), while `pub` is what the reader should see. `pub_src` records how
        # it was derived so a wrong guess is traceable instead of mysterious.
        for col, decl in (("pub", "TEXT"), ("pub_src", "TEXT")):
            have = {r[1] for r in c.execute("PRAGMA table_info(sources)")}
            if col not in have:
                c.execute(f"ALTER TABLE sources ADD COLUMN {col} {decl}")
        c.commit()
    finally:
        c.close()


def ro():
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)


class Writer:
    """Single writer: submit(fn) runs fn(conn) serially on the owned conn."""
    def __init__(self):
        self.q = queue.Queue()
        self.conn = sqlite3.connect(DB, timeout=60, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=60000")
        self.t = threading.Thread(target=self._loop, daemon=True)
        self.t.start()

    def _loop(self):
        while True:
            fn, done = self.q.get()
            try:
                out = fn(self.conn)
                self.conn.commit()
                done.put((True, out))
            except Exception as e:
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                done.put((False, e))

    def submit(self, fn):
        done = queue.Queue(maxsize=1)
        self.q.put((fn, done))
        ok, out = done.get()
        if not ok:
            raise out
        return out
#!/usr/bin/env python3
"""Command line for Woodshed Studio: the parts you need before/without the UI.

  py -3 studio.py where          show the resolved paths (root, db, inbox, trash)
  py -3 studio.py init           create the full index schema (idempotent)
  py -3 studio.py scan           index every PDF in the library not indexed yet
  py -3 studio.py scan --dry-run list what would be indexed, change nothing
  py -3 studio.py scan --root D  scan another tree (must stay under the root
                                 that index.db belongs to, or paths break)
  py -3 studio.py serve          start the web app (same as server.py)
  py -3 studio.py paths --root D --db E --inbox F   write config.json
  py -3 studio.py embed            build the semantic page vectors (resumable)
  py -3 studio.py embed --status   how many pages already have a vector

Examples
--------
  # index a library that is not next to the app folder
  set WOOD_ROOT=D:\\MyBooks
  py -3 studio.py init
  py -3 studio.py scan
  py -3 studio.py serve

`store.py` reads WOOD_ROOT / WOOD_INDEX_DB / WOOD_INBOX / WOOD_TRASH and
config.json, so nothing here has to run before the server starts.
"""
import argparse
import os
import sys
import time

import store
import ingest
import vectors


def cmd_where(a):
    d = store.describe()
    print("app folder     :", d["app"])
    print("library root   :", d["library_root"],
          "" if os.path.isdir(d["library_root"]) else "  <-- MISSING")
    print("index db       :", d["index_db"],
          f"({d['db_size'] / 1048576:.1f} MB)" if d["db_exists"] else "  <-- not created yet")
    print("drop folder    :", d["inbox"],
          "" if os.path.isdir(d["inbox"]) else "  <-- will be created")
    print("trash          :", d["trash"])
    print("env overrides  :", ", ".join(
        f"{k}={os.environ[k]}" for k in
        ("WOOD_ROOT", "WOOD_INDEX_DB", "WOOD_INBOX", "WOOD_TRASH") if k in os.environ)
        or "none")
    print("config.json    :", "read" if store.CFG else "absent (defaults in use)")
    return 0


def cmd_init(a):
    store.init_db()
    print("schema ready:", store.DB)
    # _tables() already returns names, not rows — indexing again here printed
    # "a, f, j, m, p, p, s" because it took the first letter of each name.
    print("  tables:", ", ".join(_tables()))
    return 0


def _tables():
    """User-facing tables, minus the FTS5 shadow tables (`pages_data` etc.)."""
    c = store.ro()
    try:
        return [r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
            "AND name NOT LIKE '%!_%' ESCAPE '!' ORDER BY name",
            )]
    finally:
        c.close()


def _db_locked():
    """True when another process is writing index.db right now.

    The design is one writer, so a CLI scan while the server runs would compete
    for the same FTS5 index. Only a non-empty `-wal` counts as evidence: a
    read-only connection also creates `-shm`, so that file alone proves nothing.
    """
    p = store.DB + "-wal"
    return os.path.exists(p) and os.path.getsize(p) > 0


def cmd_scan(a):
    root = os.path.abspath(a.root) if a.root else store.ROOT
    if not os.path.isdir(root):
        print("not a folder:", root, file=sys.stderr)
        return 2
    if _db_locked():
        print("note: the web app is probably running; two writers on one "
              "index.db is not supported. Stop the server first.", file=sys.stderr)

    found = ingest.walk_library(root)
    c = store.ro()
    try:
        done = {os.path.normcase(os.path.abspath(r[0]))
                for r in c.execute("SELECT path FROM sources")}
        skip = {os.path.normcase(os.path.abspath(r[0]))
                for r in c.execute(
                    "SELECT path FROM jobs WHERE status='duplicate' AND path")}
    finally:
        c.close()

    fresh = [p for p in found
             if os.path.normcase(p) not in done and os.path.normcase(p) not in skip]
    print(f"{len(found)} pdf(s) under {root}")
    print(f"{len(found) - len(fresh)} already indexed, {len(fresh)} new")
    for p in fresh[:40]:
        print("  +", p)
    if len(fresh) > 40:
        print(f"  ... and {len(fresh) - 40} more")
    if a.dry_run:
        return 0
    if not fresh:
        return 0

    import server                       # single writer + one indexer thread
    server.writer = store.Writer()
    t0 = time.time()

    def on_event(status, title, detail, path=None):
        print(f"  [{status}] {title} {detail[:60]}")
    ok = fail = 0
    for i, p in enumerate(fresh, 1):
        try:
            status, detail = ingest.index_file(p, server.writer, on_event)
        except Exception as e:
            print(f"  ! {os.path.basename(p)}: {e}", file=sys.stderr)
            fail += 1
            continue
        ok += status == "done"
        fail += status != "done"
        print(f"[{i}/{len(fresh)}] {status:11} {os.path.basename(p)[:60]}")
    print(f"done in {time.time() - t0:.0f}s: {ok} indexed, {fail} other")
    return 0


def cmd_paths(a):
    cfg = dict(store.CFG)
    if a.root:
        cfg["library_root"] = os.path.abspath(a.root)
    if a.db:
        cfg["index_db"] = os.path.abspath(a.db)
    if a.inbox:
        cfg["inbox"] = os.path.abspath(a.inbox)
    if a.trash:
        cfg["trash"] = os.path.abspath(a.trash)
    import json
    with open(store.CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    print("wrote", store.CONFIG_PATH)
    for k, v in cfg.items():
        print(f"  {k} = {v}")
    return 0


def cmd_embed(a):
    """Build the semantic page vectors. Incremental: resumes where it stopped."""
    import ocr_worker
    st = vectors.status()
    print(f"model  : {st['model'] or '(not built yet)'}  dim={st['dim']}")
    print(f"store  : {st['dir']}  {st['bytes'] / 1048576:.1f} MB")
    print(f"vectors: {st['embedded']} / {st['pages']} pages ({st['pct']}%)")
    print(f"engine : {'CUDA' if ocr_worker.cuda_available() else 'CPU'}")
    if a.status:
        return 0
    if a.reset:
        vectors._reset()
        st = vectors.status()
        print("reset:", st["embedded"], "vectors left")
    if not a.reset:
        n = vectors.prune()
        if n:
            print(f"pruned {n} vector(s) whose page rows are gone")
    r = vectors.build(limit=a.limit)
    if r.get("error"):
        print("error:", r["error"], file=sys.stderr)
        return 1
    print(f"embedded {r.get('embedded', 0)} page(s) in {r.get('seconds', 0)}s")
    return 0


def cmd_serve(a):
    import server
    if a.port:
        server.PORT = a.port
    server.main()
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="studio.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("where", help="show the resolved paths").set_defaults(f=cmd_where)
    sub.add_parser("init", help="create the index schema").set_defaults(f=cmd_init)

    s = sub.add_parser("scan", help="index every new PDF in the library")
    s.add_argument("--root", help="folder to scan (default: the library root)")
    s.add_argument("--dry-run", action="store_true", help="only list what is new")
    s.set_defaults(f=cmd_scan)

    c = sub.add_parser("paths", help="write config.json")
    c.add_argument("--root")
    c.add_argument("--db")
    c.add_argument("--inbox")
    c.add_argument("--trash")
    c.set_defaults(f=cmd_paths)

    v = sub.add_parser("serve", help="start the web app")
    v.add_argument("--port", type=int)
    v.set_defaults(f=cmd_serve)

    e = sub.add_parser("embed", help="build the semantic page vectors")
    e.add_argument("--limit", type=int, help="stop after N pages")
    e.add_argument("--status", action="store_true", help="print and exit")
    e.add_argument("--reset", action="store_true", help="drop all vectors first")
    e.set_defaults(f=cmd_embed)

    a = p.parse_args(argv)
    if not getattr(a, "f", None):
        p.print_help()
        return 1
    return a.f(a)


if __name__ == "__main__":
    sys.exit(main())
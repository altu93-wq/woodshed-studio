#!/usr/bin/env python3
"""Replace the Taunton archive's ghost `sources` row with one real row per PDF.

The problem this fixes
----------------------
`pages` already holds the whole archive: 230 issues, 25,268 pages, each row
carrying the *good* title ("Fine Woodworking Winter 1975"), the right year and
`fwwmap` pointing at the real PDF and page number. Search, the reader and
"Open PDF" all work.

What was missing is the book list. `sources` had no rows for those PDFs, only
one summary row pointing at `.../Data-FWW/DB/FWW.db` - a SQLite file, not a
PDF - with `status='imported-from-db'`. Three consequences:

  * every scan saw the 230 PDFs as new and offered to index them again, which
    would have stored the same 25,268 pages a second time under a worse title;
  * the book list showed 230 rows as "not indexed";
  * Health showed 92% coverage, because `sum(indexed_pages)` counted 25,268
    pages that no row in `pages` backs.

So: don't reindex anything. Read the truth out of `pages` + `fwwmap`, write
230 `sources` rows, drop the ghost. No page row is touched, so nothing can be
duplicated and the existing titles survive exactly as they are.

    py -3 fwwfix.py            # dry run: prints the plan, changes nothing
    py -3 fwwfix.py --apply    # writes, after backing index.db up

Backing up is not optional: this writes to the one file the whole app depends
on. The backup is a plain file copy next to index.db; restore by replacing it.
"""
import os
import shutil
import sys
import time

import store

GHOST = "imported-from-db"


def plan():
    """What the fix would do, as data. Reads only."""
    c = store.ro()
    try:
        ghosts = c.execute(
            "SELECT rowid,collection,title,path,pages,indexed_pages,chars,status "
            "FROM sources WHERE status=?", (GHOST,)).fetchall()
        if not ghosts:
            return {"ghosts": [], "books": [], "error": "no ghost source row"}
        # The archive is identified by fwwmap, not by a folder name: those rows
        # are the only thing that actually knows which PDF each page came from.
        books = c.execute(
            "SELECT fm.pdf_path, MIN(pg.title), MIN(pg.collection),"
            "       COUNT(*), COALESCE(SUM(LENGTH(pg.text)),0),"
            "       MAX(fm.pdf_page)"
            "FROM fwwmap fm JOIN pages pg ON pg.rowid=fm.rowid "
            "WHERE pg.path NOT LIKE '%:\\%' AND pg.path NOT LIKE '%.pdf' "
            "GROUP BY fm.pdf_path ORDER BY fm.pdf_path").fetchall()
        missing = [b for b in books if not os.path.exists(b[0])]
        already = {r[0] for r in c.execute(
            "SELECT path FROM sources WHERE status!=?", (GHOST,))}
        fresh = [b for b in books if b[0] not in already]
        return {"ghosts": ghosts, "books": fresh, "missing_on_disk": missing,
                "pages_total": sum(b[3] for b in fresh),
                "chars_total": sum(b[4] for b in fresh),
                "ghosts_pages": sum(g[5] or 0 for g in ghosts)}
    finally:
        c.close()


def report(p, limit=8):
    print(f"index db : {store.DB}")
    print(f"ghost sources rows : {len(p['ghosts'])}")
    for g in p["ghosts"]:
        print(f"   #{g[0]} {g[1]!r} / {g[2]!r}")
        print(f"       path          : {g[3]}")
        print(f"       indexed_pages : {g[5]}  chars: {g[6]}")
    print(f"\nreplacement sources rows : {len(p['books'])}")
    print(f"pages they will account for : {p['pages_total']:,}")
    print(f"characters                 : {p['chars_total']:,}")
    print(f"mapped PDFs missing on disk: {len(p.get('missing_on_disk') or [])}")
    print("\nfirst rows:")
    for b in p["books"][:limit]:
        print(f"   {b[3]:4d}p  {b[4]:>8,} chars  {b[1]}")
    if len(p["books"]) > limit:
        print(f"   ... and {len(p['books']) - limit} more")
    print("\nwhat it does NOT touch: pages, pyear, fwwmap, articles, jobs.")


def backup():
    """A copy of index.db next to it. WAL is checkpointed into it first."""
    dst = store.DB + f".bak-{time.strftime('%Y%m%d-%H%M%S')}"
    src = store.ro()
    try:
        src.execute("PRAGMA wal_checkpoint(FULL)")
    except Exception as e:
        print("checkpoint warning:", e, file=sys.stderr)
    finally:
        src.close()
    shutil.copy2(store.DB, dst)
    print(f"backup   {dst}  ({os.path.getsize(dst):,} bytes)")
    return dst


def apply(p):
    if not p["books"]:
        print("nothing to do")
        return 1
    books = p["books"]
    w = store.Writer()

    def _write(conn):
        # Insert first, delete second: if the process dies in between, the next
        # run finds both rows and simply re-inserts the ones that are missing.
        conn.executemany(
            "INSERT INTO sources(collection,title,path,pages,indexed_pages,"
            "chars,status) VALUES(?,?,?,?,?,?,'indexed')",
            [(b[2], b[1], b[0], b[5], b[3], b[4]) for b in books])
        dropped = conn.execute(
            "DELETE FROM sources WHERE status=?", (GHOST,)).rowcount
        # The map row counted these pages towards coverage; they are now real.
        conn.execute("INSERT OR REPLACE INTO meta VALUES('fww_sources_fixed',?)",
                     (str(time.time()),))
        return len(books), dropped

    n, dropped = w.submit(_write)
    print(f"wrote {n} sources rows, dropped {dropped} ghost row(s)")
    c = store.ro()
    try:
        tot = c.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
        pg = c.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
        ip = c.execute("SELECT COALESCE(SUM(indexed_pages),0) FROM sources").fetchone()[0]
        print(f"sources={tot:,}  pages={pg:,}  sum(indexed_pages)={ip:,}")
        print("coverage is now "
              f"{round(100 * min(pg, ip) / max(1, ip))}%" if ip else "")
    finally:
        c.close()
    return 0


def main():
    dry = "--apply" not in sys.argv
    p = plan()
    if p.get("error"):
        print(p["error"], file=sys.stderr)
        return 1
    report(p)
    if dry:
        print("\nDRY RUN - nothing was written. Re-run with --apply.")
        return 0
    backup()
    return apply(p)


if __name__ == "__main__":
    sys.exit(main())
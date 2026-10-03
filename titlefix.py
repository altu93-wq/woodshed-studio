#!/usr/bin/env python3
"""Resolve a publication name for the books already in the index.

`titles.py` runs at ingest time, so anything added from now on is named
when it lands. This is the other half: the books that are already indexed
were written before `sources.pub` existed, so they carry no derived name.

It follows the `yearfix.py` shape the user already trusts - dry run by
default, print the plan, write a CSV, and only touch the database with
`--apply` (which takes a backup first).

    py -3 titlefix.py           # dry run: prints the plan, changes nothing
    py -3 titlefix.py --apply   # write, after backing index.db up

What it changes
---------------
Only `sources.pub` / `sources.pub_src`. `sources.title` and all 99k
`pages.title` rows are left exactly as they are: `title` is the honest
filename-derived name that `fwwmap` and the page rows agree with, and the
derived name sits beside it as the display title.

On the current library this is expected to be a no-op - every filename
already names its book - so the useful run is after the magazine folders
have been added, where a folder of bare `7.pdf`, `8.pdf` files needs the
folder rule to give them a publication name.
"""
import csv
import os
import shutil
import sys
import time

import store
import titles

CSV = "titlefix_changes.csv"


def plan(limit=None):
    """(rows, counts) - what would be written, and how it was decided."""
    c = store.ro()
    q = ("SELECT path,collection,title,pub,pub_src,pages FROM sources "
         "ORDER BY collection,title")
    db = c.execute(q).fetchall()
    c.close()
    if limit:
        db = db[:limit]
    rows, counts = [], {}
    for path, coll, title, pub, src, pages in db:
        new, how = titles.detect(path, collection=coll)
        counts[how] = counts.get(how, 0) + 1
        if new and new != (pub or ""):
            rows.append({"path": path, "collection": coll or "",
                         "old_title": title or "", "old_pub": pub or "",
                         "new_pub": new, "source": how, "pages": pages})
    return rows, counts


def report(rows, counts):
    print(f"index db : {store.DB}")
    print("\nhow each book was decided:")
    for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
        note = {"keep": "filename already names it - left alone",
                "folder": "publication taken from the folder name",
                "metadata": "publication taken from the PDF Title field",
                "masthead": "publication read from the first page",
                "fallback": "nothing found - filename kept, unmarked"}
        print(f"  {k:10} {v:5}   {note.get(k, '')}")
    print(f"\nbooks that would get a new display name : {len(rows)}")
    for r in rows[:25]:
        print(f"  [{r['source']}] {r['old_title'][:34]:36} -> {r['new_pub']}")
    if len(rows) > 25:
        print(f"  ... and {len(rows) - 25} more")
    print("\nwhat it does NOT touch: sources.title, pages, pyear, fwwmap, "
          "articles, jobs - and no PDF is opened or rewritten.")


def backup():
    src, dst = store.DB, f"{store.DB}.bak-titlefix-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(src, dst)
    return dst


def apply(rows):
    w = store.Writer()

    def _w(c):
        c.executemany("UPDATE sources SET pub=?, pub_src=? WHERE path=?",
                      [(r["new_pub"], r["source"], r["path"]) for r in rows])
    w.submit(_w)
    c = store.ro()
    got = c.execute("SELECT COUNT(*) FROM sources WHERE pub IS NOT NULL "
                    "AND pub<>''").fetchone()[0]
    c.close()
    return got


def main():
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])
    rows, counts = plan(limit)
    report(rows, counts)
    if not rows:
        print("\nnothing to do.")
        return 0

    with open(CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"\nfull plan written to {CSV}")

    if "--apply" not in sys.argv:
        print("\ndry run - nothing written. Re-run with --apply to store it.")
        return 0

    b = backup()
    print(f"backup    : {os.path.basename(b)}")
    got = apply(rows)
    print(f"applied   : {len(rows)} rows, {got} books now carry a pub name")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
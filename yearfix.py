#!/usr/bin/env python3
"""Re-derive the publication year of every book from its own front pages.

`ingest.py` stamps a year when a book first enters the index. This walks the
whole library again with `yearfind` and repairs what the cheap rule got wrong.

    py -3 yearfix.py            # dry run: prints the plan, writes nothing
    py -3 yearfix.py --apply    # write the changes into pyear

Run it dry first and read the plan. A wrong year is worse than a missing one,
so anything below the confidence floor is reported and left alone.

pyear is keyed by the `pages` rowid, and one book may have rows spread across
several paths when a duplicate copy was indexed, so every path that shares the
book's canonical path is updated together.
"""
import argparse
import collections
import csv
import os
import sys

import store
import yearfind

HEAD = 12
TAIL = 8
LOG = "yearfix_changes.csv"


def page_text(path, first=HEAD, last=TAIL):
    """Return (head_text, tail_text, n_pages, chars) for one path."""
    rows = _PAGES.get(path, [])
    head = " ".join(t for _, t in rows[:first])
    tail = " ".join(t for _, t in rows[-last:]) if len(rows) > last else ""
    return head, tail, len(rows), sum(len(t) for _, t in rows[:first])


_PAGES = {}


def load():
    """Read every page and every stored year in two queries.

    Doing this per book meant opening 422 sqlite connections and running 422
    aggregate queries, which is minutes instead of a second.
    """
    global _PAGES
    c = store.ro()
    books = c.execute(
        "SELECT path,title,status FROM sources ORDER BY title").fetchall()
    rows = c.execute(
        "SELECT p.path, p.page, p.text, py.year FROM pages p "
        "LEFT JOIN pyear py ON py.rowid=p.rowid").fetchall()
    c.close()

    by_path = collections.defaultdict(list)
    years = collections.defaultdict(collections.Counter)
    for path, page, text, y in rows:
        try:
            n = int(page)
        except (TypeError, ValueError):
            n = 10 ** 9
        by_path[path].append((n, text or ""))
        if y and y > 0:
            years[path][y] += 1
    for path, lst in by_path.items():
        lst.sort(key=lambda r: r[0])
    _PAGES = by_path
    return books, years


def plan():
    """Return (changes, stats). Nothing is written."""
    books, years = load()

    changes = []
    stats = collections.Counter()
    for path, title, status in books:
        head, tail, npages, chars = page_text(path)
        if not npages:
            stats["no pages"] += 1
            continue
        y, conf, why, title_only = yearfind.detect(title, head, tail)
        tally = years.get(path)
        cur = tally.most_common(1)[0][0] if tally else None
        new, action = yearfind.decide(cur, y, conf, title_only, why)
        stats[action] += 1
        if action in ("assign", "replace"):
            changes.append({
                "action": action, "old": cur, "new": new, "conf": conf,
                "why": why, "title": title or "", "path": path,
                "pages": npages, "head_chars": chars,
            })
    return changes, stats


def apply(changes):
    """Write the new years. Every page row of the book gets the same year."""
    c = store.ro()
    ids = collections.defaultdict(list)
    for path, rowid in c.execute(
            "SELECT p.path, p.rowid FROM pages p WHERE p.path IN (%s)"
            % ",".join("?" * len(changes)),
            [ch["path"] for ch in changes]) if changes else []:
        ids[path].append(rowid)
    c.close()

    w = store.Writer()
    written = 0

    def _do(conn):
        n = 0
        for ch in changes:
            rids = ids.get(ch["path"], [])
            if not rids:
                continue
            conn.executemany("INSERT OR REPLACE INTO pyear VALUES(?,?)",
                             [(r, ch["new"]) for r in rids])
            n += len(rids)
        return n

    try:
        written = w.submit(_do)
    except Exception as e:
        print("WRITE FAILED: %s" % e, file=sys.stderr)
        return 0
    return written


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write to the database (default is a dry run)")
    args = ap.parse_args()

    changes, stats = plan()
    print("=== year plan for the whole library ===")
    for k, v in sorted(stats.items()):
        print("  %-26s %d" % (k, v))
    print()
    print("=== %d change(s) ===" % len(changes))
    for ch in changes:
        print("%-8s %-5s -> %-5s c=%-3d %-46s %s" % (
            ch["action"], ch["old"] or "-", ch["new"], ch["conf"],
            ch["title"][:44], ch["why"][:60]))
    print()

    # The plan log is only rewritten when there is something to record, so a
    # verification run cannot erase the log of the run that actually changed
    # the database.
    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), LOG)
    if changes:
        with open(log_path, "w", newline="", encoding="utf-8") as f:
            wtr = csv.DictWriter(f, fieldnames=list(changes[0].keys()))
            wtr.writeheader()
            for ch in changes:
                wtr.writerow(ch)
        print("plan written to %s" % log_path)
    else:
        print("no changes; %s left as-is" % log_path)

    if not args.apply:
        print("\nDRY RUN -- nothing written. Re-run with --apply to commit.")
        return
    if not changes:
        print("\nnothing to do")
        return
    rows = apply(changes)
    print("\napplied: %d page rows updated across %d book(s)"
          % (rows, len(changes)))


if __name__ == "__main__":
    main()

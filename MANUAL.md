# Woodshed Studio — Manual

The day-to-day guide. [`README.md`](README.md) is the overview, install and
architecture; [`NOTES.md`](NOTES.md) is *why* things are built the way they are.
This file is **how you actually use it**.

- [1. Starting it up](#1-starting-it-up)
- [2. Reaching it from another machine](#2-reaching-it-from-another-machine)
- [3. Pointing it at a library](#3-pointing-it-at-a-library)
- [4. Getting books in](#4-getting-books-in)
- [5. Searching](#5-searching)
- [6. The Topic Map](#6-the-topic-map)
- [7. The Reader](#7-the-reader)
- [8. The book table](#8-the-book-table)
- [9. Scanned books and OCR](#9-scanned-books-and-ocr)
- [10. Semantic search](#10-semantic-search)
- [11. Deleting things](#11-deleting-things)
- [12. Health](#12-health)
- [13. Everyday commands](#13-everyday-commands)
- [14. When something is wrong](#14-when-something-is-wrong)
- [15. Backups](#15-backups)

---

## 1. Starting it up

Double-click **`studio.cmd`**. It opens <http://localhost:8766> in your browser
and starts the server. Close the black console window to stop the server.

From a terminal instead:

```
py -3 studio.py serve
```

The banner prints two addresses. The second one (for example
`http://192.168.1.107:8766`) is the same app on your phone, as long as the
phone is on the same Wi-Fi — the whole UI is built for touch.

Nothing is uploaded anywhere — the library stays on this disk. The
server does listen on the network, so read
[Reaching it from another machine](#2-reaching-it-from-another-machine)
before you forward a port.

---

## 2. Reaching it from another machine

The server listens on **every network interface** on port 8766. Anything that
can route to this computer can open it: the rest of the house on Wi-Fi, and —
if you forward the port — the internet. Listening broadly is what makes phone
use on the same network work, and it is exactly what you do not want beyond
that. So pick deliberately.

**Same Wi-Fi** — nothing to set up. The banner prints the address, for example
`http://192.168.1.107:8766`; open it on the phone and the whole UI is built for
touch.

**Anywhere else** — set a password first.

### Setting a password

Create `config.json` next to `studio.py`:

```json
{ "password": "choose-something" }
```

or set the `WOOD_PASSWORD` environment variable, which wins over the file.
Restart the server afterwards.

The browser asks once and then remembers it for that origin, so the activity
stream, the reader's page requests and everything after it carry it
automatically — there is no login form to submit and no token in any URL. Leave
the username blank; only the password matters.

The startup banner says which state you are in:

```
> Password : required
> Password : not set (open to anyone who can reach this port)
```

Leaving it unset changes nothing, and that is the right default for a laptop
nobody else can reach.

### From home and away with Tailscale

The simplest way to use the library somewhere else. Install Tailscale
(<https://tailscale.com>) on this PC and on the device you want to browse from,
sign both into the same account, and open:

```
http://<this-machine>:8766
```

No port forwarding, no public IP, no certificate to renew — and it works on
networks where you do not have a public address at all. Traffic between your
devices is already encrypted, so the password is the second lock rather than
the only one.

The machine name and address come from the Tailscale admin console, or from
`tailscale ip -4` on this PC.

### Letting somebody else in

**Do not forward the port.** Anyone who finds the address gets a UI whose every
`/api` endpoint can delete from the index, start OCR, or write files.

Use sharing instead: in the Tailscale admin console, **Machines** → this
machine → **Share**, then either send an invite by email or copy an invite link.
They accept it with **their own** Tailscale account and can reach **only this
machine** — nothing else on your tailnet. Revoke it from the same dialog
whenever you like.

On their side the address is the fully qualified name; the short one does not
resolve for them:

```
http://<this-machine>.<tailnet>.ts.net:8766
```

> **What the activity log records.** It logs *actions* — indexing, OCR, uploads,
> deletions, merges — not *access*. Someone opening the library and searching
> leaves nothing behind, and neither does a wrong password. If you share access,
> read the log as a record of what was done to the index, not of who was logged
> in.

## 3. Pointing it at a library

The **library root** is the folder that contains your book folders. If the app
sits in `<root>/_index/studio/`, it finds itself with no configuration at all —
which is the layout it ships in:

```
MyLibrary\
├── _index\
│   ├── index.db        the search index
│   ├── vectors\        embeddings, rebuildable
│   └── studio\         the app (this folder)
├── new books\          drop folder
└── ...your book folders
```

To point it somewhere else, run this once:

```
py -3 studio.py paths --root D:\MyBooks
```

That writes `config.json`. For a one-off, without writing anything:

```
set WOOD_ROOT=D:\MyBooks
```

Three more knobs exist — `WOOD_INDEX_DB`, `WOOD_INBOX`, `WOOD_TRASH` — and
each has a matching key in `config.json`; see
[`config.example.json`](config.example.json).

**Check what resolved** whenever something looks wrong:

```
py -3 studio.py where
```

Add & OCR and Health both print the same paths at the bottom, so you can confirm
in the browser without leaving it.

> One app folder can serve several libraries. Because `index.db` and the
> embeddings live *inside* the library, pointing `WOOD_ROOT` at a second tree
> shows that library instead, with nothing shared between them.

---

## 4. Getting books in

There are three routes, in increasing order of surprise.

### Drop folder

Put PDFs in `<root>/new books/`. A watcher notices within a second or two and
indexes them by themselves. You can also drag files onto the **Add & OCR** tab —
they land in the drop folder, so they behave identically.

This only looks at that one folder.

### Scan whole library

**Add & OCR → Scan whole library** walks the entire tree under the library root
and indexes every PDF it finds that is not in the index yet.

This is the button you want for an archive you never reorganised. It is
incremental: a second run only picks up what is new.

Useful first line of defence before a long scan:

```
py -3 studio.py scan --dry-run
```

which prints what it *would* index and changes nothing.

### Already indexed elsewhere?

If you migrated from another tool and your books are already in the database,
do **not** re-index them — you will get duplicate pages under worse titles.
`fwwfix.py` exists for exactly that case and writes the missing book rows while
leaving every page row alone.

---

## 5. Searching

Type in the big box. Results appear as you type; keep scrolling for more —
paging never repeats a page you already saw, because results are diversified so
one thick book cannot eat the list.

### In the main box

| you type | you get |
|---|---|
| `dovetail` | pages containing the word |
| `"through dovetail"` | that exact phrase |
| `dovetail -mortise` | pages with *dovetail* but not *mortise* |

### Advanced

**Advanced ▾** opens the fields that do not fit in one line:

| field | meaning |
|---|---|
| All these words | every one must be present |
| This exact phrase | the quoted words, in order |
| Any of these words | at least one |
| None of these words | removed afterwards |
| Year range | between two publication years |

The fields combine as `all AND phrase AND any`, then `none` is subtracted.

### Reading results

Each hit shows the title, the page number, the year if known, a highlighted
snippet, and which collections it came from. Clicking the title opens the real
PDF at that page.

Beneath the title is the **PDF filename**, always. For most books the title and
the filename are the same thing; for a magazine issue stored as `7.pdf` the
title line shows the publication it belongs to (`Fine Woodworking No. 7`) and
the line below shows the file, so a result is always traceable back to disk.

How that title is found, strongest first:

1. **the file already names it** — nothing changes, the filename is the title
2. **the folder name** — `Fine Woodworking 2025/7.pdf` → `Fine Woodworking No. 7`
3. **the PDF's own Title field**
4. **a masthead read from page 1** — unreliable on scans, so it is tried last
5. **nothing found** — the filename is kept

The year is shown as its own badge next to the title rather than being written
into it, so `Fine Woodworking 2025` reads as a name plus a year, not as one
long string.

Results are ranked **hybrid** when the semantic layer is ready and plain keyword
when it is not. You can tell which from the badge next to the result count.
Sorting can be forced to *Title A–Z*, *Newest first* or *Oldest first* — useful
when you are looking for a book rather than a fact.

### Searching in another language

`lexicon.py` expands US/UK spelling variants (`rebate`/`rabbet`) and carries a
small Turkish-to-English seed list, so a Turkish query reaches the English
corpus. The semantic layer is English; this is expansion, not translation.

---

## 6. The Topic Map

**🗺️ Topic Map** shows which words actually occur together in *your* library.
It is not a general knowledge graph — it is a map of your books.

- Drag empty space to pan, scroll or pinch to zoom, use `+` / `−` / `⤢`.
- Drag a node to rearrange it.
- Tap or click a node to search that term.

**Related topics** under a search lists the terms that co-occur most
distinctively with your query, ranked by how surprising the pairing is rather
than by raw frequency — so common words do not swamp the list.

The tag layer behind it (`concepts.npz`) is derived from your library's own
article index, which is why it lives in `<root>/_index/vectors/` and not inside
the app.

---

## 7. The Reader

Hit **📖 Reader**, or click a result title. The reader opens the actual PDF at
the page you hit and highlights the word you searched for.

It uses the browser's own viewer for whole-book opening and a bundled PDF.js
build for the in-page view, so it works with no internet connection.

Loading a very large PDF takes a moment; the page-by-page reader stays fast
because it renders only the page you are on.

---

## 8. The book table

**📥 Add & OCR** lists the whole library. Columns: checkbox, Book, Year, Pages,
Status, Modified, Actions.

**Filters**: by title text, by status, by page count, duplicates only. The status
filter is the useful one:

| status | meaning |
|---|---|
| Indexed | text extracted, searchable |
| Image-only | scanned — needs OCR |
| Not indexed | a PDF on disk the index does not know about |
| Merged copy | a duplicate you collapsed; ignored by later scans |
| File missing | indexed once, no longer on disk |
| OCR done | scanned book that has been through OCR |

The header line reads *N books indexed · M not indexed*, so a discrepancy
between "what is on disk" and "what I can search" is visible immediately.

**Sorting** is per column; the checkbox in the header selects every row the
filters are currently showing. Ticking a row never redraws the table, so
multi-select does not drop clicks.

The **Book** column shows the publication name when one could be worked out
(see Searching), and the PDF filename underneath it.

### Re-indexing

Re-index drops that book's rows and reads the PDF again. Safe to do; it fixes a
book that indexed badly. Your PDF is not touched.

---

## 9. Scanned books and OCR

A PDF with no extractable text is indexed anyway as **image-only** — zero
searchable pages, but present in the table and flagged, rather than silently
skipped.

To make it searchable, run OCR over it:

- **OCR all image-only** — queue every scanned book, one at a time.
- Select rows, then **OCR selected** — queue just those.
- **Stop** — finishes the current page and keeps everything already done.

OCR runs in the background; you can keep searching. It is resumable: stop it,
restart the server, and it picks up where it stopped.

OCR is slow and imperfect. It exists to make a scanned book *findable*, not to
produce a clean text edition — expect to verify hits against the page image.

Without an NVIDIA GPU, OCR runs on the CPU and takes roughly 3.5x longer. The
Health tab shows which engine is active.

---

## 10. Semantic search

Keyword search finds pages that contain your words. Semantic search also finds
pages that *describe* your words — search "stop tearout" and reach the page that
explains the technique without ever using the phrase.

The two are fused with Reciprocal Rank Fusion, which is why you do not have to
choose.

Building it:

- **Health → Build vectors**, or `py -3 studio.py embed`. Once a build is partly
  done the same button reads **Top up vectors**; **Stop** appears beside it while
  a build is running.

Progress is live. On an RTX 3080 with the default model, 98k pages takes about
**70 seconds**; on a CPU, roughly **1.8 hours**. It is incremental — stop it and
restart and it continues. Until coverage passes 20% the app uses keyword search
only and says so.

Health shows `vectors` / `total` / percentage, plus how many near-empty pages
were skipped (a page needs real text to be worth embedding).

To rebuild from scratch:

```
py -3 studio.py embed --reset
```

Everything under `vectors/` is derived data. Deleting it costs you the vectors
and nothing else.

---

## 11. Deleting things

Every delete asks one question with three answers, spelled out rather than
hidden behind a browser `confirm()`:

| answer | what happens |
|---|---|
| **Remove from index only** (default) | the book's rows are deleted; **the PDF is untouched** |
| **Move the PDF to trash** | rows deleted *and* the file moved to `<root>/_trash/` |
| **Cancel** | nothing happens |

The PDF is never erased from disk. If you choose to move it, it goes to
`_trash/` where you can still recover it — the app does not have a permanent
delete.

Bulk actions act on the ticked rows. **Delete selected from index** still asks
the same question once for the whole selection.

After a delete, the semantic layer is pruned automatically in the background so
stale vectors cannot linger.

---

## 12. Health

**🩺 Health** is the honest dashboard — real counts, not a marketing number.

| what | read it as |
|---|---|
| Pages / Sources | what you have indexed |
| Image-only | scanned books waiting for OCR |
| Active jobs | anything currently running |
| Double-indexed / Merged copies | duplicate rows; `Clean duplicates` fixes them |
| Pages with text | how much of the library is actually searchable — **this is the coverage number, not "100%"** |
| Semantic vectors | embedding coverage and the model used |
| Concept layer | age of the topic-tag layer, and where it was read from |
| OCR queue / engine | whether OCR is busy, and on GPU or CPU |

The footer restates the resolved paths and the exact database file in use.

> **Coverage.** "Pages with text" reads e.g. 92% on a large mixed library, and
> that is correct: the remaining 8% are blank pages and image-only scans, which
> are not searchable until OCR runs. It is not a warning sign by itself.

---

## 13. Everyday commands

```
py -3 studio.py where            show the resolved paths
py -3 studio.py init             create the index schema (idempotent)
py -3 studio.py scan             index every new PDF under the library root
py -3 studio.py scan --dry-run   list what would be indexed, change nothing
py -3 studio.py scan --root D:\  scan another tree
py -3 studio.py embed            build the semantic vectors (resumable)
py -3 studio.py embed --status   how many pages already have one
py -3 studio.py embed --reset   drop all vectors and rebuild
py -3 studio.py paths --root D   write config.json
py -3 studio.py serve [--port N] start the web app
```

`scan` and `embed` are both incremental and both safe to interrupt.

Environment variables worth knowing:

| variable | effect |
|---|---|
| `WOOD_ROOT`, `WOOD_INDEX_DB`, `WOOD_INBOX`, `WOOD_TRASH` | paths |
| `WOOD_EMBED_MODEL` | embedding model, default `BAAI/bge-small-en-v1.5` |
| `WOOD_EMBED_PROVIDERS` | force `CPUExecutionProvider` to silence CUDA warnings |
| `WOOD_EMBED_AUTOBUILD=0` | do not finish a partial build at startup |
| `WOOD_OCR_PROVIDERS` | force CPU for OCR |

Changing the embedding model invalidates every stored vector automatically —
cosine space is not comparable across models, so mixing them would return
nonsense instead of failing.

---

## 14. When something is wrong

**A search returns nothing you expect.**
Check the result badge: if it says *keyword*, the semantic layer is not ready.
Try fewer words, or drop a word from **All these words**. A phrase in quotes is
strict — `"end grain"` will not match across a line break.

**A book is "not indexed" but it is there.**
Run **Scan whole library**. The drop folder only ever looks at one folder.

**A scanned book has no results.**
It is image-only. Run OCR.

**Two rows for the same book.**
Usually the same file indexed under `D:\...` and `d:\...`. The **Clean
duplicates** button collapses them and marks the copy ignored so later scans do
not re-add it.

**"not a pdf" / "not indexed" / 403 when opening a file.**
Only PDFs inside your library root and present in the index can be opened, by
design.

**The GPU is not being used.**
Health shows the engine. On CPU-only onnxruntime you will see a few CUDA
warnings that are harmless — it falls back to the CPU. Install
`onnxruntime-gpu` (replacing `onnxruntime`) to use it.

**Something looks broken after an upgrade.**
`py -3 studio.py init` is idempotent and safe; it creates any missing table and
touches nothing else.

---

## 15. Backups

**`index.db` is the only file worth backing up.** It is the entire library:
every page of text, every title, every year.

Everything under `_index/vectors/` is a cache derived from it. Losing it costs
you a re-embed (seconds on a GPU) and nothing else.

A copy of `index.db` taken while the server is running is consistent — SQLite is
in WAL mode, so copy `index.db`, `index.db-wal` and `index.db-shm` together if
you want to be certain.

Never commit either to Git. `.gitignore` already excludes them.

---

## See also

- [`README.md`](README.md) — what it does, install, configuration reference
- [`NOTES.md`](NOTES.md) — design decisions and the measurements behind them
- [`config.example.json`](config.example.json) — every path setting, annotated
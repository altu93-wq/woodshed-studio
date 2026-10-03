# Woodshed Studio — design notes

Why the code is shaped the way it is. For what the app does and how to run it,
read [`README.md`](README.md).

Run: `studio.cmd` → <http://localhost:8766>, or `py -3 studio.py serve`.

## Where the library is

`store.py` resolves four paths once, at import, from the first source that
gives a value:

| what | environment variable | `config.json` key | default |
|---|---|---|---|
| library root | `WOOD_ROOT` | `library_root` | app folder's parent (if it owns an `_index` folder) |
| index database | `WOOD_INDEX_DB` | `index_db` | `<root>/_index/index.db` |
| drop folder | `WOOD_INBOX` | `inbox` | `<root>/new books` |
| trash | `WOOD_TRASH` | `trash` | `<root>/_trash` |

So the shipped layout `<library>/_index/<app>/` needs no configuration, and any
other arrangement is one command:

```
py -3 studio.py paths --root D:\MyBooks        # writes config.json once
set WOOD_ROOT=D:\MyBooks                       # or per session
py -3 studio.py where                          # what did it resolve to?
```

Everything that used to hard-code a path goes through `store.ROOT` /
`store.DB` / `store.INBOX` / `store.TRASH`, including the PDF-serving guard
("nothing outside the library may be served") and the delete-to-trash move.

## Setting up a library from scratch

```
py -3 studio.py init      # create the full schema (idempotent)
py -3 studio.py scan      # index every PDF under the library root
py -3 studio.py serve
```

`studio.py scan --dry-run` lists what would be indexed and changes nothing.
The same walk is one button in the UI: **Add & OCR ▸ Scan whole library**. A
whole-library scan queues the files on a single background thread rather than
one thread per file, because a few hundred concurrent PyMuPDF extractions
would thrash the disk and the single FTS writer.

`init_db()` creates the complete schema — `sources`, the `pages` FTS5 table,
`pyear`, `fwwmap`, `jobs`, `meta`, `articles` — with `IF NOT EXISTS`, so an
empty archive gets a working index on the first run and an existing one is
left untouched.

## Design

- **Stdlib only.** `http.server`, no web framework, no build step. `studio.html`
  and `viewer.html` are served as-is; PDF.js is vendored under `vendor/`.
- **One writer, many readers.** `store.Writer` holds the only writable
  connection to `index.db` and runs every write on its own thread
  (`submit(fn)` gives you a connection and returns the result). Queries open
  `mode=ro`. WAL + `busy_timeout=60000` means long writes never block reads.
- **Jobs live in the database.** The `jobs` table (`store.init_db`) is the
  queue and the progress display. Statuses: `queued`, `extracting`, `indexing`,
  `ocr_running`, `done`, `failed`, `image-only`, `duplicate`.
- **One ingest path.** `ingest.index_file` is used for a fresh PDF, a Re-index
  and a drop-folder scan, so there is only one place where the classification
  rules (`MIN_PAGE_CHARS`, `IMG_ONLY_RATIO`, year assignment) live.

## Paths

`normpath()` is the single comparison rule for filesystem paths. It applies
`normcase(abspath())`, so `E:\wood\book.pdf`, `e:\wood\book.pdf` and paths with
doubled backslashes all collapse to one key. Every place that decides "is this
already indexed?" goes through it — that is what stopped books from being
indexed twice.

When de-duplicating, the delete is compared **exactly** (`COLLATE BINARY`), not
`NOCASE`: the two rows differ only by drive-letter case, so a case-insensitive
delete would remove the copy we are trying to keep.

## Related topics

Ranking is normalized PMI (`npmi_score`) with a minimum-support floor and a
redundancy penalty, so generic tags (`Hand Tools`, `Design`) sink and specific
ones rise. Candidates come from three pools on one scale:

1. FWW editor tags,
2. terms mined from every page in the library,
3. a tag embedding layer (`concepts.npz`, MiniLM via `fastembed`), stored per
   library beside its own `index.db`.

Library terms and semantic labels compete in the same pool, so a semantic
neighbour only appears when it outranks the lexical ones. Turkish and US/UK
spellings are normalised through `lexicon.py`. Candidates are cached per page
count with a 60 s TTL, and document frequencies are cached, which is what keeps
the whole-library fallback fast enough to be interactive.

## Book list

Page counts come from `sum(indexed_pages)` on `sources`, **not**
`count(*) FROM pages`. They are equal (99 112 either way on a 651-book
library), but the sum reads 651 rows instead of scanning ~99k page rows:
0.8 ms versus 350 ms, which is the difference between polling the count every
1.5 s and not being able to.

Note the two columns are *not* the same number: `pages` counts every page of a
book, `indexed_pages` only the ones with enough text to search. On a real
library the gap is ~8%, and it is blank versos and covers, not a failure — so
Health labels it "pages with text" rather than "index coverage", which read
like 8% of the library was broken.

`GET /api/books` returns every indexed source plus any un-indexed PDF found
anywhere under the library root (not just the drop folder). Each row carries `collection`,
`pages`, `indexed_pages`, `status`, `mtime`, `dup` and `dup_files`. Grouping
for the "same book twice" badge uses `dup_key()`, which is folder-scoped —
once the list spans the library, two folders may legitimately hold the same
title.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/books` | the whole library as a table |
| `GET /api/stats` | just the page + source counts, for the header |
| `GET /rawfile?path=` | the whole PDF file, for the browser's own viewer |

| `POST /api/book_index` | Add (fresh) or Re-index (drops the old rows first) |
| `POST /api/book_delete` | remove index rows only; the PDF is untouched |
| `POST /api/book_merge` | two real files, one book: keep one, ignore the other |
| `POST /api/book_restore` | undo a merge |
| `POST /api/bulk_reindex` | re-index many books on one background worker |
| `POST /api/bulk_delete` | remove many books from the index; PDFs kept |
| `POST /api/bulk_ocr` | queue OCR for many image-only books at once |

Selection is kept in a `Set` of paths, so it survives sorting, filtering and
re-rendering, and the header checkbox means "every row the filters are showing".
Ticking a box must **not** redraw the table: that destroys the checkbox that was
just clicked, so fast multi-select silently dropped clicks.
| `POST /api/book_dedup` | collapse `E:\` vs `e:\` rows of one book |
| `POST /api/dup_fix` | collapse every duplicated book at once |
| `POST /api/rescan` | queue new PDFs from the drop folder |
| `POST /api/ocr_one` · `/api/ocr_all` · `/api/ocr_stop` | OCR control |

Merged copies are recorded in `jobs` with status `duplicate`; `scan_inbox()`
skips them, so Rescan does not resurrect what you just merged away.

## Layout and the topic map

**The map is laid out for the space it actually gets.** On mobile the Topic Map
panel starts `display:none`, so a layout pass that runs on load measures a 0x0
canvas, the force simulation packs every node into a blob around the origin,
and re-fitting that blob cannot recover it. `relayout()` re-runs the layout
whenever the canvas transitions from "no size" to "has size" (ResizeObserver +
tab switch + window resize).

Two more map rules worth keeping: `clampOutliers()` pulls weakly connected
nodes back to ~1.8x the median radius (left alone, two strays dominate the
bounding box and "fit" zooms out until the real cluster is a dot), and when
zoomed out only the largest nodes are labelled — all 150 at once was an
unreadable mat.

**Gestures.** One Pointer Events path covers mouse and touch: one finger on
empty space pans, one finger on a node drags it, two fingers pinch-zoom and
pan together, a tap (movement < 6px) searches the node. The canvas needs
`touch-action:none`, or the browser scrolls the page instead of delivering the
gesture. The +/−/fit buttons exist because a phone has no wheel and pinching a
dense graph is fiddly.

**Mobile.** The fixed chrome is kept to two rows (wordmark + search, then Sort +
Advanced) plus one scrolling nav strip; everything else — collapsed drop zone,
one scrollable filter row, tinted bulk bar — sits above the table, which keeps
its own scroll box so the header stays sticky and the table scrolls sideways
instead of crushing every column to one word per line.

**A trap that made Enter look broken.** `switchTab('add'|'health')` sets
`main.full` to hide the results panel. Searching from either of those tabs ran
the query correctly and then showed nothing, because `switchMobileView('results')`
never cleared `main.full` — so the panel stayed `display:none` while the hits
were already in the DOM. Pressing Search in Advanced worked because it is a
button the user clicks after the view had already changed.

## Opening a book as a real PDF

Clicking a title in the book table opens `/rawfile?path=…` in a new tab, which
streams the untouched PDF file so the browser's own viewer takes over. The
endpoint refuses anything that is not an indexed source (404) and anything
outside the library tree — checked in two independent layers, the `sources`
membership test and the `E:\wood` prefix test, so a traversal attempt is
rejected even if the first check is ever loosened.

It streams with `Accept-Ranges`, because a browser reading a large PDF issues
`Range` requests; without that it falls back to downloading the file.

`_inline_pdf_name()` exists because HTTP headers are latin-1 and this library
contains names like `Fine Woodworking №209 ….pdf`. U+2116 raised
`UnicodeEncodeError` mid-handshake and the browser saw a dropped connection
with no response at all. The header now carries an ASCII fallback plus an RFC
6266 `filename*=UTF-8''…` part.

## The viewer renders at device pixels

`viewer.html` used to size the canvas backing store in CSS pixels
(`cv.width = vp.width`). On a HiDPI screen that is half the pixels, so the
browser upscaled a soft image and the page looked lower-resolution than the
same PDF opened natively. It now renders at `scale * devicePixelRatio` and
presents the result at the CSS size.

## Search results are diversified per book

Flat relevance ranked 22 of the 60 visible hits for "joiner" on a single book,
which reads as a broken result list. `_diversify()` keeps the ranking exactly
as-is but caps how many pages one book may contribute (`PER_BOOK = 2`); "joiner"
goes from 30 distinct books to 51.

Three details make it safe:

- Only `sort=relevance` is diversified. Someone who explicitly picks Title or
  Newest asked for every hit in that order and gets it.
- `offset` now counts *diversified* hits, so infinite scroll walks the
  interleaved stream with no repeats: four pages of 60 returned 240 unique ids.
- The rank runs over a wider window on cheap columns, and `snippet()` is then
  run only for the rows that survive, in one batched `rowid IN (...)` pass.
  Doing it the obvious way — snippet inside the ranked query — cost ~70 ms per
  search. Splitting it made things **faster than before diversification**:
  median 116 ms vs 137 ms, because a bare ranked scan beats ranking plus 60
  inline snippet computations.

## Semantic search: keyword + vectors, fused with RRF

FTS5 is exact and blind. It finds every page containing "mortise tenon" and it
cannot find the page that says "the loose wedge keeps the joint from sliding"
when you typed "mortise". `vectors.py` closes that gap: one embedding vector
per page, blended into the relevance ranking.

**Why not `sqlite_vec`.** It is not installed and not needed. 98,373 pages x
384 float32 is 144 MB, which fits in RAM, so the whole matrix is a numpy array
and a query is one matrix-vector product — about 2 ms. A vector index would buy
nothing at this size and would add a native dependency to install.

**Storage** (`<db folder>/vectors/`, next to `index.db`):

```
pages.f32   raw float32 matrix; row i is the vector for rowids[i]
map.db      (rowid PRIMARY KEY, slot) - which vector lives where
meta.json   model, dim, progress
```

The map table is not decoration. Page rowids are not contiguous (a delete
leaves holes) and re-indexing moves them, so "slot == rowid" would be wrong.
It is also what makes the build resumable: the map holds exactly the slots
that are on disk, and the file is only ever appended to.

**Build.** `py -3 studio.py embed`, or the button in Health. It embeds only
the pages that have no vector, commits the map rows per batch, and is
incremental — the server finishes a partial build at startup, so a restart
resumes rather than redoes the library. Measured on an RTX 3080 with
`BAAI/bge-small-en-v1.5`: **~0.7 ms/page on the GPU (all 98k pages in ~70 s)**,
65 ms/page on the CPU (~1.8 h). Pages under 40 characters are skipped: they
carry no meaning, and `status()` reports them as `skipped` so the UI can say
"98,373 vectors, 739 near-empty pages skipped" instead of offering a build
that can only find nothing.

Changing the model invalidates every stored vector automatically — cosine space
is not comparable across models, so mixing them returns nonsense rather than
failing. `hybrid_ready()` refuses to blend below 20% coverage, which is what
makes a half-finished build safe to leave alone.

**Fusion.** Reciprocal Rank Fusion (`1/(60 + rank)`), summed over the two
lists. RRF needs no score calibration — only each list's *order* — so a bm25
rank and a cosine rank combine without pretending they are the same unit.
`KW_CANDIDATES=800` on the keyword side, `VEC_CANDIDATES=400` on the vector
side, fused pool capped at 1200.

Three things are deliberate:

- **Only `sort=relevance` blends.** Sorting by year or title is an explicit
  request for that order.
- **The collection/year filters apply to both sides.** A vector hit can be any
  page in the library, so the filters SQL applied to the keyword side have to
  be applied again to the vector hits.
- **Past the fused pool, plain bm25 takes over** (`mode: "hybrid-tail"`). The
  keyword list is the complete one, so paging through it must not stop at 1200
  while 5,560 keyword hits exist.

Measured on this library (99k pages): median **53 ms hybrid vs 76 ms keyword**
— hybrid is *faster*, because the keyword path's diversify-and-widen loop does
more SQL work than one 144 MB matrix multiply. A concrete win: "how to sharpen
a card scraper" has only 24 keyword hits, and the vector side adds Taunton's
*Complete Illustrated Guide to Finishing*, which never says that phrase.

**Staying fresh.** A delete or re-index removes page rowids, so
`server._semantic_dirty()` prunes the map in the background (an anti-join on
99k rows). Both the matrix cache and `meta.json` are keyed on the file's size
and mtime, so a build run from the CLI while the server is up is picked up
without a restart — verified by resetting the store mid-session and watching
`mode` flip `hybrid → keyword → hybrid`.

## Collections are deliberately absent from the UI

Which folder a page came from is not shown anywhere: not in the result list, not
as a facet row, not in Advanced, not as a column in the book table. A hit shows
title, year, page and snippet. The API still accepts `?coll=`, so an old URL or
a script keeps working, and `/api/books` still returns `collection` per row —
but nothing in the UI sets or displays it. Say the word and it comes back.

## Two traps worth knowing

**`jobs` is not "work in progress".** It is a history of every book ever
touched, plus whatever transient rows an event wrote. The OCR queue ends a run
by emitting `idle`; `on_event` used to persist that as a row with path `-` and
title `-`, and Health (`status NOT IN ('done','failed')`) counted it as an
active job forever — the KPI read "1 active jobs" with nothing running. `idle`
events are now UI-only, Health also excludes the status, and startup deletes any
leftover sentinel rows.

**`sources.rowid` and `pages.rowid` are unrelated.** `pages` is an FTS5 virtual
table with its own rowids; `sources` is a plain rowid-less table. Joining them
on `rowid` "works" — a few hundred of 99 112 pages match — but only because the
integer ranges happen to overlap. Never join the two on rowid. `delete_book`
matches on `pages.path` and collects the page rowids only to clear `pyear` /
`fwwmap`.

## Deleting: index rows vs. the PDF

Every delete goes through one three-button dialog (`askDelete`) that spells out
both consequences instead of a browser `confirm()`:

- **Remove from index only** — the default. `pages` / `sources` / `pyear` /
  `fwwmap` / `jobs` rows go, the file on disk does not.
- **Delete the PDF too** — the file is **moved**, not unlinked, to
  `<library>/_trash/<name>`. Nothing is ever destroyed, so a mistaken click is
  recoverable by hand. Toast reports the new location.
- **Cancel.**

One row (`data-del`) and the bulk bar (`/api/bulk_delete`) use the same dialog,
with the count of affected page rows and PDFs.

**Automatic re-indexing only watches the drop folder.** `/api/rescan` scans
the drop folder; "Scan whole library" (`/api/scan_library`, `studio.py scan`)
walks the entire tree. That split is deliberate — re-reading 650 books on every
startup would take minutes and hammer the disk, while the drop folder is the
only place a new file is *expected* to land. A book deleted from anywhere else
is not re-queued on its own: use Re-index, or run a library scan.

## Year column

`pyear` is per page, but the Year column shows one year per book: the mode
across its pages, ties falling to the earliest. On the library this was
developed against, 364 of 422 dated books had one (1813–2024); the other 58 are
genuinely undated scans, not a gap. Unknown years sort to the **bottom in both
directions** — treating "no year" as 0 put 60 rows above the real 1813 entry.

`book_years()` cost 10 s per `/api/books` call at first: it joined `sources` on
`lower(s.path)=lower(p.path)`, which defeats both indexes and turns the lookup
into a 99 112 × 422 cartesian product. `pages.path` is already the book path, so
no join is needed — the scan drops to ~360 ms. It is memoised on a cheap
fingerprint of `sources` (count + indexed_pages + pages + chars + max rowid) and
`pyear` (count + max rowid + sum year), ~6 ms, so the 600 ms SSE refresh does
not pay for it. Cached `/api/books` calls run in 40–60 ms.

### Re-deriving the years: `yearfind.py` + `yearfix.py`

`years.py` still stamps a year at ingest time — it is one regex over the title
plus the first 6 and last 4 pages, cheap and good enough for a book nobody has
looked at. But it was never re-run over the library that already existed, and it
makes two mistakes worth fixing:

- `title_year()` wins unconditionally, so "Puzzles in Wood-E M Wyatt
  (1956-2007)" was filed under 1956 although the page says "Fox Chapel
  Publishing, 2007" — the title range is the *original* edition.
- The weak fallback (most common bare year) picks up model numbers ("T-1810")
  and phone numbers ("593-1777").

`yearfind.py` turns every plausible 4-digit run into a weighted candidate that
carries the sentence it came from, and the heaviest one wins. Two rules that
came out of reading real failures:

- **Judging the whole context window was wrong.** Filters ran over ~70 chars
  around the year, so a URL printed on the same line
  (`www.creativepub.com  Copyright (c) 2009 Creative Publishing`) threw away the
  real copyright year. Disqualifying words now count only when they sit *near*
  the number.
- **A year found in the file name may fill a blank but never overwrite one.**
  "Pain - The Builder's Companion (1762)" names the original; this scan is a
  1931 reprint and only the pages know that.

Impostor years, each of which really occurred here: a government printer
("U.S. Government Printing Office, 1940"), a quoted source ("This day-book was
published in 1873 by Louis Courajod"), a craftsman's dates ("DUCORS (Barthelemy),
menuisier (1707)"), a bibliography entry in the back matter ("Chippendale, T.
Gentleman (First Edition) 1754"), a series line ("BOOKS ON FURNITURE &
DECORATION / Published in England previous to 1800"). Back-matter evidence is
weighted at 0.55 for the same reason — the last pages are full of other books'
years.

Printings are handled explicitly: in a chain like `COPYRIGHT 1910 / SECOND
EDITION, 1911 / THIRD EDITION, 1912` the original is demoted and the year after
an edition statement is rewarded, but when the chain cannot be resolved the book
is left alone rather than guessed.

```
py -3 yearfix.py           # dry run: prints the plan, writes nothing
py -3 yearfix.py --apply   # write, logging every change to yearfix_changes.csv
```

The run changed **18 books** (3 171 page rows), each verified by hand against
the page image text; four turned out to be wrong in the database rather than in
the detector (A Japanese Touch was 1986, the sixth printing, not the 1982 first
edition; World Woods in Colour was 1993, a reprint, not the 1986 publication).
It is idempotent — a second run reports 0 changes.

Only 58 books remain undated and their front pages really do contain no year:
pattern books from the 1900s whose title pages are half-plate photographs, and
modern saw manuals. Nothing was written for them, on purpose.

## OCR on the GPU

The engine runs on CUDA when a real session can be created. Measured on an RTX
3080 over **48 pages of a real scanned book** (`Wood Carving 1996`, an
image-only scan):

| | total | rate | per page |
|---|---|---|---|
| CPU | 48.2 s | 1.00 pages/s | 1.00 s |
| CUDA | 13.9 s | **3.46 pages/s** | 0.29 s |

3.5x, and the text is equivalent — the GPU/CPU difference over all 48 pages is
five lines that differ by a trailing space or a split, plus page 16 where the
GPU reads *"V-parting tool"* and the CPU *"Y-parting tool"* (the GPU is right).
OCR is therefore GPU by default; Health shows `GPU` / `CPU` under *OCR engine*.

Two things make it work, and both are easy to lose:

- **The DLLs.** `pip install nvidia-cudnn-cu12 nvidia-cublas-cu12
  nvidia-cuda-runtime-cu12` puts the CUDA runtime under
  `site-packages/nvidia/*/bin`, which onnxruntime's `LoadLibrary` does not
  search. `ocr_worker.enable_cuda()` adds those directories to PATH *and*
  `os.add_dll_directory()` before any session is built. Without it, CUDA
  silently falls back to the CPU.
- **The `rec_use_cuda` patch.** rapidocr 1.2.3 has a bug:
  `UpdateParameters.update_rec_params` strips the `rec_` prefix from
  `rec_model_path` only, so `rec_use_cuda=True` lands in the config under the
  literal key `rec_use_cuda` and `OrtInferSession` never sees `use_cuda`. The
  detector has the correct behaviour, which makes the failure look like "GPU
  works" while ~96% of the work — the recogniser — stays on the CPU.
  `_patch_rapidocr_cuda_flag()` fixes it; call it before building the engine.

**Do not "fix" `cudnn_conv_algo_search`.** It is widely reported as the reason
GPU OCR is slow — docling #4167 measures CUDA at 4.4x *slower* than the CPU
and prescribes `cudnn_conv_algo_search: DEFAULT`, on the theory that the
recogniser's ever-changing crop shapes make cuDNN's exhaustive algorithm search
unprofitable. Measured here, that advice inverts: same 48 pages, 61.7 s
(0.78 pages/s) with `DEFAULT` versus 13.9 s (3.46 pages/s) with the default
`EXHAUSTIVE`, i.e. `DEFAULT` is slower than the CPU. The default is left alone.
(docling #2727 is the same symptom from the other side — GPU at 1% usage.)

What did **not** help, so nobody re-tries it: bigger recognition batches (slower),
extra OCR threads (ONNX already uses every core), and a higher render DPI
(`det_limit_side_len=960` re-scales the image anyway).

`WOOD_OCR_PROVIDERS=CPU` forces the CPU. `WOOD_EMBED_PROVIDERS` does the same
for the semantic layer's embeddings.

## Rebuilds

```bat
py -3 build_concepts.py
```

Rebuilds `concepts.npz` / `concepts.json` from the current index (needs
`pip install fastembed`; the ONNX model downloads once, ~90 MB). Run it after a
bulk re-index — Health shows the layer's age. Studio works without it: only the
semantic neighbours drop out.

**Where it is written matters.** It lands in `<db folder>/vectors/`, beside
`index.db`, and not next to `build_concepts.py`. The tags are derived from one
library's `articles` table, so a file inside the app folder is shared by every
library that uses it: the second-library test showed Health reporting a
17-hour-old tag layer built from an entirely different set of books. `search_api`
still reads a copy in the app folder once, for installs that predate the move,
but never writes one there.

**An empty `articles` table is a normal exit, not a crash.** The vector comes
from `np.vstack` over the per-tag embeddings, so a library with no taxonomy rows
yet used to die with numpy's `need at least one array to concatenate` — which
reads like a bug and hides the real cause (nothing to embed). `main()` now
returns before the model is even loaded, printing which db it looked in and what
to do, and it leaves an existing `concepts.npz` untouched rather than
overwriting a good layer with an empty one.

## Logs

`logs/studio.log` (append) and the SSE stream behind `/api/events`. The UI
throttles the stream to at most one refresh every 600 ms so a long OCR or
re-index does not flood the UI with requests.
## Archives that were never reorganised

The original design assumed one thing that a real library breaks: books arrive
in a drop folder and stay there. In practice the tree is a decade of
accumulation, and 230 issues of a magazine archive sat next to the books with
no `sources` row at all.

Three fixes, in increasing order of how much they change:

- `ingest.walk_library()` walks the whole root and skips `store.SKIP_DIRS`
  (`_index`, `_trash`, `vendor`, dotfolders) so it can never index its own
  files. 0.7 s for 653 PDFs.
- **Scan whole library** (the button) and `studio.py scan` both use it.
  `books_payload()` uses the same walk for its not-indexed rows, so a PDF
  anywhere in the tree shows up as "not indexed" instead of being invisible.
- The walk is cached for 20 s (`library_files()`), because `/api/books` is
  re-fetched on every SSE tick and walking is far more expensive than a stat.

**One thread, not one per file.** The drop-folder scan still spawns a thread
per file, because that folder is small and latency matters. A whole-library
scan feeds the single `_index_queue` instead: 650 concurrent PyMuPDF extractions
would thrash the disk and contend for the one FTS writer, and the only visible
effect would be that everything got slower.

## A `sources` row that pointed at a database

`fwwfix.py` exists because of one bad row. A previous import had left the Taunton
archive as a single summary row:

```
sources: ('Fine Woodworking', 'Taunton Fine Woodworking Archive (230 issues)',
          'E:\...\Data-FWW\DB\FWW.db', 25268, 25268, 0, 'imported-from-db')
```

`FWW.db` is a 170 MB SQLite file, not a PDF. It claimed 25,268 indexed pages
that no row in `pages` backed, and the 230 real PDFs had no `sources` row at
all. So every scan saw them as new, the book list showed 230 phantom
"not indexed" rows, and Health reported 92% coverage.

The tempting fix — reindex the 230 PDFs — was wrong, and measurably so. The
pages were *already there*, with better metadata than a fresh ingest would
produce: `pages.title` is "Fine Woodworking Winter 1975", not
"0001-1975-Winter", and `fwwmap` already mapped each of the 25,268 rowids to
its real PDF and page number, so "Open PDF" worked. Reindexing would have
stored the same content twice under worse titles.

The repair reads the truth out of `pages` + `fwwmap`, writes one `sources` row
per PDF, and drops the ghost. No page row is touched, so duplication is not
possible. It checkpoints WAL and copies `index.db` next to itself first — this
writes to the one file everything depends on, so that is not optional.

## Keeping the semantic layer honest

Three things can invalidate a stored vector, and each has its own detection:

| what changed | how it is caught |
|---|---|
| the model changed | `meta.json` records it; a mismatch resets the store |
| a page row disappeared | `_semantic_dirty()` → `vectors.prune()` after any delete |
| another process rebuilt the store | matrix cached by file size, `meta.json` by mtime |

The last one is the subtle one. The CLI is documented as safe to run while the
server is up, and it was silently not: `_meta()` returned the dict it had read
at startup forever, so a completed build in another process left the server
believing there were no vectors. Caching on mtime fixed it, and the whole
transition is verified by resetting the store mid-session and watching `mode`
go `hybrid → keyword → hybrid` with no restart.

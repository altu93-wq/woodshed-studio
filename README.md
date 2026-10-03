# Woodshed Studio

A local web app for a folder of PDFs. It indexes every page of every book into
one SQLite database and makes the whole library searchable — full text, then
semantics on top — with a reader that opens the actual page you hit.

No cloud, no account, no upload. Your PDFs stay where they are; only the text
goes into a database you own.

## What it does

- **Search** — FTS5 with exact phrases, `-exclude`, any/all/word groups, year
  and collection filters, facets and highlighted snippets. Results are
  diversified so one book cannot eat the whole page.
- **Hybrid ranking** — keyword results and page-embedding results are fused
  with Reciprocal Rank Fusion, so "how do I stop tearout" finds the page that
  describes the technique without ever using those words.
- **Topic Map** — a co-occurrence graph of the terms that actually appear
  together in your library. Click a node to search it.
- **Reader** — opens the hit's real PDF at the right page with the searched
  word highlighted, so you can confirm a result in context.
- **Add & OCR** — the whole library as a sortable table. Drop PDFs in and they
  index themselves; **Scan whole library** walks the entire tree, so an archive
  you never reorganised works as-is. Scanned books are detected and flagged
  `image-only`, then unlocked with one-click OCR.
- **Names that mean something** — a magazine issue stored as `7.pdf` is shown
  as `Fine Woodworking No. 7`, worked out from the folder it sits in. The PDF
  filename always stays visible underneath so a result is never a guess you
  cannot check.
- **Health** — real counts, index coverage, duplicate detection, the OCR queue,
  semantic-vector coverage and a live log.

## Requirements

Python 3.12+. Everything else is the standard library.

3.12 is the floor rather than a technical one — the code also parses as 3.10
syntax — but 3.12 is the oldest interpreter this has actually been run on.

```
py -3 -m pip install -r requirements.txt
```

An NVIDIA GPU is optional. It makes semantic indexing about 90x faster (0.7
ms/page instead of 65) and OCR about 3.5x faster; without one everything still
works, just slower, and `onnxruntime` prints a few harmless CUDA warnings while
it falls back to the CPU.

## Quick start

```
py -3 studio.py init      # create the index schema (empty folders are fine)
py -3 studio.py scan      # index every PDF under the library root
py -3 studio.py embed     # build the semantic vectors (optional but good)
py -3 studio.py serve     # http://localhost:8766
```

On Windows, `studio.cmd` opens the browser and starts the server.

## Pointing it at your library

The library root is the folder that holds your book folders. Two ways to say
where it is:

```
py -3 studio.py paths --root D:\MyBooks     # write config.json once
set WOOD_ROOT=D:\MyBooks                    # or per session
```

With no configuration at all, the app uses its own parent folder — which is
correct for the layout it ships in:

```
MyLibrary\
├── _index\
│   └── studio\          <- the app (this folder)
├── new books\           <- drop folder
└── ...your book folders
```

| what | env var | `config.json` | default |
|---|---|---|---|
| library root | `WOOD_ROOT` | `library_root` | app's parent, if it has an `_index` folder |
| index database | `WOOD_INDEX_DB` | `index_db` | `<root>/_index/index.db` |
| drop folder | `WOOD_INBOX` | `inbox` | `<root>/new books` |
| trash | `WOOD_TRASH` | `trash` | `<root>/_trash` |

See `config.example.json`. `py -3 studio.py where` prints what actually
resolved — the first thing to check when something looks wrong.

## Command line

```
py -3 studio.py where            show the resolved paths
py -3 studio.py init             create the index schema (idempotent)
py -3 studio.py scan             index every new PDF under the library root
py -3 studio.py scan --dry-run   list what would be indexed, change nothing
py -3 studio.py scan --root D:\  scan another tree
py -3 studio.py embed            build the semantic vectors (resumable)
py -3 studio.py embed --status   how many pages already have one
py -3 studio.py paths --root D   write config.json
py -3 studio.py serve [--port N] start the web app
```

`scan` and `embed` are incremental: re-running them only does what is missing,
and both can be interrupted and resumed.

## Environment variables

| variable | effect |
|---|---|
| `WOOD_ROOT`, `WOOD_INDEX_DB`, `WOOD_INBOX`, `WOOD_TRASH` | paths, see above |
| `WOOD_EMBED_MODEL` | embedding model, default `BAAI/bge-small-en-v1.5` |
| `WOOD_EMBED_PROVIDERS` | ONNX providers, e.g. `CPUExecutionProvider` |
| `WOOD_EMBED_AUTOBUILD=0` | do not finish a partial vector build at startup |
| `WOOD_OCR_PROVIDERS` | OCR providers, e.g. `CPUExecutionProvider` |

Changing `WOOD_EMBED_MODEL` invalidates every stored vector automatically —
cosine space is not comparable across models, so mixing them would return
nonsense rather than fail.

## Rules it keeps

- **One writer.** `store.Writer` owns the only writable connection to the
  database and runs every write on its own thread. Queries open it read-only.
  WAL mode plus a busy timeout means a long ingest never blocks a search.
- **Delete is index-only by default.** Removing a book deletes its rows and
  never touches the PDF. You are asked separately whether to move the file, and
  if you do, it goes to `_trash/` rather than being erased.
- **Your files are read, never moved or renamed** unless you explicitly ask.
  PDFs outside the library root are never served over HTTP.
- **Duplicates are handled, not hidden.** A book indexed under both `D:\...`
  and `d:\...` shows as one entry with a badge; Merge keeps one copy and marks
  the other ignored so a later scan does not re-add it.

## Where the data lives

```
MyLibrary\
├── _index\
│   ├── index.db          <- SQLite + FTS5, one row per page
│   └── vectors\          <- per-library model artefacts
│       ├── pages.f32     <-   page embeddings + map.db
│       └── concepts.npz  <-   topic-tag embeddings
└── ...
```

`index.db` is the only thing worth backing up; everything under `vectors/` is a
cache (`studio.py embed`, `build_concepts.py`) derived from it. Both are in
`.gitignore`.

Because those artefacts are per library, one app folder can serve several
libraries — point `WOOD_ROOT` somewhere else and nothing is shared.

## Scripts in this repo

| file | role |
|---|---|
| `studio.py` | CLI: paths, schema, scan, embed, serve |
| `server.py` | HTTP server, folder watcher, book and job endpoints |
| `search_api.py` | querying: search, related topics, topic graph, text |
| `vectors.py` | page embeddings, cosine search, incremental build |
| `store.py` | paths, schema, single-writer connection |
| `ingest.py` | PDF to text with PyMuPDF, classifies indexed vs image-only |
| `ocr_worker.py` | OCR pass for scanned books (stop/resume safe) |
| `years.py`, `yearfind.py`, `yearfix.py` | publication-year detection and repair |
| `lexicon.py` | US/UK spelling, Turkish-to-English term seeds |
| `build_concepts.py` | rebuilds the topic-tag embedding layer (per library) |
| `titles.py`, `titlefix.py` | publication name for a book; repair pass for already-indexed ones |
| `fwwfix.py` | one-off repair for archives imported from another database |
| `studio.html`, `viewer.html`, `vendor/` | UI; PDF.js is vendored (Apache-2.0, see [`vendor/README.md`](vendor/README.md)) so it works offline |

[`MANUAL.md`](MANUAL.md) is the day-to-day guide: how to get books in, what
each search field does, what every button in Health means, and what to do when
something looks wrong.

[`NOTES.md`](NOTES.md) explains why each of those is built the way it is, with
the measurements behind the decisions.

## Honest limits

- **The first semantic build is not instant.** 100k pages is ~70 s on a GPU,
  ~1.8 h on a CPU. Until it passes 20% coverage the app uses keyword search
  only and says so.
- **OCR is slow and imperfect.** It exists to make a scanned book findable,
  not to produce a clean text edition.
- **Semantic search is English.** The default model is an English one;
  Turkish queries are expanded by `lexicon.py`, not translated.
- **One writer.** Do not run the server and `studio.py scan` at the same time;
  the CLI warns you if the database is locked.

## License

MIT. See [LICENSE](LICENSE).

The vendored PDF.js build in [`vendor/`](vendor/) is © Mozilla Foundation and
stays under Apache-2.0 — see [`vendor/LICENSE.pdfjs`](vendor/LICENSE.pdfjs).

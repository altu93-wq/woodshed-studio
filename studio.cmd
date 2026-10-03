@echo off
REM Woodshed Studio - one web app over a PDF library index.
REM Search-first homepage + Topic Map + Add & OCR + Health.
REM
REM The library is found automatically (this folder's parent if it holds an
REM "_index" folder). To point it somewhere else, either:
REM     set WOOD_ROOT=D:\MyBooks
REM or run once:  py -3 studio.py paths --root D:\MyBooks
REM
REM PDFs in the drop folder are auto-indexed; "Scan whole library" in the
REM Add & OCR tab indexes everything under the library root; scanned books are
REM flagged image-only -> one-click OCR.
start "" http://localhost:8766
py -3 "%~dp0studio.py" serve

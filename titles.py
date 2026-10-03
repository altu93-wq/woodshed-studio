#!/usr/bin/env python3
"""Shared publication-title logic for Studio (the counterpart of `years.py`).

`years.py` answers "which year is this book?". This answers "what is this
thing called?" - the question that matters for a magazine issue stored as
`1.pdf`, where the filename carries nothing at all.

The one rule that matters
-------------------------
**A filename that already names the book is never rewritten.** Turning
`Anarchists Tool Chest - Christopher Schwarz.pdf` into `Anarchists Tool
Chest Christopher Schwarz` is a regression, not an improvement: the
existing `sources.title` keeps the author's punctuation and reads better
than anything derived from it. So `detect()` returns `None` for those, and
the UI keeps showing what it shows today.

Only a filename with no name of its own - a bare `7.pdf`, or
`issue 12 final.pdf` - is handed to the resolver below, and only for those
can a real publication name appear.

Why a separate column instead of rewriting `sources.title`
--------------------------------------------------------
`title` is the filename-derived name and is what `pages.title` and `fwwmap`
agree on. Overwriting it would rewrite 99k page rows and break the link
between the archive and the database it was imported from. The derived name
lives in `sources.pub` and is what the reader sees; `title` stays the honest
provenance underneath it.

Where the name comes from, strongest source first
-------------------------------------------------
1. `folder`   - the top-level folder under the library root IS the
   publication (`collection_for()` already put it in `sources.collection`).
   This is what rescues a folder of numbered issues, and it needs no PDF
   to be opened: `7.pdf` inside `Fine Woodworking 2025` resolves without
   ever parsing the file.
2. `metadata` - the PDF's own Title field. Reliable on digital PDFs, EMPTY
   on most scans, and frequently the scanner's name rather than the
   publication's, so a person-looking value is rejected.
3. `masthead` - a known publication name in the first page's text. The
   masthead is a logo on most magazines, so OCR mangles it: on a 230-issue
   scanned archive an elastic match recovered the name for only 13% of
   issues. Kept because it is free on digital PDFs and because the folder
   rule usually beats it anyway.
4. `fallback` - nothing found: keep the filename and mark it, so the book
   list can show that this one was not actually identified.

Every result carries `src`, so `titlefix.py` can write an audit CSV and a
wrong guess is traceable rather than mysterious.
"""
import os
import re

# Publications whose masthead is worth looking for on page 1.
KNOWN_PUBS = [
    "Fine Woodworking", "Popular Woodworking", "Fine Homebuilding",
    "The Woodworker's Journal", "Woodworker's Journal", "Shop Notes",
    "American Woodworker", "Modern Woodworking", "Family Handyman",
    "Popular Mechanics", "Woodsmith",
]

# Folder names that are structure, not a publication.
_NOT_A_PUB = re.compile(
    r"^(books?|pdfs?|magazines?|issues?|serie[sz]es?|new\s+books?|"
    r"archive|downloads?|library|scans?|various|misc|\d{3,4})$", re.I)

# Words that appear in "issue 12 final", "draft v2", "scan 3" - scaffolding
# around a number, never the publication's name.
_SCAFFOLD = re.compile(
    r"^\W*(issue|issues|no|nos?|number|part|vol|volume|final|draft|v\d+|"
    r"scan|copy|new|old|full|complete|the|a|an)\W*$", re.I)

# A trailing issue number, separated by punctuation or a word boundary.
# Bare `\s*` would let "Woodworking" be read as the name and stop at the
# trailing digits, turning "Fine Woodworking 234" into "Fine".
_SEP = r"[\s_\-–—.]*"
_NUM = re.compile(rf"(?:no\.?|issue|#)?{_SEP}(\d{{1,4}})\s*$", re.I)
_FENCE = re.compile(r"[_\-–—.,:;]+")
# A stem that is nothing but digits and separators carries no name at all.
_ONLY_NUM = re.compile(r"^[\s_\-–—.]*\d{1,5}[\s_\-–—.]*$")


def stem(path):
    """Filename without extension - what the UI would otherwise show."""
    return os.path.splitext(os.path.basename(path))[0]


def issue_of(path):
    """The trailing issue number of a filename, or None ('7.pdf' -> '7').

    A version marker is not an issue: 'draft v2.pdf' is issue-less, otherwise
    a draft of issue 2 would be filed as issue 2.
    """
    s = stem(path).strip()
    if re.search(r"\bv\d+\s*$", s, re.I):
        return None
    m = _NUM.search(s)
    return m.group(1) if m else None


def from_filename(path):
    """The publication name a filename states outright, or None.

    Used only to decide whether a file needs a derived name at all; the
    return value is not what gets displayed.
    """
    s = stem(path).strip()
    if _ONLY_NUM.match(s):
        return None
    name = re.sub(r"\s+", " ", _FENCE.sub(" ", _NUM.sub("", s))).strip(" .-")
    if not name or _NOT_A_PUB.match(name):
        return None
    # A real name needs at least one substantial word: three letters or more
    # that is not scaffolding. "issue 12 final" and "scan 3" have digits and
    # filler words but no name, so they go on to the folder/metadata rules.
    if not any(len(w) >= 3 and not _SCAFFOLD.match(w)
               for w in name.split()):
        return None
    return name


def from_folder(collection):
    """The top-level folder is the publication: 'Fine Woodworking 2025'.

    A trailing year is dropped from the name, so the title reads
    'Fine Woodworking' and the year stays a year rather than text.
    """
    c = (collection or "").strip()
    if not c or _NOT_A_PUB.match(c):
        return None
    m = re.match(r"^(.*?)[\s_\-]*(\d{4})$", c)
    if m and m.group(1).strip():
        return m.group(1).strip()
    return c


def from_metadata(pdf_title):
    """The PDF's Title field, unless it looks like a person or a filename."""
    t = re.sub(r"\s+", " ", (pdf_title or "")).strip(" ._-")
    if not t or len(t) > 90:
        return None
    low = t.lower()
    # Scanners put their own name here ("Francois Cournoyer"), so a value
    # with no publication-ish keyword in it is dropped rather than shown.
    if not any(k in low for k in ("wood", "magazine", "journal", "issue",
                                  "shop", "craft", "work", "home")):
        return None
    if low.endswith(".pdf"):
        return None
    return t


def from_masthead(text, pubs=None):
    """A known publication name in the first page's text, OCR-tolerantly.

    Up to six junk characters are allowed between words, because a real
    masthead comes out of OCR as "Fine WoodWorking , , , ,".
    """
    t = text or ""
    for pub in (pubs or KNOWN_PUBS):
        pat = r"[^a-z0-9]{0,6}".join(re.escape(w) for w in pub.split())
        if re.search(pat, t, re.I):
            return pub
    return None


def combine(pub, issue, year=None):
    """One readable title from the parts.

    The year is deliberately NOT appended: the folder rule already strips a
    trailing year off 'Fine Woodworking 2025' so that it can be shown as its
    own badge, and appending it here would print 'Fine Woodworking No. 7 2025'
    next to a '2025' badge. `year` is kept in the signature because the
    callers pass it and a title may legitimately want it.
    """
    if not pub:
        return None
    return f"{pub} No. {issue}" if issue else pub.strip()


def detect(path, collection=None, meta_title=None, head_text=None, year=None):
    """Resolve (display_title_or_None, source).

    `None` means "the filename already names this well enough - keep the
    existing title". `head_text` should be the first page's text; it is the
    most expensive input and is only consulted after the cheap sources have
    been tried.
    """
    if from_filename(path):
        return None, "keep"

    issue = issue_of(path)

    pub = from_folder(collection)
    if pub:
        return combine(pub, issue, year), "folder"

    pub = from_metadata(meta_title)
    if pub:
        return combine(pub, issue, year), "metadata"

    pub = from_masthead(head_text)
    if pub:
        return combine(pub, issue, year), "masthead"

    return None, "fallback"
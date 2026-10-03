#!/usr/bin/env python3
"""Publication-year detection for books already in the index.

`years.py` (the ingest-time rule) is deliberately cheap: one regex over the
title plus the first 6 and last 4 pages. It is fine for freshly added books,
but it was never re-run over the 422 books already in the library, and it makes
mistakes that matter for a field the user called important:

  1. `title_year()` wins unconditionally. "Puzzles in Wood (1956-2007)" was
     filed under 1956 although the page text says "Fox Chapel Publishing,
     2007" -- the title range is the *original* edition, the text is *this*
     edition.
  2. The weak fallback ("most common bare year") happily picks a model number
     ("T-1810") or a phone number ("593-1777").

Approach
--------
Every plausible year becomes a weighted candidate carrying the sentence it
came from, and the heaviest one wins. Candidates are judged by their *own*
surrounding context rather than by pre-stripping the text: an earlier version
blanked any line containing the word "each" (it looks like a price list) and
that silently deleted the copyright lines of 47 books.

    from yearfind import detect
    year, conf, why = detect(title, head_text, tail_text)
"""
import re

MIN_PLAUSIBLE = 1450
MAX_PLAUSIBLE = 2026

YEAR = re.compile(r"(?<!\d)(\d{4})(?!\d)")

# ---------------------------------------------------------------------------
# context tests. Each exists because it bit us on a real book.
# ---------------------------------------------------------------------------
# "T-1810", "T1812", "HP40", "Model T-1533" -- a part number. The digits must
# touch a letter or follow a short all-caps code, or the token is a model.
MODEL = re.compile(r"\b[A-Za-z]{1,4}[-‐‑]\d{2,4}[A-Za-z]?\b|"
                   r"\b[A-Z]{1,3}\d{3,4}\b|"
                   r"\bmodel\s+no?\.?\s*\d+\b", re.I)
# "Phone: (610) 593-1777", "Fax 593-2002", "e-mail a@b.com/1997"
PHONE = re.compile(r"\(\d{3}\)\s*\d{3}|\b\d{3}[-‐‑]\d{4}\b")
CONTACT_WORD = re.compile(r"\b(?:phone|fax|tel|telephone|e-?mail|www|http|url)\b", re.I)
# "only $9.95", "each 24.50", "save $5 off", "US $14.95"
PRICE = re.compile(r"[$£€]\s*\d|\b(?:price|each|coupon|order now|"
                   r"subscription|gift)\b", re.I)
# Dimension and metric tables: "0.390625 9.9219 19.0500 13/32 22.6219". These
# pages are full of bare 4-digit floats that look exactly like years.
METRICS = re.compile(r"\d\.\d{2,}|\b\d{1,3}/\d{1,3}\b|\b(?:mm|cm|inches|"
                     r"thick|dia\.?|width|height|length)\b")
# "circa 1880-1910", "the 1776 period", "about 1900". NB: no bare `\bc\b`
# alternative -- it matched the "C" in "Washington, D.C." and threw away a
# real "Â© 1979" copyright line.
CIRCA = re.compile(r"\b(?:circa|about|approximately|antiqu|period|style of|"
                   r"reprinted)\b", re.I)
# A run glued to other digits or a range dash on BOTH sides: "1,904 copies",
# "pp. 32-1880-39". A single dash followed by a letter is a filename
# separator ("1855-TheArtOfStairBuilding") and must not disqualify anything.
RANGE_BOTH = re.compile(r"[\d,]\s*[-‐‑/–—]\s*$|^\s*[-‐‑/–—]\s*\d")
NEIGHBOUR_DIGIT = re.compile(r"[\d,]\s*$|^\s*[\d,.]")

# ---------------------------------------------------------------------------
# patterns in the page text, strongest first
# ---------------------------------------------------------------------------
TEXT_MARKERS = [
    (re.compile(r"first\s+published\s*(?:in\s+)?[^\n]{0,60}?(\d{4})", re.I), 96,
     "first published"),
    (re.compile(r"first\s+edition\s*[^\n]{0,40}?(\d{4})", re.I), 94, "first edition"),
    (re.compile(r"printing\s+date\s*:?\s*(\d{4})", re.I), 92, "printing date"),
    # "Â© 1979" -- the leading Â is an OCR artefact of the copyright sign.
    (re.compile(r"[Ââ]?\s*©\s*[^\n]{0,24}?(\d{4})", re.I), 88, "copyright sign"),
    (re.compile(r"(?:copyright|copyrighted|copy\s?right|\(c\)|cpyright|"
                r"cpyr\.?ight|coryright|copyright\s+ed)\s*[^\n]{0,24}?(\d{4})", re.I),
     88, "copyright line"),
    (re.compile(r"\bCIP\b[^\n]{0,90}?copyright\s*[@©]?\s*(\d{4})", re.I), 88,
     "copyright line (CIP)"),
    (re.compile(r"catalog(?:uing|ing)\s+in\s+publication\s+data\s*"
                r"[^.]{0,220}?(\d{4})", re.I | re.S), 86,
     "library of congress publication block"),
    # "published" as a bare verb is ambiguous: the furniture books say "the
    # Director published in 1754" while describing a DIFFERENT piece. The
    # pattern must not start mid-sentence with a capital-looking subject, and
    # "in <year>" is required, which is how a real imprint reads.
    (re.compile(r"(?:^|[.;]\s|(?:was|were|is)\s)published\s+(?:in|at)\s+[^.\n]{0,24}?(\d{4})",
                re.I | re.M), 84, "published in"),
    # "was published in London in 1762 and was in use in the Colonies in 1763"
    # -- a modern author describing an EARLIER book, not this printing. A real
    # imprint puts the year at the end of the phrase with nothing after it.
    (re.compile(r"originally\s+published[^\n]{0,30}?(\d{4})", re.I), 56,
     "originally published (the original edition, not this printing)"),
    (re.compile(r"printed\s+in\s*[^\n]{0,30}?(\d{4})", re.I), 80, "printed in"),
    (re.compile(r"printing\s*[^\n]{0,24}?(\d{4})", re.I), 74, "printing"),
    # "SECOND EDITION, 1911   THIRD EDITION, 1912" -- a chain of printings with
    # no marker of its own. The last year in the chain is this printing.
    (re.compile(r"\b(?:second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|"
                r"eleventh|twelfth|thirteenth|fourteenth|fifteenth|sixteenth|"
                r"seventeenth|eighteenth|nineteenth|twentieth|\d+(?:st|nd|rd|th))"
                r"\s+(?:edition|printing)[,.\s]{0,6}(\d{4})", re.I), 76,
     "edition/printing chain"),
    (re.compile(r"originally\s+published[^\n]{0,30}?(\d{4})", re.I), 56,
     "originally published (the original edition, not this printing)"),
]
CATALOGUE = re.compile(r"\b(?:CIP|IPC|LCCN|catalog(?:uing|ing) in publication data|"
                       r"bookshelf)\b", re.I)
REPRINT = re.compile(r"re-?print|reissue|re-?issued|revised|rev\.\s|new edition|"
                     r"(?:second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|"
                     r"eleventh|twelfth|thirteenth|fourteenth|fifteenth|sixteenth|"
                     r"seventeenth|eighteenth|nineteenth|twentieth)\s+edition", re.I)


def _ok(y):
    return isinstance(y, int) and MIN_PLAUSIBLE <= y <= MAX_PLAUSIBLE


def _clean(t):
    """Collapse whitespace, including the hard line breaks OCR leaves.

    Words get split across lines ("U. S. Gov-\\nernment Printing Office"), which
    hid a government printer behind a word boundary. Only a lowercase letter
    after the hyphen is treated as a split word, so genuine ranges
    ("1874-\\n1754") keep their dash and stay rejectable.
    """
    t = re.sub(r"([a-z])-\s*\n\s*(?=[a-z])", r"\1", t or "")
    return re.sub(r"\s+", " ", t)


# A back-matter list of OTHER books on the subject. "BOOKS ON FURNITURE &
# DECORATION / Published in England previous to 1800 / White, R." was being
# read as this book's imprint and overwrote a correct 1924.
OTHER_BOOKS = re.compile(
    r"\b(?:books?\s+on|also\s+by|see\s+also|bibliograph|list\s+of\s+books|"
    r"further\s+reading|recommended|references|catalog(?:ue)?\s+of|"
    r"works\s+by|works\s+of|read\s+also)\b", re.I)


# Years belonging to something OTHER than this book's imprint, each seen in
# this library:
#   "U. S. Government Printing Office, 1940"  -> a US government printing
#   "This day-book was published in 1873 by Louis Courajod" -> a quoted source
#   "DUCORS (Barthelemy), menuisier (1707)"    -> a craftsman's birth year
IMPOSTOR = re.compile(
    r"\b(?:government(?:\s+printing)?(?:\s+office)?|"
    r"his\s+majesty|her\s+majesty|crown|royal|state\s+department|"
    r"department\s+of\s+agriculture|congress|bureau\s+of|"
    r"printing\s+office|press\s+of|lithograph|engraved\s+by)\b", re.I)
# A relative clause naming someone else: "X was published in 1873 by Y".
BY_SOMEONE = re.compile(r"\b(?:by|edited\s+by|translated\s+by|collected\s+by|"
                        r"revised\s+by|described\s+by)\s+[A-Z]|\b[A-Z][a-z]+\s+[A-Z]", re.M)
# "(1707)" straight after a name-like token -> a person's dates, or a plate
# figure number.
PAREN_DATE = re.compile(r"[A-Za-z]{2,}\s*[(,]\s*\d{4}\s*[),]")
# A quoted source: "This day-book was published in 1873 by Louis Courajod".
QUOTED_SOURCE = re.compile(
    r"\b(?:this|that|the\s+above|it)\b[^.]{0,60}\b(?:was|were|had\s+been)\s+"
    r"(?:published|printed|issued|reprinted)\b|\bday-?book\b", re.I)
# "COPYRIGHT 1912 IRA S. GRIFFITH   FOURTH EDITION. 1917" -- the copyright is
# the original edition, the number after the edition statement is the printing
# in your hand. Demote a copyright year when an edition/printing count sits
# between it and the end of the sentence.
EDITION_AFTER = re.compile(
    r"(?:\b(?:second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|"
    r"eleventh|twelfth|thirteenth|fourteenth|fifteenth|sixteenth|seventeenth|"
    r"eighteenth|nineteenth|twentieth|\d+(?:st|nd|rd|th))\s+(?:edition|printing)\b"
    r"|\b(?:edition|printing|reprint|reissue)\b)", re.I)


def _judge(before, after, strong=False):
    """Return None if this number cannot be a publication year.

    Disqualifying words only count when they sit NEAR the number. An earlier
    version tested the whole 70-char window, so a URL printed on the same line
    ("www.creativepub.com  Copyright (c) 2009 Creative Publishing") threw away
    the real copyright year of the book.

    Every check below applies to `strong` years too. An earlier version let a
    "strong" year skip them, which let "circa 1800" in a caption masquerade as
    the publication year of a 1924 book.
    """
    if NEIGHBOUR_DIGIT.search(before[-6:]) or NEIGHBOUR_DIGIT.search(after[:6]):
        return None
    if RANGE_BOTH.search(before[-12:]) or RANGE_BOTH.search(after[:12]):
        return None
    if PHONE.search(before[-14:]) or PHONE.search(after[:14]):
        return None
    if OTHER_BOOKS.search(before[-40:]) or OTHER_BOOKS.search(after[:40]):
        return None
    if IMPOSTOR.search(before[-60:]) or IMPOSTOR.search(after[:30]):
        return None
    if PAREN_DATE.search(before[-40:]):
        return None
    if QUOTED_SOURCE.search(before[-64:]):
        return None
    if CONTACT_WORD.search(before[-26:]) or CONTACT_WORD.search(after[:26]):
        return None
    if PRICE.search(before[-18:]) or PRICE.search(after[:18]):
        return None
    if METRICS.search(before[-22:]) or METRICS.search(after[:22]):
        return None
    if MODEL.search(before[-22:]) or MODEL.search(after[:22]):
        return None
    if CIRCA.search(before[-26:]) or CIRCA.search(after[:20]):
        return None
    return ""


def _add(cands, y, weight, label, before, after, strong=False):
    """Add a candidate unless its context disqualifies it."""
    if not _ok(y):
        return
    note = _judge(before, after, strong)
    if note is None:
        return
    prev = cands.get(y)
    if prev is None or weight > prev[0]:
        cands[y] = (weight, label)


def detect(title, head_text="", tail_text="", min_conf=0):
    """Return (year, confidence 0-100, evidence).

    head_text is the first few pages joined, tail_text the last few. They are
    scored as one candidate set: a copyright page usually states several years
    and the rivals are what tells you which one is the printing in your hand.
    """
    head = _clean(head_text)
    tail = _clean(tail_text)
    t = _clean(title)
    cands = {}

    # ---- explicit publication statements ---------------------------------
    # Back matter is much weaker evidence: the last pages of a book are full of
    # bibliographies ("Chippendale, T. Gentleman (First Edition) 1754") and
    # further-reading lists. The imprint lives at the front.
    for block, where, mult in ((head, "front matter", 1.0),
                               (tail, "back matter", 0.55)):
        for rx, weight, label in TEXT_MARKERS:
            for m in rx.finditer(block):
                if not m.groups():
                    continue
                y = int(m.group(1))
                if y < 1600:
                    continue
                before = block[max(0, m.start() - 70):m.start()]
                after = block[m.end():m.end() + 50]
                w = int(round(weight * mult))
                # An edition chain is only this book's when a copyright or
                # publication statement introduces it. "2nd edition, 1856"
                # inside a bibliography describes somebody else's volume.
                if label == "edition/printing chain" and not re.search(
                        r"copyright|©|\(c\)|printed|published|imprint|edition\s+of",
                        before[-60:], re.I):
                    continue
                if REPRINT.search(before + " " + after):
                    w -= 4
                label_here = label
                # "COPYRIGHT 1910   SECOND EDITION, 1911   THIRD EDITION, 1912"
                # -- a chain of printings. The year of the printing in your
                # hand is the LAST one, so demote any year that is followed by
                # a further edition statement.
                if EDITION_AFTER.search(after[:60]):
                    w -= 22
                    label_here = label + " (before an edition statement)"
                # ...and reward the year that comes AFTER such a statement.
                elif EDITION_AFTER.search(before[-60:]) and not OTHER_BOOKS.search(
                        before[-60:] + after[:40]):
                    w += 18
                    label_here = label + " (this printing)"
                _add(cands, y, w, "%s in the %s" % (label_here, where), before, after,
                     strong=True)

    # ---- the title -------------------------------------------------------
    # "1855-TheArtOfStairBuilding-Perry": the user names these files by
    # publication year, so a LEADING year is strong. "Puzzles in Wood
    # (1956-2007)" is a range: the original edition, so weak.
    for m in YEAR.finditer(t):
        y = int(m.group(1))
        if not _ok(y):
            continue
        before = t[max(0, m.start() - 8):m.start()]
        after = t[m.end():m.end() + 8]
        # "T-1810 Manual": the digits belong to the model, not to a year. The
        # title is checked strictly -- there is no copyright line to vouch for
        # a year that only looks like a date.
        if MODEL.search(before[-8:]) or MODEL.search(after[:8]):
            continue
        lead = m.start() <= 1
        both_sides = bool(re.search(r"[\d,]\s*[-–—]\s*$", before)) and \
            bool(re.match(r"\s*[-–—]\s*\d", after))
        if both_sides:
            _add(cands, y, 30,
                 "year range in the title (the period the book covers, not its "
                 "publication)", before, after, strong=True)
        elif lead:
            _add(cands, y, 78, "year at the front of the file name", before, after,
                 strong=True)
        else:
            _add(cands, y, 62, "year in the title", before, after)

    # ---- last resort: a bare year ----------------------------------------
    # Deliberately light. A bare number on a contents page is far more often a
    # page count than a date, and 62/78 for a title year always outranks it.
    cat = bool(CATALOGUE.search(head) or CATALOGUE.search(tail))
    for block, where in ((head, "front matter"), (tail, "back matter")):
        for m in YEAR.finditer(block):
            y = int(m.group(1))
            if y < 1850 or not _ok(y):
                continue
            before = block[max(0, m.start() - 70):m.start()]
            after = block[m.end():m.end() + 50]
            if cat:
                _add(cands, y, 46, "year inside a catalogue block in the %s" % where,
                     before, after)
            else:
                _add(cands, y, 26, "bare year in the %s" % where, before, after)

    if not cands:
        return None, 0, "no year candidate in the title or the first/last pages", False

    y, (conf, why) = max(cands.items(), key=lambda kv: (kv[1][0], kv[0]))
    ranked = sorted(cands.items(), key=lambda kv: (-kv[1][0], -kv[0]))
    from_title_only = "title" in why
    rivals = [(yy, c) for yy, (c, _) in ranked if yy != y]
    if rivals and rivals[0][1] >= conf - 6:
        conf -= 10
        why += " — contested by %s" % ", ".join(
            "%s (%d)" % (yy, c) for yy, c in rivals[:2])
    if conf < min_conf:
        return None, conf, "below threshold: " + why, False
    return y, conf, why, from_title_only


# A "strong" marker that is also followed by an edition statement is really
# describing the ORIGINAL edition ("COPYRIGHT 1910  SECOND EDITION, 1911
# THIRD EDITION, 1912"). Which printing you hold cannot be resolved from the
# text alone, so the evidence does not justify overwriting anything.
NEEDS_CORROBORATION = 70


# ---------------------------------------------------------------------------
# policy: what we are actually willing to write to the database
# ---------------------------------------------------------------------------
# A bare year in the front matter is right maybe half the time; a year next to
# a copyright or "first published" marker is right nearly always. Guessing on a
# field the user called important is worse than leaving it empty, so weak
# evidence may fill a gap but may never overwrite an existing year.
MIN_TO_ASSIGN = 60        # weak evidence may fill a blank ...
MIN_TO_OVERWRITE = 60     # ... and replace one, but only at this strength
# A year that came from the FILE NAME alone may fill a blank but never replace
# what the pages already told us. "Pain - The Builder's Companion (1762)" says
# 1762, but that is the ORIGINAL edition; this scan is a 1931 reprint and only
# the pages can know that.
TITLE_ONLY_MAX = 70


def decide(current, detected, conf, from_title_only=False, why_hint=""):
    """Combine the stored year with a fresh detection.

    Returns (year, action) where action is one of:
        keep      -- leave the stored year alone
        assign    -- no year was stored, write the detected one
        replace   -- overwrite the stored year
        none      -- nothing good enough to store
    """
    if detected is None or conf < MIN_TO_ASSIGN:
        return current, ("keep" if current else "none")
    if current is None:
        return detected, "assign"
    if detected == current:
        return current, "keep"
    if from_title_only and conf < TITLE_ONLY_MAX:
        return current, "keep"
    if "before an edition statement" in why_hint and conf < NEEDS_CORROBORATION:
        return current, "keep"
    if conf >= MIN_TO_OVERWRITE:
        return detected, "replace"
    return current, "keep"


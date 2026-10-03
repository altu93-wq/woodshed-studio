#!/usr/bin/env python3
"""Woodworking lexicon for the Studio RELATED-TOPICS layer (not the main search).

Two jobs:

  1. Spelling / orthography variants.  FTS5 (porter unicode61) does not know that
     `rebate` and `rabbet` are the same joint, nor that `mitre` and `miter` are the
     same cut, so a query on one spelling found 0 tagged articles while the other
     found 94.  Every group member expands to `(member OR "other member")`.

  2. A small tr->en seed so a Turkish query still reaches the corpus
     (zıvana -> tenon/mortise, kırlangıç kuyruğu -> dovetail, ...).

Used only by search_api.fts_query_exp(), which /api/related and /api/subgraph call.
api_search() (the main search box) is deliberately untouched.
"""

import re

# --- spelling variant groups: every member expands to the others -------------
# Multi-word members are supported (quoted phrases).
GROUPS = [
    # woodworking terms first — these are the ones that actually break searches
    ("rebate", "rabbet"),
    ("mitre", "miter"),
    ("mortise", "mortice"),
    ("moulding", "molding"),
    ("bandsaw", "band saw"),
    ("tablesaw", "table saw"),
    ("halfblind", "half blind"),
    ("drawbore", "draw bore"),
    ("freehand", "free hand"),
    ("handheld", "hand held"),
    # general US/UK orthography
    ("center", "centre"),
    ("plough", "plow"),
    ("grey", "gray"),
    ("colour", "color"),
    ("aluminium", "aluminum"),
    ("fibre", "fiber"),
    ("metre", "meter"),
    ("jewellery", "jewelry"),
    ("catalogue", "catalog"),
    ("draught", "draft"),
]

# --- tr -> en seed (longest phrase is matched first) ------------------------
# ASCII-folded twins are included so a query typed without Turkish characters works.
TR_EN = [
    ("kırlangıç kuyruğu", "dovetail"),
    ("kirlangic kuyrugu", "dovetail"),
    ("kırlangıçkuyruğu", "dovetail"),
    ("zıvana", "tenon mortise"),
    ("zivana", "tenon mortise"),
    ("gönye", "miter"),
    ("gonye", "miter"),
    ("zımpara", "sandpaper"),
    ("zimpara", "sandpaper"),
    ("yapıştırıcı", "glue"),
    ("yapistirici", "glue"),
    ("çekmece", "drawer"),
    ("cekmece", "drawer"),
    ("menteşe", "hinge"),
    ("mentese", "hinge"),
    ("marangozluk", "woodworking"),
    ("marangoz", "woodworking"),
    ("çerçeve", "frame"),
    ("cerceve", "frame"),
    ("çekiç", "hammer"),
    ("cekic", "hammer"),
    ("testere", "saw"),
    ("sandalye", "chair"),
    ("sandık", "chest"),
    ("sandik", "chest"),
    ("ahşap", "wood"),
    ("ahsap", "wood"),
    ("vernik", "varnish"),
    ("dolap", "cabinet"),
    ("tezgah", "bench"),
    ("pencere", "window"),
    ("tabure", "stool"),
    ("tutkal", "glue"),
    ("yüzey", "surface"),
    ("yuzey", "surface"),
    ("kapı", "door"),
    ("kapi", "door"),
    ("masa", "table"),
    ("cila", "finish polish"),
]

# word characters incl. Turkish letters (used for word-boundary matching)
_LAT = "0-9A-Za-zÇĞİÖŞÜçğıöşü"
_WORD = re.compile(r"[0-9A-Za-zÇĞİÖŞÜçğıöşü][0-9A-Za-zÇĞİÖŞÜçğıöşü'\-]*")

ALIAS = {}
for _g in GROUPS:
    for _w in _g:
        ALIAS.setdefault(_w, set()).update(x for x in _g if x != _w)

_TR = sorted(TR_EN, key=lambda x: -len(x[0]))


def words(s):
    """Tokens for the expansion pass — keeps Turkish letters that the main
    fts_query() tokenizer (ASCII only) would drop."""
    return _WORD.findall(s or "")


def aliases(w):
    """Spelling variants of a single token (lower-cased)."""
    return sorted(ALIAS.get(w.lower(), ()))


def phrase_aliases(p):
    """Spelling variants of a whole quoted phrase, e.g. 'band saw' -> 'bandsaw'."""
    return sorted(ALIAS.get(p.strip().lower(), ()))


def translate(s):
    """Replace Turkish terms with their English equivalents (word-boundary safe)."""
    out = s or ""
    for tr, en in _TR:
        if tr not in out and tr.upper() not in out and tr.capitalize() not in out:
            continue
        out = re.sub(r"(?<![" + _LAT + r"])" + re.escape(tr) + r"(?!["
                     + _LAT + r"])", en, out, flags=re.IGNORECASE)
    return out

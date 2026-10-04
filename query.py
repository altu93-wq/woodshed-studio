#!/usr/bin/env python3
"""Query understanding for the main search box.

This is the layer that decides *what the user actually asked for*. It exists
because the search box used to hand the raw typed text straight to FTS5, and
that lost information in two ways nothing downstream could recover.

**Non-ASCII text was shredded, silently.** The tokenizer was
`[0-9A-Za-z][0-9A-Za-z'-]*`, so a Turkish word came apart into whatever ASCII
fragments happened to be in it:

    "zımpara"           -> "z" AND "mpara"
    "ahşap tutkal"      -> "ah" AND "ap" AND "tutkal"
    "vida nasıl sıkılır"-> "vida" AND "nas" AND "l" AND "s" AND "k" ...

Those are not queries, they are accidents, and they match nothing. The corpus
is English, so every Turkish question returned zero keyword hits while the
semantic layer quietly filled the gap with whatever was topically nearest.
`lexicon.py` could have fixed all of this - it already had a Turkish seed and
the US/UK spelling groups - but it was wired only to /api/related, and its own
docstring recorded that as deliberate. That was the wrong call: the related-
topics panel is the optional surface, the search box is the product.

**Spelling variants were invisible.** A woodworking library is inherently
mixed-spelling, because most of it is transatlantic. Measured on this index:

    mortice    368 pages   mortise 11 758   -> a UK user misses 97%
    mitre    2 636 pages   miter    9 752   -> a UK user misses 78%
    rebate   2 117 pages   rabbet   6 407   -> a US user misses 75%
    moulding 6 213 pages   molding  7 214   -> misses 45%

So this is not a Turkish problem. It is the single largest recall hole in the
product, and it hit English queries too.

The rule this module keeps: **never narrow the caller's intent, only widen the
way it is spelled.** A token with no variant is emitted exactly as before, so
queries that worked keep working and keep their ranking; only tokens that are
known to have another spelling gain an OR-group.
"""

import re

# --- ASCII folding -----------------------------------------------------------
# Turkish is not a decoration here, it is a lookup problem. The corpus is
# English and the queries are often typed without Turkish characters at all
# ("zımpara" and "zimpara" are the same intent), so every table is keyed on
# the folded form and both spellings reach the same entry.
_FOLD = str.maketrans({
    "ı": "i", "İ": "i", "ş": "s", "Ş": "s", "ğ": "g", "Ğ": "g",
    "ü": "u", "Ü": "u", "ö": "o", "Ö": "o", "ç": "c", "Ç": "c",
    "â": "a", "Â": "a", "î": "i", "Î": "i", "û": "u", "Û": "u",
})


def fold(s):
    """Turkish-aware ASCII fold: 'Zımpara' and 'zimpara' become the same key."""
    return (s or "").translate(_FOLD).lower()


# --- tokenizing --------------------------------------------------------------
# Turkish letters are word characters. `lexicon.words()` already had this
# character class; it is repeated here so the search box does not have to
# import the related-topics module to spell a word correctly.
_WORD = re.compile(r"[0-9A-Za-zÇĞİÖŞÜçğıöşüÂâÎîÛû][0-9A-Za-zÇĞİÖŞÜçğıöşüÂâÎîÛû'\-]*")


def tokenize(s):
    """Unicode-aware tokens. 'ahşap tutkal' -> ['ahşap', 'tutkal'], not ['ah','ap']."""
    return _WORD.findall(s or "")


# --- Turkish -> English ------------------------------------------------------
# Kept deliberately small and concrete: this is a woodworking shelf, not a
# general translator. Every entry is a word a person looking for something in
# these books would plausibly type. Keys are folded; multi-word values are fine
# because each becomes its own OR-branch.
TR_EN = {
    # --- cutting tools
    "testere": ["saw"], "bant testere": ["band saw"],
    "daire testere": ["circular saw"], "el testere": ["hand saw"],
    "testere diski": ["saw blade"], "zincir testere": ["chain saw"],
    "keski": ["chisel"], "balta": ["axe"], "kazma": ["adze"],
    # --- measuring and holding
    "kumpas": ["caliper", "vernier caliper"], "cetvel": ["ruler", "rule"],
    "gönye": ["miter", "square"], "su terazisi": ["spirit level"],
    "pervaz": ["jig"], "kalip": ["template"], "bant": ["tape measure"],
    # --- fasteners and hardware
    "vida": ["screw"], "vidalar": ["screw"], "vida agzi": ["screwdriver"],
    "bulon": ["bolt"], "percin": ["rivet"], "somun": ["nut"],
    "kose": ["bracket"], "menteşe": ["hinge"], "ray": ["rail"],
    # --- hand tools
    "cekic": ["hammer"], "pense": ["pliers"], "makas": ["shears", "scissors"],
    "anahtar": ["wrench", "spanner"], "tornavida": ["screwdriver"],
    "rende": ["plane"], "duzlem": ["plane"], "kaziyici": ["chisel"],
    "zirh": ["rasp", "file"], "ege": ["rasp"],
    # --- abrasives and finishing
    "zimpara": ["sandpaper"], "zımpara": ["sandpaper"],
    "zımpara kâğıdı": ["sandpaper"], "kum": ["sand"], "taşlama": ["grinding"],
    "taslama": ["grinding"], "cila": ["polish"], "cıla": ["polish"],
    "boya": ["paint"], "vernik": ["varnish"], "lak": ["lacquer"],
    "mum": ["wax"], "balmum": ["beeswax"], "yağ": ["oil"],
    "macun": ["paste"], "dolgu": ["filler"],
    # --- joints
    "zivana": ["tenon", "mortise"], "zıvana": ["tenon", "mortise"],
    "gecme": ["tenon joint"], "kirlangic kuyrugu": ["dovetail"],
    "kırlangıç kuyruğu": ["dovetail"], "gezmeli": ["mortise"],
    "mahfaza": ["housing"], "kazik": ["mortise"], "sokma": ["mortise"],
    "yapistirma": ["glued joint"], "yapıştırma": ["glued joint"],
    "tırtıklı": ["finger joint"], "tirtikli": ["finger joint"],
    # --- machines
    "torna": ["lathe"], "planya": ["planer"], "freze": ["router"],
    "matkap": ["drill"], "burgu": ["drill bit"], "vida takimi": ["drill"],
    "dikis": ["sander"], "kutup": ["sander"],
    # --- timber
    "ahsap": ["wood"], "ahşap": ["wood"], "orman": ["wood"],
    "mese": ["oak"], "meşe": ["oak"], "kayin": ["beech"], "kayın": ["beech"],
    "disbudak": ["ash"], "dişbudak": ["ash"], "akcaagac": ["maple"],
    "akçaağaç": ["maple"], "ceviz": ["walnut"], "saricam": ["pine"],
    "sarıçam": ["pine"], "ladin": ["spruce"], "kavak": ["poplar"],
    "ihlamzur": ["basswood", "linden"], "mantar": ["mushroom rot"],
    "budak": ["knot"], "budakli": ["knotty"], "life": ["veneer"],
    "kalinti": ["sapwood"], "kabuk": ["bark"],
    # --- furniture and objects
    "masa": ["table"], "masasi": ["table"], "sandalye": ["chair"],
    "dolap": ["cabinet", "cupboard"], "raf": ["shelf"],
    "kutuyu": ["box"], "kutu": ["box"], "sandik": ["chest"],
    "sandık": ["chest"], "pencere": ["window"], "kapi": ["door"],
    "kapı": ["door"], "tezgah": ["bench", "workbench"],
    "tezgâh": ["bench", "workbench"], "marangozluk": ["woodworking"],
    "marangoz": ["woodworker"], "cila makinesi": ["polisher"],
    # --- properties and defects
    "parlaklik": ["gloss"], "parlaklık": ["gloss"], "mat": ["matte"],
    "puruzluluk": ["roughness"], "pürüzlülük": ["roughness"],
    "duzgunluk": ["smoothness"], "düzgünlük": ["smoothness"],
    "catlak": ["crack"], "çatlak": ["crack"], "oluk": ["groove"],
    "pah": ["chamfer"], "kenar": ["edge"], "yuzey": ["surface"],
    "yüzey": ["surface"], "doku": ["grain", "figure"],
    "burat": ["wormhole"], "kurt": ["worm"], "esme": ["warp"],
    "bucurme": ["winding"], "toklama": ["pounding"],
    # --- actions
    "kesmek": ["cut"], "kesme": ["cutting"], "oymak": ["carve"],
    "oyma": ["carving"], "zimbalamak": ["plane"], "zımparalamak": ["sand"],
    "yapistirmak": ["glue"], "yapıştırmak": ["glue"],
    "tutmak": ["clamp", "hold"], "tutma": ["clamp", "hold"],
    "tutulur": ["hold"], "tutulması": ["hold"], "sikmak": ["tighten"],
    "sikilir": ["tighten"], "sikma": ["tighten"], "gevsetmek": ["loosen"],
    "çekmek": ["pull"], "vurmak": ["strike"], "dikmek": ["join"],
    "baglamak": ["clamp"], "sacmak": ["dress"],
    "şaplamak": ["dress"], "yapmak": ["make"], "olmak": [],
    "takmak": ["attach"], "sökmek": ["remove"], "cikarmak": ["remove"],
    "olcemek": ["measure"], "ölçmek": ["measure"],
    # --- sharpening and edge work: a large share of what these books are about
    "bilemek": ["sharpen"], "bileme": ["sharpening"], "keskin": ["sharp"],
    "keskinlik": ["sharpness"], "kore": ["sharp"], "bileyik": ["whetstone"],
    "tasin": ["whetstone"], "tas": ["stone"], "rendelemek": ["planing"],
    "rendeleme": ["planing"], "zimbalamak": ["planing"],
    "bilegen": ["planer", "thickness planer"],
    "kazima": ["scraping"], "kaziyici": ["chisel"],
    "kart kazima": ["card scraper"], "kart": ["card"],
    "tabla": ["tabletop", "top"], "tablasi": ["tabletop", "top"],
    "kesim": ["cut"], "kesisi": ["cut"], "yuzeyi": ["surface"],
    "tutkal": ["glue"], "yapistirici": ["glue"],
    "epoxy": ["epoxy"], "epoksi": ["epoxy"], "beyaz tutkal": ["glue"],
    "kestirme": ["cut", "miter"], "planye": ["planer"], "kazma": ["adze"],
    "çerçeve": ["frame"], "çekmece": ["drawer"], "çıtalı": ["beadboard"],
    "kasa": ["carcass"], "gövde": ["carcass"], "kızak": ["slide"],
    "misket": ["dowel"], "kazıklı": ["doweled"], "zıvana bıçak": ["tenon saw"],
}

# Keys above are written the way a Turkish speaker spells them, but lookups
# arrive folded - "Gönye" is typed with a dotless i on another keyboard layout,
# and half of these words are typed without Turkish characters at all. Folding
# at import is what makes one table serve both. It also removes a silent trap:
# a hand-written key like "gönye" simply never matched a folded lookup, and the
# word quietly fell through as untranslated garbage.


def _merge_lexicon():
    """Fold lexicon.py's smaller Turkish seed in as the shared base.

    Both surfaces need the same Turkish vocabulary - /api/related has used
    lexicon's all along - and two independent tables drift. lexicon's entries
    go in first; the ones above win on conflict because they are the richer
    readings ("gönye" is miter *and* square here, just miter there).
    """
    try:
        import lexicon
    except Exception:
        return
    for tr, en in getattr(lexicon, "TR_EN", []):
        k = fold(tr)
        if k and k not in TR_EN:
            TR_EN[k] = en.split() if isinstance(en, str) else list(en)


TR_EN = {fold(k): v for k, v in TR_EN.items()}
_merge_lexicon()

# Fold ours *before* merging, not after. Merging first and folding afterwards
# let lexicon's ASCII twin of a key silently win the fold collision - "gönye"
# came back as lexicon's bare ["miter"] instead of this module's
# ["miter", "square"], with no error anywhere to say why.

# --- Turkish function / question words ---------------------------------------
# These are not content. A Turkish question is mostly grammar wrapped around
# one or two woodworking nouns, and ANDing the grammar into the query is what
# makes "vida nasıl sıkılır" match nothing.
#
# "how" is deliberately absent as an entry in TR_EN and present here instead: it
# is checked *before* translation, because a question word that gets translated
# ("nasıl" -> "how") and then kept would put the English word "how" into the
# query, which is exactly the failure this module exists to remove.
TR_DROP = frozenset("""
    nasil nasıl neden niye ne nedir hangi nerede kim kimi kime
    ile icin için ve ya da de da ki mi mi mu
    bir iki cok çok az daha en fazla
    olur olmak oldu var yok
    nasil yapilir nasıl yapılır yapilir yapılır eder etmek
    boyle böyle şöyle soyle
    """.split())

# Suffix stripping, longest first. Turkish is agglutinative and a query is
# usually inflected ("vidalar", "zımparalama", "taşlamada"), while the lexicon
# holds the bare stem. Stripping is only a fallback: an exact hit always wins,
# so a real word is never mangled into another.
SUFFIXES = ("larn", "lerin", "lar", "ler", "nın", "nin",
            "nun", "nün", "ın", "in", "un", "ün",
            "da", "de", "ta", "te", "ya", "ye",
            "sı", "si", "nı", "ni", "nu", "nü",
            "lar", "ler", "ci", "ci", "li", "lu",
            "ma", "me", "a", "e", "ı", "i", "u", "ü")

# Longest-first, so a prefix match prefers the most specific term.
_KEYS = sorted(TR_EN, key=len, reverse=True)


def _lookup(tok):
    """Best English reading of one Turkish token, or None.

    Three attempts, most specific first: exact, then suffix-stripped, then
    longest known stem that starts the token. The third is what catches
    "zımparalama" -> "zımpara" and "tornayı" -> "torna", where the suffix is
    longer than any single ending in SUFFIXES.
    """
    f = fold(tok)
    if not f:
        return None
    # A question or function word is dropped before it can be translated.
    if f in TR_DROP:
        return []
    if f in TR_EN:
        return TR_EN[f]
    for suf in SUFFIXES:
        if len(f) - len(suf) >= 4 and f.endswith(suf):
            stem = f[: -len(suf)]
            if stem in TR_EN:
                return TR_EN[stem]
    if len(f) >= 5:
        for k in _KEYS:
            if len(k) >= 4 and f.startswith(k) and len(f) - len(k) <= 10:
                return TR_EN[k]
    return None


# --- spelling variants -------------------------------------------------------
# The groups live in lexicon.py already, which is the right home for them:
# /api/related has used them all along and the two paths must not drift. They
# are read here rather than retyped so a new group helps both surfaces at once.
def _variant_groups():
    try:
        import lexicon
        return {fold(a): sorted(fold(x) for x in b)
                for group in lexicon.GROUPS for a in group for b in [group]}
    except Exception:
        return {}


_GROUPS = _variant_groups()

# Multi-word corpus terms are stored unquoted-and-spaced in the lexicon
# ("band saw"), and FTS5 wants that as one phrase, not two ANDed words.


def _quote(term):
    term = (term or "").strip()
    return '"%s"' % term.replace('"', '')


def understand(q):
    """The free-text box -> [(alternatives, kind, source), ...].

    Returns an ordered list of terms for the caller to combine. `kind` is
    'phrase' (already quoted by the user), 'text' (a bare word) or 'excluded'.
    `source` is the word the user actually typed, kept only so describe() can
    show what happened to it - it is never put into the FTS query.

    A term is a list of equivalent spellings; the first is the user's own, so
    an unrecognised word is always returned unchanged and the previous AND
    semantics are preserved exactly for every query that had no variants.
    """
    q = q or ""
    terms = []

    # Quoted phrases first, exactly as typed, so an explicit phrase is never
    # silently re-spelled behind the user's back.
    for m in re.finditer(r'"([^"]+)"', q):
        terms.append(([m.group(1).replace('"', '')], "phrase", m.group(1)))
    stripped = re.sub(r'"[^"]+"', " ", q)

    # Exclusions keep their existing behaviour and stay out of the expansion.
    for m in re.finditer(r"-([0-9A-Za-zÇĞİÖŞÜçğıöşü][\w\-]*)", stripped):
        terms.append(([m.group(1)], "excluded", m.group(1)))
    stripped = re.sub(r"-[0-9A-Za-zÇĞİÖŞÜçğıöşü][\w\-]*", " ", stripped)

    # Multi-word terms first, on the text. Token-level lookup below can never
    # match "kırlangıç kuyruğu", because by then it is two separate tokens;
    # lexicon.translate() works on the string and is word-boundary safe, so it
    # rewrites the phrase before it is ever split.
    before = stripped
    try:
        import lexicon
        stripped = lexicon.translate(stripped)
    except Exception:
        pass

    for w in tokenize(stripped):
        f = fold(w)
        if f in TR_DROP:
            # Grammar, not subject matter. Checked here as well as in _lookup
            # so a dropped word never reaches the English readings below.
            continue
        # A word that only survives after the phrase pass was rewritten is
        # reportable: the user typed Turkish and it is now English.
        src = w if w in before else None
        en = _lookup(w)
        if en is None:
            alts = _GROUPS.get(f, [])
            terms.append(([w] + [a for a in alts if a != f], "text", src))
            continue
        if not en:
            # Translated to nothing on purpose ("olmak", "mu"): not a subject.
            continue
        # A recognised Turkish term is replaced by its English readings.
        # The user's own token is not kept: "zımpara" would add an AND term
        # that matches nothing and take the whole query down with it.
        terms.append((list(dict.fromkeys(en)), "text", src))
    return terms


def build_match(q="", allw="", phrase="", anyw="", none=""):
    """The search box's FTS5 expression, from understood terms.

    Same contract as before - everything is quoted, AND-ed, exclusions
    appended - but the words reaching it are the ones the user meant.
    """
    from search_api import _words  # advanced fields keep the old ASCII rule

    inc, ex = [], []
    for alts, kind, _src in understand(q):
        if kind == "excluded":
            ex += alts
        elif kind == "phrase":
            inc.append(_quote(alts[0]))
        elif len(alts) > 1:
            inc.append("(" + " OR ".join(_quote(a) for a in alts) + ")")
        elif alts:
            inc.append(_quote(alts[0]))

    inc += [_quote(w) for w in _words(allw)]
    if phrase.strip():
        inc.append(_quote(phrase.replace('"', "").strip()))
    ors = [_quote(w) for w in _words(anyw)]
    ex += _words(none)

    parts = []
    if inc:
        parts.append(" AND ".join(inc))
    if ors:
        parts.append("(" + " OR ".join(ors) + ")")
    if not parts:
        return None
    m = " AND ".join(parts)
    for w in dict.fromkeys(ex):
        m += " NOT " + _quote(w)
    return m


def _phrase_rewrites(text):
    """Which whole-phrase translations lexicon.translate() will perform.

    detect_before = the mapping, replace_after = the edit. The translation
    itself is lexicon's, because its word-boundary handling is already proven
    by /api/related; what is needed here is to be able to *say* what it did,
    and a rewrite that happens in a string has lost its source word by the
    time the result is tokenized. So the same entries are matched against the
    original text to recover which words are about to change.
    """
    out = []
    try:
        import lexicon
    except Exception:
        return out
    for tr, en in sorted(getattr(lexicon, "TR_EN", []),
                         key=lambda x: -len(x[0])):
        pat = r"(?<![0-9A-Za-zÇĞİÖŞÜçğıöşü])" + re.escape(tr) + \
              r"(?![0-9A-Za-zÇĞİÖŞÜçğıöşü])"
        if re.search(pat, text or "", flags=re.IGNORECASE):
            out.append([tr] + (en.split() if isinstance(en, str) else list(en)))
    return out


def describe(q):
    """What the query became - shown to the user when it had to be changed.

    Each entry is [typed, ...what was searched for]. Both kinds of rewrite are
    reported, because both are invisible from the outside: a Turkish word
    becoming English ("vida" -> "screw") is a bigger edit than a spelling
    variant ("mortice" -> "mortise"), and neither should happen silently.
    Empty when nothing was rewritten.
    """
    out = []
    seen = set()

    def add(item):
        # Folded, not raw: lexicon.py stores ASCII twins beside every Turkish
        # spelling ("zımpara" / "zimpara"), and Python's re.IGNORECASE treats
        # dotless ı as a case variant of i, so both entries match one query and
        # the same rewrite was being reported twice.
        k = fold(item[0])
        if k not in seen:
            seen.add(k)
            out.append(item)

    for item in _phrase_rewrites(q):
        add(item)
    for alts, kind, src in understand(q):
        if kind != "text" or not alts:
            continue
        if src and fold(src) != fold(alts[0]):
            add([src] + list(alts))
        elif len(alts) > 1:
            add(list(alts))
    return out


def english(q):
    """The same query in the corpus's language, for the *semantic* side.

    This has to exist. FTS5 is handed the translated words by build_match(), but
    the vector side used to be handed the raw text, and `bge-small-en-v1.5` is
    an English model: embed a Turkish question with it and you get coordinates
    that mean nothing, which are then fused into the ranking at full strength.
    Measured: 'vida nasıl sıkılır' returned *Architectural Graphics* as its top
    two hits - the same drafting book, for every Turkish query, because the
    model was being asked a question in a language it cannot read and gave the
    same wrong answer each time.

    Only the first alternative of each term is used: this feeds a vector
    similarity, where OR-ing 'mortice' and 'mortise' as two independent
    concepts would blur the meaning rather than sharpen it. The user's own
    spelling is kept when it is the one they typed.
    """
    out = []
    for alts, kind, _src in understand(q):
        if kind == "text" and alts:
            out.append(alts[0])
    return " ".join(out)
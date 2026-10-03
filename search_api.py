#!/usr/bin/env python3
"""Studio search API: query logic, topic graphs, related topics & full-text search.
Fully synchronized with index.db (pages, articles, pyear, fwwmap).
"""
import os, re, json, sqlite3, itertools, math, time, threading
import store, lexicon

def _words(s):
    return re.findall(r"[0-9A-Za-z][0-9A-Za-z'\-]*", s or "")

def build_match(q="", allw="", phrase="", anyw="", none=""):
    inc, ex = [], []
    if q:
        for m in re.finditer(r'"([^"]+)"', q):
            inc.append('"' + m.group(1).replace('"', '') + '"')
        rest = re.sub(r'"[^"]+"', " ", q)
        for w in re.findall(r"-([0-9A-Za-z][\w\-]*)", rest):
            ex.append(w)
        rest = re.sub(r"-[0-9A-Za-z][\w\-]*", " ", rest)
        inc += ['"' + w + '"' for w in _words(rest)]
    inc += ['"' + w + '"' for w in _words(allw)]
    if phrase.strip():
        inc.append('"' + phrase.replace('"', "").strip() + '"')
    ors = ['"' + w + '"' for w in _words(anyw)]
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
        m += ' NOT "' + w + '"'
    return m

def fts_query(raw, any_mode=False):
    """User text -> safe FTS5 MATCH: quoted phrases kept, bare words AND/OR-ed."""
    parts = ['"' + m.group(1).replace('"', '') + '"'
             for m in re.finditer(r'"([^"]+)"', raw or "")]
    stripped = re.sub(r'"[^"]+"', ' ', raw or "")
    parts += ['"' + w + '"' for w in re.findall(r"[0-9A-Za-z][0-9A-Za-z'\-]*", stripped)]
    return (" OR " if any_mode else " ").join(parts) if parts else None

def fts_query_exp(raw, any_mode=False):
    """fts_query() + lexicon expansion.

    Same safety rules (everything is quoted), but every bare token may become an
    OR-group of its spelling variants (`rebate` -> `("rebate" OR "rabbet")`) and
    Turkish terms are translated first.  Used by /api/related and /api/subgraph
    only — the main search box keeps the plain fts_query().
    """
    phrases = []
    for m in re.finditer(r'"([^"]+)"', raw or ""):
        phrases.append(m.group(1).replace('"', ''))
    stripped = re.sub(r'"[^"]+"', ' ', raw or "")
    stripped = lexicon.translate(stripped)
    parts = []
    for p in phrases:
        alts = lexicon.phrase_aliases(lexicon.translate(p))
        parts.append('(' + ' OR '.join(['"' + p + '"'] + ['"' + a + '"' for a in alts]) + ')'
                     if alts else '"' + p + '"')
    for w in lexicon.words(stripped):
        alts = lexicon.aliases(w)
        parts.append('(' + ' OR '.join(['"' + w + '"'] + ['"' + a + '"' for a in alts]) + ')'
                     if alts else '"' + w + '"')
    # FTS5 needs an explicit operator between a bare term and a parenthesised
    # group ("a" (b OR c)) is a syntax error — use AND instead of implicit AND.
    return (" OR " if any_mode else " AND ").join(parts) if parts else None

ORDERBY = {
    "relevance": "bm25(pages)",
    "title": "pages.title, CAST(pages.page AS INT)",
    "newest": "py.year IS NULL, py.year DESC, pages.title",
    "oldest": "py.year IS NULL, py.year ASC, pages.title",
}

PER_BOOK = 2      # max pages from one book in a relevance result page
DIVERSIFY_CAP = 2000   # never pull more than this many candidate rows

# ---- hybrid search (keyword + vector, fused with RRF) ----------------------
# FTS5 is exact: "mortise tenon" finds pages containing both words. It is also
# blind: it cannot find the page that says "the loose wedge keeps the joint
# from sliding" when you searched for "mortise". Vectors fix that and bring
# their own failure: they happily return pages that are merely topically
# similar. Reciprocal Rank Fusion is the standard answer because it needs no
# score calibration - only each list's ORDER - so a bm25 rank and a cosine
# rank can be combined without pretending they are the same unit.
RRF_K = 60             # the constant from Cormack et al. 2009; higher flattens
KW_CANDIDATES = 800    # how deep the keyword side is read
VEC_CANDIDATES = 400   # how deep the vector side is read
HYBRID_POOL = 1200     # fused candidates kept for pagination
HYBRID_MIN_COVER = 20  # % of pages that must have a vector for hybrid to run

_VEC = {"stamp": None, "ok": False}


def _vec_stamp():
    """A cheap fingerprint of the vector store, for caching readiness."""
    import vectors
    try:
        return (vectors._count(), vectors._vecfile_size(),
                (vectors._meta().get("dim") or 0))
    except Exception:
        return None


def hybrid_ready():
    """Is the semantic layer built well enough to blend in?

    Cached on a fingerprint of the vector store rather than on "asked once":
    the vectors can be built from another process or deleted from this one, and
    a one-shot cache would keep answering with the answer it saw at startup.
    """
    stamp = _vec_stamp()
    if stamp is not None and stamp == _VEC["stamp"]:
        return _VEC["ok"]
    _VEC["stamp"] = stamp
    try:
        import vectors
        s = vectors.status()
        _VEC["ok"] = bool(s["dim"] and s["embedded"] >= 100
                          and s["pct"] >= HYBRID_MIN_COVER)
    except Exception:
        _VEC["ok"] = False
    return _VEC["ok"]


def hybrid_reset():
    """Forget the readiness answer - after a build, delete or re-index."""
    _VEC.update(stamp=None, ok=False)


def _fetch_rows(c, rowids):
    """The display columns for a set of rowids, in the order given.

    One IN-list query instead of a query per row, and the order is restored in
    Python because SQL does not promise to return rows in list order.
    """
    if not rowids:
        return []
    q = ",".join("?" * len(rowids))
    got = c.execute(
        "SELECT pages.rowid,pages.title,pages.collection,pages.page,pages.path,"
        "py.year,fm.pdf_path FROM pages "
        "LEFT JOIN pyear py ON py.rowid=pages.rowid "
        "LEFT JOIN fwwmap fm ON fm.rowid=pages.rowid "
        f"WHERE pages.rowid IN ({q})", rowids).fetchall()
    by_id = {r[0]: r for r in got}
    return [by_id[i] for i in rowids if i in by_id]


def _hybrid_ranked(c, q, coll="", ymin=None, ymax=None):
    """[(rowid, score)] - the fused ranking of the keyword and vector lists.

    Returns None when the semantic layer is not usable, so the caller falls
    back to the plain FTS ranking instead of returning a worse result.
    """
    m = fts_query(q)
    if not m or not hybrid_ready():
        return None
    import vectors
    kw = []
    if m:
        where, p = "WHERE pages MATCH ?", [m]
        if coll:
            where += " AND pages.collection=?"; p.append(coll)
        if ymin:
            where += " AND py.year>=?"; p.append(int(ymin))
        if ymax:
            where += " AND py.year<=?"; p.append(int(ymax))
        kw = [r[0] for r in c.execute(
            "SELECT pages.rowid FROM pages "
            "LEFT JOIN pyear py ON py.rowid=pages.rowid "
            f"{where} ORDER BY bm25(pages) LIMIT ?", p + [KW_CANDIDATES])]
    vec = [rid for rid, _ in vectors.search(q, k=VEC_CANDIDATES)]
    if not vec:
        # The vector store can be built and still be unusable at query time:
        # `fastembed` is an optional dependency, so an interpreter without it
        # cannot embed the query even though pages.f32 is right there. Fusing
        # one list is just keyword search wearing a "semantic" badge, so say so
        # instead - the caller falls back and the UI drops the badge.
        return None
    if not kw:
        return [(rid, 1.0 / (RRF_K + i)) for i, rid in enumerate(vec, 1)][
            :HYBRID_POOL]

    # A vector hit can be any page in the library, so the collection/year
    # filters - which SQL applied to the keyword side - have to be applied here
    # too. Filtering after the fact keeps one candidate budget for both sides
    # instead of two, each tuned for its own hit rate.
    if vec and (coll or ymin or ymax):
        ok = _pass_filters(c, vec, coll, ymin, ymax)
        vec = [r for r in vec if r in ok]

    fused = {}
    for i, rid in enumerate(kw, 1):
        fused[rid] = fused.get(rid, 0.0) + 1.0 / (RRF_K + i)
    for i, rid in enumerate(vec, 1):
        fused[rid] = fused.get(rid, 0.0) + 1.0 / (RRF_K + i)
    ranked = sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))
    return ranked[:HYBRID_POOL]


def _pass_filters(c, rowids, coll, ymin, ymax):
    """Which of `rowids` survive the collection / year filters."""
    if not rowids or not (coll or ymin or ymax):
        return set(rowids)
    ok = set()
    ids = list(rowids)
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        q = ",".join("?" * len(part))
        where, p = [""], list(part)
        if coll:
            where.append("pages.collection=?"); p.append(coll)
        if ymin:
            where.append("py.year>=?"); p.append(int(ymin))
        if ymax:
            where.append("py.year<=?"); p.append(int(ymax))
        ok.update(r[0] for r in c.execute(
            "SELECT pages.rowid FROM pages "
            "LEFT JOIN pyear py ON py.rowid=pages.rowid "
            f"WHERE pages.rowid IN ({q})" + " AND " + " AND ".join(where), p))
    return ok


def _with_snippets(c, rows):
    """Attach the highlighted snippet to already-selected rows.

    `snippet()` has to run inside a query over the FTS table, so the snippets
    are fetched in one batched second pass instead of being computed for every
    candidate the ranker looked at.
    """
    if not rows:
        return rows
    ids = [r[0] for r in rows]
    q = ",".join("?" * len(ids))
    sn = dict(c.execute(
        f"SELECT rowid,snippet(pages,0,'<mark>','</mark>',' … ',18) "
        f"FROM pages WHERE rowid IN ({q})", ids).fetchall())
    return [r + (sn.get(r[0], ""),) for r in rows]


def _diversify(rows, per_book=PER_BOOK):
    """Keep at most `per_book` pages per book, preserving the ranked order.

    Flat relevance puts 22 of 60 hits on a single book, which reads as "this
    result list is broken". Ranking is untouched; only how many consecutive
    hits one book may contribute changes.
    """
    seen, out = {}, []
    for r in rows:
        key = (r[1], r[4])          # (title, path) — the same book, same file
        n = seen.get(key, 0)
        if n >= per_book:
            continue
        seen[key] = n + 1
        out.append(r)
    return out


def api_search(q="", allw="", phrase="", anyw="", none="", coll="",
               ymin=None, ymax=None, sort="relevance", offset=0, limit=60,
               hybrid=True):
    m = build_match(q, allw, phrase, anyw, none)
    if not m:
        return {"hits": [], "total": 0, "facets": [], "match": None}
    c = store.ro()
    where, p = "WHERE pages MATCH ?", [m]
    if coll:
        where += " AND pages.collection=?"; p.append(coll)
    if ymin:
        where += " AND py.year>=?"; p.append(int(ymin))
    if ymax:
        where += " AND py.year<=?"; p.append(int(ymax))
    base = ("FROM pages LEFT JOIN pyear py ON py.rowid=pages.rowid "
            "LEFT JOIN fwwmap fm ON fm.rowid=pages.rowid " + where)
    order = ORDERBY.get(sort, ORDERBY["relevance"])
    sel = ("SELECT pages.rowid,pages.title,pages.collection,pages.page,pages.path,"
           "py.year,fm.pdf_path ")
    # Hybrid only replaces the relevance ranking: sorting by year or title is an
    # explicit request for that order, and blending vectors into it would be a
    # lie about what the list is.
    ranked = (_hybrid_ranked(c, q, coll, ymin, ymax)
              if hybrid and sort == "relevance" and not anyw else None)
    try:
        if ranked is not None and offset < len(ranked):
            want = offset + limit
            fetch = min(max(want * 5, 150), HYBRID_POOL)
            rows = _diversify(_fetch_rows(c, [rid for rid, _ in ranked[:fetch]]))
            while len(rows) < want and fetch < len(ranked):
                fetch = min(fetch * 2, len(ranked))
                rows = _diversify(_fetch_rows(
                    c, [rid for rid, _ in ranked[:fetch]]))
            total = c.execute(f"SELECT COUNT(*) {base}", p).fetchone()[0]
            rows = _with_snippets(c, rows[offset:offset + limit])
            mode = "hybrid"
        elif sort == "relevance":
            # Past the fused pool, or with no semantic layer: plain bm25. The
            # keyword list is the complete one, so paging through it must not
            # stop at HYBRID_POOL while thousands more keyword hits exist.
            # Diversifying changes what a page of results *is*, so offset has to
            # count diversified hits, not raw rows. Two phases: rank a wider
            # window on cheap columns, then build snippets only for the rows we
            # actually keep. snippet() is the expensive part and running it on
            # every candidate cost ~70ms per query.
            want = offset + limit
            fetch = min(max(want * 5, 150), DIVERSIFY_CAP)
            while True:
                rows = _diversify(c.execute(
                    sel + f"{base} ORDER BY {order} LIMIT ?", p + [fetch]).fetchall())
                if len(rows) >= want or fetch >= DIVERSIFY_CAP:
                    break
                nxt = min(fetch * 2, DIVERSIFY_CAP)
                if nxt == fetch:
                    break
                fetch = nxt
            total = c.execute(f"SELECT COUNT(*) {base}", p).fetchone()[0]
            rows = rows[offset:offset + limit]
            rows = _with_snippets(c, rows)
            mode = "hybrid-tail" if ranked is not None else "keyword"
        else:
            rows = c.execute(
                sel + f"{base} ORDER BY {order} LIMIT ? OFFSET ?",
                p + [limit, offset]).fetchall()
            total = c.execute(f"SELECT COUNT(*) {base}", p).fetchone()[0]
            rows = _with_snippets(c, rows)
            mode = sort
    except sqlite3.OperationalError as e:
        c.close()
        return {"hits": [], "total": 0, "error": str(e), "match": m}
    facets = ([{"collection": cc, "count": n} for cc, n in c.execute(
        f"SELECT pages.collection,COUNT(*) {base} GROUP BY pages.collection ORDER BY 2 DESC", p)]
        if offset == 0 else [])
    hits = [{"id": rid, "title": t, "collection": cl, "page": pg,
             "pdf": bool(fmp or (path and path.lower().endswith(".pdf") and os.path.exists(store.resolve_path(path)))),
             "year": yy, "snippet": re.sub(r"\s+", " ", sn).strip()}
            for rid, t, cl, pg, path, yy, fmp, sn in rows]
    c.close()
    return {"hits": hits, "total": total, "facets": facets,
            "offset": offset, "limit": limit, "match": m, "sort": sort,
            "mode": mode, "semantic": hybrid_ready()}

# ---- topic co-occurrence stats (built once) -------------------------------
TOPIC = None

def _norm_tag(t):
    return re.sub(r"\s+", " ", (t or "")).strip()

def _canon(t):
    """Case/punctuation-insensitive form used to spot duplicate tags."""
    return re.sub(r"[^a-z0-9]+", "", t.lower())

def _ed1(a, b):
    """True when two strings differ by exactly one edit (sub/ins/del)."""
    if a == b:
        return False
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la > lb:
        a, b, la, lb = b, a, lb, la
    i = j = diff = 0
    while i < la and j < lb:
        if a[i] == b[j]:
            i += 1; j += 1
        else:
            diff += 1
            if diff > 1:
                return False
            if la == lb:
                i += 1
            j += 1
    diff += (la - i) + (lb - j)
    return diff == 1

def _tag_remap(names, freq):
    """Map (a) duplicate spellings of one tag and (b) typo variants that are at
    least 5x less frequent than the canonical tag onto that canonical tag."""
    remap = {}
    groups = {}
    for t in names:
        groups.setdefault(_canon(t), []).append(t)
    for members in groups.values():
        main = max(members, key=lambda n: (freq[n], -len(n)))
        for n in members:
            remap[n] = main
    mains = sorted({remap[t] for t in names}, key=lambda n: (-freq[n], n))
    by_len = {}
    for m in mains:
        by_len.setdefault(len(m), []).append(m)
    for m in mains:
        for L in (len(m) - 1, len(m), len(m) + 1):
            for other in by_len.get(L, ()):
                if other == m or remap.get(other, other) != other:
                    continue
                if freq[m] >= 5 * max(1, freq[other]) and _ed1(m.lower(), other.lower()):
                    remap[other] = m
    return remap

def topic_stats():
    global TOPIC
    if TOPIC is None:
        c = store.ro()
        total = c.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        docs = []
        for (tax,) in c.execute("SELECT taxonomy FROM articles"):
            tags = {_norm_tag(t) for t in re.split(r"[;,]", tax or "") if _norm_tag(t)}
            if tags:
                docs.append(sorted(tags))
        c.close()
        raw = {}
        for tags in docs:
            for t in tags:
                raw[t] = raw.get(t, 0) + 1
        remap = _tag_remap(list(raw), raw)
        def root(t):
            for _ in range(5):
                nxt = remap.get(t, t)
                if nxt == t:
                    break
                t = nxt
            return t
        freq, co = {}, {}
        for tags in docs:
            tags = sorted({root(t) for t in tags})   # dedupe AFTER the merge
            for t in tags:
                freq[t] = freq.get(t, 0) + 1
            for a, b in itertools.combinations(tags, 2):
                co[(a, b)] = co.get((a, b), 0) + 1
        TOPIC = {"freq": freq, "co": co, "total": total,
                 "merged": {k: v for k, v in remap.items() if k != v}}
    return TOPIC

def cobetween(a, b, co):
    return co.get((a, b) if a < b else (b, a), 0)

# ---- Adım 1: NPMI ranking + minimum support + redundancy penalty ----------
MIN_SUPPORT = 4          # articles (6.5k docs)
MIN_SUPPORT_LIB = 5      # library fallback (100k+ docs)
REDUNDANCY = 0.62        # multiplier per stem already picked
FREE_PER_STEM = 3        # ...but the first 3 of a family come at full score
TOP_N = 16
EDGE_SCALE = 8           # NPMI is 0..1, graph wants a visible 1..8 weight

_GENERIC = frozenset("""
joint joints hand hands tool tools wood woods work works make makes made using
use used design style type types part parts new best good great simple easy basic
""".split())

def npmi_score(co_count, global_count, nq, total):
    """Normalized Pointwise Mutual Information between the query-hit set and a
    tag:  PMI / -ln p(x,y), bounded to [-1, 1].

    Plain lift scores a tag seen once in 400 hits as if it were a perfect
    association (its denominator is tiny).  NPMI's denominator -ln p(x,y) grows
    exactly as the joint probability shrinks, so the rare-tag bubble collapses
    without any hand-tuned damping constant.  select_topics() still applies a
    minimum-support floor on top, because NPMI alone was measured to promote
    one-off tags and even misspelt ones.
    """
    if co_count <= 0 or global_count <= 0 or nq <= 0 or co_count >= total:
        return 0.0
    pmi = math.log((co_count * total) / (nq * global_count))
    return pmi / -math.log(co_count / total)

_STEM_SUF = ("iness", "ments", "ment", "ings", "ing", "ied", "ed", "ly")
_STEMCACHE = {}

def _stem(w):
    """Light English stemmer: enough to make board/boards, tool/tools,
    box/boxes, spokeshave/spokeshaves and sharpening/sharpen collapse together.
    Used both for the redundancy penalty and for de-duplicating fallback terms.
    """
    if len(w) >= 4 and w.endswith("ies"):
        w = w[:-3] + "y"
    elif len(w) >= 6 and w.endswith(("ches", "shes", "xes", "zes")):
        w = w[:-2]
    elif len(w) >= 5 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        w = w[:-1]
    for suf in _STEM_SUF:
        if len(w) - len(suf) >= 4 and w.endswith(suf):
            return w[:-len(suf)]
    return w

def _stems(name):
    s = _STEMCACHE.get(name)
    if s is None:
        s = set()
        for w in re.findall(r"[a-z]+", name.lower()):
            if len(w) >= 4:
                st = _stem(w)
                if len(st) >= 4 and st not in _GENERIC:
                    s.add(st)
        _STEMCACHE[name] = s
    return s

def select_topics(scored, n=TOP_N, min_support=MIN_SUPPORT, used=None):
    """scored: [(name, count, npmi)] -> top-n.

    Greedy MMR-style pick: a candidate's score is multiplied by REDUNDANCY for
    every stem it shares with an already-chosen topic, so one query cannot come
    back with 10 variants of the same head word (measured: 'sharpen' returned
    10x 'Sharpening *' and hid Water Stones / Honing Guides).  The first
    FREE_PER_STEM members of a family are exempt, because for 'chair' the
    Windsor/Rocking/Dining subtypes ARE what the reader wants to see.
    """
    pool = [(t, c, s) for t, c, s in scored if c >= min_support and s > 0]
    pool.sort(key=lambda x: -x[2])
    used = {} if used is None else used     # shared across stats + semantic picks
    out = []
    while pool and len(out) < n:
        bi, best = 0, -1e9
        for i, (t, c, s) in enumerate(pool):
            v = s
            for k in _stems(t):
                v *= REDUNDANCY ** max(0, used.get(k, 0) - FREE_PER_STEM + 1)
            if v > best:
                best, bi = v, i
        t, c, s = pool.pop(bi)
        out.append((t, c, best))
        for k in _stems(t):
            used[k] = used.get(k, 0) + 1
    return out

def build_topic_graph(top_nodes=150, min_edge=3):
    st = topic_stats(); freq, co = st["freq"], st["co"]
    keep = {t for t, _ in sorted(freq.items(), key=lambda x: -x[1])[:top_nodes]}
    nodes = [{"id": t, "label": t, "count": freq[t]} for t in keep]
    edges = [{"from": a, "to": b, "w": w} for (a, b), w in co.items()
             if w >= min_edge and a in keep and b in keep]
    return {"nodes": nodes, "edges": edges}

def api_subgraph(q, n=18):
    """Focused ego-network for a searched keyword: the term + its related topics, ranked by specificity."""
    m = fts_query_exp(q)
    if not m:
        return {"nodes": [], "edges": [], "center": q}
    st = topic_stats(); freq, co, total = st["freq"], st["co"], st["total"]
    c = store.ro()
    rows = c.execute("SELECT taxonomy FROM articles WHERE articles MATCH ?", (m,)).fetchall()
    c.close()
    nq = len(rows)
    scored = [(t, cnt, npmi_score(cnt, freq.get(t, cnt), nq, total))
              for t, cnt in _tags_of(rows, q, exact_only=True).items()]
    budget = {}
    rel = select_topics(scored, n, MIN_SUPPORT, budget) if nq else []
    n_stats = len(rel)
    if len(rel) < n:
        # same merged pool as api_related: library terms and concept tags
        # compete on one scale so the graph never contradicts the list
        lib = library_related(q, m, budget=budget)
        sem, _ = semantic_topics(q, m, have={t for t, _, _ in rel},
                                 k=n - len(rel), budget=budget)
        fill, _, _ = _merge_pool(rel, lib["topics"], sem, n)
        rel = rel + fill
    if not rel:
        return {"nodes": [], "edges": [], "center": q}
    if not rel:
        return {"nodes": [], "edges": [], "center": q}

    center = q
    nodes = [{"id": center, "label": q, "count": max([cnt for _, cnt, _ in rel] + [1]), "center": True}]
    edges = [{"from": center, "to": name, "w": max(1, int(round(score * EDGE_SCALE)))}
             for name, cnt, score in rel]
    names = [name for name, _, _ in rel]
    for name, cnt, score in rel:
        nodes.append({"id": name, "label": name, "count": cnt, "score": round(score, 3)})
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            w = cobetween(names[i], names[j], co)
            if w >= 3:
                edges.append({"from": names[i], "to": names[j], "w": w})
    return {"nodes": nodes, "edges": edges, "center": center}

GRAPH = None
def graph():
    global GRAPH
    if GRAPH is None:
        GRAPH = build_topic_graph()
    return GRAPH

# ---- library fallback: mine the 100k+ pages when no article matches --------
_LIB_STOP = frozenset("""
a an the and or but if then than that this these those of in on at by for with
from to into over under between during before after above below up down out off
again further once here there all any both each few more most other some such no
nor not only own same so too very can will just should now is are was were be
been being have has had having do does did doing i you he she it we they me him
her them my your his its our their what which who whom whose when where why how
also may might must shall could would one two three first second new make made
using use used get got like well way many much even still page pages chapter
chapters figure figures isbn copyright publisher press edition vol volume fig
index appendix contents published
wood woods piece pieces thing things time times year years number part parts
side sides end ends top bottom next last usually often various several early
later held called known shown example however therefore rather almost already
great work making finished forms form dimensions space content products
industry wooden air note
found least less produce quality available look looks best order low high stock
throughout introduction shop materials material construction weight moving
need needs help helps keep gives taken come comes go goes see seen say says
able sure case value sense general generally possible probably perhaps thus
hence since while whether although because until upon within without among
across behind toward towards per via instead enough quite own
description heavy together small old process area rest present following report
summary result results position condition degree amount section chapter title
create around along another beyond upon itself thus either neither
""".split())

# exact page count is a 350 ms full-scan on FTS5 — reuse it for a minute
_TOTAL = [0.0, 0.0]
def _pages_total(c):
    now = time.time()
    if _TOTAL[0] and now - _TOTAL[1] < 60:
        return int(_TOTAL[0])
    n = c.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
    _TOTAL[:] = [n, now]
    return n

# global term document frequencies are stable — memoize them across requests
_DF = {}
def _df_many(c, terms):
    need = [t for t in terms if t not in _DF]
    if need:
        sel = ["SELECT COUNT(*) FROM pages WHERE pages MATCH ?"] * len(need)
        vals = [r[0] for r in c.execute(" UNION ALL ".join(sel),
                                        ['"' + t + '"' for t in need])]
        for t, v in zip(need, vals):
            if len(_DF) < 40000:
                _DF[t] = v
    return [_DF.get(t) for t in terms]

def _candidates(texts, q, min_df=4, cap=30):
    """Unigrams + bigrams present in >= min_df of the sampled pages.

    Three filters: prose stopwords, collapse of inflectional twins
    (board/boards, form/forms) and removal of the searched term's own family so
    `pallet` does not come back as its own top related topic.
    """
    qtok = {w.lower() for w in lexicon.words(lexicon.translate(q or ""))}
    df = {}
    for tx in texts:
        toks = []
        for w in re.findall(r"[a-z][a-z'\-]{2,}", (tx or "").lower()):
            if w.strip("'-") in _LIB_STOP:
                continue
            toks.append(w)
        seen = set()
        for i, w in enumerate(toks):
            seen.add(w)
            if i + 1 < len(toks):
                seen.add(w + " " + toks[i + 1])
        for s in seen:
            df[s] = df.get(s, 0) + 1
    out = []
    for s, n in df.items():
        if n < min_df:
            continue
        ws = s.split()
        if all(w in qtok for w in ws):
            continue                      # the searched term is not a topic
        if len(ws) == 1 and any(len(t) >= 4 and
                                s[:min(5, len(t))] == t[:min(5, len(t))]
                                for t in qtok):
            continue                      # pallet->pallets, airdry->airdried
        out.append((n, len(s), s))
    out.sort(key=lambda x: (-x[0], -x[1]))
    # collapse inflectional twins: keep the most frequent spelling per stem
    best = {}
    for n, L, s in out:
        k = s if " " in s else _stem(s)
        if k not in best:
            best[k] = (n, L, s)
    out = sorted(best.values(), key=lambda x: (-x[0], -x[1]))
    return [s for _, _, s in out[:cap]]

def _page_counts(c, cands, m):
    """Global document frequency AND co-occurrence with the query, for every
    candidate, in two combined statements."""
    if not cands:
        return [], []
    df = _df_many(c, cands)
    sel = ["SELECT COUNT(*) FROM pages WHERE pages MATCH ?"] * len(cands)
    co = [r[0] for r in c.execute(
        " UNION ALL ".join(sel), ['"' + t + '" AND (' + m + ')' for t in cands])]
    return df, co

def library_related(q, m, books=None, sample=40, budget=None):
    """No tagged FWW article matches -> mine the whole library instead.

    Candidates come from the top-bm25 hits, then every candidate gets an exact
    NPMI over all pages, so the ranking rule is identical to the articles path.
    """
    c = store.ro()
    try:
        total = _pages_total(c)
        nq = c.execute("SELECT COUNT(*) FROM pages WHERE pages MATCH ?", (m,)).fetchone()[0]
        texts = []
        if nq:
            texts = [r[0] for r in c.execute(
                "SELECT text FROM pages WHERE pages MATCH ? "
                "ORDER BY bm25(pages) LIMIT ?", (m, sample)).fetchall()]
        cands = _candidates(texts, q)
        df, co = _page_counts(c, cands, m)
    finally:
        c.close()
    scored = [(t, cn, npmi_score(cn, d, nq, total)) for t, cn, d in zip(cands, co, df)]
    topics = select_topics(scored, TOP_N, MIN_SUPPORT_LIB, budget)
    return {"topics": [{"name": t, "count": cnt, "score": round(s, 3)}
                        for t, cnt, s in topics],
            "books": books or [], "source": "library" if topics else "none",
            "scope": total, "matched": nq}

# ---- Step 2: semantic concept layer (embeddings) --------------------------
# concepts.npz holds one L2-normalised 384-d vector per canonical tag, built by
# build_concepts.py from the tag name + its articles' headlines.  This is the
# layer that reaches the vocabulary the FTS index cannot: synonymy, inflection
# and a query in another language.
#
# It lives next to index.db, NOT next to this file. The tags are derived from
# one library's `articles` table, so a copy shipped inside the app folder would
# be a second library's vocabulary being offered as its own - which is exactly
# what the second-library test exposed.
HERE = os.path.dirname(os.path.abspath(__file__))
LEGACY_DIR = HERE          # pre-multi-library location, read-only fallback
_CONC = {"tags": [], "mat": None, "model": None, "meta": None,
         "err": None, "lock": threading.Lock()}


def concept_path(name="concepts.npz"):
    """Where the concept layer for *this* library lives."""
    import vectors
    return vectors.concept_path(name)


def _concept_store():
    if _CONC["mat"] is None and _CONC["err"] is None:
        with _CONC["lock"]:
            if _CONC["mat"] is None and _CONC["err"] is None:
                import numpy as np
                npz = concept_path("concepts.npz")
                if not os.path.exists(npz):
                    # An install that predates per-library concept layers.
                    # Read it once so nothing is lost, but never write back
                    # there: the next build_concepts.py run writes to the
                    # library folder and leaves the old copy behind.
                    npz = os.path.join(LEGACY_DIR, "concepts.npz")
                if not os.path.exists(npz):
                    # "Not built yet" is a normal state, not a failure. Leaving
                    # `err` unset means the next call retries, so a server left
                    # running picks the layer up as soon as build_concepts.py
                    # writes it - a sticky FileNotFoundError would require a
                    # restart for no reason.
                    return _CONC
                try:
                    z = np.load(npz, allow_pickle=True)
                    _CONC["tags"] = [str(t) for t in z["tags"]]
                    _CONC["mat"] = np.asarray(z["vectors"], dtype="float32")
                    try:
                        with open(os.path.splitext(npz)[0] + ".json",
                                  encoding="utf-8") as f:
                            _CONC["meta"] = json.load(f)
                    except Exception:
                        _CONC["meta"] = {}
                except Exception as e:
                    _CONC["err"] = e
    return _CONC

def embed_providers():
    """ONNX execution providers to request from fastembed.

    fastembed tries CUDA by default. Without the CUDA runtime on the DLL search
    path, onnxruntime prints three lines that look like a crash but are
    harmless — it falls back to the CPU. ocr_worker.enable_cuda() puts the
    pip-installed runtime on PATH and, if a real session comes up, we use the
    GPU; otherwise we ask for the CPU explicitly and start silently.
    """
    env = os.environ.get("WOOD_EMBED_PROVIDERS", "").strip()
    if env:
        return [p.strip() for p in env.split(",") if p.strip()]
    try:
        import ocr_worker
        if ocr_worker.cuda_available():
            return ["CUDAExecutionProvider", "CPUExecutionProvider"]
    except Exception:
        pass
    return ["CPUExecutionProvider"]

def _concept_model():
    """Lazily create the ONNX embedding model (kept for the process lifetime)."""
    st = _concept_store()
    if st["err"] is not None or not st["tags"]:
        return None
    if st["model"] is None:
        with st["lock"]:
            if st["model"] is None:
                try:
                    from fastembed import TextEmbedding
                    name = (st["meta"] or {}).get(
                        "model", "sentence-transformers/all-MiniLM-L6-v2")
                    st["model"] = TextEmbedding(model_name=name,
                                                providers=embed_providers())
                except Exception as e:
                    st["err"] = e
    return st["model"]

def warm_concepts():
    """Pre-load vectors + model at server start so the first query is not slow."""
    _concept_store()
    _concept_model()

def concept_neighbors(q, k=20, min_cos=0.25):
    """[(tag, cosine)] — the tags whose profile is semantically nearest to q."""
    st = _concept_store()
    if not q or st["mat"] is None or st["err"] is not None:
        return []
    model = _concept_model()
    if model is None:
        return []
    try:
        import numpy as np
        v = np.asarray(next(iter(model.embed([q]))), dtype="float32")
        n = float(np.linalg.norm(v))
        if n == 0:
            return []
        cos = st["mat"] @ (v / n)
    except Exception as e:
        st["err"] = e
        return []
    qwords = {w.lower() for w in lexicon.words(lexicon.translate(q))}
    out = []
    for i in np.argsort(-cos):
        c = float(cos[i])
        if c < min_cos or len(out) >= k:
            break
        tag = st["tags"][int(i)]
        tl = tag.lower()
        if tl in qwords or tl in (q or "").lower():
            continue                      # the searched term is not a topic
        out.append((tag, round(c, 3)))
    return out

def semantic_topics(q, m, have=(), k=10, budget=None):
    """Concept neighbours, validated statistically: every candidate is scored
    with the SAME NPMI used everywhere else, measured over all 100k+ pages.
    So a semantic neighbour has to actually co-occur with the query in the
    library to make the list — embeddings propose, statistics decide.
    """
    cands = [t for t, _ in concept_neighbors(q, k=12, min_cos=0.25)
             if t not in set(have)][:k * 2]
    if not cands or not m:
        return [], 0
    c = store.ro()
    try:
        total = _pages_total(c)
        nq = c.execute("SELECT COUNT(*) FROM pages WHERE pages MATCH ?",
                       (m,)).fetchone()[0]
        df, co = _page_counts(c, cands, m)
    finally:
        c.close()
    if not nq:
        return [], 0
    scored = [(t, cn, npmi_score(cn, d, nq, total))
              for t, cn, d in zip(cands, co, df)]
    return select_topics(scored, k, MIN_SUPPORT_LIB, budget), nq

def _merge_pool(base, lib_topics, sem, n):
    """Fill the remaining slots from library terms + concept tags on one scale.

    Case-folded keys, because library terms come from lower-case page text and
    concept tags from Title Case taxonomy — `japanese` and `Japanese` are the
    same topic and must not appear twice.  Ties go to the later (tag) spelling.
    """
    have = {t[0].casefold() for t in base}
    lib_cf = {t["name"].casefold() for t in lib_topics}
    sem_cf = {t[0].casefold() for t in sem}
    pool = {}
    for name, cnt, s in ([(t["name"], t["count"], t["score"])
                          for t in lib_topics] + sem):
        k = name.casefold()
        if k in have or (k in pool and pool[k][2] > s):
            continue
        pool[k] = (name, cnt, s)
    fill = sorted(pool.values(), key=lambda x: -x[2])[:max(0, n - len(base))]
    return (fill,
            sum(1 for x in fill if x[0].casefold() in lib_cf),
            sum(1 for x in fill if x[0].casefold() in sem_cf))

def _tags_of(rows, q, exact_only=False):
    """Count taxonomy tags across matched articles.

    exact_only=False  -> drop any tag contained in the query string (api_related:
                         'dovetail' must not be reported as its own topic)
    exact_only=True   -> drop only an exact match (api_subgraph, where the query
                         is already the graph's center node)
    """
    tags = {}
    q = (q or "").lower()
    for (tax,) in rows:
        for t in re.split(r"[;,]", tax or ""):
            t = _norm_tag(t)
            if not t:
                continue
            tl = t.lower()
            if (tl == q) if exact_only else (tl in q):
                continue
            tags[t] = tags.get(t, 0) + 1
    return tags

def api_related(q, any_mode=False):
    m = fts_query_exp(q, any_mode)
    if not m:
        return {"topics": [], "books": [], "source": "none", "scope": 0, "matched": 0}
    st = topic_stats(); freq, total = st["freq"], st["total"]
    c = store.ro()
    rows = c.execute("SELECT taxonomy FROM articles WHERE articles MATCH ?", (m,)).fetchall()
    books = [{"title": t, "collection": coll, "hits": n} for t, coll, n in c.execute(
        "SELECT title,collection,COUNT(*) n FROM pages WHERE pages MATCH ? "
        "GROUP BY title ORDER BY n DESC LIMIT 12", (m,))]
    c.close()

    nq = len(rows)
    tags = _tags_of(rows, q)
    scored = [(t, cnt, npmi_score(cnt, freq.get(t, cnt), nq, total))
              for t, cnt in tags.items()]
    budget = {}
    stats_topics = select_topics(scored, TOP_N, MIN_SUPPORT, budget)
    topics = list(stats_topics)
    n_stats = len(topics)
    lib = None
    lib_used = sem_n = 0
    if len(topics) < TOP_N:
        # Step 2: statistics -> then ONE pool in which attested library terms
        # and concept-layer tags compete on the same scale (both are NPMI
        # measured over all 100k+ pages).  Embedding proposes, statistics decide.
        lib = library_related(q, m, books, budget=budget)
        sem, _ = semantic_topics(q, m, have={t for t, _, _ in topics},
                                 k=TOP_N - len(topics), budget=budget)
        fill, lib_used, sem_n = _merge_pool(topics, lib["topics"], sem, TOP_N)
        topics = topics + fill
    if n_stats:
        source, scope, matched = "articles", total, nq
    elif lib and lib_used:
        source, scope, matched = "library", lib["scope"], lib["matched"]
    elif sem_n:
        source, scope, matched = ("concepts", len(_concept_store()["tags"]),
                                  sem_n)
    else:
        source, scope, matched = "none", 0, 0
    return {"topics": [{"name": t, "count": cnt, "score": round(s, 3)}
                        for t, cnt, s in topics],
            "books": books, "source": source, "scope": scope, "matched": matched,
            "semantic": sem_n}

def api_text(rid):
    c = store.ro()
    r = c.execute("SELECT title,collection,page,path,text FROM pages WHERE rowid=?", (rid,)).fetchone()
    fm = c.execute("SELECT pdf_path FROM fwwmap WHERE rowid=?", (rid,)).fetchone() if r else None
    c.close()
    if not r:
        return {"error": "not found"}
    pdf_p = store.resolve_path(r[3]) if r[3] else (store.resolve_path(fm[0]) if fm else None)
    has_pdf = bool(pdf_p and pdf_p.lower().endswith(".pdf") and os.path.exists(pdf_p))
    return {"title": r[0], "collection": r[1], "page": r[2],
            "pdf": has_pdf, "text": r[4]}

def api_terms(rowid, query_words):
    c = store.ro()
    r = c.execute("SELECT title,collection,page,path FROM pages WHERE rowid=?",
                  (rowid,)).fetchone()
    fm = c.execute("SELECT pdf_path FROM fwwmap WHERE rowid=?", (rowid,)).fetchone() if r else None
    c.close()
    if not r:
        return {"error": "not found"}
    pdf_p = store.resolve_path(r[3]) if r[3] else (store.resolve_path(fm[0]) if fm else None)
    has_pdf = bool(pdf_p and pdf_p.lower().endswith(".pdf") and os.path.exists(pdf_p))
    return {"id": rowid, "title": r[0], "collection": r[1], "page": r[2],
            "pdf": has_pdf,
            "terms": [w for w in query_words if w][:12]}

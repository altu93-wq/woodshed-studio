#!/usr/bin/env python3
"""Step 2: build the semantic concept layer for Studio's related topics.

One vector per taxonomy tag (~549 after the step-1 merge), built from the tag
name plus the headlines/subheads of the articles carrying that tag.  At query
time the query is embedded with the same model and the nearest tags become
candidate topics — that is how `spalting`, `wood movement` or `end grain` reach
the curated FWW vocabulary even though those words never occur in a taxonomy
string (and how a Turkish query reaches it without a lexicon entry).

Output (never touches index.db, per the production rule):
    concepts.npz   float32 [n_tags, 384] L2-normalised matrix + tag list
    concepts.json  model name, dim, built_at, tag count

Run:  py -3 build_concepts.py            (model downloads once, ~90 MB)
"""
import json
import os
import re
import time

import numpy as np
import store

HERE = os.path.dirname(os.path.abspath(__file__))
# Written next to index.db, not into the app folder: the tags are derived from
# *this* library's `articles` table, so a copy shipped inside the app folder
# would hand one library another's vocabulary.
import vectors
OUT_NPZ = vectors.concept_path()
OUT_JSON = vectors.concept_path("concepts.json")
MODEL = "sentence-transformers/all-MiniLM-L6-v2"
MAX_TAGS_PER_DOC = 8          # snippets kept per tag
PROFILE_CHARS = 700           # cap of one profile document


def _split(tax):
    return [t.strip() for t in re.split(r"[;,]", tax or "") if t.strip()]


def collect_profiles():
    """tag -> profile document (tag name + its articles' headlines).

    Tag keys go through search_api's normalisation + typo merge so the concept
    layer speaks exactly the same 549 canonical tags as topic_stats().
    """
    import search_api
    remap = search_api.topic_stats()["merged"]

    def canon(t):
        t = search_api._norm_tag(t)
        for _ in range(5):
            nxt = remap.get(t)
            if not nxt or nxt == t:
                break
            t = nxt
        return t

    c = store.ro()
    rows = c.execute(
        "SELECT taxonomy, headline, subhead FROM articles").fetchall()
    c.close()
    heads = {}
    for tax, hl, sh in rows:
        for raw in _split(tax):
            t = canon(raw)
            if not t:
                continue
            bag = heads.setdefault(t, [])
            if len(bag) >= MAX_TAGS_PER_DOC:
                continue
            txt = (hl or sh or "").strip()
            if txt:
                bag.append(re.sub(r"\s+", " ", txt))
    profiles = []
    # merged spelling variants belong in the profile too, so a query typed as
    # "handplanes" still lands on the canonical "Hand Planes" vector
    variants = {}
    for variant, canonical in remap.items():
        if variant != canonical:
            variants.setdefault(canonical, []).append(variant)
    for tag, bag in heads.items():
        doc = tag
        if tag in variants:
            doc += " (also " + ", ".join(sorted(variants[tag])) + ")"
        if bag:
            doc += ". " + ". ".join(bag)
        profiles.append((tag, doc[:PROFILE_CHARS]))
    profiles.sort(key=lambda x: x[0])
    return profiles


def main():
    from fastembed import TextEmbedding

    profiles = collect_profiles()
    docs = [d for _, d in profiles]
    tags = [t for t, _ in profiles]
    print(f"{len(tags)} tags from articles")

    # No tagged articles means nothing to embed: a fresh library, or one scanned
    # before the taxonomy import.  Bailing out here (before the model is even
    # loaded, and before anything is written) keeps the existing concepts.npz
    # intact and reports the actual cause instead of numpy's
    # "need at least one array to concatenate".
    if not tags:
        print(f"no taxonomy tags in {store.DB} -> nothing to embed.")
        print("The concept layer needs an `articles` table with at least one")
        print("taxonomy tag; run the taxonomy import, or use the app's")
        print("related-topics panel which silently skips the layer when empty.")
        print("Existing concepts.npz left untouched.")
        return

    t0 = time.time()
    model = TextEmbedding(model_name=MODEL)
    print(f"model loaded in {time.time() - t0:.1f}s")

    t0 = time.time()
    mat = np.vstack([np.asarray(v, dtype=np.float32)
                     for v in model.embed(docs, batch_size=32)])
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    mat = mat / norms
    print(f"embedded {mat.shape} in {time.time() - t0:.1f}s")

    np.savez_compressed(OUT_NPZ, vectors=mat, tags=np.array(tags, dtype=object))
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump({"model": MODEL, "dim": int(mat.shape[1]),
                   "tags": len(tags), "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "docs": len(docs)}, f, ensure_ascii=False, indent=2)
    print(f"wrote {os.path.basename(OUT_NPZ)} "
          f"({os.path.getsize(OUT_NPZ) / 1024:.0f} kB) + concepts.json")


if __name__ == "__main__":
    main()

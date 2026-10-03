#!/usr/bin/env python3
"""Server-side display_name: invariants + agreement with prettyName().

`titles.display_name()` and `prettyName()` in studio.html are two
implementations of the same rule. If they ever disagree, the same book shows
two different names depending on which screen it appears on - worse than not
cleaning names at all. This checks that they agree on every title in the
library, and writes the expected results so nametest.js can compare
against them.

    py -3 nametest.py
"""
import json
import os
import re
import sys

import titles

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "_b.json")
EXPECT = os.path.join(HERE, "_expect.json")
API = "http://127.0.0.1:8766/api/books"

RESIDUE = re.compile(
    r"[\s_(\[]*(?:vk[_ .-]?com[_ .-]?en(?:glishmagazines)?|englishmagazines?|"
    r"pdfdrive|libgen|annas-?archive)\b|[-_]ne(?:[_ .-]?en)?\b|"
    r"\{[0-9a-f]{6,}\}", re.I)


def load():
    if not os.path.exists(CACHE):
        import urllib.request
        try:
            with urllib.request.urlopen(API, timeout=120) as r:
                data = r.read()
        except OSError as e:
            sys.exit("kitap listesi alinamadi (%s) ve %s yok" % (e, CACHE))
        with open(CACHE, "wb") as f:
            f.write(data)
        print("(veri canli API'den alindi)")
    with open(CACHE, encoding="utf-8") as f:
        j = json.load(f)
    return j.get("books") or j


def core(s):
    return "".join(re.findall(r"[^\W_]+", s, re.UNICODE)).lower()


def main():
    books = load()
    names = [(b.get("pub") or b.get("title") or "").strip() for b in books]
    out = [titles.display_name(n) for n in names]

    fails = 0
    checks = 0

    def ok(cond, label, detail=""):
        nonlocal fails, checks
        checks += 1
        if not cond:
            fails += 1
            print("  BASARISIZ  " + label)
            if detail:
                print("            " + detail.replace("\n", "\n            "))

    print("=== display_name: %d isim ===" % len(names))

    ok(all(o.strip() for o in out), "sonuc hicbir isimde bos degil",
       " | ".join(n for n, o in zip(names, out) if not o.strip())[:200])

    twice = [titles.display_name(o) for o in out]
    bad = [(a, b) for a, b in zip(out, twice) if a != b]
    ok(not bad, "display_name idempotent",
       "\n".join("%r -> %r" % x for x in bad[:3]))

    ok(not [s for s in out if "_" in s], "alt cizgi kalmadi",
       " | ".join(s for s in out if "_" in s)[:200])
    ok(not [s for s in out if re.search(r"vk[_ .-]?com|pdfdrive|englishmagazines"
                                        r"|libgen|annas-?archive", s, re.I)],
       "indirme sitesi kalinti kalmadi",
       " | ".join(s for s in out
                  if re.search(r"vk[_ .-]?com|pdfdrive", s, re.I))[:200])
    ok(not [s for s in out if re.search(r"[-_]ne(?:[_ .-]?en)?\s*$", s, re.I)],
       "-ne kesik kalmadi",
       " | ".join(s for s in out
                  if re.search(r"[-_]ne(?:[_ .-]?en)?\s*$", s, re.I))[:200])
    ok(not [s for s in out if re.search(r"\{[0-9a-f]{6,}\}", s, re.I)],
       "indirme karmasi kalmadi",
       " | ".join(s for s in out
                  if re.search(r"\{[0-9a-f]{6,}\}", s, re.I))[:200])

    core_bad = ["%r -> %r" % (n, o) for n, o in zip(names, out)
                if core(o) != core(RESIDUE.sub(" ", n))]
    ok(not core_bad, "harf/rakam cekirdegi korunuyor (%d isim)" % len(names),
       "\n".join(core_bad[:5]))

    grown = [o for o, n in zip(out, names) if len(o) > len(n)]
    ok(not grown, "temizlenmis isim daha uzun degil", " | ".join(grown[:3]))

    spacey = [s for s in out if re.search(r"\s{2,}|[\s,]\s*[).]", s)]
    ok(not spacey, "bosluk / punctuasyon hijyeni yok", " | ".join(spacey[:3]))

    imb = lambda s: (s.count("(") - s.count(")"))
    new_imb = ["%r -> %r" % (n, o) for n, o in zip(names, out)
               if abs(imb(o)) > abs(imb(n))]
    ok(not new_imb, "temizlik yeni parantez dengesizligi uretmiyor",
       "\n".join(new_imb[:3]))

    changed = sum(1 for n, o in zip(names, out) if n != o)
    print("\n=== istatistik ===")
    print("  degisen        : %d / %d (%d%%)"
          % (changed, len(names), round(100 * changed / len(names))))
    print("  bos sonuc      : %d" % sum(1 for o in out if not o.strip()))

    with open(EXPECT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print("  beklenen yazildi: %s" % os.path.basename(EXPECT))

    print("\n=== sonuc ===")
    print("%d kontrol, %d basarisiz" % (checks, fails))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
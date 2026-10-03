#!/usr/bin/env python3
"""Shared year logic for Studio lab (single copy of add_year.py rules).
1) 4-digit year in title/filename (leading year trusted, model numbers ignored).
2) publication year near copyright markers on first/last pages. Floor 1850.
"""
import re
from collections import Counter

YEAR = re.compile(r"\b(1[6-9]\d\d|20[0-2]\d)\b")
CR = re.compile(r"copyright|\(c\)|first published|first edition|"
                r"all rights reserved|printed in|library of congress|"
                r"isbn|\bpublished\b", re.I)

def title_year(t):
    if not t:
        return None
    m = re.match(r"\s*(1[6-9]\d\d|20[0-2]\d)\b", t)
    if m:
        return int(m.group(1))
    yrs = [int(y) for y in re.findall(r"(?<![\w-])(1[6-9]\d\d|20[0-2]\d)\b", t)
           if int(y) >= 1850]
    return max(yrs) if yrs else None

def text_year(text):
    near, allc = [], []
    for m in YEAR.finditer(text or ""):
        y = int(m.group(1))
        if y < 1850:
            continue
        allc.append(y)
        if CR.search(text[max(0, m.start() - 45): m.end() + 10]):
            near.append(y)
    if near:
        return max(near)
    return Counter(allc).most_common(1)[0][0] if allc else None

def book_year(title, head_text="", tail_text=""):
    return title_year(title) or text_year(head_text + " " + tail_text)

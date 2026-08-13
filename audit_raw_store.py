#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Inventory saved raw HTML by PROVENANCE only — never by content.

    python audit_raw_store.py
    python audit_raw_store.py --source cumhuriyet --top 40
    python audit_raw_store.py --csv audit.csv

Answers one question: what is actually IN the raw store, and would the CURRENT
scoping rules have collected it? A crashed or early run can contain authors and
sections that later curation excluded (sport, lifestyle, magazin, gastronomy),
and those are invisible in a file count.

Reads only the first 16 KB of each page — the <head> — so 80k files take
seconds, not an hour. Nothing is written unless --csv is given.

WHAT IT CANNOT TELL YOU
  Whether the collected subset is a DEFENSIBLE SAMPLE. A run that stopped when
  it crashed has no documentable sampling frame: you know what you have, not
  what you would have had. That judgement is yours; this only removes the
  guesswork about composition.
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from collections import Counter, defaultdict

from reextract import ADAPTERS, _TR

HEAD_BYTES = 16384

_OG_URL = re.compile(rb'<meta[^>]+property=["\']og:url["\'][^>]+content=["\']([^"\']+)', re.I)
_CANON = re.compile(rb'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)', re.I)
_OG_DESC = re.compile(rb'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)', re.I)
_ART_AUTH = re.compile(rb'<meta[^>]+property=["\']article:author["\'][^>]+content=["\']([^"\']+)', re.I)
_PUB = re.compile(rb'<meta[^>]+property=["\']article:published_time["\'][^>]+content=["\']([^"\']+)', re.I)
_YAZDI = re.compile(r"^(.{2,60}?)\s+yazdı", re.I | re.UNICODE)

# author segment in the URL path, where the site puts one there
_URL_AUTHOR = {
    "cumhuriyet": re.compile(r"/yazarlar/([^/]+)/"),
    "sabah":      re.compile(r"/yazarlar/([^/]+)/\d{4}/"),
    "odatv":      re.compile(r"/yazarlar/([^/]+)/"),
}


def head(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read(HEAD_BYTES)


def probe(path: str, source: str) -> dict:
    h = head(path)
    m = _OG_URL.search(h) or _CANON.search(h)
    url = m.group(1).decode("utf-8", "replace") if m else ""
    pub = _PUB.search(h)
    date = pub.group(1).decode("utf-8", "replace")[:10] if pub else ""

    author = ""
    rx = _URL_AUTHOR.get(source)
    if rx and url:
        am = rx.search(url)
        if am:
            author = am.group(1)
    if not author:                       # BilginPro: author only in the head
        am = _ART_AUTH.search(h)
        if am:
            author = am.group(1).decode("utf-8", "replace").strip()
        if (not author or author.lower() == "odatv"):
            dm = _OG_DESC.search(h)
            if dm:
                ym = _YAZDI.match(dm.group(1).decode("utf-8", "replace").strip())
                if ym:
                    author = ym.group(1).strip()
    return {"file": os.path.basename(path), "url": url,
            "author": author or "(unknown)", "date": date}


def allowlist(source: str) -> set | None:
    """Operator allow-list for this source, if one is shipped."""
    path = f"{source}_authors_allowlist.txt"
    if not os.path.exists(path):
        return None
    out = set()
    for line in open(path, encoding="utf-8"):
        line = line.split("#")[0].strip()
        if line:
            out.add(line)
    return out or None


def audit(source: str, top: int, rows_out: list) -> None:
    cls = ADAPTERS[source]
    jsonl, raw_dir = cls().paths()
    if not os.path.isdir(raw_dir):
        print(f"  no raw store at {raw_dir}")
        return
    files = [f for f in os.listdir(raw_dir) if f.endswith(".html")]
    if not files:
        print(f"  {raw_dir}: empty")
        return

    recs = []
    for i, fn in enumerate(sorted(files)):
        if i and i % 5000 == 0:
            print(f"    …{i}/{len(files)}", file=sys.stderr)
        r = probe(os.path.join(raw_dir, fn), source)
        r["source"] = source
        recs.append(r)
        rows_out.append(r)

    by_author = Counter(r["author"] for r in recs)
    dates = sorted(r["date"] for r in recs if r["date"])
    no_url = sum(1 for r in recs if not r["url"])
    allow = allowlist(source)

    print(f"  files                : {len(recs)}")
    print(f"  distinct authors     : {len(by_author)}")
    if dates:
        print(f"  date range           : {dates[0]} … {dates[-1]}")
    same_day = Counter(dates).most_common(1)
    if same_day and dates and same_day[0][1] > len(dates) * 0.5:
        print(f"  !! {same_day[0][1]} of {len(dates)} share ONE date ({same_day[0][0]}) "
              f"— the masthead-date bug signature")
    if no_url:
        print(f"  !! {no_url} page(s) with no canonical/og:url — unrecoverable")

    if allow:
        inn = sum(n for a, n in by_author.items() if a in allow)
        out = len(recs) - inn
        print(f"  vs {source}_authors_allowlist.txt: "
              f"{inn} in scope, {out} OUT OF SCOPE ({100*out/len(recs):.0f}%)")
        offenders = [(a, n) for a, n in by_author.most_common() if a not in allow]
        if offenders:
            print(f"     off-list authors: " +
                  ", ".join(f"{a}({n})" for a, n in offenders[:12]))

    print(f"  top authors          :")
    for a, n in by_author.most_common(top):
        mark = "" if allow is None else ("  ok" if a in allow else "  OFF-LIST")
        print(f"     {n:>7}  {a}{mark}")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=sorted(ADAPTERS))
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--csv", help="write the per-file inventory here")
    args = ap.parse_args()

    rows = []
    for src in ([args.source] if args.source else sorted(ADAPTERS)):
        print(f"\n=== {src} ===")
        audit(src, args.top, rows)

    if args.csv and rows:
        with open(args.csv, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["source", "file", "url", "author", "date"])
            w.writeheader()
            w.writerows(rows)
        print(f"\n  inventory -> {args.csv} ({len(rows)} rows)")


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Draw a stratified sample for the HUMAN gold validation set.

Strata = (source, section) × length-bin. Length bins are quantile cut on
word_count so each bin holds a comparable share of the corpus. Sampling is
deterministic (seeded) and allocates as evenly as possible across strata so the
gold set spans short↔long and every section, not just the median mass.

Output is a BLIND labeling sheet: article_id + metadata + full content and EMPTY
gold columns. It contains NO judge scores and NO codebook cues, so the human
labels the same construct the judge sees (genre / stance-presence / discourse)
without anchoring.

    python stratify_gold.py --in output/ria_analitika.jsonl --n 60 --bins 4
"""
from __future__ import annotations

import argparse
import csv
import json
import random
from collections import defaultdict


def _load(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(l) for l in fh if l.strip()]


def _quantile_bins(values: list[int], k: int) -> list[float]:
    s = sorted(values)
    if not s:
        return []
    return [s[min(len(s) - 1, int(round(q * (len(s) - 1))))]
            for q in [i / k for i in range(1, k)]]


def _bin_of(v: int, edges: list[float]) -> int:
    for i, e in enumerate(edges):
        if v <= e:
            return i
    return len(edges)


def stratified_sample(records: list[dict], n: int, bins: int = 4,
                      seed: int = 20260722) -> list[dict]:
    rng = random.Random(seed)
    wc = [int(r.get("word_count", len(r.get("body", "").split()))) for r in records]
    edges = _quantile_bins(wc, bins)

    strata: dict[tuple, list[dict]] = defaultdict(list)
    for r, w in zip(records, wc):
        key = (r.get("source", "?"), r.get("section", "?"), _bin_of(w, edges))
        strata[key].append(r)

    keys = sorted(strata.keys())
    for k in keys:
        rng.shuffle(strata[k])

    # Even allocation across strata, round-robin until we hit n or exhaust.
    chosen, i = [], 0
    while len(chosen) < min(n, len(records)):
        progressed = False
        for k in keys:
            if strata[k]:
                chosen.append((k, strata[k].pop()))
                progressed = True
                if len(chosen) >= min(n, len(records)):
                    break
        if not progressed:
            break

    out = []
    for (src, sec, b), r in chosen:
        out.append({
            "article_id": r["article_id"],
            "source": src, "section": sec,
            "word_count": int(r.get("word_count", 0)),
            "length_bin": b,
            "title": r.get("title", ""),
            "content": r.get("content", ""),
            "url": r.get("url", ""),
        })
    return out


GOLD_COLUMNS = [
    "article_id", "source", "section", "word_count", "length_bin", "url",
    "title", "content",
    # ---- human fills these, blind to the judge ----
    "gold_genre",          # analysis_opinion | reported_news | wire_or_listing | other
    "gold_stance",         # 0..3
    "gold_discourse",      # 0..3
    "gold_keep",           # 1 = in-scope for the corpus, 0 = out
    "notes",
]


def write_sheet(rows: list[dict], csv_path: str, jsonl_path: str) -> None:
    with open(csv_path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=GOLD_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({**{c: "" for c in GOLD_COLUMNS}, **r})
    with open(jsonl_path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps({**{c: "" for c in GOLD_COLUMNS}, **r},
                                ensure_ascii=False) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True, help="corpus JSONL")
    ap.add_argument("--n", type=int, default=60, help="gold sample size")
    ap.add_argument("--bins", type=int, default=4, help="length quantile bins")
    ap.add_argument("--seed", type=int, default=20260722)
    ap.add_argument("--out-csv", default="gold_sheet.csv")
    ap.add_argument("--out-jsonl", default="gold_sheet.jsonl")
    a = ap.parse_args()

    recs = _load(a.inp)
    rows = stratified_sample(recs, a.n, a.bins, a.seed)
    write_sheet(rows, a.out_csv, a.out_jsonl)

    from collections import Counter
    dist = Counter((r["section"], r["length_bin"]) for r in rows)
    print(f"Sampled {len(rows)}/{len(recs)} across {len(dist)} strata "
          f"(section × {a.bins} length bins):")
    for k in sorted(dist):
        print(f"  section={k[0]} length_bin={k[1]} : {dist[k]}")
    print(f"  -> {a.out_csv} , {a.out_jsonl}  (fill gold_* columns blind)")


if __name__ == "__main__":
    main()

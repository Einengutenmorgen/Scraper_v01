#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Inter-rater / rater-vs-gold agreement metrics, dependency-free.

- cohen_kappa            : nominal agreement (used for the genre facet).
- quadratic_weighted_kappa: ordinal agreement (used for stance / discourse).
- krippendorff_alpha_ordinal: ordinal reliability, handles >2 raters / missing.
- binary_prf            : precision/recall/F1 for the keep/drop decision.
"""
from __future__ import annotations

from collections import Counter
from itertools import combinations
from typing import Optional, Sequence


def cohen_kappa(a: Sequence, b: Sequence) -> float:
    assert len(a) == len(b) and a, "need equal, non-empty sequences"
    n = len(a)
    labels = set(a) | set(b)
    po = sum(1 for x, y in zip(a, b) if x == y) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum((ca[l] / n) * (cb[l] / n) for l in labels)
    return 1.0 if pe == 1.0 else (po - pe) / (1 - pe)


def quadratic_weighted_kappa(a: Sequence[int], b: Sequence[int],
                             min_r: Optional[int] = None,
                             max_r: Optional[int] = None) -> float:
    assert len(a) == len(b) and a
    lo = min_r if min_r is not None else min(min(a), min(b))
    hi = max_r if max_r is not None else max(max(a), max(b))
    r = list(range(lo, hi + 1))
    idx = {v: i for i, v in enumerate(r)}
    k = len(r)
    if k == 1:
        return 1.0
    O = [[0] * k for _ in range(k)]
    for x, y in zip(a, b):
        O[idx[x]][idx[y]] += 1
    n = len(a)
    row = [sum(O[i]) for i in range(k)]
    col = [sum(O[i][j] for i in range(k)) for j in range(k)]
    num = den = 0.0
    for i in range(k):
        for j in range(k):
            w = ((i - j) ** 2) / ((k - 1) ** 2)
            e = row[i] * col[j] / n
            num += w * O[i][j]
            den += w * e
    return 1.0 if den == 0 else 1 - num / den


def krippendorff_alpha_ordinal(matrix: Sequence[Sequence[Optional[int]]]) -> float:
    """matrix: rows = items, cols = raters, None = missing. Ordinal metric."""
    # Collect ratings per item (units with >=2 ratings contribute).
    units = [[v for v in row if v is not None] for row in matrix]
    units = [u for u in units if len(u) >= 2]
    if not units:
        return float("nan")
    all_vals = [v for u in units for v in u]
    lo, hi = min(all_vals), max(all_vals)
    vals = list(range(lo, hi + 1))
    # ordinal distance between two values a,b (squared interval metric).
    freq = Counter(all_vals)

    def delta(a, b):
        if a == b:
            return 0.0
        x, y = sorted((a, b))
        s = sum(freq[g] for g in vals if x <= g <= y)
        return (s - (freq[x] + freq[y]) / 2.0) ** 2

    # observed disagreement
    Do_num = 0.0
    Do_den = 0.0
    for u in units:
        m = len(u)
        pair_w = 1.0 / (m - 1)
        for a, b in combinations(u, 2):
            Do_num += 2 * pair_w * delta(a, b)   # unordered pairs *2
        Do_den += m
    Do = Do_num / Do_den if Do_den else 0.0
    # expected disagreement
    N = len(all_vals)
    De_num = 0.0
    seq = all_vals
    # sum over all ordered pairs of the whole value pool
    cnt = Counter(all_vals)
    De = 0.0
    for a in vals:
        for b in vals:
            De += cnt[a] * cnt[b] * delta(a, b)
    De = De / (N * (N - 1)) if N > 1 else 0.0
    return 1.0 if De == 0 else 1 - Do / De


def binary_prf(pred: Sequence[bool], gold: Sequence[bool]) -> dict:
    tp = sum(1 for p, g in zip(pred, gold) if p and g)
    fp = sum(1 for p, g in zip(pred, gold) if p and not g)
    fn = sum(1 for p, g in zip(pred, gold) if not p and g)
    tn = sum(1 for p, g in zip(pred, gold) if not p and not g)
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    acc = (tp + tn) / len(pred) if pred else 0.0
    return {"precision": round(prec, 3), "recall": round(rec, 3),
            "f1": round(f1, 3), "accuracy": round(acc, 3),
            "tp": tp, "fp": fp, "fn": fn, "tn": tn}

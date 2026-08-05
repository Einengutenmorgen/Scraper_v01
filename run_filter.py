#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Run the genre/stance filter panel over a corpus JSONL and write decisions.

    export OPENROUTER_API_KEY=sk-or-...
    python run_filter.py --in output/ria_analitika.jsonl \
        --out output/filter_decisions.jsonl --rule genre_stance_disc

Outputs one record per article: aggregated facets, decision (keep/borderline/
drop), contested flag + reasons, and every raw judge vote for audit. Nothing is
deleted from the corpus — the decision is a label you filter on later.
"""
from __future__ import annotations
from dotenv import load_dotenv

import argparse
import json
import os
from collections import Counter
from dataclasses import asdict

from judges import OpenRouterJudge, DEFAULT_JUDGE_MODELS, Cache, run_panel
from aggregate import aggregate_votes, DEFAULT_RULE

load_dotenv()

def _load(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(l) for l in fh if l.strip()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", default="filter_decisions.jsonl")
    ap.add_argument("--models", nargs="*", default=DEFAULT_JUDGE_MODELS,
                    help="OpenRouter model ids for the judge panel.")
    ap.add_argument("--rule", default=DEFAULT_RULE,
                    choices=["genre_only", "genre_discourse", "genre_stance_disc"])
    ap.add_argument("--cache-dir", default="output/judge_cache")
    a = ap.parse_args()

    records = _load(a.inp)
    judges = [OpenRouterJudge(m) for m in a.models]
    cache = Cache(a.cache_dir)
    print(f"[filter] {len(records)} articles × {len(judges)} judges "
          f"(rule={a.rule})")

    votes_by_id = run_panel(records, judges, cache=cache)

    decisions = []
    with open(a.out, "w", encoding="utf-8") as out:
        for r in records:
            agg = aggregate_votes(r["article_id"], votes_by_id[r["article_id"]],
                                  rule=a.rule)
            row = {**{k: r.get(k) for k in ("article_id", "url", "title",
                                            "word_count", "section", "source")},
                   **asdict(agg)}
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
            decisions.append(agg)

    dcount = Counter(d.decision for d in decisions)
    contested = sum(1 for d in decisions if d.contested)
    gdist = Counter(d.genre for d in decisions)
    print("\n=== FILTER SUMMARY ===")
    print(f"  decisions : {dict(dcount)}")
    print(f"  genres    : {dict(gdist)}")
    print(f"  contested (routed to human): {contested}")
    print(f"  -> {a.out}")


if __name__ == "__main__":
    main()

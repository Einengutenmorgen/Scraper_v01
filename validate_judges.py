#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Validate judges against the HUMAN gold set and select the final panel.

Inputs:
  --gold   filled gold_sheet.jsonl (human gold_genre / gold_stance /
           gold_discourse / gold_keep per article_id)
  --votes  filter_decisions.jsonl from run_filter.py (carries every raw judge
           vote under .votes), OR a judge_cache dir.

For each judge model it computes agreement WITH GOLD:
  - genre     : Cohen's kappa + accuracy   (nominal)
  - stance    : quadratic-weighted kappa   (ordinal, 0..3)
  - discourse : quadratic-weighted kappa   (ordinal, 0..3)
  - decision  : per-judge keep/drop vs gold_keep -> precision/recall/F1
It also reports the ENSEMBLE (aggregated) decision vs gold, and ranks judges by a
combined agreement score so you can pick the final 3 (favouring high gold
agreement AND lab diversity — the ranking is a starting point, not an oracle).

    python validate_judges.py --gold gold_sheet.jsonl --votes output/filter_decisions.jsonl --final 3
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict

from metrics import (cohen_kappa, quadratic_weighted_kappa,
                     krippendorff_alpha_ordinal, binary_prf)
from aggregate import JudgeVote, aggregate_votes


def _load_jsonl(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(l) for l in fh if l.strip()]


def _to_bool(v):
    return str(v).strip() in ("1", "true", "True", "yes", "keep")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", required=True)
    ap.add_argument("--votes", required=True, help="filter_decisions.jsonl")
    ap.add_argument("--rule", default="genre_stance_disc")
    ap.add_argument("--final", type=int, default=3)
    a = ap.parse_args()

    gold = {g["article_id"]: g for g in _load_jsonl(a.gold)
            if str(g.get("gold_genre", "")).strip()}
    decisions = {d["article_id"]: d for d in _load_jsonl(a.votes)}

    ids = [i for i in gold if i in decisions]
    if not ids:
        raise SystemExit("No overlap between gold ids and decisions ids.")

    # Gather per-judge aligned vectors over the gold items.
    per_judge = defaultdict(lambda: {"genre_p": [], "genre_g": [],
                                      "stance_p": [], "stance_g": [],
                                      "disc_p": [], "disc_g": [],
                                      "keep_p": [], "keep_g": []})
    ens_keep_p, ens_keep_g = [], []

    for i in ids:
        g = gold[i]
        gg, gs, gd = g["gold_genre"].strip(), int(g["gold_stance"]), int(g["gold_discourse"])
        gk = _to_bool(g["gold_keep"])
        votes = [JudgeVote(**v) for v in decisions[i].get("votes", [])]
        for v in votes:
            if not v.ok:
                continue
            pj = per_judge[v.model]
            pj["genre_p"].append(v.genre); pj["genre_g"].append(gg)
            pj["stance_p"].append(v.stance_presence); pj["stance_g"].append(gs)
            pj["disc_p"].append(v.discourse); pj["disc_g"].append(gd)
            # single-judge decision, same rule, treated as a 1-judge panel
            solo = aggregate_votes(i, [v], rule=a.rule)
            pj["keep_p"].append(solo.decision == "keep"); pj["keep_g"].append(gk)
        # ensemble decision vs gold
        ens_keep_p.append(decisions[i]["decision"] == "keep")
        ens_keep_g.append(gk)

    rows = []
    for model, pj in per_judge.items():
        genre_k = cohen_kappa(pj["genre_p"], pj["genre_g"])
        stance_k = quadratic_weighted_kappa(pj["stance_p"], pj["stance_g"], 0, 3)
        disc_k = quadratic_weighted_kappa(pj["disc_p"], pj["disc_g"], 0, 3)
        prf = binary_prf(pj["keep_p"], pj["keep_g"])
        combined = round((genre_k + stance_k + disc_k + prf["f1"]) / 4, 3)
        rows.append((model, genre_k, stance_k, disc_k, prf, combined, len(pj["genre_p"])))

    rows.sort(key=lambda r: r[5], reverse=True)

    print(f"\nJudge validation on {len(ids)} gold items "
          f"(rule={a.rule}). Sorted by combined gold-agreement.\n")
    print(f"{'model':<38} {'genreκ':>7} {'stnceκ':>7} {'discκ':>7} "
          f"{'keepF1':>7} {'comb':>6} {'n':>4}")
    for m, gk, sk, dk, prf, comb, n in rows:
        print(f"{m:<38} {gk:>7.2f} {sk:>7.2f} {dk:>7.2f} "
              f"{prf['f1']:>7.2f} {comb:>6.2f} {n:>4}")

    ens = binary_prf(ens_keep_p, ens_keep_g)
    print(f"\nENSEMBLE decision vs gold: P={ens['precision']} R={ens['recall']} "
          f"F1={ens['f1']} acc={ens['accuracy']}  "
          f"(tp={ens['tp']} fp={ens['fp']} fn={ens['fn']} tn={ens['tn']})")

    print(f"\nSuggested final {a.final} judges (top combined agreement — "
          f"still sanity-check lab diversity):")
    for m, *_ in rows[:a.final]:
        print(f"  - {m}")


if __name__ == "__main__":
    main()

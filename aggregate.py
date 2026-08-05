#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Aggregate per-judge facet scores into one decision by a STATED rule.

Two deliberate separations of concern:
  1. Judges score facets independently (see rubric.py). They never see the
     keep/drop rule, so the rule can be changed/audited without re-querying.
  2. The keep/drop rule is applied here to the AGGREGATED facets, and it is
     explicit and configurable — decomposed rubric in, stated rule out.

Cross-judge aggregation:
  - genre       : plurality vote; ties / weak plurality => contested.
  - stance,     : ordinal medians.
    discourse
  - contested   : True when judges disagree beyond tolerance on any facet;
                  such items are routed to a human rather than auto-decided.
"""
from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass, field, asdict
from typing import Optional

from rubric import GENRE_LABELS

# ---- Stated combination rules ------------------------------------------------
# Each rule is a named, explicit predicate over AGGREGATED facets. Pick the
# strictness that matches how conservative you want to be about circularity.
#
#   "genre_only"        : keep analysis_opinion regardless of stance/discourse.
#                         Most conservative w.r.t. the stance/L3 correlation worry,
#                         because it never conditions on stance presence at all.
#   "genre_discourse"   : keep analysis_opinion with developed discourse.
#   "genre_stance_disc" : keep analysis_opinion with stance AND discourse (default).
DEFAULT_RULE = "genre_stance_disc"

# Thresholds for the ordinal facets (used by the rules that reference them).
STANCE_MIN = 2
DISCOURSE_MIN = 2
# Borderline band (kept for human review, not auto-dropped).
STANCE_BORDER = 1
DISCOURSE_BORDER = 1

# Cross-judge disagreement tolerances -> contested (route to human).
GENRE_MIN_AGREEMENT = 0.6      # fraction of judges on the winning genre
ORDINAL_MAX_RANGE = 2          # max-min across judges on an ordinal facet


@dataclass
class JudgeVote:
    model: str
    genre: str
    stance_presence: int
    discourse: int
    rationale: str = ""
    ok: bool = True            # False if the judge call failed / unparseable
    error: str = ""


@dataclass
class Aggregated:
    article_id: str
    n_judges: int
    genre: str
    genre_agreement: float
    stance_presence: float     # median
    discourse: float           # median
    contested: bool
    contested_reasons: list = field(default_factory=list)
    decision: str = ""         # keep | borderline | drop
    rule: str = ""
    votes: list = field(default_factory=list)   # raw per-judge votes (audit)


def _mode_with_agreement(labels: list[str]) -> tuple[str, float, bool]:
    counts = Counter(labels)
    top, top_n = counts.most_common(1)[0]
    agreement = top_n / len(labels)
    # tie for the top count?
    tied = [g for g, n in counts.items() if n == top_n]
    weak = agreement < GENRE_MIN_AGREEMENT
    if len(tied) > 1:
        # Tie-break conservatively: never let a tie hand a "keep-eligible"
        # analysis_opinion label win outright; mark contested.
        non_analysis = [g for g in tied if g != "analysis_opinion"]
        chosen = sorted(non_analysis)[0] if non_analysis else sorted(tied)[0]
        return chosen, agreement, True
    return top, agreement, weak


def aggregate_votes(article_id: str, votes: list[JudgeVote],
                    rule: str = DEFAULT_RULE) -> Aggregated:
    good = [v for v in votes if v.ok and v.genre in GENRE_LABELS]
    if not good:
        return Aggregated(article_id=article_id, n_judges=len(votes),
                          genre="other", genre_agreement=0.0,
                          stance_presence=0, discourse=0, contested=True,
                          contested_reasons=["no_valid_judge_output"],
                          decision="drop", rule=rule,
                          votes=[asdict(v) for v in votes])

    genres = [v.genre for v in good]
    stances = [int(v.stance_presence) for v in good]
    discs = [int(v.discourse) for v in good]

    genre, agreement, genre_weak = _mode_with_agreement(genres)
    stance_med = statistics.median(stances)
    disc_med = statistics.median(discs)

    reasons = []
    if genre_weak:
        reasons.append(f"genre_agreement={agreement:.2f}<{GENRE_MIN_AGREEMENT}")
    if max(stances) - min(stances) > ORDINAL_MAX_RANGE:
        reasons.append(f"stance_range={max(stances)-min(stances)}")
    if max(discs) - min(discs) > ORDINAL_MAX_RANGE:
        reasons.append(f"discourse_range={max(discs)-min(discs)}")
    contested = bool(reasons)

    decision = _decide(genre, stance_med, disc_med, rule)
    # A contested item is never auto-kept; it becomes borderline for a human.
    if contested and decision == "keep":
        decision = "borderline"

    return Aggregated(
        article_id=article_id, n_judges=len(good), genre=genre,
        genre_agreement=round(agreement, 3), stance_presence=stance_med,
        discourse=disc_med, contested=contested, contested_reasons=reasons,
        decision=decision, rule=rule, votes=[asdict(v) for v in good],
    )


def _decide(genre: str, stance: float, disc: float, rule: str) -> str:
    is_analysis = genre == "analysis_opinion"
    if not is_analysis:
        return "drop"

    if rule == "genre_only":
        return "keep"

    if rule == "genre_discourse":
        if disc >= DISCOURSE_MIN:
            return "keep"
        if disc >= DISCOURSE_BORDER:
            return "borderline"
        return "drop"

    if rule == "genre_stance_disc":
        if stance >= STANCE_MIN and disc >= DISCOURSE_MIN:
            return "keep"
        if stance >= STANCE_BORDER and disc >= DISCOURSE_BORDER:
            return "borderline"
        return "drop"

    raise ValueError(f"Unknown combination rule: {rule!r}")

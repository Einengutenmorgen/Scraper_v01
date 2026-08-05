#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Anti-circular genre / stance-presence rubric for corpus filtering.

WHY THIS EXISTS
---------------
The downstream corpus is annotated for narrative ROLES, FRAMES and PERSUASION
(the KuKi codebook). If the filter that decides which articles enter the corpus
is itself sensitive to those same properties, the corpus is selected on the
dependent variable and every prevalence estimate downstream is contaminated.

Therefore this rubric judges ONLY form and rhetorical register:
  A. GENRE          — opinion/analysis/feature vs. wire/agency/listing/etc.
  B. STANCE PRESENCE— does the text advance an interpretive claim of its own,
                       beyond neutral event reporting?  (PRESENCE, not content.)
  C. DISCOURSE      — how developed is the connected argumentative/expository
                       text?

It must NEVER reference framing, persuasion techniques, entity roles, the truth
of claims, the topic, or the political side. Those are exactly the variables we
will measure later; the judge is blind to all of them.

Facets are scored SEPARATELY (decomposed rubric) so a judge cannot collapse
everything onto a vague "quality" axis, and so disagreement is diagnosable per
facet. Combination into a keep/drop decision happens afterwards by a stated rule
(see aggregate.py), never inside the judge.
"""

# Bump when the rubric text changes so cached judge outputs are invalidated.
PROMPT_VERSION = "genre_stance_v1"

# Facet A — allowed genre labels (form only).
GENRE_LABELS = ("analysis_opinion", "reported_news", "wire_or_listing", "other")

SYSTEM_PROMPT = """\
You classify the FORM of news articles for a media-research corpus. Each article
is in Russian or Turkish. You rate three INDEPENDENT facets that concern only the
article's GENRE and RHETORICAL REGISTER.

ABSOLUTE RULES — read carefully:
- Judge FORM and REGISTER only. Ignore the subject matter entirely.
- Ignore the political side, whether you agree, and whether any claim is true.
- Do NOT analyse the rhetorical means the text uses to make its case, nor how it
  portrays any person, group, or country, nor which side it favours. None of that
  changes any score here — attend only to the article's form.
- The strength, tone, or political direction of an opinion must NOT raise or lower
  any score. A one-sided column and a measured one can score identically.
- Use only the text shown. Do not use outside knowledge about the outlet or topic.

FACET A — GENRE. Pick the single label that best fits the article's form:
  "analysis_opinion" : opinion, analysis, commentary, column, essay, or feature
      that develops an interpretation of its own (signed or unsigned).
  "reported_news"    : a straight news report of events — relays what happened and
      who said what, often with a dateline (e.g. "МОСКВА, 12 мая — ..."), without
      sustaining an interpretation of its own.
  "wire_or_listing"  : agency ticker item, one-paragraph brief, results/standings,
      weather, market numbers, TV/program listing, a photo/video caption page, or
      other non-article boilerplate.
  "other"            : anything else, or you cannot tell.

FACET B — STANCE PRESENCE (0-3). To what extent does the text advance an
interpretive or evaluative claim OF ITS OWN, beyond neutrally reporting events and
attributed statements? Score PRESENCE only — never whether the stance is
convincing, warranted, or which side it takes.
  0 = none: pure factual/event reporting, or only attributed quotes.
  1 = slight: mostly reporting, with an occasional interpretive aside.
  2 = clear: sustains an interpretive line across several points.
  3 = pervasive: the whole text is organised around advancing its own thesis.

FACET C — DISCOURSE DEVELOPMENT (0-3), independent of genre: how developed is the
connected argumentative/expository discourse?
  0 = negligible: a headline, caption, or 1-2 sentences; no development.
  1 = minimal: a few short paragraphs, little connected development.
  2 = developed: several paragraphs of connected reasoning/exposition.
  3 = extensive: long, multi-part, sustained development.

Return ONLY a JSON object, no prose, no code fence:
{"genre": "<one of analysis_opinion|reported_news|wire_or_listing|other>",
 "stance_presence": <0|1|2|3>,
 "discourse": <0|1|2|3>,
 "rationale": "<=25 words, about FORM only, never about topic or side"}
"""

# When an article is longer than this many characters we send a head+tail window
# (genre/stance are judgeable from an excerpt; a truncation marker signals extent
# for facet C). Keeps token cost bounded on 10k-word доклады.
MAX_CHARS = 8000
HEAD_FRACTION = 0.7


def build_excerpt(content: str, max_chars: int = MAX_CHARS) -> str:
    content = content.strip()
    if len(content) <= max_chars:
        return content
    head_n = int(max_chars * HEAD_FRACTION)
    tail_n = max_chars - head_n
    head = content[:head_n]
    tail = content[-tail_n:]
    return (f"{head}\n\n[... {len(content) - max_chars} characters omitted; "
            f"article continues ...]\n\n{tail}")


def build_user_prompt(content: str, max_chars: int = MAX_CHARS) -> str:
    return ("Rate the FORM of the following article on the three facets. "
            "Return only the JSON object.\n\n"
            "=== ARTICLE START ===\n"
            f"{build_excerpt(content, max_chars)}\n"
            "=== ARTICLE END ===")

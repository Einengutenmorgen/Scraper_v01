#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Merge every per-source JSONL into one corpus file.

    python merge_corpus.py --out corpus.csv
    python merge_corpus.py --out corpus.jsonl --format jsonl
    python merge_corpus.py --out corpus.csv --with-source-meta   # analysis view

Source paths come from reextract.py's adapters, so the two layout conventions
(RU: output/<name>.jsonl, TR: out/<source>.jsonl) stay in ONE place.

WHAT THIS EMITS
  `content` (= title + "\\n\\n" + body) is the field Label Studio renders as
  $content, so it is always present and is the annotation unit. `title` and
  `body` are kept alongside it for analysis, NOT as the annotation input.

  Outlet-level judgements are NOT emitted by default. `country` and `lang` are
  (they are facts, and every downstream split needs them); `orientation`,
  `factuality_tier` and `expected_genre` require --with-source-meta.

  This default is not cosmetic. The genre/stance filter runs on the merged
  corpus, and its whole design is that it must not see anything correlated with
  the labels it is estimating. Ship the clean corpus to the filter; use
  --with-source-meta only for analysis, after filtering.

FAIL-LOUD
  Unknown source, missing sources.csv row, duplicate doc_id, a record carrying
  an outlet-level field, an empty `content`, or a schema surprise all abort the
  merge rather than producing a quietly wrong corpus.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import statistics
import sys
from collections import Counter, defaultdict

from reextract import ADAPTERS, BANNED

REGISTRY = "sources.csv"

# Dates outside this range are parse failures, not old articles. The oldest
# source in the corpus starts in 2007; anything before 2000 came from a regex
# that matched a year mentioned IN the article, or from an epoch default.
EARLIEST_PLAUSIBLE = "2000-01-01"
LATEST_PLAUSIBLE = (dt.date.today() + dt.timedelta(days=2)).isoformat()

# Field name differences between scrapers, normalised on the way in.
ALIASES = {"byline": "author"}      # RIA calls it byline; everyone else author

# Emitted for every record, in this order.
CORE = ["doc_id", "source", "section", "country", "lang", "article_id", "url",
        "published_at", "title", "subtitle", "body", "content",
        "author", "has_byline", "stated_reading_time", "suspected_interview",
        "char_count", "word_count", "paragraph_count", "prose_paragraph_count",
        "mean_paragraph_len", "sentence_count"]

OUTLET = ["orientation", "factuality_tier", "expected_genre"]


def load_registry(path=REGISTRY) -> dict:
    if not os.path.exists(path):
        raise SystemExit(
            f"{path} not found. It maps `source` to country/lang and to the "
            f"outlet-level judgements — see SOURCES.md.")
    reg = {r["source"]: r for r in csv.DictReader(open(path, encoding="utf-8"))}
    if not reg:
        raise SystemExit(f"{path} has no rows")
    return reg


def source_paths(root: str | None = None) -> dict:
    """{adapter_name: jsonl_path} — single source of truth, from reextract.

    Both families write under a single <ROOT>/output/ tree anchored to the
    scripts' directory (or $KUKI_ROOT), never the cwd. `--root` rebases onto
    another tree, which is also what makes this testable.
    """
    paths = {}
    for name, cls in ADAPTERS.items():
        p = cls().paths()[0]
        if root:
            p = os.path.join(root, "output", os.path.basename(p))
        paths[name] = p
    return paths


def read_source(name: str, path: str, registry: dict) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        raw = [json.loads(l) for l in fh if l.strip()]
    if not raw:
        raise SystemExit(f"{name}: {path} is empty")

    out, bad_dates = [], []
    for i, r in enumerate(raw):
        for old, new in ALIASES.items():
            if old in r and new not in r:
                r[new] = r.pop(old)

        leaked = BANNED & set(r)
        if leaked:
            raise SystemExit(
                f"{name} record {i}: outlet-level field(s) {sorted(leaked)} on "
                f"the record. Re-scrape or re-extract with the current code — "
                f"these belong in {REGISTRY} (see SOURCES.md).")

        src = r.get("source")
        if src not in registry:
            raise SystemExit(
                f"{name} record {i}: source {src!r} has no row in {REGISTRY}. "
                f"Add it before merging — country/lang cannot be guessed.")
        meta = registry[src]

        d = r.get("date") or ""
        if d and not (EARLIEST_PLAUSIBLE <= d <= LATEST_PLAUSIBLE):
            bad_dates.append((r.get("article_id"), d))

        content = r.get("content") or ""
        if not content.strip():
            raise SystemExit(
                f"{name} record {i} ({r.get('article_id')}): empty `content`. "
                f"That is the field Label Studio annotates; a blank one would "
                f"become an unannotatable task.")

        # Sabah's article_id is the TITLE SLUG, and columnists reuse slugs across
        # years -- 62 collisions observed in a single run. Its `uid`
        # ("YYYY-MM-DD__slug") is unique by construction, so prefer it wherever a
        # scraper supplies one. doc_id is the annotation key; a collision would
        # silently merge two different articles into one task.
        key = r.get("uid") or r["article_id"]
        rec = {
            "doc_id": f"{src}:{key}",
            "source": src,
            "section": r.get("section", ""),
            "country": meta["country"],
            "lang": meta["lang"],
            "article_id": r["article_id"],
            "url": r.get("url", ""),
            "published_at": r.get("date", ""),
            "title": r.get("title", ""),
            "subtitle": r.get("subtitle") or "",
            "body": r.get("body", ""),
            "content": content,
            "author": r.get("author") or "",
            "has_byline": bool(r.get("has_byline")),
            "stated_reading_time": r.get("stated_reading_time"),
            "suspected_interview": r.get("suspected_interview"),
            "char_count": r.get("char_count"),
            "word_count": r.get("word_count"),
            "paragraph_count": r.get("paragraph_count"),
            "prose_paragraph_count": r.get("prose_paragraph_count"),
            "mean_paragraph_len": r.get("mean_paragraph_len"),
            "sentence_count": r.get("sentence_count"),
            "_outlet": {k: meta.get(k, "") for k in OUTLET},
        }
        out.append(rec)

    if bad_dates:
        # 1970-01-01 is an epoch default (a parse that returned 0 instead of
        # failing); a 1939 or 1945 date is a year lifted out of the article TEXT
        # by a too-greedy date regex. Both are silent parse failures, and a wrong
        # date silently corrupts any time-window or trend analysis.
        print(f"  !! {name}: {len(bad_dates)} implausible date(s) "
              f"(outside {EARLIEST_PLAUSIBLE}..{LATEST_PLAUSIBLE}): "
              f"{bad_dates[:6]}")
    return out


def report(records: list[dict], registry: dict, missing: dict) -> None:
    by_src = defaultdict(list)
    for r in records:
        by_src[r["source"]].append(r)

    print("\n" + "=" * 72)
    print("MERGED CORPUS")
    print("=" * 72)
    hdr = f"  {'source':<12}{'lang':<6}{'n':>7}{'median wc':>11}{'min':>7}{'max':>8}{'byline%':>9}"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for src in sorted(by_src):
        rs = by_src[src]
        wc = sorted(x["word_count"] or 0 for x in rs)
        byl = 100 * sum(1 for x in rs if x["has_byline"]) / len(rs)
        print(f"  {src:<12}{registry[src]['lang']:<6}{len(rs):>7}"
              f"{statistics.median(wc):>11.0f}{wc[0]:>7}{wc[-1]:>8}{byl:>8.0f}%")

    lang = Counter(r["lang"] for r in records)
    print(f"\n  total {len(records)} records  " +
          "  ".join(f"{k}={v}" for k, v in sorted(lang.items())))

    # signals a non-Russian/Turkish reader can act on
    short = [r["doc_id"] for r in records if (r["word_count"] or 0) < 150]
    nobyl = [s for s in by_src if not any(r["has_byline"] for r in by_src[s])]
    interviews = [r for r in records if r["suspected_interview"]]
    if short:
        print(f"\n  !! {len(short)} record(s) under 150 words: {short[:6]}"
              f"{' …' if len(short) > 6 else ''}")
    if nobyl:
        print(f"  !! source(s) with ZERO bylines — extractor is probably broken: {nobyl}")
    if interviews:
        print(f"  ·  {len(interviews)} flagged suspected_interview "
              f"({', '.join(sorted({r['source'] for r in interviews}))})")
    if missing:
        print(f"\n  ·  not merged (no JSONL yet): {', '.join(sorted(missing))}")

    unfilled = [s for s in registry if not registry[s].get("factuality_tier")]
    if unfilled:
        print(f"\n  ·  factuality_tier still blank for {len(unfilled)} source(s) — "
              f"pending the source-typing sheet")
    print("=" * 72 + "\n")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="corpus.csv")
    ap.add_argument("--format", choices=["csv", "jsonl"], default=None,
                    help="default: inferred from --out")
    ap.add_argument("--sources", help="comma-separated subset (default: all found)")
    ap.add_argument("--registry", default=REGISTRY)
    ap.add_argument("--root", help="rebase all source paths under <root>/output/")
    ap.add_argument("--with-source-meta", action="store_true",
                    help="add orientation / factuality_tier / expected_genre "
                         "(analysis view — do NOT feed this to the filter)")
    ap.add_argument("--drop-body", action="store_true",
                    help="omit title/body/subtitle, keep only `content`")
    args = ap.parse_args()

    registry = load_registry(args.registry)
    paths = source_paths(args.root)
    wanted = ([s.strip() for s in args.sources.split(",")] if args.sources
              else sorted(paths))
    unknown = [s for s in wanted if s not in paths]
    if unknown:
        raise SystemExit(f"unknown source(s) {unknown}; known: {sorted(paths)}")

    records, missing = [], {}
    for name in wanted:
        p = paths[name]
        if not os.path.exists(p):
            missing[name] = p
            continue
        records.extend(read_source(name, p, registry))

    if not records:
        raise SystemExit(
            "no per-source JSONL found. Run the scrapers first. Looked for:\n  "
            + "\n  ".join(f"{k}: {v}" for k, v in sorted(paths.items())))

    dupes = [d for d, n in Counter(r["doc_id"] for r in records).items() if n > 1]
    if dupes:
        raise SystemExit(
            f"{len(dupes)} duplicate doc_id(s), e.g. {dupes[:5]}. doc_id is the "
            f"annotation key and must be unique.")

    records.sort(key=lambda r: (r["lang"], r["source"], r["published_at"] or "",
                                r["article_id"]))

    cols = list(CORE)
    if args.drop_body:
        cols = [c for c in cols if c not in ("title", "subtitle", "body")]
    if args.with_source_meta:
        cols += OUTLET

    rows = []
    for r in records:
        row = {c: r.get(c, r["_outlet"].get(c, "")) for c in cols}
        rows.append(row)

    fmt = args.format or ("jsonl" if args.out.endswith(".jsonl") else "csv")
    if fmt == "csv":
        with open(args.out, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, quoting=csv.QUOTE_ALL)
            w.writeheader()
            w.writerows(rows)
    else:
        with open(args.out, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    report(records, registry, missing)
    print(f"  wrote {len(rows)} rows x {len(cols)} cols -> {args.out} ({fmt})")
    if args.with_source_meta:
        print("  NOTE: outlet columns included — this view is for analysis, "
              "not for the genre/stance filter.")


if __name__ == "__main__":
    sys.exit(main())

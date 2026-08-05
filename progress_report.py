#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
progress_report.py — after a crash/interrupt, summarize what got saved.

Run from the same folder the scrapers run in (the one holding out/ and
raw_store/):   python progress_report.py

Key fact about the scrapers: raw HTML is written to raw_store/<source>/ PER
ARTICLE as it is fetched, but out/<source>.jsonl is written only when a run
FINISHES. So after a crash:
  * raw_store count  = articles actually fetched (recoverable — no re-fetch)
  * jsonl count      = records from the last COMPLETED run (may be older/absent)
  * summary.json     = present only if that run finished cleanly
If a source crashed mid-run you can re-extract records from raw_store without
re-downloading anything.
"""

import glob
import json
import os

SOURCES = ["cumhuriyet", "sabah", "yenicag", "odatv"]
OUT_DIR = "out"
RAW_DIR = "raw_store"


def count_lines(path):
    n = 0
    with open(path, encoding="utf-8") as fh:
        for _ in fh:
            n += 1
    return n


def jsonl_stats(path):
    """Return (records, unique_ids, min_date, max_date) for a jsonl file."""
    ids, dates = set(), []
    n = 0
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            n += 1
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue  # a partially-written trailing line
            if r.get("article_id"):
                ids.add(r["article_id"])
            if r.get("date"):
                dates.append(r["date"])
    return n, len(ids), (min(dates) if dates else "-"), (max(dates) if dates else "-")


def main():
    print(f"{'source':<12}{'raw_html':>10}{'jsonl_recs':>12}"
          f"{'unique':>9}{'summary?':>10}  status / date range")
    print("-" * 78)
    for src in SOURCES:
        raw_glob = os.path.join(RAW_DIR, src, "*.html")
        raw_n = len(glob.glob(raw_glob))

        jsonl = os.path.join(OUT_DIR, f"{src}.jsonl")
        if os.path.isfile(jsonl):
            recs, uniq, dmin, dmax = jsonl_stats(jsonl)
            drange = f"{dmin} … {dmax}"
        else:
            recs, uniq, drange = 0, 0, "(no jsonl)"

        summ_path = jsonl + ".summary.json"
        finished = os.path.isfile(summ_path)
        n_skip = ""
        if finished:
            try:
                with open(summ_path, encoding="utf-8") as fh:
                    summ = json.load(fh)
                n_skip = f" (skipped {summ.get('n_skipped', 0)})"
            except (OSError, json.JSONDecodeError):
                pass

        if finished and recs:
            status = "COMPLETED" + n_skip
        elif raw_n and not finished:
            status = f"CRASHED mid-run — {raw_n} raw saved, jsonl NOT written"
        elif raw_n and finished:
            status = "completed" + n_skip
        else:
            status = "nothing saved / not started"

        print(f"{src:<12}{raw_n:>10}{recs:>12}{uniq:>9}"
              f"{'yes' if finished else 'no':>10}  {status}")
        if recs:
            print(f"{'':<53}dates {drange}")

    print("-" * 78)
    print("raw_html = articles fetched & saved (recoverable). jsonl_recs = "
          "records from the last COMPLETED run.")
    print("A CRASHED source's fetched articles are safe in raw_store/<source>/ "
          "— they can be re-extracted without re-downloading.")


if __name__ == "__main__":
    main()

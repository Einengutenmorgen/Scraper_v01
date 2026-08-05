#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""End-to-end wiring test for run(): collection is stubbed (validated separately)
and article HTML is served from the synthetic RIA-shaped fixture, so this proves
JSONL output, per-article raw-HTML storage, and the run-summary distribution."""
import json, os, tempfile
import v1_ria_analitika_scraper as R
from test_extraction import build_article_html

REFS = [
    R.ArticleRef("https://ria.ru/20260317/iran-2081087907.html", "2081087907", "20260317"),
    R.ArticleRef("https://ria.ru/20251024/ukraina-2050229360.html", "2050229360", "20251024"),
    R.ArticleRef("https://ria.ru/20250507/putin-2015308036.html", "2015308036", "20250507"),
]
SIGNED = {"2081087907": True, "2050229360": False, "2015308036": True}

def main():
    tmp = tempfile.mkdtemp()
    R.OUTPUT_DIR = tmp
    R.OUTPUT_JSONL = os.path.join(tmp, "ria_analitika.jsonl")
    R.RAW_HTML_DIR = os.path.join(tmp, "raw_html")
    R.RUN_SUMMARY_JSON = os.path.join(tmp, "run_summary.json")
    R.REQUEST_DELAY_SEC = (0.0, 0.01)

    R.collect = lambda extra_articles, method: REFS               # stub collection
    R.fetch_article_html = lambda session, ref: build_article_html(SIGNED[ref.article_id])

    summary = R.run(extra_articles=0, method="browser")

    # JSONL: one line per article, all required fields present.
    lines = open(R.OUTPUT_JSONL, encoding="utf-8").read().strip().splitlines()
    assert len(lines) == 3, len(lines)
    required = {"url","article_id","date","title","body","content","byline",
                "char_count","word_count","paragraph_count","mean_paragraph_len",
                "sentence_count","has_byline","source","section","orientation",
                "factuality_tier","genre"}
    for ln in lines:
        rec = json.loads(ln)
        assert required <= set(rec), required - set(rec)
        assert rec["content"].startswith(rec["title"])
    # raw HTML saved per article
    for ref in REFS:
        assert os.path.exists(os.path.join(R.RAW_HTML_DIR, f"{ref.article_id}.html"))
    # summary distribution present
    s = json.load(open(R.RUN_SUMMARY_JSON, encoding="utf-8"))
    assert s["extracted_ok"] == 3 and s["failed"] == 0
    assert s["word_count_distribution"]["n"] == 3
    assert s["signed_bylines"] == 2 and s["institutional_bylines"] == 1
    print("\n  JSONL lines:", len(lines))
    print("  raw_html files:", sorted(os.listdir(R.RAW_HTML_DIR)))
    print("  summary word_count dist:", s["word_count_distribution"])
    print("\nRUN INTEGRATION TEST PASSED")

if __name__ == "__main__":
    main()

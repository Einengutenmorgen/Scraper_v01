#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Re-extract the corpus from the ALREADY-SAVED raw HTML — no re-scraping.

Use this after tuning extraction (e.g. the byline classifier or trafilatura
config). It reads output/raw_html/<id>.html, rebuilds every record with the
current extract_record(), and rewrites the JSONL + run summary in place.

    python reextract.py

The article url/id/date come from the existing JSONL if present, otherwise are
reconstructed from a <link rel="canonical">/og:url inside the saved HTML.
"""
import json
import os
import re

import v1_ria_analitika_scraper as R
from bs4 import BeautifulSoup


def _ref_from_saved(article_id: str, html: str, prior: dict | None) -> R.ArticleRef:
    if prior and prior.get("url"):
        m = R.ARTICLE_RE.search(prior["url"])
        if m:
            return R.ArticleRef(m.group(0), m.group("id"), m.group("date"))
    # Fall back to the canonical URL embedded in the page.
    soup = BeautifulSoup(html, "html.parser")
    url = None
    link = soup.find("link", rel="canonical")
    if link and link.get("href"):
        url = link["href"]
    if not url:
        og = soup.find("meta", attrs={"property": "og:url"})
        if og and og.get("content"):
            url = og["content"]
    if url:
        m = R.ARTICLE_RE.search(url)
        if m:
            return R.ArticleRef(m.group(0), m.group("id"), m.group("date"))
    raise RuntimeError(f"Cannot reconstruct ArticleRef for {article_id}")


def main():
    prior = {}
    if os.path.exists(R.OUTPUT_JSONL):
        with open(R.OUTPUT_JSONL, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    r = json.loads(line)
                    prior[r["article_id"]] = r

    files = sorted(f for f in os.listdir(R.RAW_HTML_DIR) if f.endswith(".html"))
    if not files:
        raise SystemExit(f"No saved HTML in {R.RAW_HTML_DIR}; run the scraper first.")

    records, failed = [], []
    with open(R.OUTPUT_JSONL, "w", encoding="utf-8") as out:
        for fn in files:
            aid = fn[:-5]
            html = open(os.path.join(R.RAW_HTML_DIR, fn), encoding="utf-8").read()
            try:
                ref = _ref_from_saved(aid, html, prior.get(aid))
                rec = R.extract_record(ref, html)
                out.write(json.dumps(R.asdict(rec), ensure_ascii=False) + "\n")
                records.append(rec)
            except Exception as exc:
                failed.append({"article_id": aid, "error": f"{type(exc).__name__}: {exc}"})
                print(f"  FAILED {aid}: {exc}")

    summary = R._summarize(len(files), records, failed)
    with open(R.RUN_SUMMARY_JSON, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)
    R._print_summary(summary)
    print(f"  Re-extracted {len(records)} records from saved HTML -> {R.OUTPUT_JSONL}")


if __name__ == "__main__":
    main()

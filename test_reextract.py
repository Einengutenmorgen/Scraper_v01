#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Round-trip test for reextract.py: scrape -> re-extract -> identical records.

Uses the synthetic RIA-shaped fixture (no network). Proves the RU adapter path,
the atomic write, the .bak, the dry-run, and the outlet-field leak guard.
"""
import json, os, tempfile, shutil

import scrape_ria as R
import reextract as RX
from test_extraction import build_article_html

SIGNED = {"2081087907": "Мир после Ирана", "2050229360": "Перепрошивание Украины"}
REFS = [R.ArticleRef(f"https://ria.ru/20260101/x-{i}.html", i, "20260101")
        for i in SIGNED]


def _seed(tmp):
    """Write a corpus the way the scraper would: JSONL + per-article raw HTML."""
    R.OUTPUT_DIR = tmp
    R.OUTPUT_JSONL = os.path.join(tmp, "ria_analitika.jsonl")
    R.RAW_HTML_DIR = os.path.join(tmp, "raw_html")
    os.makedirs(R.RAW_HTML_DIR, exist_ok=True)
    with open(R.OUTPUT_JSONL, "w", encoding="utf-8") as out:
        for ref in REFS:
            html = build_article_html(SIGNED[ref.article_id])
            open(os.path.join(R.RAW_HTML_DIR, f"{ref.article_id}.html"),
                 "w", encoding="utf-8").write(html)
            out.write(json.dumps(R.asdict(R.extract_record(ref, html)),
                                 ensure_ascii=False) + "\n")


def main():
    tmp = tempfile.mkdtemp()
    try:
        _seed(tmp)
        before = [json.loads(l) for l in open(R.OUTPUT_JSONL, encoding="utf-8")]

        # 1. dry run writes nothing
        mtime = os.path.getmtime(R.OUTPUT_JSONL)
        stats = RX.reextract("ria", dry_run=True)
        assert os.path.getmtime(R.OUTPUT_JSONL) == mtime, "dry run wrote the file"
        assert stats["reextracted"] == len(REFS), stats
        assert stats["changed"] == 0, f"unchanged extraction reported a diff: {stats}"
        print("  dry-run: no write, no spurious diff  OK")

        # 2. real run is idempotent
        RX.reextract("ria")
        after = [json.loads(l) for l in open(R.OUTPUT_JSONL, encoding="utf-8")]
        assert before == after, "re-extraction changed records with no code change"
        assert os.path.exists(R.OUTPUT_JSONL + ".bak"), "no .bak kept"
        print("  round-trip identical + .bak kept  OK")

        # 3. an extraction change is REPORTED, not silent
        orig = R._strip_boilerplate
        R._strip_boilerplate = lambda t: orig(t) + "\n\nSENTINEL PARAGRAPH."
        try:
            stats = RX.reextract("ria", dry_run=True)
        finally:
            R._strip_boilerplate = orig
        assert stats["changed"] == len(REFS), stats
        assert "body" in stats["fields_changed"], stats["fields_changed"]
        assert "word_count" in stats["fields_changed"], stats["fields_changed"]
        print(f"  extraction change surfaced in diff: {list(stats['fields_changed'])[:4]}  OK")

        # 4. outlet-level fields must not come back
        assert not (RX.BANNED & set(after[0])), RX.BANNED & set(after[0])
        orig_er = R.extract_record
        R.extract_record = lambda ref, html: orig_er(ref, html)
        leaky = RX.ADAPTERS["ria"]()
        real_rebuild = leaky.rebuild
        try:
            RX.ADAPTERS["ria"].rebuild = lambda s, st, h, p: {
                **real_rebuild(st, h, p), "factuality_tier": "disinfo_prone"}
            try:
                RX.reextract("ria", dry_run=True)
            except SystemExit as exc:
                assert "factuality_tier" in str(exc), exc
                print("  leak guard fires on a reintroduced outlet field  OK")
            else:
                raise AssertionError("leak guard did NOT fire")
        finally:
            RX.ADAPTERS["ria"].rebuild = real_rebuild
            R.extract_record = orig_er

        print("\nREEXTRACT ROUND-TRIP TEST PASSED")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()

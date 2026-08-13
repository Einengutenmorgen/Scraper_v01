#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Recovery of crashed TR runs from raw HTML alone (no JSONL row).

The TR scrapers write raw HTML per article as it is fetched, but the JSONL only
when a run FINISHES. A crash therefore strands real fetched pages. reextract's
TR adapters recover the url from the page's own canonical/og:url, so those pages
cost only re-extraction, never a re-fetch.
"""
import json, os, shutil, tempfile

import reextract as RX

BODY = ("Bu yeterince uzun bir paragraf metnidir ve iceriginde cok sayida kelime "
        "bulunmaktadir, boylece cikarim algoritmasi bunu ana metin olarak sayar. ")

CASES = {
    "yenicag":    "https://www.yenicaggazetesi.com/bir-baslik-123456h.htm",
    "odatv":      "https://www.odatv.com/yazarlar/soner-yalcin/bir-baslik-123456789",
    "cumhuriyet": "https://www.cumhuriyet.com.tr/yazarlar/olaylar-ve-gorusler/bir-baslik-2510001",
    "sabah":      "https://www.sabah.com.tr/yazarlar/ardic/2026/01/15/bir-baslik",
}


def page(url, author="Mehmet Yazar", title="Bir Baslik"):
    return (f'<html><head><meta property="og:url" content="{url}">'
            f'<link rel="canonical" href="{url}">'
            f'<meta property="og:title" content="{title}">'
            f'<meta property="og:description" content="{author} yazdi...">'
            f'<meta property="article:author" content="{author}">'
            f'<meta property="article:published_time" content="2026-01-15T08:00:00+03:00">'
            f'<meta itemprop="datePublished" content="2026-01-15">'
            f'<meta itemprop="articleAuthor" content="{author}">'
            f'<time datetime="2026-01-15T08:00:00+03:00">15 Ocak 2026</time>'
            f'</head><body><article><h1>{title}</h1>'
            + "".join(f"<p>{BODY}</p>" for _ in range(5))
            + "</article></body></html>")


def main():
    tmp = tempfile.mkdtemp()
    try:
        ok = 0
        for src, url in CASES.items():
            ad = RX.ADAPTERS[src](raw_dir=os.path.join(tmp, "raw_store"))
            stem = url.rstrip("/").rsplit("/", 1)[-1]

            # NO prior JSONL row -- the crashed-run case
            rec = ad.rebuild(stem, page(url), None)

            assert rec["url"] == url, (src, rec["url"])
            assert rec["article_id"], (src, "no article_id recovered")
            assert rec["content"].strip(), (src, "empty content")
            assert rec.get("_recovered_from_raw") is True, src
            assert rec["word_count"] > 0, (src, rec["word_count"])
            if src == "sabah":
                assert rec["date"] == "2026-01-15", rec["date"]
                assert rec["uid"] == "2026-01-15__bir-baslik", rec["uid"]
            print(f"  {src:<12} recovered id={rec['article_id']!s:<12} "
                  f"words={rec['word_count']:<4} date={rec.get('date')}")
            ok += 1

        # a page with no canonical/og:url must FAIL, not guess
        ad = RX.ADAPTERS["yenicag"](raw_dir=os.path.join(tmp, "raw_store"))
        try:
            ad.rebuild("x", "<html><body><p>hi</p></body></html>", None)
        except RuntimeError as e:
            assert "cannot recover the article url" in str(e), e
            print("  no canonical url -> fails loud, does not guess  OK")
        else:
            raise AssertionError("should have failed")

        # an off-scope url must be rejected, not silently admitted
        off = page("https://www.yenicaggazetesi.com/spor/mac-sonucu-999h.htm")
        try:
            ad.rebuild("y", off.replace("yenicaggazetesi.com/spor",
                                        "example.com/spor"), None)
        except RuntimeError as e:
            assert "out of scope" in str(e), e
            print("  out-of-scope url rejected  OK")
        else:
            raise AssertionError("should have rejected off-scope url")

        # prior JSONL still wins when present, and is NOT marked recovered
        url = CASES["odatv"]
        prior = {"url": url, "article_id": "123456789", "date": "2026-01-15"}
        rec = RX.ADAPTERS["odatv"](raw_dir=os.path.join(tmp, "raw_store")) \
            .rebuild("123456789", page(url), prior)
        assert "_recovered_from_raw" not in rec
        print("  existing JSONL row still takes precedence  OK")

        print(f"\nTR RECOVERY TEST PASSED ({ok}/4 sources recoverable from raw HTML)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()

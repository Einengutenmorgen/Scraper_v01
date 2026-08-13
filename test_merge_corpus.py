#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for merge_corpus.py against synthetic per-source JSONL.

Covers both scraper families (RIA's `byline` alias, Holod's
`suspected_interview`, TR's `uid`), the sources.csv join, the default
exclusion of outlet-level columns, and every fail-loud path.
"""
import csv, json, os, subprocess, sys, tempfile, shutil

HERE = os.path.dirname(os.path.abspath(__file__))
BODY = "Абзац текста. " * 40


def rec(source, aid, **kw):
    title = kw.pop("title", f"Заголовок {aid}")
    r = {"url": f"https://x/{aid}", "article_id": aid, "date": "2026-01-15",
         "source": source, "section": "opinions", "title": title,
         "subtitle": "", "body": BODY, "content": title + "\n\n" + BODY,
         "author": "Иван Иванов", "has_byline": True, "char_count": len(BODY),
         "word_count": 120, "paragraph_count": 5, "prose_paragraph_count": 5,
         "mean_paragraph_len": 24.0, "sentence_count": 40,
         "stated_reading_time": 5}
    r.update(kw)
    return r


def write(path, records):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def run(cwd, *args, expect_fail=False):
    p = subprocess.run([sys.executable, os.path.join(HERE, "merge_corpus.py"), *args],
                       cwd=cwd, capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": HERE})
    out = p.stdout + p.stderr
    if expect_fail:
        assert p.returncode != 0, f"expected failure, got 0:\n{out}"
    else:
        assert p.returncode == 0, f"expected success:\n{out}"
    return out


def setup(tmp):
    shutil.copy(os.path.join(HERE, "sources.csv"), tmp)
    # RU family: RIA uses `byline`, no subtitle/reading time; Holod has the flag
    ria = rec("ria_novosti", "2081087907", section="analitika")
    ria["byline"] = ria.pop("author")
    write(os.path.join(tmp, "output", "ria_analitika.jsonl"), [ria])
    write(os.path.join(tmp, "output", "holod_opinions.jsonl"),
          [rec("holod", "slug-a", suspected_interview=True),
           rec("holod", "slug-b", suspected_interview=False)])
    # TR family: extra `uid`, same output/ root
    tr = rec("cumhuriyet", "2510001", section="yazarlar")
    tr["uid"] = "2026-01-15__slug"
    write(os.path.join(tmp, "output", "cumhuriyet.jsonl"), [tr])
    return tmp


def main():
    tmp = setup(tempfile.mkdtemp())
    try:
        out = run(tmp, "--out", "corpus.csv", "--root", tmp)
        rows = list(csv.DictReader(open(os.path.join(tmp, "corpus.csv"), encoding="utf-8", newline="")))
        assert len(rows) == 4, len(rows)
        print(f"  merged {len(rows)} rows across 3 sources / 2 layouts  OK")

        by = {r["doc_id"]: r for r in rows}
        assert set(by) == {"ria_novosti:2081087907", "holod:slug-a",
                           "holod:slug-b", "cumhuriyet:2510001"}, sorted(by)
        print("  doc_id = source:article_id  OK")

        # RIA's `byline` normalised to `author`
        assert by["ria_novosti:2081087907"]["author"] == "Иван Иванов"
        print("  RIA `byline` aliased to `author`  OK")

        # registry join
        assert by["cumhuriyet:2510001"]["lang"] == "tr"
        assert by["cumhuriyet:2510001"]["country"] == "TR"
        assert by["holod:slug-a"]["lang"] == "ru"
        print("  country/lang joined from sources.csv  OK")

        # content is present and is title + blank line + body
        for r in rows:
            assert r["content"].startswith(r["title"]), r["doc_id"]
            assert r["content"] != r["body"], r["doc_id"]
        print("  `content` preserved as the Label Studio annotation unit  OK")

        # outlet columns absent by default, present on request
        for banned in ("orientation", "factuality_tier", "expected_genre"):
            assert banned not in rows[0], banned
        print("  outlet columns EXCLUDED by default  OK")

        run(tmp, "--out", "an.csv", "--with-source-meta", "--root", tmp)
        arows = list(csv.DictReader(open(os.path.join(tmp, "an.csv"), encoding="utf-8", newline="")))
        assert arows[0]["orientation"], arows[0]
        assert arows[0]["factuality_tier"] == "", "tier should still be blank"
        print("  --with-source-meta adds them; factuality_tier still blank  OK")

        # jsonl round trip
        run(tmp, "--out", "corpus.jsonl", "--root", tmp)
        j = [json.loads(l) for l in open(os.path.join(tmp, "corpus.jsonl"), encoding="utf-8")]
        assert len(j) == 4 and j[0]["doc_id"]
        print("  jsonl output  OK")

        # --- fail-loud paths ---
        write(os.path.join(tmp, "output", "ng_opinions.jsonl"),
              [rec("ng", "x1", factuality_tier="mixed")])
        o = run(tmp, "--out", "bad.csv", "--root", tmp, expect_fail=True)
        assert "outlet-level field" in o, o[-300:]
        os.remove(os.path.join(tmp, "output", "ng_opinions.jsonl"))
        print("  rejects a record carrying an outlet field  OK")

        write(os.path.join(tmp, "output", "ng_opinions.jsonl"),
              [rec("unknown_outlet", "x2")])
        o = run(tmp, "--out", "bad.csv", "--root", tmp, expect_fail=True)
        assert "no row in sources.csv" in o, o[-300:]
        os.remove(os.path.join(tmp, "output", "ng_opinions.jsonl"))
        print("  rejects a source missing from the registry  OK")

        e = rec("ng", "x3"); e["content"] = "   "
        write(os.path.join(tmp, "output", "ng_opinions.jsonl"), [e])
        o = run(tmp, "--out", "bad.csv", "--root", tmp, expect_fail=True)
        assert "empty `content`" in o, o[-300:]
        os.remove(os.path.join(tmp, "output", "ng_opinions.jsonl"))
        print("  rejects an empty content field  OK")

        write(os.path.join(tmp, "output", "ng_opinions.jsonl"), [rec("holod", "slug-a")])
        o = run(tmp, "--out", "bad.csv", "--root", tmp, expect_fail=True)
        assert "duplicate doc_id" in o, o[-300:]
        os.remove(os.path.join(tmp, "output", "ng_opinions.jsonl"))
        print("  rejects duplicate doc_id  OK")

        # zero-byline source is REPORTED (the Holod symptom), not swallowed
        write(os.path.join(tmp, "output", "ng_opinions.jsonl"),
              [rec("ng", "y1", author="", has_byline=False)])
        o = run(tmp, "--out", "corpus.csv", "--root", tmp)
        assert "ZERO bylines" in o and "ng" in o, o[-400:]
        print("  warns loudly on a source with zero bylines  OK")

        print("\nMERGE CORPUS TEST PASSED")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()

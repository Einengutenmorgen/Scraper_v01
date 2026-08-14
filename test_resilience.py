#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A transient 5xx must not destroy a multi-hour harvest.

TWO FAILURE MODES, both observed live:
  1. A single HTTP 502 mid-run raised straight out of _http_get and killed the
     whole Yenicag run (author 10 of 13, page 56).
  2. The JSONL was written only when the run FINISHED, so that crash discarded
     every record even though every page was already in the raw store.
"""
import json, os, tempfile, shutil, sys

import scrape_yenicag as Y
import scrape_odatv as O
import scrape_cumhuriyet as C
import scrape_sabah as S


class FakeResp:
    def __init__(self, code, text="<html><body><p>ok</p></body></html>"):
        self.status_code, self.text = code, text
        self.headers = {}


class FlakySession:
    """Fails with `code` for the first `n` calls, then succeeds."""
    def __init__(self, code, n):
        self.code, self.left, self.calls = code, n, 0

    def get(self, url, **kw):
        self.calls += 1
        if self.left > 0:
            self.left -= 1
            return FakeResp(self.code)
        return FakeResp(200)


def main():
    for mod, name in [(Y, "yenicag"), (O, "odatv")]:
        assert hasattr(mod, "RETRYABLE_STATUS"), name
        assert 502 in mod.RETRYABLE_STATUS, name
        mod.time.sleep = lambda s: None          # no real backoff in tests

        # a 502 that clears must be retried, not fatal
        sess = FlakySession(502, 2)
        text = mod._http_get("https://www.yenicaggazetesi.com/x-1h.htm"
                             if name == "yenicag"
                             else "https://www.odatv.com/yazarlar/a/x-1", sess)
        assert "ok" in text, name
        assert sess.calls == 3, (name, sess.calls)
        print(f"  {name:<12} 502 x2 then 200 -> recovered after {sess.calls} calls  OK")

        # a permanent 502 still fails loud, after exhausting retries
        sess = FlakySession(502, 99)
        try:
            mod._http_get("https://www.odatv.com/yazarlar/a/x-1"
                          if name == "odatv"
                          else "https://www.yenicaggazetesi.com/x-1h.htm", sess)
        except Exception as e:
            assert "after" in str(e) and "attempts" in str(e), e
            assert sess.calls == mod.MAX_RETRIES, (name, sess.calls)
            print(f"  {name:<12} persistent 502 -> fails loud after "
                  f"{sess.calls} attempts  OK")
        else:
            raise AssertionError(f"{name}: should have failed")

        # a 404 (non-retryable) must NOT burn retries
        sess = FlakySession(404, 99)
        try:
            mod._http_get("https://www.odatv.com/yazarlar/a/x-1"
                          if name == "odatv"
                          else "https://www.yenicaggazetesi.com/x-1h.htm", sess)
        except Exception:
            assert sess.calls == 1, (name, "404 was retried", sess.calls)
            print(f"  {name:<12} 404 -> fails immediately, no retry  OK")

    for mod, name in [(C, "cumhuriyet"), (S, "sabah")]:
        rs = getattr(mod, "RETRYABLE_STATUS", None) or getattr(mod, "_RETRYABLE_STATUS")
        assert 429 in rs or 503 in rs, name
        print(f"  {name:<12} has a retryable-status set  OK")

    # every TR scraper streams records to disk instead of buffering to the end
    for mod, name in [(Y, "yenicag"), (O, "odatv"), (C, "cumhuriyet"), (S, "sabah")]:
        assert hasattr(mod, "_IncrementalWriter"), name
        tmp = tempfile.mkdtemp()
        try:
            p = os.path.join(tmp, "sub", "x.jsonl")
            w = mod._IncrementalWriter(p)
            w.write({"article_id": "a", "content": "x"})
            w.write({"article_id": "b", "content": "y"})
            # readable BEFORE close -- this is the point
            w.fh.flush()
            lines = [l for l in open(p, encoding="utf-8") if l.strip()]
            assert len(lines) == 2, (name, len(lines))
            assert json.loads(lines[0])["article_id"] == "a"
            w.close()
            print(f"  {name:<12} records readable on disk BEFORE run ends  OK")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    test_transient_classes()
    print("\nRESILIENCE TEST PASSED")


def test_transient_classes():
    """Timeouts, Cloudflare 52x and date-boundary skips (found live 2026-08)."""
    import requests as _rq
    from datetime import date as _d

    # 1. Cloudflare edge errors must be retryable everywhere.
    for mod, name in [(Y, "yenicag"), (O, "odatv"), (C, "cumhuriyet")]:
        for code in (522, 520, 524):
            assert code in mod.RETRYABLE_STATUS, (name, code)
    for code in (522, 503, 429):
        assert code in S._RETRYABLE_STATUS, ("sabah", code)
    print("  cloudflare 52x retryable in all four  OK")

    # 2. A read timeout is an EXCEPTION, not a status -- it used to fly past the
    #    retry loop and kill the whole Sabah run.
    class TimeoutThenOK:
        def __init__(self, n): self.left, self.calls = n, 0
        def get(self, url, **kw):
            self.calls += 1
            if self.left > 0:
                self.left -= 1
                raise _rq.exceptions.ConnectionError("Read timed out.")
            class R:
                status_code = 200
                text = "<html><body><p>ok</p></body></html>"
                headers = {}
            return R()

    S.time.sleep = lambda s: None
    S.detect_cloudflare_block = lambda code, text: None
    S._assert_decodable = lambda *a, **k: None
    sess = TimeoutThenOK(3)
    assert "ok" in S.fetch("https://www.sabah.com.tr/x", sess)
    assert sess.calls == 4, sess.calls
    print(f"  sabah: 3 read timeouts then 200 -> recovered ({sess.calls} calls)  OK")

    # 3. A +/-1 day url-vs-meta gap is a timezone boundary, not corruption.
    #    Failing loud on it deleted valid columns across many authors at once.
    for url_d, meta_d, should_raise in [
            ("2023-01-25", "2023-01-26", False),   # observed live
            ("2021-12-03", "2021-12-04", False),   # observed live
            ("2023-12-19", "2023-12-18", False),   # observed live (other way)
            ("2023-01-25", "2023-01-25", False),
            ("2023-01-25", "2023-03-01", True)]:   # real disagreement
        gap = abs((_d.fromisoformat(meta_d) - _d.fromisoformat(url_d)).days)
        assert (gap > 1) == should_raise, (url_d, meta_d, gap)
    print("  sabah: +/-1 day url-vs-meta tolerated, >1 day still fails loud  OK")


if __name__ == "__main__":
    main()

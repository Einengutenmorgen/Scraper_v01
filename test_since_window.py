#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""--since window for Sabah and Cumhuriyet.

Sabah and Cumhuriyet archives run back to ~2006; an unbounded --full run is
effectively unbounded (limit is 100000 PER AUTHOR). The window bounds the
harvest and, more importantly, gives every source a comparable time frame.

Sabah:      date is in the article URL path and the archive is newest-first,
            so pagination STOPS at the window -- out-of-window pages are never
            fetched.
Cumhuriyet: no date on the author listing (it lives in the article meta), so
            only the sitemap path can prune before fetching, via <lastmod>.
"""
import scrape_sabah as S
import scrape_cumhuriyet as C

SINCE = "2020-01-01"


def sabah_page(dates):
    return "".join(
        f'<a href="/yazarlar/ardic/{d[:4]}/{d[5:7]}/{d[8:10]}/slug-{i}"></a>'
        for i, d in enumerate(dates))


class SabahSession:
    def __init__(self, pages):
        self.pages, self.fetched = pages, []


def main():
    # ---- Sabah: pagination stops at the window -------------------------------
    pages = {1: sabah_page(["2026-05-0%d" % (i + 1) for i in range(5)]),
             2: sabah_page(["2021-03-0%d" % (i + 1) for i in range(5)]),
             3: sabah_page(["2019-11-0%d" % (i + 1) for i in range(5)]),
             4: sabah_page(["2018-01-0%d" % (i + 1) for i in range(5)])}
    fetched = []

    def fake_fetch(url, session, allow_404=False):
        p = 1 if "page=" not in url else int(url.split("page=")[1])
        fetched.append(p)
        return pages.get(p)

    S.fetch = fake_fetch
    links, reason = S.collect_author_archive("ardic", 10_000, None, since=SINCE)
    assert reason.startswith("stop_since"), reason
    assert fetched == [1, 2, 3], fetched          # page 4 never requested
    assert len(links) == 10, len(links)           # pages 1 + 2 only
    assert all(l[2] >= SINCE for l in links), [l[2] for l in links]
    print(f"  sabah: stopped at page 3 ({reason}), page 4 never fetched  OK")
    print(f"  sabah: kept {len(links)} in-window links, all >= {SINCE}  OK")

    # without --since the whole archive is walked
    fetched.clear()
    links, reason = S.collect_author_archive("ardic", 10_000, None)
    assert len(links) == 20 and 4 in fetched, (len(links), fetched)
    print(f"  sabah: no --since -> full walk ({len(links)} links)  OK")

    # a typo must not silently widen the frame
    for bad in ("2020", "01-01-2020", "yesterday"):
        try:
            S._valid_since(bad)
        except SystemExit:
            pass
        else:
            raise AssertionError(f"accepted {bad!r}")
    assert S._valid_since(SINCE) == SINCE and S._valid_since(None) is None
    print("  --since validated up front, bad values rejected  OK")

    # ---- Cumhuriyet: sitemap prunes on <lastmod> -----------------------------
    xml = "<urlset>" + "".join(
        f"<url><loc>https://www.cumhuriyet.com.tr/yazarlar/olaylar-ve-gorusler/"
        f"slug-{i}-{2500000 + i}</loc><lastmod>{d}</lastmod></url>"
        for i, d in enumerate(["2026-04-01", "2021-06-15", "2019-02-02",
                               "2017-08-08"])) + "</urlset>"
    kept = C.parse_sitemap_urls(xml, since=SINCE)
    assert len(kept) == 2, [k[0] for k in kept]
    print(f"  cumhuriyet: sitemap <lastmod> pruned 4 -> {len(kept)} in window  OK")

    allk = C.parse_sitemap_urls(xml)
    assert len(allk) == 4, len(allk)
    print("  cumhuriyet: no --since -> all 4 kept  OK")

    # an entry with no <lastmod> is KEPT (the article's own date decides)
    xml2 = ("<urlset><url><loc>https://www.cumhuriyet.com.tr/yazarlar/"
            "olaylar-ve-gorusler/slug-x-2500999</loc></url></urlset>")
    assert len(C.parse_sitemap_urls(xml2, since=SINCE)) == 1
    print("  cumhuriyet: missing <lastmod> kept, not silently dropped  OK")

    # run() must actually accept `since` -- it used to reference an undefined
    # name, so --sitemap raised NameError before fetching anything.
    import inspect
    assert "since" in inspect.signature(C.run).parameters, "run() lost `since`"
    print("  cumhuriyet: run() takes `since` (--sitemap NameError fixed)  OK")

    print("\nSINCE WINDOW TEST PASSED")


if __name__ == "__main__":
    main()

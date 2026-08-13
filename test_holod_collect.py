#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Holod load-more collector, against a fixture of the REAL response shape.

CONFIRMED LIVE 2026-08 (probe_holod.py):
  * /opinions/ is NOT a WP category -- it is a `material_type` META filter, so
    the old `categories?slug=opinions` lookup returned [] every time.
  * `/opinions/page/N/` does not exist.
  * The loader is admin-ajax `load_more_opinions`, posting the button's
    `data-param-posts` blob verbatim plus a `page` number, and answering with an
    HTML fragment of 8 cards. `data-max-pages` was 55; page 1 renders 24.
"""
import scrape_holod as H

CARD = ('<div class="opinions__col col">'
        '<a class="quotes-card quotes-card--catalog" '
        'href="https://holod.media/2024/{mm}/{dd}/slug-{n}/">'
        '<div class="quotes-card__img"></div>'
        '<span>{rt} минут чтения</span></a></div>')


def frag(nums, mm="11", rt=7):
    return "\r\n".join(CARD.format(mm=mm, dd=f"{(i % 28) + 1:02d}", n=n, rt=rt)
                       for i, n in enumerate(nums))


def listing(n_cards=24, max_pages=55, query='{"post_type":"post"}'):
    return ("<html><body>" + frag(range(1000, 1000 + n_cards)) +
            f"<button class=\"more-btn js-load-more-opinions\" type=\"button\" "
            f"data-param-posts='{query}' data-max-pages=\"{max_pages}\">"
            f"</button></body></html>")


class FakeSession:
    def __init__(self, pages, fail=None):
        self.pages, self.fail, self.posts = pages, fail or {}, []

    def get(self, url, **kw):
        class R:
            status_code = 200
            text = listing()
        return R()

    def post(self, url, data=None, **kw):
        self.posts.append(dict(data))
        p = int(data["page"])
        class R:
            status_code = 200
            text = self.pages.get(p, "")
        R.status_code = self.fail.get(p, 200)
        return R()


def main():
    H._sleep = lambda *a, **k: None
    H._get = lambda session, url, **kw: session.get(url)

    # 1. button parsing
    q, mx = H._load_more_params(listing(max_pages=55))
    assert mx == 55 and q == '{"post_type":"post"}', (mx, q)
    print("  reads data-param-posts + data-max-pages off the button  OK")

    # 2. missing button must fail loud, not return page 1
    for bad in ("<html><body>no button</body></html>",
                listing(max_pages=0)):
        try:
            H._load_more_params(bad)
        except H.ScrapeError as e:
            assert "Refusing" in str(e) or "stop condition" in str(e), e
        else:
            raise AssertionError("should have failed")
    print("  missing button / no max-pages -> fails loud  OK")

    # 3. full walk: 24 on page 1 + 8 per ajax page
    pages = {p: frag(range(p * 1000, p * 1000 + 8)) for p in range(2, 56)}
    sess = FakeSession(pages)
    refs = H._collect_ajax(sess, target=10_000)
    assert len(refs) == 24 + 54 * 8, len(refs)
    assert len({r.article_id for r in refs}) == len(refs), "duplicate refs"
    assert sess.posts[0]["action"] == "load_more_opinions"
    assert sess.posts[0]["query"] == '{"post_type":"post"}'
    assert [p["page"] for p in sess.posts] == list(range(2, 56))
    print(f"  full walk: {len(refs)} refs, pages 2..55, query replayed verbatim  OK")

    # 4. empty fragment = clean exhaustion
    sess = FakeSession({2: frag(range(20000, 20008)), 3: ""})
    refs = H._collect_ajax(sess, target=10_000)
    assert len(refs) == 32, len(refs)
    print("  empty fragment -> clean exhaustion  OK")

    # 5. all-seen while more pages advertised = ANOMALY, not a quiet stop
    repeat = frag(range(1000, 1008))          # same ids as page 1
    sess = FakeSession({p: repeat for p in range(2, 56)})
    try:
        H._collect_ajax(sess, target=10_000)
    except H.ScrapeError as e:
        assert "not advancing" in str(e), e
        print("  pagination stuck -> fails loud, no partial-as-complete  OK")
    else:
        raise AssertionError("should have raised on stuck pagination")

    # 6. target caps the walk
    sess = FakeSession({p: frag(range(p * 1000, p * 1000 + 8)) for p in range(2, 56)})
    refs = H._collect_ajax(sess, target=40)
    assert 40 <= len(refs) <= 48, len(refs)
    print(f"  target=40 stops early ({len(refs)} refs)  OK")

    print("\nHOLOD COLLECTOR TEST PASSED")


if __name__ == "__main__":
    main()

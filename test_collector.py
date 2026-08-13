#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Local verification of the Playwright load-more collector loop.

ria.ru is unreachable from the build sandbox, so we serve a fixture that mimics
the REAL Россия Сегодня behaviour observed live:
  - the "Ещё" button PERSISTS in the DOM even when the section is exhausted;
  - the more.html endpoint then returns an EMPTY fragment (the true end signal);
  - the page advertises its size as "N материалов".

We verify: initial parse, click-driven DOM growth, dedup by id, stop-at-target,
CLEAN stop at exhaustion (empty fragment + lingering button — the exact case that
previously mis-fired), and the two genuine FAIL-LOUD paths: an HTTP 5xx from the
loader, and a loader that returns unseen ids which never get injected."""
import http.server
import socketserver
import threading
from urllib.parse import urlparse, parse_qs

import scrape_ria as R

ALL_IDS = list(range(2050000000, 2050000000 - 55, -1))   # 55 articles
PAGE = 5
MODE = "normal"          # normal | http_error | injection_anomaly


def card(aid: int) -> str:
    return (f'<div class="list-item" data-id="{aid}">'
            f'<a class="list-item__title" '
            f'href="https://ria.ru/20251020/slug-{aid}.html">Заголовок {aid}</a></div>')


def page_after(cursor):
    start = 0 if cursor is None else ALL_IDS.index(cursor) + 1
    chunk = ALL_IDS[start:start + PAGE]
    return "".join(card(a) for a in chunk)


# Button is NEVER removed — end is signalled only by an empty fragment, exactly
# like the live site. "55 материалов" gives the advertised-total anchor.
SECTION_HTML = """<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<title>Аналитика</title></head><body>
<div class="rubric-count">{total} материалов</div>
<div class="list" id="list">{initial}</div>
<div class="list-more" id="more" data-id="{cursor}">Ещё 20 материалов</div>
<script>
document.getElementById('more').addEventListener('click', async function() {{
  const btn = document.getElementById('more');
  const r = await fetch('/more.html?id=' + btn.getAttribute('data-id') + '&view=supertag');
  const html = await r.text();
  if ({inject}) {{
    document.getElementById('list').insertAdjacentHTML('beforeend', html);
    const items = document.querySelectorAll('.list-item');
    if (items.length) btn.setAttribute('data-id', items[items.length-1].getAttribute('data-id'));
  }}
  // button intentionally left in the DOM even when nothing came back
}});
</script></body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        p = urlparse(self.path)
        if p.path.startswith("/more.html"):
            if MODE == "http_error":
                self.send_response(503)
                self.end_headers()
                self.wfile.write(b"upstream throttled")
                return
            if MODE == "injection_anomaly":
                # Always returns fresh unseen cards for a FIXED cursor (page never
                # advances) — and the page's JS won't inject them.
                body = page_after(ALL_IDS[PAGE - 1]).encode()
            else:
                cur = int(parse_qs(p.query)["id"][0])
                body = page_after(cur).encode()          # empty near the end
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
        else:
            inject = "false" if MODE == "injection_anomaly" else "true"
            html = SECTION_HTML.format(initial=page_after(None),
                                       cursor=ALL_IDS[PAGE - 1],
                                       total=len(ALL_IDS), inject=inject)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode())


def serve():
    httpd = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def _point(port):
    R.SECTION_URL = f"http://127.0.0.1:{port}/"
    R.LOADMORE_DELAY_SEC = (0.02, 0.05)


def test_target_reached():
    global MODE
    MODE = "normal"
    httpd, port = serve(); _point(port)
    try:
        refs = R._collect_browser(target_total=32)
    finally:
        httpd.shutdown()
    ids = [r.article_id for r in refs]
    assert len(ids) == len(set(ids)), "DUPLICATE ids"
    assert len(refs) >= 32, f"target not reached: {len(refs)}"
    assert all(r.url.startswith("https://ria.ru/") for r in refs)
    print(f"  target_reached: {len(refs)} unique refs (dedup OK)")


def test_clean_exhaustion_button_persists():
    global MODE
    MODE = "normal"
    httpd, port = serve(); _point(port)
    try:
        # Ask for far more than exist (55). Must stop cleanly at the empty
        # fragment / advertised total — NOT raise, even though the button stays.
        refs = R._collect_browser(target_total=500)
    finally:
        httpd.shutdown()
    assert len(refs) == 55, f"expected all 55, got {len(refs)}"
    print(f"  clean_exhaustion: collected all {len(refs)}, button lingered, "
          "no false fail-loud (regression fixed)")


def test_fail_loud_http_error():
    global MODE
    MODE = "http_error"
    httpd, port = serve(); _point(port)
    raised = False
    try:
        R._collect_browser(target_total=500)
    except R.ScrapeError as e:
        raised = True
        print(f"  fail_loud_http: raised on 5xx -> {str(e)[:70]}...")
    finally:
        httpd.shutdown(); MODE = "normal"
    assert raised, "must fail loud on loader HTTP error"


def test_fail_loud_injection_anomaly():
    global MODE
    MODE = "injection_anomaly"
    httpd, port = serve(); _point(port)
    raised = False
    try:
        R._collect_browser(target_total=500)
    except R.ScrapeError as e:
        raised = True
        print(f"  fail_loud_anomaly: raised on unseen-ids-not-injected -> "
              f"{str(e)[:60]}...")
    finally:
        httpd.shutdown(); MODE = "normal"
    assert raised, "must fail loud when unseen ids never appear on the page"


if __name__ == "__main__":
    test_target_reached()
    test_clean_exhaustion_button_persists()
    test_fail_loud_http_error()
    test_fail_loud_injection_anomaly()
    print("\nALL COLLECTOR ASSERTIONS PASSED")

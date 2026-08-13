#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Local verification of the METHOD='http' collector and cursor parsing against a
fixture that mimics RIA's more.html fragment endpoint (cursor = last id)."""
import http.server, socketserver, threading
from urllib.parse import urlparse, parse_qs
import scrape_ria as R

ALL_IDS = list(range(2050000000, 2050000000 - 23, -1))  # 23 articles
PAGE = 5
EMPTY_ENDPOINT = False  # when True, more.html always returns no cards

def card(aid):
    return (f'<div class="list-item" data-id="{aid}">'
            f'<a href="https://ria.ru/20251020/slug-{aid}.html">t{aid}</a></div>')

def frag_after(cursor):
    start = ALL_IDS.index(cursor) + 1
    return "".join(card(a) for a in ALL_IDS[start:start + PAGE])

class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        p = urlparse(self.path)
        if p.path.endswith("/more.html"):
            body = "" if EMPTY_ENDPOINT else frag_after(int(parse_qs(p.query)["id"][0]))
        else:
            initial = "".join(card(a) for a in ALL_IDS[:PAGE])
            body = (f'<html><body><div class="list">{initial}</div>'
                    f'<div class="list-more" data-id="{ALL_IDS[PAGE-1]}">Ещё</div>'
                    f'</body></html>')
        b = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers(); self.wfile.write(b)

def serve():
    httpd = socketserver.TCPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]

def _point(port):
    R.SECTION_URL = f"http://127.0.0.1:{port}/"
    R.MORE_ENDPOINT = R.SECTION_URL.rstrip("/") + "/more.html"
    R.LOADMORE_DELAY_SEC = (0.0, 0.01)

def test_cursor_from_datamore():
    html = ('<div class="list-more" data-id="2050000004">Ещё</div>')
    refs = R.parse_article_refs('<a href="https://ria.ru/20251020/x-2050000004.html">t</a>')
    cid, _ = R._last_cursor(refs, html)
    assert cid == "2050000004", cid
    print("  cursor: reads data-id from list-more button ->", cid)

def test_http_paginates_and_exhausts():
    httpd, port = serve(); _point(port)
    try:
        refs = R._collect_http(target_total=500)  # more than exist
    finally:
        httpd.shutdown()
    ids = [r.article_id for r in refs]
    assert len(ids) == len(set(ids)) == 23, (len(ids), len(set(ids)))
    print(f"  http paginate: collected all {len(refs)}, dedup OK, clean exhaustion")

def test_http_fail_loud():
    global EMPTY_ENDPOINT
    EMPTY_ENDPOINT = True
    httpd, port = serve(); _point(port)
    raised = False
    try:
        R._collect_http(target_total=100)
    except R.ScrapeError as e:
        raised = True
        print("  http fail_loud: raised on empty first more.html ->", str(e)[:70], "...")
    finally:
        httpd.shutdown(); EMPTY_ENDPOINT = False
    assert raised, "http collector must fail loudly, not return page 1"

if __name__ == "__main__":
    test_cursor_from_datamore()
    test_http_paginates_and_exhausts()
    test_http_fail_loud()
    print("\nALL HTTP-COLLECTOR ASSERTIONS PASSED")

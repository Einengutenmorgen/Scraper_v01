#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scrape_cumhuriyet.py — KuKi corpus per-source scraper for Cumhuriyet columnists.

Standalone module. NO shared abstraction with the other TR scrapers
(scrape_sabah / scrape_yenicag / scrape_odatv) — duplication is deliberate and
matches the RU convention (scrape_theinsider / scrape_ng_vision / scrape_holod).

Fixed axis tags for this source
    source            = cumhuriyet
    section           = yazarlar
    orientation       = secular_kemalist_opposition
    factuality_tier   = high_factuality
    genre             = opinion_column

Collection
    Author list:  https://www.cumhuriyet.com.tr/yazarlar/{author-slug}
    ~40 articles render server-side, then a JS "Daha Fazla Yazı Göster"
    load-more. Two supported strategies (operator picks per measured perf):
      (a) HTTP: replay the load-more XHR directly (RIA pattern) — PREFERRED if
          the endpoint is stable/fast. Pass --endpoint "<url-template>" once
          confirmed; the confirmed value is written to
          discovered_endpoint_cumhuriyet.txt.
      (b) Playwright: click the button and read the injected cards (Insider
          pattern). Use --playwright when no stable HTTP endpoint exists.
    End-of-author: load-more returns an empty fragment OR only already-seen
    article IDs -> clean stop. 4xx/5xx or an unseen-id injection that never
    lands -> FAIL LOUD.

Article
    URL:        /yazarlar/{author-slug}/{title-slug}-{numericID}
    article_id: trailing numeric ID
    Meta (confirmed present): meta-datePublished (ISO), meta-articleAuthor,
    meta-articleSection: columnist. Prefer these.
    Body:       <h1> is the title; body runs to the "İlgili Konular:" tag block
                — strip it and everything after (related rail,
                "Yazarın Son Yazıları"). '###' subheadings kept as breaks.

Live-only steps (cannot verify offline — flagged in the run summary):
    * Confirmed load-more endpoint shape / whether Playwright is required.

Run:
    python scrape_cumhuriyet.py --smoke                 # target=15/author, HTTP
    python scrape_cumhuriyet.py --smoke --playwright    # driven browser
    python scrape_cumhuriyet.py --full --authors a,b,c
"""

import argparse
import json
import os
import re
import statistics
import sys
from datetime import datetime, date
from urllib.parse import urljoin, urlsplit, urlunsplit

import trafilatura
from lxml import html as lxml_html

# ----------------------------------------------------------------------------
# Fixed axis tags / source constants
# ----------------------------------------------------------------------------
SOURCE = "cumhuriyet"
SECTION = "yazarlar"
ORIENTATION = "secular_kemalist_opposition"
FACTUALITY_TIER = "high_factuality"
GENRE = "opinion_column"

BASE = "https://www.cumhuriyet.com.tr"
ALLOWED_HOST = "www.cumhuriyet.com.tr"

# High-purity institutional opinion/analysis STREAMS (same /yazarlar/{slug} URL
# shape; confirmed live) — the cleanest Cumhuriyet sources. Per-article byline is
# still read from meta-articleAuthor.
COLLECTIVE_STREAMS = [
    "olaylar-ve-gorusler",        # op-ed page (~5,900 pieces)
    "olaylarin-ardindaki-gercek", # daily political analysis
    "cumhuriyet",                 # institutional editorial / leader
    "konuk-yazarlar",             # guest op-ed stream
]
# Example individual columnists (hyphenated). The streams above lead the default.
DEFAULT_AUTHORS = COLLECTIVE_STREAMS + ["emre-kongar", "ali-sirmen", "ozgur-mumcu"]

# Category listing pages whose authors are OFF-target; subtract from the master
# roster during --discover.
EXCLUDE_CATEGORY_PAGES = ["spor-yazarlari", "yasam-yazarlari"]
# Non-author slugs that appear under /yazarlar/ and must not be treated as
# roster authors during discovery.
_NON_AUTHOR_SLUGS = set(COLLECTIVE_STREAMS) | {
    "spor-yazarlari", "yasam-yazarlari", "konuk-yazarlari",
}
# Sitemap that lists every article URL (gzip) — bypasses the JS load-more so a
# full per-author back-catalogue is reachable.
SITEMAP_POSTS = f"{BASE}/sitemaps/posts.xml"

# Full browser header set. A minimal UA-only set makes Cumhuriyet bounce the
# request through a consent/anti-bot redirect loop (TooManyRedirects); the full
# set + cookie priming (see run()) resolves it.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
    # gzip/deflate only — never advertise 'br'/'zstd' unless the brotli/zstandard
    # package is installed, or requests hands back undecodable bytes and resp.text
    # is garbage (0 article links parsed, no <h1>).
    "Accept-Encoding": "gzip, deflate",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Connection": "keep-alive",
}

LOW_CONTENT_WORDS = 150
SMOKE_TARGET = 15


class ScrapeError(RuntimeError):
    """Raised on any loud failure (HTTP error, structural anomaly)."""


# ----------------------------------------------------------------------------
# Turkish text helpers (locale-independent; NEVER naive .lower())
# ----------------------------------------------------------------------------
_TR_UPPER_TO_LOWER = {
    "I": "ı", "İ": "i", "Ş": "ş", "Ğ": "ğ", "Ç": "ç", "Ö": "ö", "Ü": "ü",
}
_TR_LOWER_TO_UPPER = {
    "ı": "I", "i": "İ", "ş": "Ş", "ğ": "Ğ", "ç": "Ç", "ö": "Ö", "ü": "Ü",
}


def tr_lower(s: str) -> str:
    """Turkish-correct lowercasing via explicit mapping (I->ı, İ->i, ...)."""
    out = []
    for ch in s:
        if ch in _TR_UPPER_TO_LOWER:
            out.append(_TR_UPPER_TO_LOWER[ch])
        else:
            out.append(ch.lower())
    return "".join(out)


def tr_upper(s: str) -> str:
    """Turkish-correct uppercasing via explicit mapping (i->İ, ı->I, ...)."""
    out = []
    for ch in s:
        if ch in _TR_LOWER_TO_UPPER:
            out.append(_TR_LOWER_TO_UPPER[ch])
        else:
            out.append(ch.upper())
    return "".join(out)


def norm_key(s: str) -> str:
    """Normalized comparison key for dedup: TR-lowered, whitespace-collapsed."""
    return re.sub(r"\s+", " ", tr_lower(s)).strip()


# ----------------------------------------------------------------------------
# Turkish date parsing (explicit month map; never system locale)
# ----------------------------------------------------------------------------
TR_MONTHS = {
    "ocak": 1, "şubat": 2, "mart": 3, "nisan": 4, "mayıs": 5, "haziran": 6,
    "temmuz": 7, "ağustos": 8, "eylül": 9, "ekim": 10, "kasım": 11,
    "aralık": 12,
}

_LONGFORM_RE = re.compile(
    r"(\d{1,2})\s+([A-Za-zÇĞİıÖŞÜçğşöü]+)\s+(\d{4})"
)


def parse_iso_date(value: str):
    """Parse an ISO-8601 datetime/date string -> 'YYYY-MM-DD' or None."""
    if not value:
        return None
    v = value.strip()
    # Trailing Z or offset handling.
    v = v.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(v).date().isoformat()
    except ValueError:
        # Bare date already?
        try:
            return date.fromisoformat(v[:10]).isoformat()
        except ValueError:
            return None


def parse_tr_longform_date(text: str):
    """Parse '24 Temmuz 2026' -> '2026-07-24' or None."""
    if not text:
        return None
    m = _LONGFORM_RE.search(text)
    if not m:
        return None
    day, month_word, year = m.group(1), tr_lower(m.group(2)), m.group(3)
    month = TR_MONTHS.get(month_word)
    if not month:
        return None
    try:
        return date(int(year), month, int(day)).isoformat()
    except ValueError:
        return None


def parse_date(iso_meta: str = "", longform_text: str = "") -> str:
    """Prefer ISO meta; fall back to Turkish long-form. FAIL LOUD if neither."""
    iso = parse_iso_date(iso_meta) if iso_meta else None
    if iso:
        return iso
    lf = parse_tr_longform_date(longform_text) if longform_text else None
    if lf:
        return lf
    raise ScrapeError(
        f"Unparseable date (iso_meta={iso_meta!r}, longform={longform_text!r})"
    )


# ----------------------------------------------------------------------------
# Link scoping (pure)
# ----------------------------------------------------------------------------
# /yazarlar/{author-slug}/{title-slug}-{numericID}
_ARTICLE_RE = re.compile(r"^/yazarlar/[^/]+/[^/]+-(\d+)/?$")


def normalize_url(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme or "https", parts.netloc, path, "", ""))


def scope_link(href: str, base: str = BASE):
    """Return article_id if href is an in-scope Cumhuriyet column, else None.

    Rejects cross-domain links and the author-root (no article segment).
    """
    if not href:
        return None
    absolute = urljoin(base + "/", href)
    parts = urlsplit(absolute)
    if parts.netloc and parts.netloc != ALLOWED_HOST:
        return None
    m = _ARTICLE_RE.match(parts.path)
    if not m:
        return None
    return m.group(1)


def parse_article_cards(fragment_html: str, base: str = BASE):
    """Parse an author-list page or load-more fragment.

    Returns a list of (absolute_url, article_id), de-duped in-order by id.
    Pure/offline-testable.
    """
    doc = lxml_html.fromstring(fragment_html)
    out = []
    seen = set()
    for a in doc.xpath("//a[@href]"):
        aid = scope_link(a.get("href"), base=base)
        if not aid or aid in seen:
            continue
        seen.add(aid)
        out.append((normalize_url(urljoin(base + "/", a.get("href"))), aid))
    return out


# ----------------------------------------------------------------------------
# Roster discovery + sitemap enumeration (opinion-isolating entry points)
# ----------------------------------------------------------------------------
_ROSTER_RE = re.compile(r"^/yazarlar/([^/]+)/?$")


def parse_roster_authors(list_html: str, base: str = BASE):
    """Author slugs from a roster / category listing page (/yazarlar or
    /yazarlar/spor-yazarlari). Excludes streams/category slugs. Pure."""
    doc = lxml_html.fromstring(list_html)
    slugs, seen = [], set()
    for a in doc.xpath("//a[@href]"):
        parts = urlsplit(urljoin(base + "/", a.get("href")))
        if parts.netloc and parts.netloc != ALLOWED_HOST:
            continue
        m = _ROSTER_RE.match(parts.path)
        if not m:
            continue
        slug = m.group(1)
        if slug in _NON_AUTHOR_SLUGS or slug in seen:
            continue
        seen.add(slug)
        slugs.append(slug)
    return slugs


_SITEMAP_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)
_PAZAR_RE = re.compile(r"^/pazar-yazilari/[^/]+-(\d+)/?$")


def parse_sitemap_urls(xml_text: str, exclude_authors=()):
    """From a posts sitemap, return (url, article_id) for opinion articles only
    — paths under /yazarlar/ (dropping excluded authors) and /pazar-yazilari/.
    Pure/offline-testable."""
    exclude = set(exclude_authors)
    out, seen = [], set()
    for loc in _SITEMAP_LOC_RE.findall(xml_text):
        parts = urlsplit(loc.strip())
        path = parts.path
        aid = None
        if path.startswith("/yazarlar/"):
            m = _ARTICLE_RE.match(path)
            if not m:
                continue
            author = path.split("/")[2]
            if author in exclude:
                continue
            aid = m.group(1)
        elif path.startswith("/pazar-yazilari/"):
            m = _PAZAR_RE.match(path)
            if not m:
                continue
            aid = m.group(1)
        else:
            continue
        if aid in seen:
            continue
        seen.add(aid)
        out.append((normalize_url(loc), aid))
    return out


# ----------------------------------------------------------------------------
# Pagination / load-more end detection (pure)
# ----------------------------------------------------------------------------
def collection_verdict(new_ids, seen_ids):
    """Decide load-more continuation.

    new_ids  : ids parsed from the latest fragment
    seen_ids : set of ids already collected (before this fragment)
    Returns 'stop_empty' | 'stop_all_seen' | 'continue'.
    """
    if not new_ids:
        return "stop_empty"
    if all(i in seen_ids for i in new_ids):
        return "stop_all_seen"
    return "continue"


# ----------------------------------------------------------------------------
# Extraction
# ----------------------------------------------------------------------------
_BODY_CUT_MARKERS = ["İlgili Konular", "Yazarın Son Yazıları"]

_ARTIFACT_LINES = re.compile(
    r"^\s*(reklam|advertisement|paylaş|whatsapp|twitter|facebook|"
    r"cumhuriyet\.com\.tr|abone ol|yorumlar)\s*$",
    re.IGNORECASE,
)


def truncate_at_markers(text: str, markers=_BODY_CUT_MARKERS) -> str:
    """Cut text at the first occurrence of any marker (marker line removed)."""
    cut = len(text)
    low = tr_lower(text)
    for mk in markers:
        idx = low.find(tr_lower(mk))
        if idx != -1:
            cut = min(cut, idx)
    return text[:cut].rstrip()


def strip_residual_artifacts(text: str) -> str:
    """Cumhuriyet-specific residual boilerplate removal. Fix at extraction."""
    kept = []
    for line in text.splitlines():
        if _ARTIFACT_LINES.match(line):
            continue
        kept.append(line)
    # collapse 3+ blank lines to a single paragraph break
    cleaned = "\n".join(kept)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _meta(doc, name=None, prop=None, itemprop=None):
    if name:
        v = doc.xpath(f"//meta[@name={name!r}]/@content")
        if v:
            return v[0].strip()
    if prop:
        v = doc.xpath(f"//meta[@property={prop!r}]/@content")
        if v:
            return v[0].strip()
    if itemprop:
        v = doc.xpath(f"//meta[@itemprop={itemprop!r}]/@content")
        if v:
            return v[0].strip()
    return ""


def extract_article(article_html: str, url: str) -> dict:
    """Extract title/subtitle/body/date/author from a Cumhuriyet article page.

    Pure/offline-testable given the raw HTML string.
    """
    doc = lxml_html.fromstring(article_html)

    # Title: <h1> preferred, else og:title.
    h1 = doc.xpath("//h1//text()")
    title = " ".join(t.strip() for t in h1 if t.strip()).strip()
    if not title:
        title = _meta(doc, prop="og:title")
    if not title:
        raise ScrapeError(f"No <h1>/og:title for {url}")

    # Subtitle: meta description / spot.
    subtitle = _meta(doc, name="description") or _meta(doc, prop="og:description")

    # Date: meta-datePublished (ISO) preferred; long-form text fallback.
    iso_meta = (
        _meta(doc, itemprop="datePublished")
        or _meta(doc, name="datePublished")
        or _meta(doc, prop="article:published_time")
    )
    longform = " ".join(doc.xpath("//time//text()")).strip()
    date_iso = parse_date(iso_meta=iso_meta, longform_text=longform)

    # Author: meta-articleAuthor preferred.
    author = (
        _meta(doc, name="articleAuthor")
        or _meta(doc, itemprop="author")
        or _meta(doc, prop="article:author")
    )

    # Body via trafilatura (markdown keeps ### subheads), then boundary cut.
    body = trafilatura.extract(
        article_html,
        include_tables=False,
        include_comments=False,
        include_images=False,
        favor_recall=True,
        output_format="markdown",
        url=url,
    ) or ""
    body = truncate_at_markers(body)
    body = strip_residual_artifacts(body)
    # Drop a leading duplicate of the title if trafilatura included it.
    body = _drop_leading_title(body, title)

    if not body.strip():
        raise ScrapeError(f"Empty body after extraction for {url}")

    has_byline = bool(author and re.search(r"[A-Za-zÇĞİıÖŞÜçğşöü]{2,}\s+"
                                           r"[A-Za-zÇĞİıÖŞÜçğşöü]{2,}", author))
    return {
        "title": title,
        "subtitle": subtitle,
        "body": body,
        "date": date_iso,
        "author": author,
        "has_byline": has_byline,
        "stated_reading_time": _stated_reading_time(doc),
    }


def _drop_leading_title(body: str, title: str) -> str:
    lines = body.splitlines()
    if not lines:
        return body
    first = lines[0].lstrip("# ").strip()
    if norm_key(first) == norm_key(title):
        return "\n".join(lines[1:]).lstrip("\n")
    return body


def _stated_reading_time(doc):
    txt = " ".join(doc.xpath("//*[contains(text(),'dakika')]/text()"))
    m = re.search(r"(\d+)\s*dakika", txt)
    return int(m.group(1)) if m else None


# ----------------------------------------------------------------------------
# Metrics (language-independent quality signals)
# ----------------------------------------------------------------------------
_SENT_RE = re.compile(r"[.!?…]+(?:\s|$)")


def split_paragraphs(body: str):
    """Default paragraph split: blank-line separated."""
    return [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]


def compute_metrics(title: str, body: str) -> dict:
    paras = split_paragraphs(body)
    prose = [p for p in paras if not p.lstrip().startswith("#")
             and len(p.split()) >= 4]
    words = body.split()
    prose_word_counts = [len(p.split()) for p in prose]
    mean_para = (sum(prose_word_counts) / len(prose_word_counts)
                 if prose_word_counts else 0.0)
    sentences = len(_SENT_RE.findall(body))
    return {
        "char_count": len(body),
        "word_count": len(words),
        "paragraph_count": len(paras),
        "prose_paragraph_count": len(prose),
        "mean_paragraph_len": round(mean_para, 2),
        "sentence_count": sentences,
    }


# ----------------------------------------------------------------------------
# Record assembly
# ----------------------------------------------------------------------------
def build_record(url: str, article_id: str, extracted: dict) -> dict:
    title = extracted["title"]
    body = extracted["body"]
    content = title + "\n\n" + body  # exact $content field for Label Studio
    metrics = compute_metrics(title, body)
    rec = {
        "url": url,
        "article_id": article_id,
        "date": extracted["date"],
        "source": SOURCE,
        "section": SECTION,
        "orientation": ORIENTATION,
        "factuality_tier": FACTUALITY_TIER,
        "genre": GENRE,
        "title": title,
        "subtitle": extracted.get("subtitle", ""),
        "body": body,
        "content": content,
        "author": extracted.get("author", ""),
        "has_byline": extracted.get("has_byline", False),
        "stated_reading_time": extracted.get("stated_reading_time"),
    }
    rec.update(metrics)
    return rec


# ----------------------------------------------------------------------------
# Raw store
# ----------------------------------------------------------------------------
def save_raw_html(article_id: str, html_text: str, raw_dir: str) -> str:
    dest_dir = os.path.join(raw_dir, SOURCE)
    os.makedirs(dest_dir, exist_ok=True)
    path = os.path.join(dest_dir, f"{article_id}.html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html_text)
    return path


# ----------------------------------------------------------------------------
# Live collection (HTTP + Playwright) — exercised in the operator env
# ----------------------------------------------------------------------------
def _encode_url(url: str) -> str:
    """Percent-encode non-ASCII in the path (â, …, curly quotes) so the server
    doesn't bounce the request into a canonicalization redirect loop."""
    p = urlsplit(url)
    from urllib.parse import quote
    return urlunsplit((p.scheme, p.netloc, quote(p.path, safe="/%-._~"),
                       p.query, p.fragment))


def _http_get(url: str, session, referer: str = None) -> str:
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer
        headers["Sec-Fetch-Site"] = "same-origin"
    url = _encode_url(url)
    try:
        resp = session.get(url, headers=headers, timeout=30, allow_redirects=True)
    except Exception as exc:  # requests.exceptions.TooManyRedirects et al.
        if exc.__class__.__name__ == "TooManyRedirects":
            raise ScrapeError(
                f"Redirect loop for {url} — Cumhuriyet is bouncing the request "
                f"(consent/anti-bot gate). Full browser headers + homepage "
                f"cookie priming are sent; if it still loops, run with "
                f"--playwright."
            ) from exc
        raise ScrapeError(f"Request failed for {url}: {exc}") from exc
    if resp.status_code >= 400:
        raise ScrapeError(f"HTTP {resp.status_code} for {url}")
    text = resp.text
    ce = resp.headers.get("Content-Encoding", "")
    head = text[:3000]
    if ("<" not in head[:2000]) and ("br" in ce or "zstd" in ce):
        raise ScrapeError(
            f"Undecodable response for {url} (Content-Encoding={ce!r}). "
            f"Install 'brotli'/'zstandard' or keep 'br' out of Accept-Encoding."
        )
    if not re.search(r"<(html|meta|body|div|article|h1)", head, re.I):
        raise ScrapeError(
            f"Response for {url} is not HTML (len={len(text)}, "
            f"Content-Encoding={ce!r}) — likely undecodable or an interstitial."
        )
    return text


def collect_author_http(author: str, limit: int, session,
                        endpoint_template: str = None,
                        max_pages: int = 200):
    """Collect article (url, id) pairs for one author over HTTP load-more.

    endpoint_template: a confirmed load-more URL with a '{page}' placeholder,
    e.g. '.../yazarlar/{author}/daha-fazla?page={page}'. When None, only the
    server-rendered first page is collected (live discovery required).
    Returns (pairs, ended_reason).
    """
    first_url = f"{BASE}/yazarlar/{author}"
    pairs = []
    seen = set()
    for u, aid in parse_article_cards(_http_get(first_url, session)):
        if aid not in seen:
            seen.add(aid)
            pairs.append((u, aid))
    if endpoint_template is None:
        return pairs[:limit], "first_page_only(no_endpoint)"

    # Record the confirmed endpoint for provenance.
    try:
        with open("discovered_endpoint_cumhuriyet.txt", "w",
                  encoding="utf-8") as fh:
            fh.write(endpoint_template + "\n")
    except OSError:
        pass

    page = 2
    while len(pairs) < limit and page <= max_pages:
        frag = _http_get(
            endpoint_template.format(author=author, page=page), session
        )
        new = parse_article_cards(frag)
        verdict = collection_verdict([a for _, a in new], seen)
        if verdict in ("stop_empty", "stop_all_seen"):
            return pairs[:limit], verdict
        for u, aid in new:
            if aid not in seen:
                seen.add(aid)
                pairs.append((u, aid))
        page += 1
    return pairs[:limit], "target_reached" if len(pairs) >= limit else "max_pages"


def collect_author_playwright(author: str, limit: int, max_clicks: int = 100):
    """Playwright fallback: click 'Daha Fazla Yazı Göster' until exhausted.

    Fail-loud contract: a click that injects NO new cards while the button is
    still present is a real anomaly (unseen-id injection failure) -> raise.
    An absent button OR an all-seen injection is a clean stop.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - env dependent
        raise ScrapeError("Playwright not installed; use HTTP --endpoint") from exc

    pairs = []
    seen = set()
    with sync_playwright() as pw:  # pragma: no cover - live only
        browser = pw.chromium.launch()
        page = browser.new_page(extra_http_headers=HEADERS)
        page.goto(f"{BASE}/yazarlar/{author}", wait_until="domcontentloaded")
        clicks = 0
        while len(pairs) < limit and clicks <= max_clicks:
            for u, aid in parse_article_cards(page.content()):
                if aid not in seen:
                    seen.add(aid)
                    pairs.append((u, aid))
            btn = page.query_selector(
                "text=Daha Fazla Yazı Göster"
            )
            if not btn:
                browser.close()
                return pairs[:limit], "no_more_button"
            before = len(seen)
            btn.click()
            page.wait_for_timeout(1500)
            after_pairs = parse_article_cards(page.content())
            after_ids = [a for _, a in after_pairs]
            if all(i in seen for i in after_ids):
                browser.close()
                return pairs[:limit], "stop_all_seen"
            if len(set(after_ids)) <= before:
                browser.close()
                raise ScrapeError(
                    "Load-more clicked but no new ids injected (anomaly)"
                )
            clicks += 1
        browser.close()
    return pairs[:limit], "target_reached"


# ----------------------------------------------------------------------------
# Run summary
# ----------------------------------------------------------------------------
def histogram(values, bins):
    counts = [0] * (len(bins) - 1)
    for v in values:
        for i in range(len(bins) - 1):
            if bins[i] <= v < bins[i + 1] or (i == len(bins) - 2
                                              and v == bins[-1]):
                counts[i] += 1
                break
    return counts


def build_summary(records, ended_reasons, live_notes, skipped=()):
    wc = [r["word_count"] for r in records]
    pc = [r["paragraph_count"] for r in records]
    low = [r["article_id"] for r in records
           if r["word_count"] < LOW_CONTENT_WORDS]

    def dist(vals):
        if not vals:
            return {"min": None, "median": None, "max": None}
        return {"min": min(vals), "median": statistics.median(vals),
                "max": max(vals)}

    wc_bins = [0, 150, 300, 500, 800, 1200, 2000, 100000]
    pc_bins = [0, 3, 6, 10, 15, 25, 1000]
    return {
        "source": SOURCE,
        "n_records": len(records),
        "word_count": {
            **dist(wc),
            "hist_bins": wc_bins,
            "hist": histogram(wc, wc_bins),
        },
        "paragraph_count": {
            **dist(pc),
            "hist_bins": pc_bins,
            "hist": histogram(pc, pc_bins),
        },
        "low_content_ids": low,
        "n_low_content": len(low),
        "n_skipped": len(skipped),
        "skipped": list(skipped)[:100],
        "ended_reasons": ended_reasons,
        "live_only_unverified_offline": live_notes,
    }


def print_summary(summary):
    s = summary
    print("=" * 68)
    print(f"RUN SUMMARY — {s['source']}  ({s['n_records']} records)")
    print("=" * 68)
    for field in ("word_count", "paragraph_count"):
        d = s[field]
        print(f"\n{field}: min={d['min']} median={d['median']} max={d['max']}")
        bins, hist = d["hist_bins"], d["hist"]
        for i, c in enumerate(hist):
            lo, hi = bins[i], bins[i + 1]
            bar = "█" * c
            print(f"  [{lo:>5}–{hi:<6}) {c:>4} {bar}")
    print(f"\nlow_content_ids (word_count<{LOW_CONTENT_WORDS}): "
          f"{s['n_low_content']}")
    if s["low_content_ids"]:
        print("  " + ", ".join(s["low_content_ids"]))
    print(f"\nskipped (per-article fetch/extract failures): {s['n_skipped']}")
    for sk in s["skipped"][:15]:
        print(f"  {sk['id']}: {sk['reason']}")
    if s["n_records"] and s["word_count"]["median"] is not None \
            and s["word_count"]["median"] < 300:
        print("\n  ** WARNING: low median word_count — section or extraction "
              "may be wrong (healthy columnist harvest skews long). **")
    print("\nended_reasons:", json.dumps(s["ended_reasons"],
                                         ensure_ascii=False))
    print("live-only (unverified offline):")
    for note in s["live_only_unverified_offline"]:
        print("  -", note)
    print("=" * 68)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
LIVE_NOTES = [
    "Cumhuriyet load-more endpoint shape / whether Playwright is required "
    "(pass --endpoint once captured; recorded to "
    "discovered_endpoint_cumhuriyet.txt).",
    "Sitemap (--sitemap) posts.xml gz size / index shape — enumerated live.",
]


def _fetch_exclude_authors(session):
    """Authors on the off-target category listing pages (sport, lifestyle)."""
    exclude = set()
    for cat in EXCLUDE_CATEGORY_PAGES:
        try:
            exclude |= set(parse_roster_authors(
                _http_get(f"{BASE}/yazarlar/{cat}", session)))
        except ScrapeError:
            pass
    return exclude


def discover_opinion_authors(session):
    """Master roster minus the sport/lifestyle category authors + streams."""
    roster = set(parse_roster_authors(_http_get(f"{BASE}/yazarlar", session)))
    authors = sorted(roster - _fetch_exclude_authors(session))
    return COLLECTIVE_STREAMS + authors


def collect_via_sitemap(session, limit, exclude_authors=(),
                        sitemap_url=SITEMAP_POSTS, _depth=0):
    """Enumerate opinion article URLs from posts.xml (gz), bypassing the JS
    load-more. Recurses one level into a sitemap index. Returns (url, id)."""
    import gzip
    resp = session.get(sitemap_url, headers=HEADERS, timeout=60)
    if resp.status_code >= 400:
        raise ScrapeError(f"HTTP {resp.status_code} for {sitemap_url}")
    try:
        xml = gzip.decompress(resp.content).decode("utf-8", "replace")
    except (OSError, EOFError):
        xml = resp.content.decode("utf-8", "replace")
    if "<sitemapindex" in xml[:2000].lower() and _depth < 2:
        pairs, seen = [], set()
        for child in _SITEMAP_LOC_RE.findall(xml):
            for u, aid in collect_via_sitemap(session, limit, exclude_authors,
                                              child.strip(), _depth + 1):
                if aid not in seen:
                    seen.add(aid)
                    pairs.append((u, aid))
            if len(pairs) >= limit:
                break
        return pairs[:limit]
    return parse_sitemap_urls(xml, exclude_authors)[:limit]


def run(authors, limit, out_path, raw_dir, endpoint, use_playwright,
        discover=False, use_sitemap=False):
    import requests
    session = requests.Session()
    # Prime consent/session cookies from the homepage before article fetches —
    # this is what breaks the redirect loop under plain requests.
    try:
        session.get(BASE, headers=HEADERS, timeout=30, allow_redirects=True)
    except Exception:
        pass
    records, ended, skipped = [], {}, []
    seen_global = set()   # dedup by article_id across all authors/streams

    def _fetch_one(url, aid, referer=None):
        """Fetch+extract+build one article; a per-article failure is RECORDED
        (loud, not silent) and skipped so one bad article can't abort a large
        harvest. Systemic errors (section 404, decode guard) still raise."""
        if aid in seen_global:      # sidebar/"most read" rails repeat articles
            return
        seen_global.add(aid)
        try:
            html_text = _http_get(url, session, referer=referer)
            save_raw_html(aid, html_text, raw_dir)
            records.append(build_record(url, aid, extract_article(html_text, url)))
        except ScrapeError as exc:
            skipped.append({"id": aid, "url": url, "reason": str(exc)[:200]})
            print(f"[skip] {aid}: {exc}", file=sys.stderr)

    if discover:
        authors = discover_opinion_authors(session)
        print(f"[discover] {len(authors)} opinion authors/streams",
              file=sys.stderr)

    if use_sitemap:
        exclude = _fetch_exclude_authors(session)
        pairs = collect_via_sitemap(session, limit, exclude)
        ended["__sitemap__"] = (f"{len(pairs)} urls from posts.xml "
                                f"(excluded {len(exclude)} off-target authors)")
        for url, aid in pairs:
            _fetch_one(url, aid)
    else:
        for author in authors:
            author_url = f"{BASE}/yazarlar/{author}"
            if use_playwright:
                pairs, reason = collect_author_playwright(author, limit)
            else:
                pairs, reason = collect_author_http(
                    author, limit, session, endpoint_template=endpoint
                )
            ended[author] = reason
            for url, aid in pairs:
                _fetch_one(url, aid, referer=author_url)

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    summary = build_summary(records, ended, LIVE_NOTES, skipped)
    with open(out_path + ".summary.json", "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)
    print_summary(summary)
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser(description="Cumhuriyet columnist scraper")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--smoke", action="store_true",
                      help=f"first-run mode, target={SMOKE_TARGET}/author")
    mode.add_argument("--full", action="store_true", help="full harvest")
    ap.add_argument("--authors", default=",".join(DEFAULT_AUTHORS),
                    help="comma-separated author slugs OR a path to a file "
                         "(one slug per line)")
    ap.add_argument("--endpoint", default=None,
                    help="confirmed HTTP load-more template with {author}/{page}")
    ap.add_argument("--playwright", action="store_true",
                    help="drive the load-more with Playwright instead of HTTP")
    ap.add_argument("--discover", action="store_true",
                    help="discover opinion authors = master roster minus the "
                         "sport/lifestyle category pages (+ institutional streams)")
    ap.add_argument("--sitemap", action="store_true",
                    help="enumerate all opinion articles from posts.xml (full "
                         "depth; bypasses the JS load-more)")
    ap.add_argument("--out", default="out/cumhuriyet.jsonl")
    ap.add_argument("--raw-dir", default="raw_store")
    args = ap.parse_args(argv)

    limit = SMOKE_TARGET if not args.full else 100000
    if os.path.isfile(args.authors):
        with open(args.authors, encoding="utf-8") as fh:
            authors = [ln.strip() for ln in fh if ln.strip()
                       and not ln.startswith("#")]
    else:
        authors = [a.strip() for a in args.authors.split(",") if a.strip()]

    try:
        run(authors, limit, args.out, args.raw_dir, args.endpoint,
            args.playwright, discover=args.discover, use_sitemap=args.sitemap)
    except ScrapeError as exc:
        print(f"FAIL LOUD: {exc}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()

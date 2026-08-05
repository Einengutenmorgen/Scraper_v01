#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RIA Novosti — "Аналитика" (Analytics) section scraper.

Single-source corpus collector for the section:
    https://ria.ru/ria-novosti-analitika/

This module is DELIBERATELY NOT GENERALIZED. It targets exactly one section of
one site. Every selector, endpoint shape and axis tag below is specific to RIA
Novosti Analytics and is not meant to be reused for other sources.

The output feeds a hand-labeled research corpus for narrative / framing
annotation. The operator does NOT read Russian, so the scraper's first job is to
capture language-independent, structural QUALITY SIGNALS (length, paragraphing,
sentence density, signed-vs-institutional byline) that let article quality be
judged WITHOUT reading the body. Raw text capture is secondary to getting those
signals right.

===========================================================================
 DISCOVERY NOTES  (what was empirically confirmed vs. what was inferred)
===========================================================================

CONFIRMED (via live inspection of https://ria.ru/ria-novosti-analitika/):

  * The initial section HTML contains a batch of ~20 article links, all of the
    form:
          https://ria.ru/YYYYMMDD/<slug>-<ID>.html
    (8-digit date, slug, then a 10-digit numeric article ID, then ".html").
    Confirmed examples actually seen on the live page:
          https://ria.ru/20260317/iran-2081087907.html      ("Мир после Ирана")
          https://ria.ru/20251024/ukraina-2050229360.html   ("Перепрошивание Украины...")
          https://ria.ru/20251020/vvp-2049278815.html        (last card of batch 1)

  * At the bottom of the list there is a JavaScript "Ещё 20 материалов"
    (load 20 more) button. It is NOT URL pagination — there is no ?page=N that
    returns section item 21+. It is an XHR-driven, cursor-based "load items
    older than ID X" loader, which is the standard Россия Сегодня CMS
    ("Версия 2023.1") list-loader behaviour.

  * robots.txt (https://ria.ru/robots.txt) disallows, for all agents:
          Disallow: /*/?*
          Disallow: /*/*?*        <- every query-string URL
          Disallow: /services/
    The load-more request is a query-string endpoint (it carries the cursor id
    as a parameter), which is exactly why it is covered by these rules. Static
    article pages (no query string) are NOT disallowed and load fine.

CONFIRMED AT RUNTIME (captured live by the browser collector, 2026):

  * The load-more XHR is a GET returning an HTML FRAGMENT of the next article
    cards (not JSON), cursor = last-seen id + its full timestamp:
          GET https://ria.ru/services/ria-novosti-analitika/more.html
              ?id=<LAST_ID>&date=<YYYYMMDDTHHMMSS>&view=supertag
    (under /services/, and with view=supertag — the earlier inferred
    ".../ria-novosti-analitika/more.html?id=&date=" shape was wrong.)

  * END-OF-SECTION BEHAVIOUR (important): this section is SMALL — its page
    advertises its size as "N материалов" (~75). When exhausted, RIA does NOT
    remove the "Ещё" button; it leaves it in the DOM and further clicks return an
    EMPTY fragment. So "button present + 0 new" is NOT a failure at the bottom.
    The browser collector therefore treats as a CLEAN end-of-section: reaching
    the advertised "N материалов", an empty/duplicate load-more fragment, or a
    click that fires no request. It still FAILS LOUD on a 4xx/5xx from the
    endpoint, or on a fragment that carries UNSEEN ids which then fail to appear
    on the page (a real cursor/injection anomaly) — never silently short.

  * DEFAULT is METHOD="browser": headless Chromium clicks "Ещё", reads the
    load-more RESPONSE body as the authoritative end signal, and records the
    observed endpoint URLs to `discovered_endpoint.txt`. METHOD="http" hits the
    confirmed endpoint directly (faster); its cursor timestamp is read from the
    list-more button's data-date.

===========================================================================
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import statistics
import sys
import time
from dataclasses import dataclass, asdict, field
from typing import Iterable, Optional

import requests
import trafilatura
from bs4 import BeautifulSoup

# ===========================================================================
# CONFIG CONSTANTS  (single-source; edit here, no CLI needed for these)
# ===========================================================================

SECTION_URL = "https://ria.ru/ria-novosti-analitika/"

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
OUTPUT_JSONL = os.path.join(OUTPUT_DIR, "ria_analitika.jsonl")
RAW_HTML_DIR = os.path.join(OUTPUT_DIR, "raw_html")           # raw per-article HTML
RUN_SUMMARY_JSON = os.path.join(OUTPUT_DIR, "run_summary.json")
DISCOVERED_ENDPOINT_FILE = os.path.join(OUTPUT_DIR, "discovered_endpoint.txt")

# How many EXTRA articles beyond the initial page to collect (operator's "i").
DEFAULT_EXTRA_ARTICLES = 40

# Collection mechanism: "browser" (Playwright, reliable + self-documenting) or
# "http" (direct more.html requests; faster but endpoint shape is unverified).
DEFAULT_METHOD = "browser"

# Politeness / robustness. RIA is a DDoS target and throttles aggressively.
REQUEST_DELAY_SEC = (1.5, 3.0)     # random uniform delay between article fetches
LOADMORE_DELAY_SEC = (1.5, 3.0)    # delay between load-more calls
HTTP_TIMEOUT = 30
MAX_RETRIES = 4
BACKOFF_BASE = 2.0

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
BASE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

# CONFIRMED load-more endpoint (captured live from the browser collector, 2026):
#   https://ria.ru/services/ria-novosti-analitika/more.html
#       ?id=<LAST_ID>&date=<YYYYMMDDTHHMMSS>&view=supertag
# Note it lives under /services/ (robots-disallowed for polite crawlers) and
# carries a FULL timestamp for the cursor plus view=supertag. Used by METHOD="http".
MORE_ENDPOINT = "https://ria.ru/services/ria-novosti-analitika/more.html"
MORE_VIEW = "supertag"

# Fixed provenance / axis tags for THIS source. Constant on every record.
PROVENANCE = {
    "source": "ria_novosti",
    "section": "analitika",
    "orientation": "state_aligned",
    "factuality_tier": "disinfo_prone",   # anchors mainstream / disinfo-prone quadrant
    "genre": "analysis_essay",
}

INSTITUTIONAL_BYLINE = "РИА Новости"

# Article URL pattern: https://ria.ru/YYYYMMDD/<slug>-<ID>.html
ARTICLE_RE = re.compile(
    r"https?://ria\.ru/(?P<date>\d{8})/(?P<slug>[^/\"'<>\s]+?)-(?P<id>\d+)\.html"
)

INITIAL_BATCH_HINT = 20   # informational: the section's first batch size


# ===========================================================================
# Small utilities
# ===========================================================================

def _sleep(bounds: tuple[float, float]) -> None:
    time.sleep(random.uniform(*bounds))


def _ensure_dirs() -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(RAW_HTML_DIR, exist_ok=True)


class ScrapeError(RuntimeError):
    """Raised to FAIL LOUDLY on unexpected structures / HTTP errors."""


# ===========================================================================
# Link / cursor parsing
# ===========================================================================

@dataclass
class ArticleRef:
    url: str
    article_id: str
    date: str          # YYYYMMDD as it appears in the URL


def parse_article_refs(html: str) -> list[ArticleRef]:
    """Extract every article reference matching the RIA pattern, de-duplicated
    by article_id, preserving first-seen order."""
    seen: set[str] = set()
    refs: list[ArticleRef] = []
    for m in ARTICLE_RE.finditer(html):
        aid = m.group("id")
        if aid in seen:
            continue
        seen.add(aid)
        refs.append(
            ArticleRef(url=m.group(0), article_id=aid, date=m.group("date"))
        )
    return refs


_SECTION_TOTAL_RE = re.compile(r"(\d[\d\s ]*)\s*материал")


def _parse_section_total(html: str) -> Optional[int]:
    """Parse the section's advertised item count ('N материалов'). Used as an
    authoritative end-of-section anchor. Returns None if not present."""
    m = _SECTION_TOTAL_RE.search(html)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(1))
    return int(digits) if digits else None


def _last_cursor(refs: list[ArticleRef], html: str) -> tuple[str, str]:
    """Return (id, date) to use as the 'load older than' cursor for the next
    more-request. Prefers an explicit data-id/data-date on the list-more button
    if present; otherwise falls back to the last article ref on the page."""
    soup = BeautifulSoup(html, "html.parser")
    more = soup.find(attrs={"class": re.compile(r"\blist-more\b")})
    if more is not None:
        cid = more.get("data-id") or more.get("data-next-id")
        cdate = more.get("data-date") or more.get("data-next-date")
        if cid:
            return str(cid), str(cdate or (refs[-1].date if refs else ""))
    if not refs:
        raise ScrapeError("Cannot determine load-more cursor: no article refs on page.")
    last = refs[-1]
    return last.article_id, last.date


# ===========================================================================
# HTTP session with retry/backoff
# ===========================================================================

def _build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(BASE_HEADERS)
    return s


def _get(session: requests.Session, url: str, *, params: Optional[dict] = None,
         referer: Optional[str] = None) -> requests.Response:
    headers = {}
    if referer:
        headers["Referer"] = referer
    last_exc: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, params=params, headers=headers,
                               timeout=HTTP_TIMEOUT)
            # Retry on transient server / throttle codes; raise on the rest.
            if resp.status_code in (429, 500, 502, 503, 504):
                raise ScrapeError(f"transient HTTP {resp.status_code} for {resp.url}")
            resp.raise_for_status()
            return resp
        except (requests.RequestException, ScrapeError) as exc:
            last_exc = exc
            if attempt == MAX_RETRIES:
                break
            backoff = BACKOFF_BASE ** attempt + random.uniform(0, 1)
            time.sleep(backoff)
    raise ScrapeError(f"GET failed after {MAX_RETRIES} attempts: {url} ({last_exc})")


# ===========================================================================
# COLLECTION — METHOD="browser"  (Playwright; reliable + self-documenting)
# ===========================================================================

def _collect_browser(target_total: int) -> list[ArticleRef]:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    collected: list[ArticleRef] = []
    seen_ids: set[str] = set()
    captured_endpoints: list[str] = []

    def _merge(refs: Iterable[ArticleRef]) -> int:
        added = 0
        for r in refs:
            if r.article_id not in seen_ids:
                seen_ids.add(r.article_id)
                collected.append(r)
                added += 1
        return added

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=USER_AGENT, locale="ru-RU")
        page = context.new_page()

        # Record every request that looks like the load-more XHR so the true
        # endpoint is documented empirically on the first real run.
        def _on_request(req):
            u = req.url
            if "more" in u.lower() and "ria.ru" in u and u != SECTION_URL:
                captured_endpoints.append(u)

        page.on("request", _on_request)

        page.goto(SECTION_URL, wait_until="domcontentloaded", timeout=60_000)
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except PWTimeout:
            pass

        _merge(parse_article_refs(page.content()))
        if not collected:
            raise ScrapeError(
                "No article links found in the initial section page — layout "
                "changed or the page did not render."
            )

        # The section advertises its size as "N материалов". Reaching it is an
        # authoritative end-of-section signal, independent of the button, which
        # RIA leaves in the DOM even when exhausted.
        section_total = _parse_section_total(page.content())
        if section_total:
            print(f"  [browser] section advertises {section_total} материалов")

        # Locate the "Ещё" load-more button by its Russian label.
        more_selectors = [
            "text=Ещё 20 материалов",
            "text=Ещё",
            "[class*=list-more]",
        ]

        stagnant_clicks = 0
        while len(collected) < target_total:
            # Advertised total reached -> exhausted (button may still linger).
            if section_total and len(collected) >= section_total:
                print(f"  [browser] reached advertised total ({section_total}) "
                      "— section exhausted.")
                break

            button = None
            for sel in more_selectors:
                loc = page.locator(sel).first
                try:
                    if loc.count() > 0 and loc.is_visible():
                        button = loc
                        break
                except PWTimeout:
                    continue

            if button is None:
                # Button gone => section exhausted. Clean stop, NOT a failure.
                print("  [browser] load-more button no longer present — section exhausted.")
                break

            # Click and capture the load-more RESPONSE itself. The fragment body
            # is the site's own end signal: an empty fragment (or only-seen ids)
            # means exhausted; a 4xx/5xx means a real failure.
            resp = None
            frag = ""
            try:
                with page.expect_response(
                        lambda r: "more.html" in r.url.lower(),
                        timeout=15_000) as resp_info:
                    button.scroll_into_view_if_needed(timeout=10_000)
                    button.click(timeout=10_000)
                resp = resp_info.value
            except PWTimeout:
                resp = None            # click fired no load-more request

            if resp is not None:
                if resp.status >= 400:
                    raise ScrapeError(
                        f"Load-more HTTP {resp.status} for {resp.url} — the "
                        "endpoint errored (throttle/block/changed). Refusing to "
                        "return a short list.")
                try:
                    frag = resp.text() or ""
                except Exception:
                    frag = ""

            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except PWTimeout:
                pass
            _sleep(LOADMORE_DELAY_SEC)

            frag_ids = {r.article_id for r in parse_article_refs(frag)}
            added = _merge(parse_article_refs(page.content()))
            print(f"  [browser] +{added} new (total {len(collected)})")

            if added > 0:
                stagnant_clicks = 0
                continue

            # No new articles injected. Decide end-of-section vs. real anomaly by
            # looking at what the loader actually returned.
            exhausted = (
                resp is None            # the click fired no request (vestigial button)
                or not frag_ids         # the fragment carried no article cards
                or frag_ids <= seen_ids  # the fragment only repeated seen ids
            )
            if exhausted:
                print("  [browser] load-more returned no new items "
                      "(empty/duplicate fragment) — section exhausted.")
                break

            # The fragment carried UNSEEN ids but the DOM did not grow: a genuine
            # injection/cursor anomaly, not an end. Fail loud after a retry.
            stagnant_clicks += 1
            if stagnant_clicks >= 2:
                raise ScrapeError(
                    "Load-more returned unseen article ids that were NOT added to "
                    "the page on 2 consecutive clicks — cursor/injection anomaly, "
                    "not end-of-section. Refusing to return a short list. "
                    f"Last fragment ids: {sorted(frag_ids)[:5]} ; "
                    f"captured endpoints: {captured_endpoints[-2:]}")

        browser.close()

    if captured_endpoints:
        _ensure_dirs()
        uniq = sorted(set(captured_endpoints))
        with open(DISCOVERED_ENDPOINT_FILE, "w", encoding="utf-8") as fh:
            fh.write("# Real load-more requests observed by the browser collector.\n")
            fh.write("# Use these to enable/tune METHOD='http'.\n\n")
            fh.write("\n".join(uniq) + "\n")
        print(f"  [browser] recorded {len(uniq)} load-more endpoint(s) -> "
              f"{DISCOVERED_ENDPOINT_FILE}")

    return collected


# ===========================================================================
# COLLECTION — METHOD="http"  (direct more.html; unverified shape, fails loud)
# ===========================================================================

def _collect_http(target_total: int) -> list[ArticleRef]:
    session = _build_session()

    resp = _get(session, SECTION_URL)
    html = resp.text
    collected = parse_article_refs(html)
    if not collected:
        raise ScrapeError("No article links in initial section page (http method).")
    seen_ids = {r.article_id for r in collected}
    print(f"  [http] initial batch: {len(collected)} articles")

    section_total = _parse_section_total(html)
    if section_total:
        print(f"  [http] section advertises {section_total} материалов")

    # NB: the confirmed endpoint wants date=<YYYYMMDDTHHMMSS> (full timestamp).
    # _last_cursor reads it from the list-more button's data-date when present;
    # if a run stalls immediately, dump the button's data-date and compare with
    # discovered_endpoint.txt. The browser method needs none of this.
    cursor_id, cursor_date = _last_cursor(collected, html)

    first_more = True
    while len(collected) < target_total:
        if section_total and len(collected) >= section_total:
            print(f"  [http] reached advertised total ({section_total}) — exhausted.")
            break

        params = {"id": cursor_id, "view": MORE_VIEW}
        if cursor_date:
            params["date"] = cursor_date

        resp = _get(session, MORE_ENDPOINT, params=params, referer=SECTION_URL)
        frag = resp.text
        new_refs = parse_article_refs(frag)
        fresh = [r for r in new_refs if r.article_id not in seen_ids]

        if first_more and not fresh:
            # The single most important failure to prevent: quietly returning
            # only page 1 because the endpoint shape is wrong.
            raise ScrapeError(
                "First load-more request returned NO new article cards.\n"
                f"  endpoint : {resp.url}\n"
                f"  status   : {resp.status_code}\n"
                f"  body head: {frag[:300]!r}\n"
                "The inferred more.html endpoint shape is wrong for this site. "
                "Run with METHOD='browser' first; it records the real endpoint "
                f"to {DISCOVERED_ENDPOINT_FILE}, then wire that shape in here."
            )
        first_more = False

        if not fresh:
            # No new ids on a later page => genuine end-of-section. Clean stop.
            print("  [http] load-more returned only seen ids — section exhausted.")
            break

        for r in fresh:
            seen_ids.add(r.article_id)
            collected.append(r)
        print(f"  [http] +{len(fresh)} new (total {len(collected)})")

        cursor_id, cursor_date = _last_cursor(collected, frag)
        _sleep(LOADMORE_DELAY_SEC)

    return collected


def collect(extra_articles: int = DEFAULT_EXTRA_ARTICLES,
            method: str = DEFAULT_METHOD) -> list[ArticleRef]:
    """Collect article references from the section.

    Collects until at least (initial_batch + extra_articles) UNIQUE article URLs
    are gathered, or the section is exhausted — whichever comes first.
    Deduplicates by article id. Fails loudly on unexpected structures / HTTP
    errors rather than silently returning a short list.
    """
    target_total = INITIAL_BATCH_HINT + int(extra_articles)
    print(f"[collect] method={method} target≈{target_total} "
          f"(initial {INITIAL_BATCH_HINT} + extra {extra_articles})")

    if method == "browser":
        refs = _collect_browser(target_total)
    elif method == "http":
        refs = _collect_http(target_total)
    else:
        raise ValueError(f"Unknown method: {method!r} (use 'browser' or 'http')")

    print(f"[collect] gathered {len(refs)} unique article URLs")
    return refs


# ===========================================================================
# PER-ARTICLE EXTRACTION + QUALITY SIGNALS
# ===========================================================================

# Trafilatura config tuned for RIA: prune the heavy tag-lists, related-article
# rails, "Ещё" widgets, comment scaffolding and social-share blocks so they do
# NOT inflate the body or the word counts. Artifact removal belongs HERE.
_TRAFILATURA_KWARGS = dict(
    output_format="txt",
    include_comments=False,     # drop comment scaffolding
    include_tables=False,       # exclude tables (per requirement)
    include_images=False,
    include_links=False,
    include_formatting=False,
    favor_precision=True,       # aggressively prune boilerplate / related rails
    deduplicate=True,
    target_language="ru",
)

# RIA author markup is inconsistent; try several signed-author locations before
# falling back to the institutional byline.
_AUTHOR_SELECTORS = [
    ".article__author-name",
    ".article__creted .article__author",
    ".article__info-author",
    'a[href*="/authors/"]',
]

# Rubric / section / institutional labels that RIA sometimes exposes in the same
# slots a signed author would occupy (e.g. meta[name=author]="Аналитика" on a
# доклад). These must NOT be counted as signed bylines. Compared case-folded.
_INSTITUTIONAL_LABELS = {
    "аналитика", "риа новости", "новости", "россия сегодня",
    'миа "россия сегодня"', "миа «россия сегодня»", "ria novosti",
    "инфографика", "мультимедиа", "видео", "фото", "радио sputnik",
    "прайм", "риа новости спорт", "спутник",
}

# A signed personal byline looks like "Имя Фамилия" (2–4 capitalised tokens,
# possibly with an initial). "Аналитика" (one token) or 'МИА "Россия сегодня"'
# (contains quotes) are not persons.
_PERSON_TOKEN = re.compile(r"^[А-ЯЁA-Z][А-ЯЁA-Zа-яёa-z\-]*\.?$")


def _looks_like_person(name: str) -> bool:
    n = (name or "").strip()
    if not n or len(n) > 80:
        return False
    if n.casefold() in _INSTITUTIONAL_LABELS:
        return False
    if any(ch.isdigit() for ch in n) or any(q in n for q in '"«»'):
        return False
    tokens = n.split()
    if not (2 <= len(tokens) <= 4):        # reject single-word rubrics
        return False
    return all(_PERSON_TOKEN.match(t) for t in tokens)


_SENTENCE_RE = re.compile(r"[.!?…]+(?=\s|$)")

# A body block that is a list item or section header rather than a prose
# paragraph: leading bullet, "1)"/"1." numbering, or "1.1"/"2.4" sub-heading.
_STRUCTURAL_BLOCK_RE = re.compile(r"^\s*(?:[-–—•*·]|\d+[.)]|\d+\.\d)")


@dataclass
class ArticleRecord:
    # CONTENT
    url: str
    article_id: str
    date: str
    title: str
    body: str
    content: str
    byline: str
    # QUALITY / NARRATIVITY SIGNALS
    char_count: int
    word_count: int
    paragraph_count: int
    prose_paragraph_count: int
    mean_paragraph_len: float
    sentence_count: int
    has_byline: bool
    # PROVENANCE / AXIS TAGS
    source: str = PROVENANCE["source"]
    section: str = PROVENANCE["section"]
    orientation: str = PROVENANCE["orientation"]
    factuality_tier: str = PROVENANCE["factuality_tier"]
    genre: str = PROVENANCE["genre"]


def _iso_date_from_url_date(d: str) -> str:
    return f"{d[0:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else d


def _extract_title(html: str, meta) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for sel in [".article__title", "h1.article__title", "h1"]:
        node = soup.select_one(sel)
        if node and node.get_text(strip=True):
            return node.get_text(strip=True)
    if meta is not None and getattr(meta, "title", None):
        return meta.title.strip()
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        return og["content"].strip()
    return ""


def _extract_byline(html: str, meta) -> tuple[str, bool]:
    """Return (byline, has_signed_byline). A signed personal byline distinguishes
    commentary from institutional essays; absence => institutional.

    Only a value that actually looks like a person's name counts as signed. RIA
    exposes rubric/agency labels ("Аналитика", 'МИА "Россия сегодня"') in the
    same slots, and those must map to institutional, not to a fake byline."""
    soup = BeautifulSoup(html, "html.parser")

    candidates: list[str] = []
    for sel in _AUTHOR_SELECTORS:
        node = soup.select_one(sel)
        if node:
            candidates.append(node.get_text(strip=True))

    if meta is not None and getattr(meta, "author", None):
        candidates.append(meta.author)
    m = soup.find("meta", attrs={"name": "author"})
    if m and m.get("content"):
        candidates.append(m["content"])

    for name in candidates:
        name = (name or "").strip()
        if _looks_like_person(name):
            return name, True

    # No personal byline found → institutional (rubric labels land here).
    return INSTITUTIONAL_BYLINE, False


def _paragraphs(body: str) -> list[str]:
    # trafilatura txt output separates blocks with newlines.
    return [p.strip() for p in re.split(r"\n+", body) if p.strip()]


def _is_structural_block(block: str) -> bool:
    """True if a body block is a list item / section header rather than prose:
    a leading bullet or number, or a short line with no sentence-terminating
    punctuation (a bare heading like 'Введение')."""
    if _STRUCTURAL_BLOCK_RE.match(block):
        return True
    words = block.split()
    if len(words) < 8 and not re.search(r"[.!?…]\s*$", block):
        return True
    return False


def compute_signals(body: str) -> dict:
    paras = _paragraphs(body)
    word_count = len(body.split())
    paragraph_count = len(paras)
    prose_paragraph_count = sum(1 for p in paras if not _is_structural_block(p))
    sentence_count = len(_SENTENCE_RE.findall(body))
    mean_paragraph_len = round(word_count / paragraph_count, 2) if paragraph_count else 0.0
    return {
        "char_count": len(body),
        "word_count": word_count,
        "paragraph_count": paragraph_count,          # all body blocks
        "prose_paragraph_count": prose_paragraph_count,  # excludes list/header lines
        "mean_paragraph_len": mean_paragraph_len,
        "sentence_count": sentence_count,
    }


def extract_record(ref: ArticleRef, html: str) -> ArticleRecord:
    """Build a full record from an article's raw HTML. Pure function (no I/O) so
    it can be re-run offline against the saved raw HTML with a tuned config."""
    body = trafilatura.extract(html, url=ref.url, **_TRAFILATURA_KWARGS) or ""
    body = body.strip()

    meta = None
    try:
        meta = trafilatura.extract_metadata(html)
    except Exception:
        meta = None

    title = _extract_title(html, meta)
    byline, has_byline = _extract_byline(html, meta)
    signals = compute_signals(body)
    content = f"{title}\n\n{body}" if title else body

    return ArticleRecord(
        url=ref.url,
        article_id=ref.article_id,
        date=_iso_date_from_url_date(ref.date),
        title=title,
        body=body,
        content=content,
        byline=byline,
        has_byline=has_byline,
        **signals,
    )


def fetch_article_html(session: requests.Session, ref: ArticleRef) -> str:
    resp = _get(session, ref.url, referer=SECTION_URL)
    return resp.text


# ===========================================================================
# OUTPUT + RUN SUMMARY
# ===========================================================================

def _save_raw_html(article_id: str, html: str) -> None:
    path = os.path.join(RAW_HTML_DIR, f"{article_id}.html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)


def _dist(values: list[int]) -> dict:
    if not values:
        return {"min": None, "median": None, "max": None, "n": 0}
    return {
        "min": min(values),
        "median": statistics.median(values),
        "max": max(values),
        "n": len(values),
    }


# Below this word_count a record is almost certainly not a narrative essay
# (photo/video/infographic stub in the section feed). Reported, not dropped.
LOW_CONTENT_WORD_FLOOR = 150


def _summarize(discovered: int, records: list[ArticleRecord], failed: list[dict]) -> dict:
    wc = [r.word_count for r in records]
    pc = [r.paragraph_count for r in records]
    ppc = [r.prose_paragraph_count for r in records]
    low = [r.article_id for r in records if r.word_count < LOW_CONTENT_WORD_FLOOR]
    return {
        "section_url": SECTION_URL,
        "urls_discovered": discovered,
        "extracted_ok": len(records),
        "failed": len(failed),
        "failed_detail": failed,
        "word_count_distribution": _dist(wc),
        "paragraph_count_distribution": _dist(pc),
        "prose_paragraph_count_distribution": _dist(ppc),
        "signed_bylines": sum(1 for r in records if r.has_byline),
        "institutional_bylines": sum(1 for r in records if not r.has_byline),
        "low_content_count": len(low),
        "low_content_ids": low,
    }


def _print_summary(summary: dict) -> None:
    wc = summary["word_count_distribution"]
    pc = summary["paragraph_count_distribution"]
    print("\n" + "=" * 64)
    print("RUN SUMMARY — RIA Novosti / Аналитика")
    print("=" * 64)
    print(f"  URLs discovered      : {summary['urls_discovered']}")
    print(f"  Extracted OK         : {summary['extracted_ok']}")
    print(f"  Failed               : {summary['failed']}")
    print(f"  Signed bylines       : {summary['signed_bylines']}")
    print(f"  Institutional bylines: {summary['institutional_bylines']}")
    print(f"  Low-content (<{LOW_CONTENT_WORD_FLOOR}w) : {summary['low_content_count']} "
          f"{summary['low_content_ids'] or ''}")
    print("  word_count       min/median/max : "
          f"{wc['min']} / {wc['median']} / {wc['max']}")
    print("  paragraph_count  min/median/max : "
          f"{pc['min']} / {pc['median']} / {pc['max']}")
    ppc = summary["prose_paragraph_count_distribution"]
    print("  prose_paragraphs min/median/max : "
          f"{ppc['min']} / {ppc['median']} / {ppc['max']}   (excl. list/header lines)")
    if summary["extracted_ok"] and (wc["median"] or 0) < 250:
        print("  !! WARNING: median word_count is low for an analytics harvest.")
        print("     Extraction may be grabbing the wrong node, or the section")
        print("     yield is off. Inspect saved raw HTML before trusting output.")
    print("=" * 64 + "\n")


def run(extra_articles: int = DEFAULT_EXTRA_ARTICLES,
        method: str = DEFAULT_METHOD) -> dict:
    _ensure_dirs()
    refs = collect(extra_articles=extra_articles, method=method)

    session = _build_session()
    records: list[ArticleRecord] = []
    failed: list[dict] = []

    with open(OUTPUT_JSONL, "w", encoding="utf-8") as out:
        for i, ref in enumerate(refs, 1):
            try:
                html = fetch_article_html(session, ref)
                _save_raw_html(ref.article_id, html)
                rec = extract_record(ref, html)
                out.write(json.dumps(asdict(rec), ensure_ascii=False) + "\n")
                out.flush()
                records.append(rec)
                print(f"  [{i}/{len(refs)}] {ref.article_id} "
                      f"words={rec.word_count} paras={rec.paragraph_count} "
                      f"byline={'signed' if rec.has_byline else 'inst'}")
            except Exception as exc:  # keep going, but record the failure
                failed.append({"url": ref.url, "article_id": ref.article_id,
                               "error": f"{type(exc).__name__}: {exc}"})
                print(f"  [{i}/{len(refs)}] FAILED {ref.article_id}: {exc}")
            _sleep(REQUEST_DELAY_SEC)

    summary = _summarize(len(refs), records, failed)
    with open(RUN_SUMMARY_JSON, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)
    _print_summary(summary)
    print(f"  JSONL   -> {OUTPUT_JSONL}")
    print(f"  raw html-> {RAW_HTML_DIR}/<article_id>.html")
    print(f"  summary -> {RUN_SUMMARY_JSON}")
    return summary


# ===========================================================================
# CLI
# ===========================================================================

def _parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="Scrape the RIA Novosti 'Аналитика' section into a JSONL corpus."
    )
    ap.add_argument("extra_articles", nargs="?", type=int,
                    default=DEFAULT_EXTRA_ARTICLES,
                    help="How many EXTRA articles beyond the initial page to collect (operator's i).")
    ap.add_argument("--method", choices=["browser", "http"], default=DEFAULT_METHOD,
                    help="Load-more mechanism (default: browser).")
    return ap.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args()
    run(extra_articles=args.extra_articles, method=args.method)

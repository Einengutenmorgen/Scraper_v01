#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
The Insider — /opinions scraper.

STANDALONE. No shared abstraction with the other scrapers by design.

DISCOVERY (confirmed live where possible):
  * Next.js site. Listing https://theins.ru/opinions shows article cards with
    author (Cyrillic name, e.g. "Георгий Чижов") and a Russian date string
    ("21 июля 2026 г.").
  * Article URL: https://theins.ru/opinions/<author-slug>/<numeric-id>
    e.g. https://theins.ru/opinions/georgy-chizhov/295003
    -> article_id = trailing numeric id; author-slug is IN the URL path (used as
       a fallback for author; the rendered Cyrillic name is preferred).
  * Article pages carry a title, a standfirst/deck (subtitle), and heavy inline
    donation CTAs ("Поддержите нас", "Нам очень нужна ваша помощь. Подпишитесь…")
    which are stripped at extraction so they do not inflate word_count.
  * Load-more: a "Загрузить ещё" button (infinite-scroll, Next.js). The exact
    data endpoint (a JSON/RSC route) is JS-driven and could NOT be verified from
    the build sandbox. So collection uses Playwright: it clicks the button, reads
    the load-more RESPONSE as the authoritative end signal, and RECORDS the
    observed endpoint to discovered_endpoint_theins.txt so a faster direct-HTTP
    path can be wired later. (Prefer direct HTTP once that endpoint is confirmed.)

FAIL-LOUD: raises on a load-more HTTP error, and when the loader returns NEW
article ids that never get rendered (cursor/injection anomaly). Clean stop on an
empty/duplicate response, a vanished button, or a click that fires no request.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import time
from dataclasses import dataclass, asdict
from typing import Optional
from urllib.parse import urljoin

import requests
import trafilatura
from bs4 import BeautifulSoup

# ============================ CONFIG ============================
SECTION_URL = "https://theins.ru/opinions"

OUTDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
OUT_JSONL = os.path.join(OUTDIR, "theinsider_opinions.jsonl")
RAW_DIR = os.path.join(OUTDIR, "raw_html", "theinsider")
RUN_SUMMARY = os.path.join(OUTDIR, "theinsider_run_summary.json")
ENDPOINT_LOG = os.path.join(OUTDIR, "discovered_endpoint_theins.txt")

DEFAULT_TARGET = 60
REQUEST_DELAY = (1.0, 2.0)
LOADMORE_DELAY = (1.0, 2.0)
HTTP_TIMEOUT = 30
MAX_RETRIES = 4
BACKOFF_BASE = 2.0
WORD_FLOOR = 150

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": USER_AGENT,
           "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"}

# Collection provenance only. Outlet-level judgements (orientation,
# factuality_tier) are properties of the OUTLET, not of the text: they live
# in sources.csv and are joined on `source` at analysis time.
FIXED = {"source": "theinsider", "section": "opinions"}
# NOTE: no `genre` field -- genre is what the genre/stance filter decides;
# asserting it here would pre-judge that gate.

# /opinions/<author-slug>/<numeric-id>. Trailing \b so it also matches ids that
# appear inside JSON (…/id") as well as in href paths (…/id or …/id/).
ARTICLE_RE = re.compile(r"/opinions/([a-z0-9-]+)/(\d+)\b")

RU_MONTHS = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5,
             "июня": 6, "июля": 7, "августа": 8, "сентября": 9, "октября": 10,
             "ноября": 11, "декабря": 12}
RU_DATE_RE = re.compile(r"(\d{1,2})\s+([а-яё]+)\s+(\d{4})", re.IGNORECASE)

# Scoped stems so "поддержать Украину" / "подписал указ" in the body survive.
BOILERPLATE_RE = re.compile(
    r"(поддержите нас|поддержать нас|поддержите редакц|поддержите независим|"
    r"подпишите|подпишись|подписывайт|подписаться|подписк|рассылк|"
    r"пожертвован|донат|краудфандинг|"
    r"нам очень нужна ваша помощь|регулярные пожертвования|"
    r"наш телеграм|телеграм-канал)", re.IGNORECASE)
_SENT_RE = re.compile(r"[.!?…]+(?=\s|$)")

# JSON-LD is the cleanest author source.
_JSONLD_RE = re.compile(
    r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', re.DOTALL | re.IGNORECASE)
_ROLE_RE = re.compile(
    r"\s+(Аспирант|Профессор|Доктор|Кандидат|Обозреватель|Редактор|Ответственн|"
    r"Основатель|Директор|Президент|Эксперт|Политолог|Журналист|Публицист|"
    r"Экономист|Военный|Член |Руководитель|Заместитель|Советник|Депутат|"
    r"Академик|Корреспондент|Аналитик|Сотрудник|Главный)", re.IGNORECASE)


def _clean_name(name):
    if not name:
        return None
    name = re.sub(r"\s+", " ", str(name)).strip()
    m = _ROLE_RE.search(name)
    if m and m.start() > 0:
        name = name[:m.start()].strip()
    return name or None


def _iter_jsonld(html):
    for m in _JSONLD_RE.finditer(html):
        try:
            data = json.loads(m.group(1).strip())
        except Exception:
            continue
        stack = [data]
        while stack:
            obj = stack.pop()
            if isinstance(obj, list):
                stack.extend(obj)
            elif isinstance(obj, dict):
                if "@graph" in obj:
                    g = obj["@graph"]
                    stack.extend(g if isinstance(g, list) else [g])
                yield obj


def _jsonld_author(html):
    for obj in _iter_jsonld(html):
        a = obj.get("author")
        if isinstance(a, list):
            a = a[0] if a else None
        if isinstance(a, dict):
            a = a.get("name")
        if isinstance(a, str) and a.strip():
            return _clean_name(a)
    return None


class ScrapeError(RuntimeError):
    pass


# ============================ helpers ============================
def _sleep(bounds=REQUEST_DELAY):
    time.sleep(random.uniform(*bounds))


def _session():
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def _get(session, url):
    last = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = session.get(url, timeout=HTTP_TIMEOUT)
            if r.status_code in (429, 500, 502, 503, 504):
                raise ScrapeError(f"transient HTTP {r.status_code}")
            r.raise_for_status()
            return r
        except (requests.RequestException, ScrapeError) as e:
            last = e
            if attempt == MAX_RETRIES:
                break
            time.sleep(BACKOFF_BASE ** attempt + random.uniform(0, 1))
    raise ScrapeError(f"GET failed after {MAX_RETRIES}: {url} ({last})")


def parse_ru_date(text: str) -> Optional[str]:
    m = RU_DATE_RE.search(text or "")
    if not m:
        return None
    day, mon, year = int(m.group(1)), m.group(2).lower(), int(m.group(3))
    if mon not in RU_MONTHS:
        return None
    return f"{year:04d}-{RU_MONTHS[mon]:02d}-{day:02d}"


def humanize_slug(slug: str) -> str:
    return " ".join(w.capitalize() for w in slug.split("-"))


@dataclass
class Ref:
    url: str
    article_id: str          # numeric id
    author_slug: str
    card_author: Optional[str] = None
    card_date: Optional[str] = None   # ISO, best-effort from the card


def parse_refs_from_dom(html: str, base: str) -> list[Ref]:
    """Parse opinion cards. article_id = numeric id; also grab per-card author
    name and Russian date where present in the card container."""
    soup = BeautifulSoup(html, "html.parser")
    refs, seen = [], set()
    for a in soup.find_all("a", href=True):
        m = ARTICLE_RE.search(a["href"])
        if not m:
            continue
        aid = m.group(2)
        if aid in seen:
            continue
        seen.add(aid)
        card = a
        for _ in range(4):
            if card.parent is not None:
                card = card.parent
        txt = card.get_text(" ", strip=True) if card else ""
        refs.append(Ref(url=urljoin(base, f"/opinions/{m.group(1)}/{aid}"),
                        article_id=aid, author_slug=m.group(1),
                        card_date=parse_ru_date(txt)))
    return refs


def _ids_in_text(text: str) -> set[str]:
    """Extract candidate article ids from a load-more response body (JSON/RSC),
    matching both /opinions/<slug>/<id> paths and bare id fields."""
    ids = set(m.group(2) for m in ARTICLE_RE.finditer(text or ""))
    return ids


# ============================ collection (Playwright) ============================
def collect(target_articles: int) -> tuple[list[Ref], dict]:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    collected: list[Ref] = []
    seen: set[str] = set()
    endpoints: list[str] = []

    def _merge(refs) -> int:
        n = 0
        for r in refs:
            if r.article_id not in seen:
                seen.add(r.article_id); collected.append(r); n += 1
        return n

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=USER_AGENT, locale="ru-RU")
        page = ctx.new_page()

        def _on_request(req):
            u = req.url
            if u != SECTION_URL and "theins.ru" in u and \
               ("opinion" in u.lower() or "_next/data" in u or "/api/" in u):
                endpoints.append(u)
        page.on("request", _on_request)

        page.goto(SECTION_URL, wait_until="domcontentloaded", timeout=60_000)
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except PWTimeout:
            pass

        _merge(parse_refs_from_dom(page.content(), SECTION_URL))
        if not collected:
            raise ScrapeError(
                "No /opinions/<slug>/<id> cards on the initial listing — layout "
                "changed or the page did not render. Refusing to continue.")

        more_selectors = ["text=Загрузить ещё", "text=Загрузить", "[class*=more]"]
        stagnant = 0
        while len(collected) < target_articles:
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
                print("  [theins] load-more button gone — section exhausted.")
                break

            resp = None
            try:
                with page.expect_response(
                        lambda r: ("opinion" in r.url.lower()
                                   or "_next/data" in r.url or "/api/" in r.url),
                        timeout=15_000) as ri:
                    button.scroll_into_view_if_needed(timeout=10_000)
                    button.click(timeout=10_000)
                resp = ri.value
            except PWTimeout:
                resp = None

            body = ""
            if resp is not None:
                if resp.status >= 400:
                    raise ScrapeError(
                        f"Load-more HTTP {resp.status} for {resp.url} — endpoint "
                        "errored (throttle/block/changed). Refusing short list.")
                try:
                    body = resp.text() or ""
                except Exception:
                    body = ""

            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except PWTimeout:
                pass
            _sleep(LOADMORE_DELAY)

            resp_ids = _ids_in_text(body)
            added = _merge(parse_refs_from_dom(page.content(), SECTION_URL))
            print(f"  [theins] +{added} new (total {len(collected)})")
            if added > 0:
                stagnant = 0
                continue

            # No new cards were rendered. Decide end vs. anomaly WITHOUT trusting
            # our ability to parse the opaque Next.js payload:
            #   * no request fired, or an EMPTY response body  -> clean end.
            #   * response repeated only already-seen ids       -> clean end.
            #   * a NON-EMPTY response that produced nothing new -> fail loud
            #     (unseen ids not injected, OR a full page we could not parse —
            #      never assume "empty" and silently return a short list).
            body_stripped = (body or "").strip()
            looks_empty = (resp is None or body_stripped in ("", "[]", "{}", "null")
                           or len(body_stripped) < 5)
            if looks_empty or (resp_ids and resp_ids <= seen):
                print("  [theins] load-more returned no new items — exhausted.")
                break
            stagnant += 1
            if stagnant >= 2:
                raise ScrapeError(
                    "Load-more returned a NON-EMPTY response but no new articles "
                    "appeared on 2 consecutive clicks — cursor/injection anomaly or "
                    "an unparsed payload. Refusing to return a short list. "
                    f"resp_ids sample: {sorted(resp_ids)[:5]}; body[:120]={body_stripped[:120]!r}; "
                    f"endpoints: {endpoints[-2:]}")

        browser.close()

    if endpoints:
        os.makedirs(OUTDIR, exist_ok=True)
        with open(ENDPOINT_LOG, "w", encoding="utf-8") as fh:
            fh.write("# load-more requests observed (enable a direct-HTTP path):\n")
            fh.write("\n".join(sorted(set(endpoints))) + "\n")

    exhausted_early = len(collected) < target_articles
    meta = {"exhausted_early": exhausted_early,
            "stop_reason": ("section exhausted" if exhausted_early
                            else f"collected target {target_articles}")}
    refs = collected[:target_articles]
    print(f"[theins] collected {len(refs)} refs ({meta['stop_reason']})")
    return refs, meta


# ============================ extraction ============================
_TRAFI = dict(output_format="txt", include_comments=False, include_tables=False,
              include_images=False, include_links=False, include_formatting=False,
              favor_precision=True, deduplicate=True, target_language="ru")


@dataclass
class Record:
    url: str
    article_id: str
    date: Optional[str]
    source: str
    section: str
    title: str
    subtitle: Optional[str]
    body: str
    content: str
    author: Optional[str]
    has_byline: bool
    char_count: int
    word_count: int
    paragraph_count: int
    mean_paragraph_len: float
    sentence_count: int
    stated_reading_time: Optional[int]


def _strip_boilerplate(text: str) -> str:
    return "\n".join(ln for ln in text.split("\n")
                     if not (ln.strip() and BOILERPLATE_RE.search(ln)))


def _paras(body: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n+", body) if p.strip()]


def _signals(body: str) -> dict:
    paras = _paras(body)
    wc = len(body.split()); pc = len(paras)
    return {"char_count": len(body), "word_count": wc, "paragraph_count": pc,
            "mean_paragraph_len": round(wc / pc, 2) if pc else 0.0,
            "sentence_count": len(_SENT_RE.findall(body))}


def _extract_meta(html: str):
    soup = BeautifulSoup(html, "html.parser")
    title = None
    for sel in ["h1", "meta[property='og:title']"]:
        node = soup.select_one(sel)
        if node:
            title = node.get_text(strip=True) if node.name != "meta" else node.get("content")
            if title:
                break
    subtitle = None
    for sel in [".lead", ".article-lead", ".subtitle", ".standfirst",
                "meta[property='og:description']", "meta[name='description']"]:
        node = soup.select_one(sel)
        if node:
            subtitle = node.get_text(strip=True) if node.name != "meta" else node.get("content")
            if subtitle:
                break
    # Author: JSON-LD first (clean, no double spaces), else DOM byline.
    author = _jsonld_author(html)
    if not author:
        for sel in [".author", ".article-author", "a[rel='author']", "[class*=author]"]:
            node = soup.select_one(sel)
            if node and node.get_text(" ", strip=True):
                cand = _clean_name(node.get_text(" ", strip=True))
                if cand and 2 <= len(cand) <= 80:
                    author = cand
                    break
    if not author:
        m = soup.select_one("meta[name='author']")
        if m and m.get("content"):
            author = _clean_name(m["content"])
    return title, subtitle, author


def extract_record(ref: Ref, html: str) -> Record:
    body = trafilatura.extract(html, url=ref.url, **_TRAFI) or ""
    body = _strip_boilerplate(body).strip()
    title, subtitle, author = _extract_meta(html)
    title = title or ""
    # Prefer the rendered Cyrillic name; fall back to the URL author-slug.
    rendered = author
    author = rendered or humanize_slug(ref.author_slug)
    date = parse_ru_date(html) or ref.card_date
    sig = _signals(body)
    content = f"{title}\n\n{body}" if title else body
    return Record(
        url=ref.url, article_id=ref.article_id, date=date, **FIXED,
        title=title, subtitle=subtitle or None, body=body, content=content,
        author=author or None, has_byline=bool(rendered),
        stated_reading_time=None, **sig)


def fetch_html(session, url) -> str:
    return _get(session, url).text


# ============================ output / summary ============================
def _pct(sv, q):
    if not sv:
        return None
    i = q * (len(sv) - 1); lo = int(i); frac = i - lo
    if lo + 1 < len(sv):
        return round(sv[lo] * (1 - frac) + sv[lo + 1] * frac, 1)
    return sv[lo]


def summarize(discovered, records, failed, meta):
    wc = sorted(r.word_count for r in records)
    pc = sorted(r.paragraph_count for r in records)
    dates = sorted(r.date for r in records if r.date)
    return {
        "source": FIXED["source"], "section": FIXED["section"],
        "urls_discovered": discovered, "extracted_ok": len(records),
        "failed": len(failed), "failed_detail": failed,
        "exhausted_early": meta.get("exhausted_early"),
        "stop_reason": meta.get("stop_reason"),
        "word_count": {"min": wc[0] if wc else None, "p25": _pct(wc, .25),
                       "median": _pct(wc, .5), "p75": _pct(wc, .75),
                       "max": wc[-1] if wc else None},
        "paragraph_count": {"min": pc[0] if pc else None,
                            "median": _pct(pc, .5), "max": pc[-1] if pc else None},
        "flagged_lt_150": sorted(r.article_id for r in records if r.word_count < WORD_FLOOR),
        "with_byline": sum(1 for r in records if r.has_byline),
        "without_byline": sum(1 for r in records if not r.has_byline),
        "date_range": {"earliest": dates[0] if dates else None,
                       "latest": dates[-1] if dates else None},
        "dates_missing": sum(1 for r in records if not r.date),
    }


def _print_summary(s):
    wc, pc = s["word_count"], s["paragraph_count"]
    print("\n" + "=" * 60)
    print("RUN SUMMARY — The Insider /opinions")
    print("=" * 60)
    print(f"  discovered / ok / failed : {s['urls_discovered']} / "
          f"{s['extracted_ok']} / {s['failed']}")
    print(f"  exhausted early          : {s['exhausted_early']} ({s['stop_reason']})")
    print(f"  word_count min/25/med/75/max : {wc['min']}/{wc['p25']}/"
          f"{wc['median']}/{wc['p75']}/{wc['max']}")
    print(f"  paragraphs  min/med/max      : {pc['min']}/{pc['median']}/{pc['max']}")
    print(f"  flagged <{WORD_FLOOR}w ({len(s['flagged_lt_150'])}) : {s['flagged_lt_150']}")
    print(f"  byline yes/no            : {s['with_byline']}/{s['without_byline']}")
    print(f"  dates missing            : {s['dates_missing']}")
    print(f"  date range               : {s['date_range']['earliest']} .. "
          f"{s['date_range']['latest']}")
    if s["extracted_ok"] and (wc["median"] or 0) < 250:
        print("  !! WARNING: median word_count low — check extraction.")
    print("=" * 60)


def run(target_articles=DEFAULT_TARGET):
    os.makedirs(RAW_DIR, exist_ok=True)
    refs, meta = collect(target_articles)
    session = _session()
    records, failed = [], []
    with open(OUT_JSONL, "w", encoding="utf-8") as out:
        for i, ref in enumerate(refs, 1):
            try:
                html = fetch_html(session, ref.url)
                with open(os.path.join(RAW_DIR, f"{ref.article_id}.html"),
                          "w", encoding="utf-8") as fh:
                    fh.write(html)
                rec = extract_record(ref, html)
                out.write(json.dumps(asdict(rec), ensure_ascii=False) + "\n")
                records.append(rec)
                flag = " !!<150" if rec.word_count < WORD_FLOOR else ""
                print(f"  [{i}/{len(refs)}] {ref.article_id} words={rec.word_count}{flag}")
            except Exception as e:
                failed.append({"url": ref.url, "error": f"{type(e).__name__}: {e}"})
                print(f"  [{i}/{len(refs)}] FAILED {ref.article_id}: {e}")
            _sleep()
    s = summarize(len(refs), records, failed, meta)
    os.makedirs(OUTDIR, exist_ok=True)
    with open(RUN_SUMMARY, "w", encoding="utf-8") as fh:
        json.dump(s, fh, ensure_ascii=False, indent=2)
    _print_summary(s)
    print(f"  JSONL -> {OUT_JSONL}\n  raw   -> {RAW_DIR}\n  summary -> {RUN_SUMMARY}")
    return s


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Scrape The Insider /opinions essays.")
    ap.add_argument("target_articles", nargs="?", type=int, default=DEFAULT_TARGET)
    a = ap.parse_args()
    run(a.target_articles)

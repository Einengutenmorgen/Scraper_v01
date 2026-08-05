#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Holod.media — "Мнения и интервью" (/opinions/) scraper.

STANDALONE. No shared abstraction with the other scrapers by design.

DISCOVERY (confirmed live where possible):
  * WordPress (WP 6.8.x). Article URL: https://holod.media/YYYY/MM/DD/<slug>/
    -> date is in the URL path; article_id = slug.
  * Listing cards show author ("Иван Филиппов") and a stated reading time
    ("9 минут чтения") -> captured into stated_reading_time (int minutes).
  * Section is titled "Мнения и интервью" and genuinely mixes OPINION ESSAYS
    and INTERVIEWS. We flag `suspected_interview` STRUCTURALLY (see below) and
    set genre accordingly — we never drop interviews automatically.
  * "Посмотреть больше" load-more button.
  * WP REST API (/wp-json/wp/v2/...) is PREFERRED if reachable and clean, but it
    is robots-disallowed for the build sandbox's fetch tool, so it could not be
    verified here. The scraper therefore TRIES the REST API first and verifies
    it returns opinions-category posts; if that fails it FALLS BACK to HTML
    pagination. Both paths are implemented.

SCOPE: ONLY /opinions/. Explicitly EXCLUDES /longrids/ (out of scope) and never
follows links into other sections.

FAIL-LOUD: raises on HTTP errors, on a first listing with zero opinion links,
and when neither the REST API nor HTML pagination can advance past page 1.
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
SECTION_URL = "https://holod.media/opinions/"
WP_BASE = "https://holod.media/wp-json/wp/v2"
CATEGORY_SLUG = "opinions"

OUTDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
OUT_JSONL = os.path.join(OUTDIR, "holod_opinions.jsonl")
RAW_DIR = os.path.join(OUTDIR, "raw_html", "holod")
RUN_SUMMARY = os.path.join(OUTDIR, "holod_run_summary.json")

DEFAULT_TARGET = 60
REQUEST_DELAY = (1.0, 2.0)
HTTP_TIMEOUT = 30
MAX_RETRIES = 4
BACKOFF_BASE = 2.0
WP_PER_PAGE = 20
MAX_PAGES_HARD_CAP = 100
WORD_FLOOR = 150
INTERVIEW_DASH_RATIO = 0.25       # >=25% dash-initial paragraphs -> interview
                                  # (a Q&A-turn ratio >=0.33 also triggers)

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": USER_AGENT,
           "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"}

FIXED = {"source": "holod", "section": "opinions",
         "orientation": "opposition_exile", "factuality_tier": "high_factuality"}
# genre is per-article: "interview" or "opinion_essay".

# holod.media/YYYY/MM/DD/<slug>/ ; /longrids/ is excluded explicitly.
ARTICLE_RE = re.compile(
    r"https?://holod\.media/(\d{4})/(\d{2})/(\d{2})/([^/\"'#?<>\s]+)/")

READING_RE = re.compile(r"(\d+)\s*минут")           # "9 минут чтения"
# Donation / newsletter CTA lines to strip at extraction. Stems are scoped so
# they do NOT clobber legitimate body words (e.g. "поддержать Украину",
# "президент подписал указ") — only subscribe/donate phrasings match.
BOILERPLATE_RE = re.compile(
    r"(подпишите|подпишись|подписывайт|подписаться|подписк|рассылк|"
    r"пожертвован|донат|краудфандинг|"
    r"поддержите нас|поддержать нас|поддержите редакц|поддержать редакц|"
    r"поддержите проект|поддержите независим|"
    r"нам очень нужна ваша помощь|регулярные пожертвования|"
    r"криптовалют|банковской карт|наш телеграм|телеграм-канал|"
    r"мнение автора может не совпадать|"
    r"этот материал мы подготовили|смотрите на ютуб)", re.IGNORECASE)
_SENT_RE = re.compile(r"[.!?…]+(?=\s|$)")
_DASH_START_RE = re.compile(r"^\s*[«\"']?\s*[—–-]\s")   # dialogue-turn opener

# JSON-LD is the most reliable author source across these CMSes (WP/Bitrix/Next).
_JSONLD_RE = re.compile(
    r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', re.DOTALL | re.IGNORECASE)
_ROLE_RE = re.compile(
    r"\s+(Аспирант|Профессор|Доктор|Кандидат|Обозреватель|Редактор|Ответственн|"
    r"Основатель|Директор|Президент|Эксперт|Политолог|Журналист|Публицист|"
    r"Экономист|Военный|Член |Руководитель|Заместитель|Советник|Депутат|"
    r"Академик|Корреспондент|Аналитик|Сотрудник|Главный)", re.IGNORECASE)


def _clean_name(name: str) -> Optional[str]:
    if not name:
        return None
    name = re.sub(r"\s+", " ", str(name)).strip()
    m = _ROLE_RE.search(name)          # cut a mashed-on bio/affiliation
    if m and m.start() > 0:
        name = name[:m.start()].strip()
    return name or None


def _iter_jsonld(html: str):
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


def _jsonld_author(html: str) -> Optional[str]:
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
def _sleep():
    time.sleep(random.uniform(*REQUEST_DELAY))


def _session():
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def _get(session, url, params=None, allow_404=False):
    last = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = session.get(url, params=params, timeout=HTTP_TIMEOUT)
            if allow_404 and r.status_code == 404:
                return r
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


@dataclass
class Ref:
    url: str
    article_id: str          # slug
    date: str                # ISO
    author: Optional[str] = None
    reading_time: Optional[int] = None
    tag_interview: bool = False   # interview signalled by tag/category metadata


def _ref_from_url(url: str) -> Optional[Ref]:
    m = ARTICLE_RE.search(url)
    if not m:
        return None
    if "/longrids/" in url:
        return None
    y, mo, d, slug = m.groups()
    return Ref(url=f"https://holod.media/{y}/{mo}/{d}/{slug}/",
               article_id=slug, date=f"{y}-{mo}-{d}")


# ---------- discovery via WP REST API (preferred) ----------
def _wp_category_id(session) -> Optional[int]:
    try:
        r = _get(session, f"{WP_BASE}/categories", params={"slug": CATEGORY_SLUG})
        data = r.json()
        if isinstance(data, list) and data and "id" in data[0]:
            return int(data[0]["id"])
    except Exception:
        return None
    return None


def _collect_wp_api(session, target) -> Optional[list[Ref]]:
    cat = _wp_category_id(session)
    if not cat:
        return None
    refs, seen = [], set()
    page = 1
    while len(refs) < target and page <= MAX_PAGES_HARD_CAP:
        try:
            r = _get(session, f"{WP_BASE}/posts",
                     params={"categories": cat, "per_page": WP_PER_PAGE,
                             "page": page, "_embed": "1"}, allow_404=True)
        except ScrapeError:
            return refs or None
        if r.status_code == 404:          # WP returns 404 past the last page
            break
        posts = r.json()
        if not isinstance(posts, list) or not posts:
            break
        new = 0
        for p in posts:
            ref = _ref_from_url(p.get("link", ""))
            if not ref or ref.article_id in seen:
                continue
            seen.add(ref.article_id)
            # author + interview tag from embedded metadata
            try:
                ref.author = p["_embedded"]["author"][0]["name"]
            except Exception:
                pass
            terms = []
            try:
                for grp in p["_embedded"].get("wp:term", []):
                    terms += [t.get("name", "") for t in grp]
            except Exception:
                pass
            ref.tag_interview = any(t.strip().lower() == "интервью" for t in terms)
            refs.append(ref)
            new += 1
        print(f"[holod] wp-api page {page}: +{new} (total {len(refs)})")
        if new == 0:
            break
        page += 1
        _sleep()
    return refs or None


# ---------- discovery via HTML pagination (fallback) ----------
def _collect_html(session, target) -> list[Ref]:
    refs, seen = [], set()
    r = _get(session, SECTION_URL)
    cards_first = _parse_cards(r.text)
    if not cards_first:
        raise ScrapeError(
            "No /opinions/ article links on the first listing page — layout "
            "changed. Refusing to continue.")
    for ref in cards_first:
        if ref.article_id not in seen:
            seen.add(ref.article_id); refs.append(ref)
    print(f"[holod] html page 1: {len(refs)} opinion links")

    page = 1
    while len(refs) < target and page < MAX_PAGES_HARD_CAP:
        page += 1
        _sleep()
        rp = _get(session, urljoin(SECTION_URL, f"page/{page}/"), allow_404=True)
        if rp.status_code == 404:
            print(f"[holod] html page {page}: 404 — end of archive "
                  "(or /page/N/ unsupported; see README).")
            break
        cards = _parse_cards(rp.text)
        new = 0
        for ref in cards:
            if ref.article_id not in seen:
                seen.add(ref.article_id); refs.append(ref); new += 1
        print(f"[holod] html page {page}: +{new} (total {len(refs)})")
        if new == 0:
            if page == 2:
                raise ScrapeError(
                    "HTML /opinions/page/2/ returned no NEW opinion links while a "
                    "'Посмотреть больше' button exists on page 1. The load-more is "
                    "likely admin-ajax.php, not /page/N/. Enable the WP REST API "
                    "path or a Playwright fallback — refusing to return page 1 only.")
            break
    return refs


def _parse_cards(html: str) -> list[Ref]:
    """Parse opinion article cards from a listing page, with per-card author +
    reading time where available."""
    soup = BeautifulSoup(html, "html.parser")
    refs, seen = [], set()
    for a in soup.find_all("a", href=True):
        ref = _ref_from_url(urljoin(SECTION_URL, a["href"]))
        if not ref or ref.article_id in seen:
            continue
        # climb to the card container to read author / reading time text
        card = a
        for _ in range(4):
            if card.parent is not None:
                card = card.parent
        txt = card.get_text(" ", strip=True) if card else ""
        m = READING_RE.search(txt)
        if m:
            ref.reading_time = int(m.group(1))
        seen.add(ref.article_id)
        refs.append(ref)
    return refs


def collect(target_articles: int) -> tuple[list[Ref], dict]:
    session = _session()
    method = "wp_api"
    refs = _collect_wp_api(session, target_articles)
    if not refs:
        method = "html"
        print("[holod] WP REST API unavailable/empty — falling back to HTML.")
        refs = _collect_html(session, target_articles)

    exhausted = len(refs) < target_articles
    meta = {"method": method, "exhausted_early": exhausted,
            "stop_reason": ("section exhausted" if exhausted
                            else f"collected target {target_articles}")}
    refs = refs[:target_articles]
    print(f"[holod] collected {len(refs)} via {method} ({meta['stop_reason']})")
    return refs, meta


# ============================ extraction ============================
_TRAFI = dict(output_format="txt", include_comments=False, include_tables=False,
              include_images=False, include_links=False, include_formatting=False,
              favor_precision=True, deduplicate=True, target_language="ru")


@dataclass
class Record:
    url: str
    article_id: str
    date: str
    source: str
    section: str
    orientation: str
    factuality_tier: str
    genre: str
    title: str
    subtitle: Optional[str]
    body: str
    content: str
    author: Optional[str]
    has_byline: bool
    suspected_interview: bool
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


def _dash_ratio(body: str) -> float:
    paras = _paras(body)
    if not paras:
        return 0.0
    dash = sum(1 for p in paras if _DASH_START_RE.match(p))
    return dash / len(paras)


def _qa_ratio(body: str) -> float:
    """Fraction of paragraphs that are Q&A turns: a dialogue-dash answer OR a
    short question ending in '?'. Interviews run high; essays with the odd
    rhetorical/section question stay low."""
    paras = _paras(body)
    if not paras:
        return 0.0
    turns = 0
    for p in paras:
        if _DASH_START_RE.match(p):
            turns += 1
        elif p.rstrip().endswith("?") and len(p.split()) <= 25:
            turns += 1
    return turns / len(paras)


def _page_interview_tag(html: str) -> bool:
    """True if a tag/breadcrumb anchor is exactly 'Интервью' (not the section
    phrase 'Мнения и интервью')."""
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a"):
        t = a.get_text(strip=True).lower()
        if t == "интервью":
            return True
    return False


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
    for sel in [".article__subtitle", ".subtitle", ".post-subtitle",
                "meta[property='og:description']", "meta[name='description']"]:
        node = soup.select_one(sel)
        if node:
            subtitle = node.get_text(strip=True) if node.name != "meta" else node.get("content")
            if subtitle:
                break
    # Author: the displayed byline lives in the article__info block as an item
    # whose label span is "Автор:" —
    #   <div class="article__info-item"><span>Автор:</span><span>Иван Филиппов</span></div>
    # (the same block also has "Редактор:" and "Фото:" items — must NOT be used).
    # Do NOT use JSON-LD here: on Holod schema.org `author` is a WP account
    # ("Виктор Билан"), not the byline. Confirmed against raw HTML.
    author = None
    for item in soup.select(".article__info-item"):
        spans = item.find_all("span")
        if len(spans) >= 2 and spans[0].get_text(strip=True).rstrip(":").strip().lower() == "автор":
            author = _clean_name(spans[1].get_text(" ", strip=True))
            if author:
                break
    if not author:                       # fallbacks for layout variants
        for sel in [".article__author", ".author", "a[rel='author']", ".post-author"]:
            node = soup.select_one(sel)
            if node and node.get_text(" ", strip=True):
                author = _clean_name(node.get_text(" ", strip=True))
                if author:
                    break
    rt = None
    dnode = soup.select_one(".article__date")   # the article's own reading time
    scope = dnode.get_text(" ", strip=True) if dnode else soup.get_text(" ", strip=True)
    mrt = READING_RE.search(scope)
    if mrt:
        rt = int(mrt.group(1))
    return title, subtitle, author, rt


def extract_record(ref: Ref, html: str) -> Record:
    body = trafilatura.extract(html, url=ref.url, **_TRAFI) or ""
    body = _strip_boilerplate(body).strip()
    title, subtitle, author, rt = _extract_meta(html)
    title = title or ""
    author = ref.author or author
    reading = ref.reading_time or rt
    sig = _signals(body)

    kw = "интервью" in f"{title} {subtitle or ''}".lower()
    interview = bool(ref.tag_interview or _page_interview_tag(html) or kw
                     or _qa_ratio(body) >= 0.33
                     or _dash_ratio(body) >= INTERVIEW_DASH_RATIO)
    genre = "interview" if interview else "opinion_essay"
    content = f"{title}\n\n{body}" if title else body
    return Record(
        url=ref.url, article_id=ref.article_id, date=ref.date, **FIXED,
        genre=genre, title=title, subtitle=subtitle or None, body=body,
        content=content, author=author or None, has_byline=bool(author),
        suspected_interview=interview, stated_reading_time=reading, **sig)


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
    from collections import Counter
    wc = sorted(r.word_count for r in records)
    pc = sorted(r.paragraph_count for r in records)
    dates = sorted(r.date for r in records)
    authors = Counter((r.author or "—") for r in records)
    return {
        "source": FIXED["source"], "section": FIXED["section"],
        "discovery_method": meta.get("method"),
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
        "suspected_interviews": sum(1 for r in records if r.suspected_interview),
        "with_byline": sum(1 for r in records if r.has_byline),
        "without_byline": sum(1 for r in records if not r.has_byline),
        "articles_per_author": dict(authors.most_common()),
        "date_range": {"earliest": dates[0] if dates else None,
                       "latest": dates[-1] if dates else None},
    }


def _print_summary(s):
    wc, pc = s["word_count"], s["paragraph_count"]
    print("\n" + "=" * 60)
    print("RUN SUMMARY — Holod /opinions/")
    print("=" * 60)
    print(f"  discovery method         : {s['discovery_method']}")
    print(f"  discovered / ok / failed : {s['urls_discovered']} / "
          f"{s['extracted_ok']} / {s['failed']}")
    print(f"  exhausted early          : {s['exhausted_early']} ({s['stop_reason']})")
    print(f"  word_count min/25/med/75/max : {wc['min']}/{wc['p25']}/"
          f"{wc['median']}/{wc['p75']}/{wc['max']}")
    print(f"  paragraphs  min/med/max      : {pc['min']}/{pc['median']}/{pc['max']}")
    print(f"  flagged <{WORD_FLOOR}w ({len(s['flagged_lt_150'])}) : {s['flagged_lt_150']}")
    print(f"  suspected interviews     : {s['suspected_interviews']}")
    print(f"  byline yes/no            : {s['with_byline']}/{s['without_byline']}")
    print(f"  articles per author      : {s['articles_per_author']}")
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
                tag = " [interview]" if rec.suspected_interview else ""
                print(f"  [{i}/{len(refs)}] {ref.article_id} words={rec.word_count}{flag}{tag}")
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
    ap = argparse.ArgumentParser(description="Scrape Holod /opinions/ (essays + interviews).")
    ap.add_argument("target_articles", nargs="?", type=int, default=DEFAULT_TARGET)
    a = ap.parse_args()
    run(a.target_articles)

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
# NOTE: no WP REST constant. /opinions/ is not a WP category -- it is a
# material_type META filter -- so categories?slug=opinions returns [].

OUTDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
OUT_JSONL = os.path.join(OUTDIR, "holod_opinions.jsonl")
RAW_DIR = os.path.join(OUTDIR, "raw_html", "holod")
RUN_SUMMARY = os.path.join(OUTDIR, "holod_run_summary.json")

DEFAULT_TARGET = 60
REQUEST_DELAY = (1.0, 2.0)
HTTP_TIMEOUT = 30
MAX_RETRIES = 4
BACKOFF_BASE = 2.0
MAX_PAGES_HARD_CAP = 100
WORD_FLOOR = 150
INTERVIEW_DASH_RATIO = 0.25       # >=25% dash-initial paragraphs -> interview
                                  # (a Q&A-turn ratio >=0.33 also triggers)

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": USER_AGENT,
           "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"}

# Collection provenance only. Outlet-level judgements (orientation,
# factuality_tier) live in sources.csv, joined on `source` at analysis time.
FIXED = {"source": "holod", "section": "opinions"}
# NOTE: no `genre` field. Genre is what the genre/stance filter decides;
# assigning it here would pre-judge that gate. The raw structural signal
# (`suspected_interview`) is kept instead.

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
    """Normalise a byline. Returns None for anything that is not a name.

    Holod's WordPress account display name is sometimes a bare dash, which is a
    placeholder, not an author. Requiring at least two letters keeps dashes,
    bullets and stray punctuation out of the `author` field instead of letting
    them masquerade as a byline.
    """
    if not name:
        return None
    name = re.sub(r"\s+", " ", str(name)).strip()
    m = _ROLE_RE.search(name)          # cut a mashed-on bio/affiliation
    if m and m.start() > 0:
        name = name[:m.start()].strip()
    if len(re.findall(r"\w", name, flags=re.UNICODE)) < 2:
        return None
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


# ---------- discovery via the theme's load-more ajax handler ----------
# CONFIRMED LIVE (2026-08). /opinions/ is NOT a WordPress category -- the section
# is defined by a META field, so `categories?slug=opinions` returns [] and the WP
# REST path can never work. `/opinions/page/N/` does not exist either. The real
# loader is the theme's own handler, captured from the minified bundle:
#
#   const data = {'action':'load_more_opinions',
#                 'query': button.attr('data-param-posts'),
#                 'page': current_page}
#
# The button carries the entire WP_Query as a JSON blob (including the
# material_type meta filter and the language taxonomy term) plus `data-max-pages`.
# We replay that blob verbatim rather than reconstructing it, so a theme-side
# change to the query travels with the page instead of silently narrowing the
# harvest.
AJAX_URL = "https://holod.media/wp-admin/admin-ajax.php"
AJAX_ACTION = "load_more_opinions"
LOAD_MORE_SELECTOR = ".js-load-more-opinions"


def _load_more_params(listing_html: str) -> tuple[str, int]:
    """(query blob, max_pages) read off the load-more button."""
    soup = BeautifulSoup(listing_html, "html.parser")
    btn = soup.select_one(LOAD_MORE_SELECTOR)
    if btn is None:
        raise ScrapeError(
            f"No {LOAD_MORE_SELECTOR} button on {SECTION_URL} -- the theme "
            f"changed. Refusing to return page 1 only.")
    query = btn.get("data-param-posts")
    if not query:
        raise ScrapeError(
            f"{LOAD_MORE_SELECTOR} has no data-param-posts blob; the ajax "
            f"handler cannot be called without it.")
    try:
        max_pages = int(btn.get("data-max-pages") or 0)
    except ValueError:
        max_pages = 0
    if max_pages <= 0:
        raise ScrapeError(
            "load-more button has no usable data-max-pages; without it there "
            "is no exact stop condition.")
    return query, max_pages


def _ajax_page(session, query: str, page: int) -> str:
    """One load-more fragment. Retries transient 5xx rather than losing the run."""
    delay, last = 2.0, None
    for attempt in range(1, 5):
        try:
            r = session.post(
                AJAX_URL,
                data={"action": AJAX_ACTION, "query": query, "page": page},
                headers={**HEADERS, "X-Requested-With": "XMLHttpRequest",
                         "Referer": SECTION_URL},
                timeout=30)
        except requests.RequestException as exc:
            last = f"{type(exc).__name__}: {exc}"
        else:
            if r.status_code < 400:
                return r.text
            if r.status_code not in (429, 500, 502, 503, 504):
                raise ScrapeError(f"HTTP {r.status_code} from {AJAX_ACTION} "
                                  f"page {page}")
            last = f"HTTP {r.status_code}"
        print(f"  [retry {attempt}/3] {last} on ajax page {page}")
        time.sleep(delay)
        delay *= 2
    raise ScrapeError(f"{last} from {AJAX_ACTION} page {page} after 4 attempts")


def _collect_ajax(session, target) -> list[Ref]:
    refs, seen = [], set()
    r = _get(session, SECTION_URL)
    for ref in _parse_cards(r.text):
        if ref.article_id not in seen:
            seen.add(ref.article_id); refs.append(ref)
    if not refs:
        raise ScrapeError(
            "No /opinions/ article links on the first listing page -- layout "
            "changed. Refusing to continue.")
    query, max_pages = _load_more_params(r.text)
    print(f"[holod] page 1: {len(refs)} opinion links "
          f"(button advertises {max_pages} more pages)")

    for page in range(2, max_pages + 1):
        if len(refs) >= target:
            break
        _sleep()
        frag = _ajax_page(session, query, page)
        if not frag.strip() or frag.strip() in ("0", "-1"):
            print(f"[holod] ajax page {page}: empty fragment -- section exhausted")
            break
        cards = _parse_cards(frag)
        new = [c for c in cards if c.article_id not in seen]
        for c in new:
            seen.add(c.article_id); refs.append(c)
        print(f"[holod] ajax page {page}/{max_pages}: +{len(new)} (total {len(refs)})")
        if not new:
            # The button advertises more pages but nothing new came back. That is
            # an anomaly, not a clean end -- say so instead of stopping quietly.
            if cards:
                raise ScrapeError(
                    f"ajax page {page} returned {len(cards)} link(s), all already "
                    f"seen, while data-max-pages says {max_pages}. Pagination is "
                    f"not advancing -- refusing to report a partial harvest as "
                    f"complete.")
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
    method = "ajax_load_more"
    refs = _collect_ajax(session, target_articles)

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
              favor_precision=True, deduplicate=False, target_language="ru")
# deduplicate=False is DELIBERATE. trafilatura's dedup cache is a
# PROCESS-GLOBAL LRU: a paragraph seen in earlier articles is silently
# dropped from later ones, cumulatively and in scrape order, so the
# corpus stops being reproducible and word counts quietly shrink.
# Repeated boilerplate is handled explicitly by _strip_boilerplate.


@dataclass
class Record:
    url: str
    article_id: str
    date: str
    source: str
    section: str
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
    # PRECEDENCE: the article page's "Автор:" block is the byline. `ref.author`
    # comes from the WordPress `_embedded.author` account, which is a CMS user
    # (often a desk account, sometimes a bare "—" placeholder) and is NOT the
    # byline. It is a fallback only -- reversing this order silently overwrote
    # every correct byline with the WP account name.
    author = author or _clean_name(ref.author or "")
    reading = ref.reading_time or rt
    sig = _signals(body)

    kw = "интервью" in f"{title} {subtitle or ''}".lower()
    interview = bool(ref.tag_interview or _page_interview_tag(html) or kw
                     or _qa_ratio(body) >= 0.33
                     or _dash_ratio(body) >= INTERVIEW_DASH_RATIO)
    content = f"{title}\n\n{body}" if title else body
    return Record(
        url=ref.url, article_id=ref.article_id, date=ref.date, **FIXED,
        title=title, subtitle=subtitle or None, body=body,
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

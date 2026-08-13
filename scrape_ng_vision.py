#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Nezavisimaya Gazeta — "Я так вижу" (/vision/) opinion scraper.

STANDALONE. No shared abstraction with the other scrapers by design — these
sites differ structurally and duplication is preferred over a base class.

DISCOVERY (confirmed live):
  * Bitrix CMS, plain server-rendered HTML. The SIMPLEST of the three: no JS
    pagination.
  * Article URL: https://www.ng.ru/vision/YYYY-MM-DD/<slug>.html
    e.g. https://www.ng.ru/vision/2026-07-21/6_9542_contact.html
    -> date is in the URL path; article_id = filename stem ("6_9542_contact").
  * Cards / article pages show author ("Олег Никифоров") and a one-line deck
    (standfirst) captured as `subtitle`.
  * Pagination: classic numbered, ?PAGEN_1=<N>. Page 1 is the bare /vision/ URL,
    N=2,3,... afterwards. At time of writing ~137 pages — detected dynamically,
    NOT hardcoded.
  * PARSING HAZARD: the /vision/ listing renders a large sidebar of OTHER NG
    sections (/politics/, /economics/, /world/, /ideas/, /monitoring/, ...).
    Link harvesting is restricted to the /vision/YYYY-MM-DD/*.html pattern ONLY,
    and every collected URL is asserted to contain "/vision/".

FAIL-LOUD: raises on HTTP errors, on a first page with zero /vision/ links
(structure changed), and on any collected URL that is not under /vision/.
Never returns a silent short list.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import statistics
import time
from dataclasses import dataclass, asdict
from typing import Optional
from urllib.parse import urljoin

import requests
import trafilatura
from bs4 import BeautifulSoup

# ============================ CONFIG ============================
SECTION_URL = "https://www.ng.ru/vision/"
PAGE_PARAM = "PAGEN_1"

OUTDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
OUT_JSONL = os.path.join(OUTDIR, "ng_opinions.jsonl")
RAW_DIR = os.path.join(OUTDIR, "raw_html", "ng")
RUN_SUMMARY = os.path.join(OUTDIR, "ng_run_summary.json")

DEFAULT_TARGET = 60
REQUEST_DELAY = (1.0, 2.0)
HTTP_TIMEOUT = 30
MAX_RETRIES = 4
BACKOFF_BASE = 2.0
MAX_PAGES_HARD_CAP = 200          # safety net; real end detected dynamically
WORD_FLOOR = 150

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": USER_AGENT,
           "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"}

# Collection provenance only. Outlet-level judgements (orientation,
# factuality_tier) are properties of the OUTLET, not of the text: they live
# in sources.csv and are joined on `source` at analysis time.
FIXED = {"source": "ng", "section": "vision"}
# NOTE: no `genre` field -- genre is what the genre/stance filter decides;
# asserting it here would pre-judge that gate.

# /vision/YYYY-MM-DD/<slug>.html  — the ONLY link shape we harvest.
ARTICLE_RE = re.compile(r"/vision/(\d{4}-\d{2}-\d{2})/([^/\"'#?<>\s]+)\.html")

# Donation / subscription boilerplate lines to strip at extraction time.
# Scoped stems so legitimate body words (поддержать/подписал) are not clobbered.
BOILERPLATE_RE = re.compile(
    r"(подпишите|подпишись|подписывайт|подписаться|подписк|рассылк|"
    r"пожертвован|донат|поддержите нас|поддержать нас|поддержите редакц|"
    r"наш телеграм|телеграм-канал|читайте так ?же|материалы по теме|"
    r"свежий номер независимой газеты)", re.IGNORECASE)

# Photo-credit caption line (leaks in as the first body block on NG):
# "<caption text> Фото Reuters" / "Фото ТАСС" / "Фото пресс-службы Кремля".
# Anchored to end-of-line and no terminal sentence punctuation, so a normal
# sentence that merely mentions "фото" is not mistaken for a caption.
CAPTION_RE = re.compile(r"Фото\s+[^.!?\n]{2,40}$", re.IGNORECASE)

# Masthead / promo strings that are NOT a real article deck.
SUBTITLE_BLACKLIST = {"свежий номер независимой газеты",
                      "независимая газета"}

_SENT_RE = re.compile(r"[.!?…]+(?=\s|$)")

# JSON-LD is the cleanest author source (name without the mashed-on bio).
_JSONLD_RE = re.compile(
    r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', re.DOTALL | re.IGNORECASE)
_ROLE_RE = re.compile(
    r"\s+(Аспирант|Профессор|Доктор|Кандидат|Обозреватель|Редактор|Ответственн|"
    r"Основатель|Директор|Президент|Эксперт|Политолог|Журналист|Публицист|"
    r"Экономист|Военный|Член |Руководитель|Заместитель|Советник|Депутат|"
    r"Академик|Корреспондент|Аналитик|Сотрудник|Главный|Кор\.)", re.IGNORECASE)


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
def _sleep():
    time.sleep(random.uniform(*REQUEST_DELAY))


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def _get(session, url, params=None):
    last = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = session.get(url, params=params, timeout=HTTP_TIMEOUT)
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
    article_id: str
    date: str      # ISO YYYY-MM-DD


def parse_refs(html: str, base: str) -> list[Ref]:
    """Harvest ONLY /vision/ article links. Deduped by url, order preserved."""
    seen, refs = set(), []
    for m in ARTICLE_RE.finditer(html):
        url = urljoin(base, m.group(0))
        if url in seen:
            continue
        seen.add(url)
        refs.append(Ref(url=url, article_id=m.group(2), date=m.group(1)))
    return refs


def detect_last_page(html: str) -> Optional[int]:
    """Read the highest ?PAGEN_1=N in the pagination control."""
    nums = [int(n) for n in re.findall(rf"{PAGE_PARAM}=(\d+)", html)]
    return max(nums) if nums else None


# ============================ collection ============================
def collect(target_articles: int) -> tuple[list[Ref], dict]:
    session = _session()
    collected: list[Ref] = []
    seen_ids: set[str] = set()
    exhausted = False
    reason = ""

    r = _get(session, SECTION_URL)
    last_page = detect_last_page(r.text)
    first_refs = parse_refs(r.text, SECTION_URL)
    if not first_refs:
        raise ScrapeError(
            "No /vision/ article links on page 1 — layout changed or the "
            "section markup moved. Refusing to continue.")
    print(f"[ng] page 1: {len(first_refs)} vision links; "
          f"last_page={last_page}")

    def _add(refs) -> int:
        n = 0
        for ref in refs:
            # HARD ASSERT: never let a non-/vision/ URL through.
            if "/vision/" not in ref.url:
                raise ScrapeError(f"Non-/vision/ URL leaked in: {ref.url}")
            if ref.article_id not in seen_ids:
                seen_ids.add(ref.article_id)
                collected.append(ref)
                n += 1
        return n

    _add(first_refs)
    page = 1
    while len(collected) < target_articles:
        if last_page and page >= last_page:
            exhausted, reason = True, f"reached last page {last_page}"
            break
        if page >= MAX_PAGES_HARD_CAP:
            exhausted, reason = True, f"hit hard cap {MAX_PAGES_HARD_CAP}"
            break
        page += 1
        _sleep()
        rp = _get(session, SECTION_URL, params={PAGE_PARAM: page})
        refs = parse_refs(rp.text, SECTION_URL)
        added = _add(refs)
        print(f"[ng] page {page}: +{added} new (total {len(collected)})")
        if added == 0:
            # No new articles on a well-formed later page => end of archive.
            if not refs:
                exhausted, reason = True, f"page {page} had no vision links"
            else:
                exhausted, reason = True, f"page {page} only repeated seen ids"
            break

    meta = {"exhausted_early": exhausted and len(collected) < target_articles,
            "stop_reason": reason or f"collected target {target_articles}",
            "last_page_detected": last_page, "pages_fetched": page}
    print(f"[ng] collected {len(collected)} refs ({meta['stop_reason']})")
    return collected[:target_articles] if len(collected) > target_articles else collected, meta


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
    kept = []
    for ln in text.split("\n"):
        s = ln.strip()
        if not s:
            continue
        if BOILERPLATE_RE.search(s):
            continue
        # Drop a leading photo-credit caption line ("… Фото Reuters"), but only
        # when it's short (a real caption), never a long body paragraph.
        if len(s) < 200 and CAPTION_RE.search(s):
            continue
        kept.append(ln)
    return "\n".join(kept)


def _paras(body: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n+", body) if p.strip()]


def _signals(body: str) -> dict:
    paras = _paras(body)
    wc = len(body.split())
    pc = len(paras)
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
    # NG deck/standfirst: prefer a real deck element / og:description, but reject
    # the masthead promo ("Свежий номер Независимой газеты") that NG serves as a
    # fallback description — better a null subtitle than a wrong one.
    subtitle = None
    for sel in [".subtitle", ".article-subtitle", "h2.subtitle",
                "meta[property='og:description']", "meta[name='description']"]:
        node = soup.select_one(sel)
        if not node:
            continue
        cand = node.get_text(strip=True) if node.name != "meta" else node.get("content")
        cand = (cand or "").strip()
        if cand and cand.lower() not in SUBTITLE_BLACKLIST:
            subtitle = cand
            break
    # Author: JSON-LD first (clean), else DOM byline, trimmed of any mashed bio.
    author = _jsonld_author(html)
    if not author:
        for sel in [".author a", ".author", "a[rel='author']", ".autor",
                    ".b-author", "[class*='author']"]:
            node = soup.select_one(sel)
            if node and node.get_text(" ", strip=True):
                author = _clean_name(node.get_text(" ", strip=True))
                if author:
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
    # Reject a subtitle that is not a real deck: some /vision/ pieces have no
    # standfirst, so og:description falls back to a photo credit + the body lead
    # (e.g. "Фото сайта akorda.kz Отношения России..."). Drop those.
    if subtitle:
        s = subtitle.strip()
        if s.startswith("Фото") or (len(s) >= 30 and s[:30] in body):
            subtitle = None
    sig = _signals(body)
    content = f"{title}\n\n{body}" if title else body
    return Record(
        url=ref.url, article_id=ref.article_id, date=ref.date, **FIXED,
        title=title, subtitle=subtitle or None, body=body, content=content,
        author=author or None, has_byline=bool(author),
        stated_reading_time=None, **sig)


def fetch_html(session, url) -> str:
    return _get(session, url).text


# ============================ output / summary ============================
def _pct(sorted_vals, q):
    if not sorted_vals:
        return None
    i = q * (len(sorted_vals) - 1)
    lo = int(i)
    frac = i - lo
    if lo + 1 < len(sorted_vals):
        return round(sorted_vals[lo] * (1 - frac) + sorted_vals[lo + 1] * frac, 1)
    return sorted_vals[lo]


def summarize(discovered, records, failed, meta):
    wc = sorted(r.word_count for r in records)
    pc = sorted(r.paragraph_count for r in records)
    dates = sorted(r.date for r in records)
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
    }


def _print_summary(s):
    wc, pc = s["word_count"], s["paragraph_count"]
    print("\n" + "=" * 60)
    print(f"RUN SUMMARY — NG /vision/")
    print("=" * 60)
    print(f"  discovered / ok / failed : {s['urls_discovered']} / "
          f"{s['extracted_ok']} / {s['failed']}")
    print(f"  exhausted early          : {s['exhausted_early']} ({s['stop_reason']})")
    print(f"  word_count min/25/med/75/max : {wc['min']}/{wc['p25']}/"
          f"{wc['median']}/{wc['p75']}/{wc['max']}")
    print(f"  paragraphs  min/med/max      : {pc['min']}/{pc['median']}/{pc['max']}")
    print(f"  flagged <{WORD_FLOOR}w ({len(s['flagged_lt_150'])}) : {s['flagged_lt_150']}")
    print(f"  byline yes/no            : {s['with_byline']}/{s['without_byline']}")
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
                with open(os.path.join(RAW_DIR, f"{ref.date}_{ref.article_id}.html"),
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
    ap = argparse.ArgumentParser(description="Scrape NG /vision/ opinion essays.")
    ap.add_argument("target_articles", nargs="?", type=int, default=DEFAULT_TARGET)
    a = ap.parse_args()
    run(a.target_articles)

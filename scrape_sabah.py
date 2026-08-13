#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scrape_sabah.py — KuKi corpus per-source scraper for Sabah columnists.

Standalone module. NO shared abstraction with the other TR scrapers
(duplication is deliberate; matches the RU convention).

Collection provenance (the only outlet fields stored per record)
    source            = sabah
    section           = yazarlar
Outlet-level judgements (orientation, factuality_tier, expected genre) are
properties of the OUTLET, not of the text: they live in sources.csv and are
joined on `source` at analysis time.
    (State-aligned mainstream counterpart to ria.ru.)

Collection
    Author archive: https://www.sabah.com.tr/yazarlar/{author}/arsiv/getall
    Returns the WHOLE author archive in ONE response — no pagination loop.
    "End" == the single page parsed. Simplest of the four.
    NOTE (confirmed live): the endpoint is a PATH segment '/arsiv/getall', not a
    '?getall=true' query, and Sabah author slugs are HYPHEN-FREE
    (e.g. 'melihaltinok', 'ardic', 'donat') — unlike the hyphenated slugs of the
    other three sources.

Article
    URL:        /yazarlar/{author}/{YYYY}/{MM}/{DD}/{title-slug}
    date:       parsed from the URL path, cross-checked against
                meta-datePublished — FAIL LOUD on mismatch.
    article_id: the {title-slug} (no numeric ID); combined with the date for
                a unique dedup key / raw filename.
    Meta:       full ISO, meta-articleSection: columnist.
    Body:       title is '## {title}'; body runs from there to the
                "Yasal Uyarı:" copyright block — strip it and the
                app-download / previous-articles rail after it.

HAZARDS handled
    1. Cloudflare — realistic UA/header set + retry/backoff. If blocked, FAIL
       LOUD with the block reason (never silently return empty).
    2. <br>-separated paragraphs (NOT <p>). A Sabah-specific splitter treats
       <br><br> AND the '*******' section dividers as paragraph boundaries;
       sentence_count is computed as an independent cross-check. See
       sabah_extract_body().
    3. Hard "kesinlikle kullanılamaz" copyright notice — acceptable for an
       internal research corpus; see README provenance section.

Live-only steps (flagged in run summary):
    * Sabah Cloudflare behavior under the chosen header set.

Run:
    python scrape_sabah.py --smoke
    python scrape_sabah.py --full --authors mahmut-ovur,okan-muderrisoglu
"""

import argparse
import json
import os
import re
import statistics
import sys
import time
from datetime import datetime, date
from urllib.parse import urljoin, urlsplit, urlunsplit

import trafilatura
from lxml import html as lxml_html

# ----------------------------------------------------------------------------
SOURCE = "sabah"
SECTION = "yazarlar"
# Collection provenance. Outlet-level judgements (orientation,
# factuality_tier) and expected genre are properties of the OUTLET, not of
# the text -- they live in sources.csv and are joined on `source` at analysis
# time. Genre in particular is what the genre/stance filter decides; asserting
# it on every record would pre-judge that gate.

BASE = "https://www.sabah.com.tr"
ALLOWED_HOST = "www.sabah.com.tr"

# Sabah slugs have mixed hyphenation (melihaltinok, ardic BUT bercan-tutar) — so
# authors are best DISCOVERED from the on-target category hubs, not guessed.
DEFAULT_AUTHORS = ["melihaltinok", "ardic", "donat"]

# Columnists are grouped by category under /yazarlar/{category} (confirmed live).
# ON-target = opinion/analysis; the rest are excluded wholesale.
ON_TARGET_CATEGORIES = ["sabah", "site", "perspektif", "bolgeler"]
# Every known category slug (so hub author-parsing never treats one as an author).
CATEGORY_SLUGS = {
    "sabah", "site", "perspektif", "bolgeler", "pazar", "cumartesi", "kitap",
    "gunaydin", "spor", "ekonomi", "magazin", "arsiv", "sabaharsiv",
}

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
    # is garbage (empty <h1>/og:title, no links).
    "Accept-Encoding": "gzip, deflate",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Connection": "keep-alive",
}

LOW_CONTENT_WORDS = 150
SMOKE_TARGET = 15
MAX_PAGES = 1000


class ScrapeError(RuntimeError):
    pass


class CloudflareBlock(ScrapeError):
    pass


# ----------------------------------------------------------------------------
# Turkish helpers (locale-independent; NEVER naive .lower())
# ----------------------------------------------------------------------------
_TR_UPPER_TO_LOWER = {
    "I": "ı", "İ": "i", "Ş": "ş", "Ğ": "ğ", "Ç": "ç", "Ö": "ö", "Ü": "ü",
}


def tr_lower(s: str) -> str:
    out = []
    for ch in s:
        out.append(_TR_UPPER_TO_LOWER.get(ch, ch.lower()))
    return "".join(out)


def norm_key(s: str) -> str:
    return re.sub(r"\s+", " ", tr_lower(s)).strip()


TR_MONTHS = {
    "ocak": 1, "şubat": 2, "mart": 3, "nisan": 4, "mayıs": 5, "haziran": 6,
    "temmuz": 7, "ağustos": 8, "eylül": 9, "ekim": 10, "kasım": 11,
    "aralık": 12,
}
_LONGFORM_RE = re.compile(r"(\d{1,2})\s+([A-Za-zÇĞİıÖŞÜçğşöü]+)\s+(\d{4})")


def parse_iso_date(value: str):
    if not value:
        return None
    v = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(v).date().isoformat()
    except ValueError:
        try:
            return date.fromisoformat(v[:10]).isoformat()
        except ValueError:
            return None


def parse_tr_longform_date(text: str):
    if not text:
        return None
    m = _LONGFORM_RE.search(text)
    if not m:
        return None
    month = TR_MONTHS.get(tr_lower(m.group(2)))
    if not month:
        return None
    try:
        return date(int(m.group(3)), month, int(m.group(1))).isoformat()
    except ValueError:
        return None


# ----------------------------------------------------------------------------
# Link scoping + date-from-URL (pure)
# ----------------------------------------------------------------------------
# /yazarlar/{author}/{YYYY}/{MM}/{DD}/{title-slug}
_ARTICLE_RE = re.compile(
    r"^/yazarlar/([^/]+)/(\d{4})/(\d{2})/(\d{2})/([^/]+?)/?$"
)


def normalize_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme or "https", parts.netloc,
                       parts.path.rstrip("/"), "", ""))


def scope_link(href: str, base: str = BASE):
    """Return (article_id, date_iso, uid) for an in-scope Sabah column, else None.

    article_id = title-slug; uid = 'YYYY-MM-DD__slug' (unique dedup / raw name).
    date comes straight from the URL path.
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
    _author, yyyy, mm, dd, slug = m.groups()
    try:
        d = date(int(yyyy), int(mm), int(dd)).isoformat()
    except ValueError:
        return None
    return slug, d, f"{d}__{slug}"


def parse_archive_links(archive_html: str, base: str = BASE):
    """Parse the single arsiv?getall=true page. Returns list of
    (url, article_id, date_iso, uid) de-duped by uid, in order. Pure."""
    doc = lxml_html.fromstring(archive_html)
    out, seen = [], set()
    for a in doc.xpath("//a[@href]"):
        scoped = scope_link(a.get("href"), base=base)
        if not scoped:
            continue
        slug, d, uid = scoped
        if uid in seen:
            continue
        seen.add(uid)
        url = normalize_url(urljoin(base + "/", a.get("href")))
        out.append((url, slug, d, uid))
    return out


# On the hubs, author links are /yazarlar/{slug}/arsiv/getall (confirmed live) —
# accept the bare /yazarlar/{slug} and the /arsiv[/getall] archive forms.
_HUB_AUTHOR_RE = re.compile(r"^/yazarlar/([^/]+)(?:/arsiv(?:/getall)?)?/?$")


def parse_hub_authors(hub_html: str, base: str = BASE):
    """Extract author slugs from a category hub page (e.g. /yazarlar/perspektif).
    Excludes category slugs and non-author links. Pure/offline-testable."""
    doc = lxml_html.fromstring(hub_html)
    slugs, seen = [], set()
    for a in doc.xpath("//a[@href]"):
        parts = urlsplit(urljoin(base + "/", a.get("href")))
        if parts.netloc and parts.netloc != ALLOWED_HOST:
            continue
        m = _HUB_AUTHOR_RE.match(parts.path)
        if not m:
            continue
        slug = m.group(1)
        if slug in CATEGORY_SLUGS or slug in seen:
            continue
        seen.add(slug)
        slugs.append(slug)
    return slugs


# ----------------------------------------------------------------------------
# Extraction — Sabah-specific <br>/***** paragraph splitter
# ----------------------------------------------------------------------------
_BODY_CUT_MARKERS = ["Yasal Uyarı", "Uygulamayı İndir", "Önceki Yazıları",
                     "Yazarın Diğer Yazıları"]
_PARA_MARK = ""


def truncate_at_markers(text: str, markers=_BODY_CUT_MARKERS) -> str:
    cut = len(text)
    low = tr_lower(text)
    for mk in markers:
        idx = low.find(tr_lower(mk))
        if idx != -1:
            cut = min(cut, idx)
    return text[:cut].rstrip()


def _find_body_node(doc):
    xpaths = [
        "//div[@itemprop='articleBody']",
        "//div[contains(@class,'yaziGovde')]",
        "//div[contains(@class,'yazarDetay')]",
        "//div[contains(@class,'newsDetailText')]",
        "//div[contains(@class,'article-body')]",
        "//div[contains(@class,'postDetayText')]",
        "//div[contains(@class,'newsBox')]",
        "//div[contains(@class,'text')]//div[contains(@class,'detail')]",
    ]
    for xp in xpaths:
        nodes = doc.xpath(xp)
        if nodes:
            return nodes[0]
    # Heuristic fallback: Sabah bodies are <br>-structured. The element with the
    # most DIRECT-CHILD <br> nodes is the body container even when the class name
    # is unknown/changed (direct-child count avoids picking an outer wrapper).
    best, best_br = None, 0
    for el in doc.xpath("//div | //article | //section | //p"):
        n = sum(1 for c in el if isinstance(c.tag, str) and c.tag == "br")
        if n > best_br:
            best, best_br = el, n
    if best is not None and best_br >= 2:
        return best
    return None


def sabah_paragraphs_from_html(inner_html: str):
    """Convert a <br>-structured body fragment into paragraph list.

    Rules: 2+ consecutive <br> => paragraph boundary; a single <br> => space;
    a '*******' divider line => paragraph boundary. Pure/offline-testable.
    """
    # 2+ <br> (optionally whitespace-separated) => paragraph marker
    s = re.sub(r"(?i)(?:\s*<br\s*/?>\s*){2,}", _PARA_MARK, inner_html)
    # remaining single <br> => space
    s = re.sub(r"(?i)<br\s*/?>", " ", s)
    text = lxml_html.fromstring("<div>%s</div>" % s).text_content()
    # '*****' section dividers => paragraph boundary
    text = re.sub(r"\*{4,}", _PARA_MARK, text)
    text = truncate_at_markers(text)
    paras = [re.sub(r"\s+", " ", p).strip()
             for p in text.split(_PARA_MARK)]
    return [p for p in paras if p]


# Footer/nav labels that must never count as body paragraphs.
_NAV_JUNK = {"veri politikası", "iş ilanları", "künye", "reklam", "abone ol",
             "günün özeti", "yasal uyarı", "gizlilik", "çerez politikası",
             "iletişim", "foto galeri", "video galeri", "yazıyı paylaş",
             "etiketler", "e-gazete"}
# "read more" teaser that gets appended to short columns.
_TEASER = re.compile(r"(?i)(ayrıntılar için|devamı için|haberin devamı).*"
                     r"tıklay")


def _has_double_br(s: str) -> bool:
    return bool(re.search(r"(?i)(?:<br\s*/?>\s*){2,}", s or ""))


def sabah_extract_body(article_html: str):
    """Return a body string with paragraphs joined by '\\n\\n' (so the default
    blank-line splitter yields the correct, non-degenerate paragraph_count).

    Confirmed live: current Sabah columns are <p>-structured (NO <br><br>). The
    <br> splitter is kept ONLY for legacy <br><br> bodies; otherwise we use
    trafilatura markdown segmentation, identical to the other three scrapers."""
    doc = lxml_html.fromstring(article_html)
    node = _find_body_node(doc)
    inner = ""
    if node is not None:
        # node.text = leading text before first child; tostring(child) carries
        # each child's tail text, so nothing is lost.
        inner = node.text or ""
        for child in node:
            inner += lxml_html.tostring(child, encoding="unicode")

    if _has_double_br(inner):
        paras = sabah_paragraphs_from_html(inner)          # legacy <br><br>
    else:
        md = trafilatura.extract(
            article_html, include_tables=False, include_comments=False,
            include_images=False, favor_recall=True, output_format="markdown",
        ) or ""
        md = re.sub(r"[*_]{1,}", "", md)   # drop emphasis + '***' dividers
        md = truncate_at_markers(md)
        paras = [re.sub(r"\s+", " ", p).strip()
                 for p in re.split(r"\n\s*\n", md) if p.strip()]

    # Drop nav labels, "read more" teasers, and exact-duplicate paragraphs.
    cleaned, seen = [], set()
    for p in paras:
        core = p.lstrip("# ").strip()
        key = norm_key(core)
        if not key or key in _NAV_JUNK or _TEASER.search(core) or key in seen:
            continue
        seen.add(key)
        cleaned.append(p)
    return "\n\n".join(cleaned)


def _meta(doc, name=None, prop=None, itemprop=None):
    for attr, val in (("name", name), ("property", prop),
                      ("itemprop", itemprop)):
        if val:
            v = doc.xpath(f"//meta[@{attr}={val!r}]/@content")
            if v:
                return v[0].strip()
    return ""


def extract_article(article_html: str, url: str, url_date: str,
                    slug: str) -> dict:
    doc = lxml_html.fromstring(article_html)

    # Author first (reliable meta) so the title extractor can guarantee title!=author.
    author = (_meta(doc, name="articleAuthor")
              or _meta(doc, itemprop="author")
              or _meta(doc, prop="article:author"))
    if not author:
        author = slug_author_from_url(url)

    title = _extract_title(doc, author=author, url=url)
    if not title:
        raise ScrapeError(f"No title for {url}")

    subtitle = _meta(doc, name="description") or _meta(doc, prop="og:description")
    subtitle = re.sub(r"[*_]{1,}", "", subtitle).strip()

    # Date cross-check: URL path date must equal meta datePublished.
    iso_meta = (_meta(doc, itemprop="datePublished")
                or _meta(doc, name="datePublished")
                or _meta(doc, prop="article:published_time"))
    meta_date = parse_iso_date(iso_meta)
    if meta_date and meta_date != url_date:
        raise ScrapeError(
            f"Date mismatch for {url}: url={url_date} meta={meta_date}"
        )
    date_iso = url_date  # authoritative (cross-checked above)

    body = sabah_extract_body(article_html)
    if not body.strip():
        raise ScrapeError(f"Empty body after extraction for {url}")

    has_byline = bool(re.search(
        r"[A-Za-zÇĞİıÖŞÜçğşöü]{2,}\s+[A-Za-zÇĞİıÖŞÜçğşöü]{2,}", author))
    return {
        "title": title,
        "subtitle": subtitle,
        "body": body,
        "date": date_iso,
        "author": author,
        "has_byline": has_byline,
        "stated_reading_time": _stated_reading_time(doc),
    }


def _extract_title(doc, author="", url=""):
    """Title, guaranteeing title != author. Some Sabah columns put the AUTHOR
    NAME in the <h1> (e.g. "ENGİN ARDIÇ"), so og:title is preferred; the URL
    title-slug is the final fallback."""
    def _clean(v):
        return re.sub(r"\s*[-|]\s*Sabah.*$", "", v or "",
                      flags=re.IGNORECASE).strip()

    for getter in (lambda: _meta(doc, prop="og:title"),
                   lambda: _meta(doc, name="twitter:title"),
                   lambda: _meta(doc, prop="twitter:title"),
                   lambda: _meta(doc, name="title")):
        v = _clean(getter())
        if v and norm_key(v) != norm_key(author):
            return v
    t = doc.xpath("//title/text()")
    if t:
        cand = _clean(t[0])
        if cand and norm_key(cand) != norm_key(author):
            return cand
    h1 = " ".join(x.strip() for x in doc.xpath("//h1//text()") if x.strip()).strip()
    if h1 and norm_key(h1) != norm_key(author):
        return h1
    # URL title-slug fallback: /.../{YYYY}/{MM}/{DD}/{title-slug}
    seg = urlsplit(url).path.rstrip("/").split("/")[-1] if url else ""
    return " ".join(w.capitalize() for w in seg.replace("-", " ").split())


def slug_author_from_url(url: str) -> str:
    m = re.search(r"/yazarlar/([^/]+)/", urlsplit(url).path)
    if not m:
        return ""
    return m.group(1).replace("-", " ").title()


def _stated_reading_time(doc):
    txt = " ".join(doc.xpath("//*[contains(text(),'dakika')]/text()"))
    m = re.search(r"(\d+)\s*dakika", txt)
    return int(m.group(1)) if m else None


# ----------------------------------------------------------------------------
# Metrics
# ----------------------------------------------------------------------------
_SENT_RE = re.compile(r"[.!?…]+(?:\s|$)")


def split_paragraphs(body: str):
    return [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]


def compute_metrics(title: str, body: str) -> dict:
    paras = split_paragraphs(body)
    prose = [p for p in paras if not p.lstrip().startswith("#")
             and len(p.split()) >= 4]
    words = body.split()
    pw = [len(p.split()) for p in prose]
    mean_para = (sum(pw) / len(pw)) if pw else 0.0
    return {
        "char_count": len(body),
        "word_count": len(words),
        "paragraph_count": len(paras),
        "prose_paragraph_count": len(prose),
        "mean_paragraph_len": round(mean_para, 2),
        "sentence_count": len(_SENT_RE.findall(body)),
    }


def build_record(url, article_id, uid, extracted):
    title, body = extracted["title"], extracted["body"]
    content = title + "\n\n" + body
    rec = {
        "url": url,
        "article_id": article_id,
        "uid": uid,
        "date": extracted["date"],
        "source": SOURCE,
        "section": SECTION,
        "title": title,
        "subtitle": extracted.get("subtitle", ""),
        "body": body,
        "content": content,
        "author": extracted.get("author", ""),
        "has_byline": extracted.get("has_byline", False),
        "stated_reading_time": extracted.get("stated_reading_time"),
    }
    rec.update(compute_metrics(title, body))
    return rec


def save_raw_html(uid, html_text, raw_dir):
    dest = os.path.join(raw_dir, SOURCE)
    os.makedirs(dest, exist_ok=True)
    path = os.path.join(dest, f"{uid}.html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html_text)
    return path


# ----------------------------------------------------------------------------
# Cloudflare-aware fetch
# ----------------------------------------------------------------------------
def _assert_decodable(text, url, resp):
    """Fail loud if the body isn't decodable HTML (e.g. brotli/zstd advertised
    but no decoder installed) instead of silently parsing garbage."""
    ce = resp.headers.get("Content-Encoding", "")
    head = text[:3000]
    if ("<" not in head[:2000]) and ("br" in ce or "zstd" in ce):
        raise ScrapeError(
            f"Undecodable response for {url} (Content-Encoding={ce!r}). "
            f"Install 'brotli'/'zstandard' or keep 'br' out of Accept-Encoding."
        )
    import re as _re
    if not _re.search(r"<(html|meta|body|div|article|h1)", head, _re.I):
        raise ScrapeError(
            f"Response for {url} is not HTML (len={len(text)}, "
            f"Content-Encoding={ce!r}) — likely undecodable or an interstitial."
        )


# Statuses that are retried with backoff (rate-limit / anti-bot), not fatal.
_RETRYABLE_STATUS = {403, 405, 429, 503}

_CF_SIGNS = ("Just a moment", "cf-browser-verification", "Attention Required",
             "Checking your browser", "cf-challenge")


def detect_cloudflare_block(status_code, text):
    if status_code in (403, 503):
        return f"HTTP {status_code} (likely Cloudflare challenge)"
    low = text[:4000]
    for sign in _CF_SIGNS:
        if sign in low:
            return f"Cloudflare interstitial detected ({sign!r})"
    return None


def fetch(url, session, max_retries=4, allow_404=False):
    delay = 1.5
    last = None
    for attempt in range(1, max_retries + 1):
        resp = session.get(url, headers=HEADERS, timeout=30)
        if resp.status_code == 404 and allow_404:
            return None
        block = detect_cloudflare_block(resp.status_code, resp.text)
        if block is None and resp.status_code < 400:
            text = resp.text
            _assert_decodable(text, url, resp)
            return text
        # 405/429 (and 403/503) are transient anti-bot / rate-limit responses on
        # Sabah under volume — RETRY with backoff rather than instant-fail.
        if resp.status_code >= 400 and resp.status_code not in _RETRYABLE_STATUS:
            raise ScrapeError(f"HTTP {resp.status_code} for {url}")
        last = block or f"HTTP {resp.status_code}"
        time.sleep(delay)
        delay *= 2
    raise CloudflareBlock(
        f"Blocked after {max_retries} attempts for {url}: {last}"
    )


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

    def dist(v):
        return ({"min": min(v), "median": statistics.median(v), "max": max(v)}
                if v else {"min": None, "median": None, "max": None})

    wc_bins = [0, 150, 300, 500, 800, 1200, 2000, 100000]
    pc_bins = [0, 3, 6, 10, 15, 25, 1000]
    return {
        "source": SOURCE, "n_records": len(records),
        "word_count": {**dist(wc), "hist_bins": wc_bins,
                       "hist": histogram(wc, wc_bins)},
        "paragraph_count": {**dist(pc), "hist_bins": pc_bins,
                            "hist": histogram(pc, pc_bins)},
        "low_content_ids": low, "n_low_content": len(low),
        "n_skipped": len(skipped), "skipped": list(skipped)[:100],
        "ended_reasons": ended_reasons,
        "live_only_unverified_offline": live_notes,
    }


def print_summary(s):
    print("=" * 68)
    print(f"RUN SUMMARY — {s['source']}  ({s['n_records']} records)")
    print("=" * 68)
    for field in ("word_count", "paragraph_count"):
        d = s[field]
        print(f"\n{field}: min={d['min']} median={d['median']} max={d['max']}")
        for i, c in enumerate(d["hist"]):
            print(f"  [{d['hist_bins'][i]:>5}–{d['hist_bins'][i+1]:<6}) "
                  f"{c:>4} {'█'*c}")
    print(f"\nlow_content_ids (word_count<{LOW_CONTENT_WORDS}): "
          f"{s['n_low_content']}")
    if s["low_content_ids"]:
        print("  " + ", ".join(s["low_content_ids"]))
    print(f"\nskipped (per-article fetch/extract failures): {s['n_skipped']}")
    for sk in s["skipped"][:15]:
        print(f"  {sk['id']}: {sk['reason']}")
    if s["n_records"] and s["word_count"]["median"] is not None \
            and s["word_count"]["median"] < 300:
        print("\n  ** WARNING: low median word_count — section/extraction "
              "may be wrong. **")
    print("\nended_reasons:", json.dumps(s["ended_reasons"], ensure_ascii=False))
    print("live-only (unverified offline):")
    for n in s["live_only_unverified_offline"]:
        print("  -", n)
    print("=" * 68)


LIVE_NOTES = [
    "Sabah Cloudflare behavior under the chosen header set (fetch() retries "
    "with backoff and FAILS LOUD as CloudflareBlock if still blocked).",
]


def collect_author_archive(author, limit, session):
    """Paginate /yazarlar/{author}/arsiv/getall?page=N until empty / all-seen /
    404. (getall alone is only the newest ~20-item window — confirmed live.)
    Returns (links, ended_reason) where each link is (url, slug, date, uid)."""
    out, seen = [], set()
    page = 1
    while len(out) < limit and page <= MAX_PAGES:
        # page 1 = bare getall (the ?page=1 form 405s for some authors); ?page=N
        # for N>=2 (confirmed live).
        base_url = f"{BASE}/yazarlar/{author}/arsiv/getall"
        url = base_url if page == 1 else f"{base_url}?page={page}"
        html = fetch(url, session, allow_404=True)
        if html is None:
            return out[:limit], f"stop_404_end(p{page})"
        links = parse_archive_links(html)
        if not links:
            return out[:limit], f"stop_empty(p{page})"
        uids = [uid for _, _, _, uid in links]
        if all(u in seen for u in uids):
            return out[:limit], f"stop_all_seen(p{page})"
        for link in links:
            if link[3] not in seen:
                seen.add(link[3])
                out.append(link)
        page += 1
    return out[:limit], ("target_reached" if len(out) >= limit else "max_pages")


def discover_authors(session, categories):
    """Fetch on-target category hubs and return their author slugs (de-duped)."""
    slugs, seen = [], set()
    for cat in categories:
        html = fetch(f"{BASE}/yazarlar/{cat}", session)
        for s in parse_hub_authors(html):
            if s not in seen:
                seen.add(s)
                slugs.append(s)
    return slugs


def run(authors, limit, out_path, raw_dir):
    import requests
    session = requests.Session()
    records, ended, skipped = [], {}, []
    seen_global = set()   # dedup by uid across all authors (shared rails)
    for author in authors:
        try:
            links, reason = collect_author_archive(author, limit, session)
        except ScrapeError as exc:
            # A persistent listing failure for ONE author (e.g. HTTP 405 after
            # retries) is recorded and skipped — it must not abort the harvest.
            ended[author] = f"author_failed({str(exc)[:60]})"
            skipped.append({"id": f"__author__{author}", "url": author,
                            "reason": str(exc)[:200]})
            print(f"[skip-author] {author}: {exc}", file=sys.stderr)
            continue
        ended[author] = reason
        for url, slug, d, uid in links:
            if uid in seen_global:
                continue
            seen_global.add(uid)
            try:
                html_text = fetch(url, session)
                save_raw_html(uid, html_text, raw_dir)
                extracted = extract_article(html_text, url, d, slug)
                records.append(build_record(url, slug, uid, extracted))
            except ScrapeError as exc:
                # Per-article failure (e.g. HTTP 405, empty body) is RECORDED and
                # skipped so one article can't abort a multi-thousand harvest.
                skipped.append({"id": uid, "url": url, "reason": str(exc)[:200]})
                print(f"[skip] {uid}: {exc}", file=sys.stderr)

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
    ap = argparse.ArgumentParser(description="Sabah columnist scraper")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--smoke", action="store_true",
                      help=f"first-run mode, target={SMOKE_TARGET}/author")
    mode.add_argument("--full", action="store_true")
    ap.add_argument("--authors", default=",".join(DEFAULT_AUTHORS),
                    help="comma-separated slugs OR a path to a file")
    ap.add_argument("--discover", action="store_true",
                    help="discover authors from the on-target category hubs "
                         f"({', '.join(ON_TARGET_CATEGORIES)}) instead of "
                         "--authors")
    ap.add_argument("--categories", default=",".join(ON_TARGET_CATEGORIES),
                    help="category hubs to discover from (with --discover)")
    ap.add_argument("--out", default="output/sabah.jsonl")
    ap.add_argument("--raw-dir", default="output/raw_store")
    args = ap.parse_args(argv)

    limit = SMOKE_TARGET if not args.full else 100000
    if args.discover:
        import requests
        cats = [c.strip() for c in args.categories.split(",") if c.strip()]
        authors = discover_authors(requests.Session(), cats)
        print(f"[discover] {len(authors)} authors from hubs: "
              f"{', '.join(cats)}", file=sys.stderr)
    elif os.path.isfile(args.authors):
        with open(args.authors, encoding="utf-8") as fh:
            authors = [ln.strip() for ln in fh if ln.strip()
                       and not ln.startswith("#")]
    else:
        authors = [a.strip() for a in args.authors.split(",") if a.strip()]

    try:
        run(authors, limit, args.out, args.raw_dir)
    except ScrapeError as exc:
        print(f"FAIL LOUD: {exc}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()

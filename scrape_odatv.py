#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scrape_odatv.py — KuKi corpus per-source scraper for Oda TV columnists.

Standalone module. CMS is BilginPro (same structural template as Yeniçağ) but
this is a SEPARATE independent file by convention — NO shared code.

Collection provenance (the only outlet fields stored per record)
    source            = odatv
    section           = yazarlar
Outlet-level judgements (orientation, factuality_tier, expected genre) are
properties of the OUTLET, not of the text: they live in sources.csv and are
joined on `source` at analysis time.

Collection
    Author list: https://www.odatv.com/yazarlar/{author-slug}?sayfa=N
    Numbered pagination ?sayfa=N; same end-detection as Yeniçağ (empty OR
    all-seen page => stop; 4xx/5xx => FAIL LOUD; never infinite-loop).

Article
    URL:        /yazarlar/{author-slug}/{title-slug}-{longID}
    article_id: trailing numeric ID
    Dates:      Turkish long-form text embedded in the list ("24 Temmuz 2026")
                — Turkish month map; FAIL LOUD if unparseable.
    Body:       strip "En Çok Okunanlar" / "En Çok İzlenenler" rails, category
                footer, cookie notice.

CONTENT HAZARD — author-level genre mixing
    Oda TV's columnist roster mixes political columnists (Soner Yalçın, Nihat
    Genç, Müyesser Yıldız — the L3/L4 payload) with gastronomy / TV / health
    columnists (GastrOda etc.). Genre is PER-AUTHOR, not per-section. This
    scraper takes an explicit author ALLOW-LIST as config input (--authors, or
    a file) rather than harvesting all columnists. Which authors are in-scope
    for the political/opinion corpus is an operator source-selection decision —
    we do NOT auto-classify per article downstream. A default allow-list ships
    as odatv_authors_allowlist.txt.

Live-only steps (flagged in run summary):
    * Oda TV true page-count per author and the exact empty-page response.

Run:
    python scrape_odatv.py --smoke
    python scrape_odatv.py --full --authors odatv_authors_allowlist.txt
"""

import argparse
import json
import os
import re
import statistics
import sys
import time
import unicodedata
from datetime import date, datetime
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
import trafilatura
from lxml import html as lxml_html

# ----------------------------------------------------------------------------
SOURCE = "odatv"
SECTION = "yazarlar"

# Output root: $KUKI_ROOT if set, else THIS SCRIPT's directory -- deliberately
# not the cwd. `output/` used to resolve against the shell's working directory,
# so running the scraper from elsewhere scattered the corpus across two trees
# and left reextract/merge looking in the wrong place.
_ROOT = os.environ.get("KUKI_ROOT") or os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(_ROOT, "output")
DEFAULT_OUT = os.path.join(OUTPUT_DIR, f"{SOURCE}.jsonl")
DEFAULT_RAW_DIR = os.path.join(OUTPUT_DIR, "raw_store")
# Collection provenance. Outlet-level judgements (orientation,
# factuality_tier) and expected genre are properties of the OUTLET, not of
# the text -- they live in sources.csv and are joined on `source` at analysis
# time. Genre in particular is what the genre/stance filter decides; asserting
# it on every record would pre-judge that gate.

BASE = "https://www.odatv.com"
ALLOWED_HOST = "www.odatv.com"

# DEFAULT allow-list = the verified political/opinion subset (research 2026-07).
# Full curated list + exclusions live in odatv_authors_allowlist.txt; genre is
# per-author (roster mixes sport/food/health/TV) so this list IS the gate.
DEFAULT_AUTHORS = [
    "soner-yalcin", "hurrem-elmasci", "kayahan-uygur", "ayse-baykal",
    "hikmet-cicek", "mert-tascilar", "ugur-can-bicer", "can-ozcelik",
    "mustafa-onsel", "meclis-kulisi", "sadik-celik", "kaan-arslanoglu",
    "fehmi-kofteoglu",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Accept-Language": "tr-TR,tr;q=0.9",
}

LOW_CONTENT_WORDS = 150
SMOKE_TARGET = 15
MAX_PAGES = 500


class ScrapeError(RuntimeError):
    pass


# ----------------------------------------------------------------------------
# Turkish helpers
# ----------------------------------------------------------------------------
_TR_UPPER_TO_LOWER = {
    "I": "ı", "İ": "i", "Ş": "ş", "Ğ": "ğ", "Ç": "ç", "Ö": "ö", "Ü": "ü",
}


def tr_lower(s: str) -> str:
    return "".join(_TR_UPPER_TO_LOWER.get(ch, ch.lower()) for ch in s)


def norm_key(s: str) -> str:
    return re.sub(r"\s+", " ", tr_lower(s)).strip()


TR_MONTHS = {
    "ocak": 1, "şubat": 2, "mart": 3, "nisan": 4, "mayıs": 5, "haziran": 6,
    "temmuz": 7, "ağustos": 8, "eylül": 9, "ekim": 10, "kasım": 11,
    "aralık": 12,
}
_LONGFORM_RE = re.compile(r"(\d{1,2})\s+([A-Za-zÇĞİıÖŞÜçğşöü]+)\s+(\d{4})")


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


# ----------------------------------------------------------------------------
# Link scoping (pure)
# ----------------------------------------------------------------------------
# /yazarlar/{author-slug}/{title-slug}-{longID}
_ARTICLE_RE = re.compile(r"^/yazarlar/[^/]+/[^/]+-(\d+)/?$")


def normalize_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme or "https", parts.netloc,
                       parts.path.rstrip("/"), "", ""))


def scope_link(href: str, base: str = BASE):
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


def parse_article_links(list_html: str, base: str = BASE):
    doc = lxml_html.fromstring(list_html)
    out, seen = [], set()
    for a in doc.xpath("//a[@href]"):
        aid = scope_link(a.get("href"), base=base)
        if not aid or aid in seen:
            continue
        seen.add(aid)
        out.append((normalize_url(urljoin(base + "/", a.get("href"))), aid))
    return out


def collection_verdict(new_ids, seen_ids):
    if not new_ids:
        return "stop_empty"
    if all(i in seen_ids for i in new_ids):
        return "stop_all_seen"
    return "continue"


# ----------------------------------------------------------------------------
# Extraction
# ----------------------------------------------------------------------------
_BODY_CUT_MARKERS = ["En Çok Okunanlar", "En Çok İzlenenler", "İlgili Haberler",
                     "Diğer Yazarlar", "KVKK", "Çerez"]


def truncate_at_markers(text: str, markers=_BODY_CUT_MARKERS) -> str:
    cut = len(text)
    low = tr_lower(text)
    for mk in markers:
        idx = low.find(tr_lower(mk))
        if idx != -1:
            cut = min(cut, idx)
    return text[:cut].rstrip()


_ARTIFACT_LINES = re.compile(
    r"^\s*(reklam|paylaş|whatsapp|twitter|facebook|abone ol|yorumlar|"
    r"çerez|kvkk|son güncelleme:?|yazıyı paylaş|etiketler)\s*$",
    re.IGNORECASE)

# Byline line like "Soner Yalçın yazdı..." must not become a body paragraph.
_BYLINE_LINE = re.compile(r"^\s*.{0,45}\syazdı\s*[.…]*\s*$", re.IGNORECASE)

_TR_WEEKDAYS = ("pazartesi", "salı", "çarşamba", "perşembe", "cuma",
                "cumartesi", "pazar")
_MASTHEAD_DATE = re.compile(
    r"^\s*\d{1,2}\s+\S+\s+\d{4}(\s+\d{2}:\d{2})?\s*(" +
    "|".join(_TR_WEEKDAYS) + r")?\s*(son güncelleme.*)?$", re.IGNORECASE)


def strip_residual_artifacts(text: str) -> str:
    text = re.sub(r"[*_]{1,}", "", text)  # drop markdown emphasis markers
    kept = []
    for ln in text.splitlines():
        s = ln.strip()
        if _ARTIFACT_LINES.match(ln) or _BYLINE_LINE.match(s):
            continue
        kept.append(ln)
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    seen, out = set(), []
    for p in re.split(r"\n\s*\n", text):
        key = norm_key(p.lstrip("# "))
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(p.strip())
    return "\n\n".join(out).strip()


def _meta(doc, name=None, prop=None, itemprop=None):
    for attr, val in (("name", name), ("property", prop),
                      ("itemprop", itemprop)):
        if val:
            v = doc.xpath(f"//meta[@{attr}={val!r}]/@content")
            if v:
                return v[0].strip()
    return ""


def extract_title(doc):
    """BilginPro <h1> == the article TITLE (confirmed live). Prefer og:title."""
    title = _meta(doc, prop="og:title") or _meta(doc, name="twitter:title")
    if not title:
        title = " ".join(t.strip() for t in doc.xpath("//h1//text()")
                         if t.strip()).strip()
    return re.sub(r"\s*[-|]\s*Odatv.*$", "", title, flags=re.IGNORECASE).strip()


def extract_author(doc, url):
    """Author is the '{Author} yazdı…' byline (og:description) — article:author
    is the institutional 'Odatv'. URL author-slug is the last resort."""
    desc = _meta(doc, prop="og:description") or _meta(doc, name="description")
    m = re.match(r"\s*(.+?)\s+yazdı\b", desc or "", flags=re.IGNORECASE)
    if m:
        return _norm_name(m.group(1))
    a = _meta(doc, name="author") or _meta(doc, prop="article:author")
    if a and " " in a.strip() and norm_key(a) != "odatv":
        return _norm_name(a)
    return _author_from_url(url)


def _norm_name(s: str) -> str:
    return unicodedata.normalize("NFC", re.sub(r"\s+", " ", s).strip()).replace(
        "̇", "")


def extract_date(doc):
    """Publication date from article:published_time / datePublished / <time>
    (confirmed live), else the .post-info-bar publish-date element — NOT any
    masthead current-date."""
    for iso in (_meta(doc, prop="article:published_time"),
                _meta(doc, itemprop="datePublished"),
                _meta(doc, name="datePublished")):
        d = parse_iso_date(iso)
        if d:
            return d
    for dt in doc.xpath("//time/@datetime"):
        d = parse_iso_date(dt)
        if d:
            return d
    txt = " ".join(doc.xpath(
        "//*[contains(@class,'publish_date') or contains(@class,'publish-date')"
        " or contains(@class,'yayin')]//text()"))
    return parse_tr_longform_date(txt)


def extract_article(article_html: str, url: str) -> dict:
    doc = lxml_html.fromstring(article_html)
    title = extract_title(doc)
    if not title:
        raise ScrapeError(f"No title element for {url}")
    author = extract_author(doc, url)
    if author and norm_key(author) == norm_key(title):
        author = _author_from_url(url)

    subtitle = _meta(doc, name="description") or _meta(doc, prop="og:description")
    subtitle = re.sub(r"[*_]{1,}", "", subtitle).strip()

    date_iso = extract_date(doc)
    if not date_iso:
        raise ScrapeError(f"Unparseable date for {url}")

    body = trafilatura.extract(
        article_html, include_tables=False, include_comments=False,
        include_images=False, favor_recall=True, output_format="markdown",
        url=url,
    ) or ""
    body = truncate_at_markers(body)
    body = strip_residual_artifacts(body)
    body = _drop_leading(body, [title, author])
    if not body.strip():
        raise ScrapeError(f"Empty body after extraction for {url}")

    has_byline = bool(author and re.search(
        r"[A-Za-zÇĞİıÖŞÜçğşöü]{2,}\s+[A-Za-zÇĞİıÖŞÜçğşöü]{2,}", author))
    return {
        "title": title, "subtitle": subtitle, "body": body, "date": date_iso,
        "author": author, "has_byline": has_byline,
        "stated_reading_time": None,
    }


def _author_from_url(url: str) -> str:
    m = re.search(r"/yazarlar/([^/]+)/", urlsplit(url).path)
    return m.group(1).replace("-", " ").title() if m else ""


def _drop_leading(body: str, candidates):
    lines = body.splitlines()
    changed = True
    while changed and lines:
        changed = False
        first = lines[0].lstrip("# ").strip()
        for c in candidates:
            if c and norm_key(first) == norm_key(c):
                lines = lines[1:]
                while lines and not lines[0].strip():
                    lines = lines[1:]
                changed = True
                break
    return "\n".join(lines).strip()


# ----------------------------------------------------------------------------
# Metrics / record / raw / summary
# ----------------------------------------------------------------------------
_SENT_RE = re.compile(r"[.!?…]+(?:\s|$)")


def split_paragraphs(body: str):
    return [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]


def compute_metrics(title: str, body: str) -> dict:
    paras = split_paragraphs(body)
    prose = [p for p in paras if not p.lstrip().startswith("#")
             and len(p.split()) >= 4]
    pw = [len(p.split()) for p in prose]
    mean_para = (sum(pw) / len(pw)) if pw else 0.0
    return {
        "char_count": len(body), "word_count": len(body.split()),
        "paragraph_count": len(paras), "prose_paragraph_count": len(prose),
        "mean_paragraph_len": round(mean_para, 2),
        "sentence_count": len(_SENT_RE.findall(body)),
    }


def build_record(url, article_id, extracted):
    title, body = extracted["title"], extracted["body"]
    rec = {
        "url": url, "article_id": article_id, "date": extracted["date"],
        "source": SOURCE, "section": SECTION,
        "title": title,
        "subtitle": extracted.get("subtitle", ""), "body": body,
        "content": title + "\n\n" + body,
        "author": extracted.get("author", ""),
        "has_byline": extracted.get("has_byline", False),
        "stated_reading_time": extracted.get("stated_reading_time"),
    }
    rec.update(compute_metrics(title, body))
    return rec


def save_raw_html(article_id, html_text, raw_dir):
    dest = os.path.join(raw_dir, SOURCE)
    os.makedirs(dest, exist_ok=True)
    path = os.path.join(dest, f"{article_id}.html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html_text)
    return path


# Transient server / rate-limit responses. A 5xx or a 429 mid-harvest is the
# site having a bad second, NOT a reason to discard hours of collection --
# retry with backoff and only fail loud once the site is genuinely unavailable.
# 429 rate-limit, 5xx upstream, and 520-527 CLOUDFLARE edge errors (522 =
# origin connection timed out). All transient under sustained crawling; failing
# on them discards articles that a second attempt would have returned.
RETRYABLE_STATUS = {429} | set(range(500, 505)) | set(range(520, 528))
REQUEST_TIMEOUT = 45          # these sites stall under sustained load
MAX_RETRIES = 6

def _http_get(url, session, allow_404=False):
    delay, last = 2.0, None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:      # dropped connection, DNS, TLS
            last = f"{type(exc).__name__}: {exc}"
            resp = None
        if resp is not None:
            # Listing 404 = page past the last one (confirmed live) = clean end.
            if resp.status_code == 404 and allow_404:
                return None
            if resp.status_code < 400:
                return resp.text
            if resp.status_code not in RETRYABLE_STATUS:
                raise ScrapeError(f"HTTP {resp.status_code} for {url}")
            last = f"HTTP {resp.status_code}"
        if attempt < MAX_RETRIES:
            print(f"  [retry {attempt}/{MAX_RETRIES - 1}] {last} for {url} "
                  f"-- waiting {delay:.0f}s")
            time.sleep(delay)
            delay *= 2
    raise ScrapeError(
        f"{last} for {url} after {MAX_RETRIES} attempts -- the site is not "
        f"just having a bad second")


def collect_author(author, limit, session):
    pairs, seen = [], set()
    page = 1
    while len(pairs) < limit and page <= MAX_PAGES:
        url = f"{BASE}/yazarlar/{author}?sayfa={page}"
        listing = _http_get(url, session, allow_404=True)
        if listing is None:                       # 404 => past the last page
            return pairs[:limit], "stop_404_end"
        new = parse_article_links(listing)
        verdict = collection_verdict([a for _, a in new], seen)
        if verdict in ("stop_empty", "stop_all_seen"):
            return pairs[:limit], verdict
        for u, aid in new:
            if aid not in seen:
                seen.add(aid)
                pairs.append((u, aid))
        page += 1
    reason = "target_reached" if len(pairs) >= limit else "max_pages"
    return pairs[:limit], reason


def histogram(values, bins):
    counts = [0] * (len(bins) - 1)
    for v in values:
        for i in range(len(bins) - 1):
            if bins[i] <= v < bins[i + 1] or (i == len(bins) - 2
                                              and v == bins[-1]):
                counts[i] += 1
                break
    return counts


def build_summary(records, ended_reasons, live_notes, in_scope_authors,
                  skipped=()):
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
        "in_scope_authors": in_scope_authors,
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
    print("in-scope authors (allow-list):", ", ".join(s["in_scope_authors"]))
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
    "Oda TV true page-count per author (collect_author stops on empty, "
    "all-seen, OR a 404 past the last ?sayfa page; article-fetch 4xx/5xx still "
    "FAIL LOUD).",
]


class _IncrementalWriter:
    """Append each record to the JSONL as it is built, not at the end of the run.

    The scrapers used to hold every record in memory and write once the harvest
    finished. A crash, a KeyboardInterrupt or an unretryable HTTP error therefore
    threw away the whole run even though every page had already been fetched and
    saved to the raw store. Appending as we go means an interrupted run leaves a
    usable partial corpus, and `reextract.py` can rebuild the rest from raw HTML.
    """

    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.fh = open(path, "w", encoding="utf-8")
        self.n = 0

    def write(self, rec):
        self.fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.n += 1
        if self.n % 200 == 0:        # survive a hard kill, not just an exception
            self.fh.flush()
            os.fsync(self.fh.fileno())

    def close(self):
        if not self.fh.closed:
            self.fh.flush()
            os.fsync(self.fh.fileno())
            self.fh.close()


def run(authors, limit, out_path, raw_dir):
    import requests
    session = requests.Session()
    records, ended, skipped = [], {}, []
    writer = _IncrementalWriter(out_path)   # records land on disk as they arrive
    seen_global = set()   # dedup by article_id across all allow-listed authors
    for author in authors:
        pairs, reason = collect_author(author, limit, session)
        ended[author] = reason
        for url, aid in pairs:
            if aid in seen_global:
                continue
            seen_global.add(aid)
            try:
                html_text = _http_get(url, session)
                save_raw_html(aid, html_text, raw_dir)
                rec = build_record(url, aid, extract_article(html_text, url))
                records.append(rec)
                writer.write(rec)
            except ScrapeError as exc:
                skipped.append({"id": aid, "url": url, "reason": str(exc)[:200]})
                print(f"[skip] {aid}: {exc}", file=sys.stderr)

    writer.close()
    # Rewrite in one pass so the finished file is clean and ordered; the
    # incremental copy has already protected against a mid-run crash.
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    summary = build_summary(records, ended, LIVE_NOTES, authors, skipped)
    with open(out_path + ".summary.json", "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)
    print_summary(summary)
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser(description="Oda TV columnist scraper")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--smoke", action="store_true",
                      help=f"first-run mode, target={SMOKE_TARGET}/author")
    mode.add_argument("--full", action="store_true")
    ap.add_argument("--authors", default=",".join(DEFAULT_AUTHORS),
                    help="ALLOW-LIST: comma-separated slugs OR a path to a file "
                         "(one slug per line). Genre is per-author on Oda TV — "
                         "list only political/opinion columnists.")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--raw-dir", default=DEFAULT_RAW_DIR)
    args = ap.parse_args(argv)

    limit = SMOKE_TARGET if not args.full else 100000
    if os.path.isfile(args.authors):
        with open(args.authors, encoding="utf-8") as fh:
            authors = [ln.strip() for ln in fh if ln.strip()
                       and not ln.startswith("#")]
    else:
        authors = [a.strip() for a in args.authors.split(",") if a.strip()]

    if not authors:
        print("FAIL LOUD: empty author allow-list", file=sys.stderr)
        sys.exit(2)

    try:
        run(authors, limit, args.out, args.raw_dir)
    except ScrapeError as exc:
        print(f"FAIL LOUD: {exc}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()

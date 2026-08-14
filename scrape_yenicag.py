#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scrape_yenicag.py — KuKi corpus per-source scraper for Yeniçağ columnists.

Standalone module. NO shared abstraction with the other TR scrapers.

Collection provenance (the only outlet fields stored per record)
    source            = yenicag
    section           = yazarlar
Outlet-level judgements (orientation, factuality_tier, expected genre) are
properties of the OUTLET, not of the text: they live in sources.csv and are
joined on `source` at analysis time.

CMS: BilginPro. Server-rendered, no JS needed.

Collection
    Author list: https://www.yenicaggazetesi.com/yazarlar/{author-slug}?sayfa=N
    Numbered pagination via ?sayfa=N, starting N=1.
    End-detection (BilginPro leaves no "next" affordance): stop when a page
    returns ZERO article links OR only already-seen article IDs. FAIL LOUD on
    4xx/5xx. Never infinite-loop on a page that keeps returning the last set
    (all-seen => stop).
    Canonical domain is '.com' — the '.com.tr' mirror resolves to the same
    content; we pick '.com' and ASSERT we never cross domains.

Article
    URL:        /{title-slug}-{ID}h.htm
    article_id: the {ID} before 'h.htm'
    Dates:      CONFIRMED LIVE — `<meta property="article:published_time">`
                (ISO) is present and reliable; also a `<time datetime>`. Use
                these, NOT the `.date` masthead span (which shows *today*).
                FAIL LOUD if none parse.
    Author:     CONFIRMED LIVE — the `<h1>` is the ARTICLE TITLE, not the
                author. The author is the "{Author} yazdı…" byline in
                og:description, or `<meta property="article:author">`.
    Body:       Strip the "Diğer Yazarlar" rail, category footer, cookie/KVKK
                notices, the masthead date line, "Yazıyı Paylaş"/"Etiketler"
                nav, and de-dup the occasional doubled column body.

Live-only steps (flagged in run summary):
    * Yeniçağ true page-count per author and the exact empty-page response.

Run:
    python scrape_yenicag.py --smoke
    python scrape_yenicag.py --full --authors selcan-hacaoglu,murat-agirel
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
SOURCE = "yenicag"
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

BASE = "https://www.yenicaggazetesi.com"
ALLOWED_HOST = "www.yenicaggazetesi.com"   # canonical .com; never cross to .com.tr

# Curated political/interpretive keep-list (research 2026-07). The /yazarlar hub
# is a FLAT mixed roster (no category URLs), so this allow-list is the gate;
# sport/magazin/lifestyle/health writers are deliberately excluded. See
# sources/tr_opinion_entrypoints.md for the full drop-list.
DEFAULT_AUTHORS = [
    "arslan-bulut", "ahmet-takan", "murat-agirel", "esfender-korkmaz",
    "yavuz-selim-demirag", "sabahattin-onkibar",
]

# Near-pure guest-opinion stream (Konuk Kalem) — add with --konuk-kalem.
KONUK_KALEM_PATH = "konuk-kalem"

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
# Link scoping (pure) — enforce single canonical domain
# ----------------------------------------------------------------------------
# /{title-slug}-{ID}h.htm
_ARTICLE_RE = re.compile(r"^/[^/]+-(\d+)h\.htm$")


def normalize_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme or "https", parts.netloc,
                       parts.path.rstrip("/"), "", ""))


def scope_link(href: str, base: str = BASE):
    """Return article_id for an in-scope Yeniçağ column on the canonical .com
    host, else None. Cross-domain (incl. the .com.tr mirror) => None."""
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
    """Parse an author-list page. Returns list of (url, article_id) de-duped
    in order. Pure/offline-testable."""
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
    """empty page => stop_empty ; all-seen page => stop_all_seen ; else continue."""
    if not new_ids:
        return "stop_empty"
    if all(i in seen_ids for i in new_ids):
        return "stop_all_seen"
    return "continue"


def assert_same_domain(url: str):
    host = urlsplit(url).netloc
    if host and host != ALLOWED_HOST:
        raise ScrapeError(
            f"Crossed domains: {host} != {ALLOWED_HOST} ({url})"
        )


# ----------------------------------------------------------------------------
# Extraction
# ----------------------------------------------------------------------------
_BODY_CUT_MARKERS = ["Diğer Yazarlar", "Diğer Yazıları", "Yazarın Diğer",
                     "KVKK", "Çerez", "Bu sitede yer alan"]


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
    r"çerez|kvkk|e-?gazete|son güncelleme:?|yazıyı paylaş|etiketler|"
    r"i̇lgili haberler|ilgili haberler)\s*$", re.IGNORECASE)

_TR_WEEKDAYS = ("pazartesi", "salı", "çarşamba", "perşembe", "cuma",
                "cumartesi", "pazar")
# Masthead current-date line, e.g. "27 Temmuz 2026 Pazartesi".
_MASTHEAD_DATE = re.compile(
    r"^\s*\d{1,2}\s+\S+\s+\d{4}\s+(" + "|".join(_TR_WEEKDAYS) + r")\s*$",
    re.IGNORECASE)


def strip_residual_artifacts(text: str) -> str:
    # Drop markdown emphasis markers (** bold **, _italic_) — always artifacts
    # in this corpus; keep '#' subheadings.
    text = re.sub(r"[*_]{1,}", "", text)
    kept = []
    for ln in text.splitlines():
        s = ln.strip()
        if _ARTIFACT_LINES.match(ln):
            continue
        if _MASTHEAD_DATE.match(s):
            continue
        kept.append(ln)
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    # De-duplicate exact-duplicate paragraphs (BilginPro sometimes renders the
    # column text twice around the masthead block).
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
    return re.sub(r"\s*[-|]\s*Yeniçağ.*$", "", title,
                  flags=re.IGNORECASE).strip()


def extract_author(doc):
    """Author is NOT the <h1>. It is the '{Author} yazdı…' byline in
    og:description, or the article:author meta (confirmed live)."""
    desc = _meta(doc, prop="og:description") or _meta(doc, name="description")
    m = re.match(r"\s*(.+?)\s+yazdı\b", desc or "", flags=re.IGNORECASE)
    if m:
        return _norm_name(m.group(1))
    a = _meta(doc, prop="article:author") or _meta(doc, name="author")
    if a and " " in a.strip() and norm_key(a) not in ("yeniçağ", "yenicag"):
        return _norm_name(a)
    return ""


def _norm_name(s: str) -> str:
    # NFC + drop spurious combining dots-above (U+0307) that pollute Turkish
    # names lowered from İ (e.g. "Önki̇bar" -> "Önkibar").
    return unicodedata.normalize("NFC", re.sub(r"\s+", " ", s).strip()).replace(
        "̇", "")


def extract_date(doc):
    """Publication date from article:published_time / datePublished / <time>
    (confirmed live) — NOT the site masthead current-date span."""
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
    return None


def extract_article(article_html: str, url: str) -> dict:
    doc = lxml_html.fromstring(article_html)
    title = extract_title(doc)
    if not title:
        raise ScrapeError(f"No title element for {url}")
    author = extract_author(doc)
    if author and norm_key(author) == norm_key(title):
        author = ""  # never let author collapse into the title

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
    body = _drop_leading(body, [author, title])
    if not body.strip():
        raise ScrapeError(f"Empty body after extraction for {url}")

    has_byline = bool(author and re.search(
        r"[A-Za-zÇĞİıÖŞÜçğşöü]{2,}\s+[A-Za-zÇĞİıÖŞÜçğşöü]{2,}", author))
    return {
        "title": title, "subtitle": subtitle, "body": body, "date": date_iso,
        "author": author, "has_byline": has_byline,
        "stated_reading_time": None,
    }


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
# Metrics / record / raw / summary  (duplicated per per-source convention)
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
    assert_same_domain(url)
    delay, last = 2.0, None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:      # dropped connection, DNS, TLS
            last = f"{type(exc).__name__}: {exc}"
            resp = None
        if resp is not None:
            # On the listing pagination, BilginPro returns 404 for the page past
            # the last one (confirmed live) -- clean end signal, NOT an anomaly.
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
    """Paginate ?sayfa=N until empty / all-seen / a 404 past the last page.
    Returns (pairs, ended_reason)."""
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


def collect_section(path, limit, session):
    """Paginate a non-author listing path (e.g. 'konuk-kalem') via ?sayfa=N with
    the same empty / all-seen / 404-end stopping. Near-pure opinion stream."""
    pairs, seen = [], set()
    page = 1
    while len(pairs) < limit and page <= MAX_PAGES:
        url = f"{BASE}/{path}?sayfa={page}"
        listing = _http_get(url, session, allow_404=True)
        if listing is None:
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
    return pairs[:limit], ("target_reached" if len(pairs) >= limit
                           else "max_pages")


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
    "Yeniçağ true page-count per author (collect_author stops on empty, "
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


def run(authors, limit, out_path, raw_dir, include_konuk=False):
    import requests
    session = requests.Session()
    records, ended, skipped = [], {}, []
    writer = _IncrementalWriter(out_path)   # records land on disk as they arrive
    seen_global = set()   # dedup by article_id across authors + konuk-kalem
    tasks = [("author", a) for a in authors]
    if include_konuk:
        tasks.append(("section", KONUK_KALEM_PATH))
    for kind, name in tasks:
        if kind == "author":
            pairs, reason = collect_author(name, limit, session)
        else:
            pairs, reason = collect_section(name, limit, session)
        ended[name] = reason
        for url, aid in pairs:
            if aid in seen_global:      # shared sidebar rails repeat articles
                continue
            seen_global.add(aid)
            try:
                html_text = _http_get(url, session)
                save_raw_html(aid, html_text, raw_dir)
                rec = build_record(url, aid, extract_article(html_text, url))
                records.append(rec)
                writer.write(rec)
            except ScrapeError as exc:
                # e.g. "Empty body after extraction" on a video/stub post — record
                # and skip rather than aborting the whole harvest.
                skipped.append({"id": aid, "url": url, "reason": str(exc)[:200]})
                print(f"[skip] {aid}: {exc}", file=sys.stderr)

    writer.close()
    # Rewrite in one pass so the finished file is clean and ordered; the
    # incremental copy has already protected against a mid-run crash.
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
    ap = argparse.ArgumentParser(description="Yeniçağ columnist scraper")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--smoke", action="store_true",
                      help=f"first-run mode, target={SMOKE_TARGET}/author")
    mode.add_argument("--full", action="store_true")
    ap.add_argument("--authors", default=",".join(DEFAULT_AUTHORS),
                    help="comma-separated slugs OR a path to a file")
    ap.add_argument("--konuk-kalem", action="store_true",
                    help="also harvest the /konuk-kalem guest-opinion stream")
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

    try:
        run(authors, limit, args.out, args.raw_dir,
            include_konuk=args.konuk_kalem)
    except ScrapeError as exc:
        print(f"FAIL LOUD: {exc}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()

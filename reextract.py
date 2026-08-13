#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Re-extract the corpus from ALREADY-SAVED raw HTML — no re-scraping.

Every scraper stores the raw HTML of each article it fetches. When extraction
logic changes (a byline selector, a boilerplate rule, a trafilatura setting),
the corpus is rebuilt offline from that stored HTML instead of hitting the sites
again. Standing pattern: fix extraction -> re-extract -> read the diff.

    python reextract.py --source holod --dry-run    # report the diff, write nothing
    python reextract.py --source holod
    python reextract.py --all

Sources: ria, theinsider, ng, holod, cumhuriyet, sabah, yenicag, odatv.

Two scraper families, so each source gets a small adapter below:

  RU (ria / theinsider / ng / holod)
      module-level path constants; extract_record(ref, html) -> dataclass
  TR (cumhuriyet / sabah / yenicag / odatv)
      CLI-driven paths; extract_article(html, url) -> dict, then
      build_record(...) -> dict

SAFETY
  Records are written to a temp file and moved into place only after the whole
  source succeeds, so an exception can never leave a truncated corpus. Any
  article that fails to re-extract aborts the write unless --allow-failures.
  The previous JSONL is kept as .bak.

  The existing JSONL is also an INPUT: it holds the url/date/author that the
  scraper learned from listing pages and that the article page alone does not
  always carry. Do not delete a JSONL and expect re-extraction to rebuild it.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import asdict as dc_asdict

from bs4 import BeautifulSoup

# --------------------------------------------------------------------------
# where output lives
# --------------------------------------------------------------------------
# ONE root for both scraper families: $KUKI_ROOT if set, else the directory
# holding these scripts -- deliberately NOT the cwd. The TR scrapers used to
# resolve `output/` against wherever the shell happened to be, so running one
# from another directory put its JSONL where nothing else looked for it.
#
#   <ROOT>/output/<name>.jsonl          RU records
#   <ROOT>/output/raw_html/<source>/    RU raw pages
#   <ROOT>/output/<source>.jsonl        TR records
#   <ROOT>/output/raw_store/<source>/   TR raw pages
REPO_ROOT = os.environ.get("KUKI_ROOT") or os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(REPO_ROOT, "output")


def default_raw_root() -> str:
    return os.path.join(OUTPUT_DIR, "raw_store")


def default_out(source: str) -> str:
    return os.path.join(OUTPUT_DIR, f"{source}.jsonl")


BANNED = {"orientation", "factuality_tier", "genre"}   # outlet-level; see SOURCES.md


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _canonical_url(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    link = soup.find("link", rel="canonical")
    if link and link.get("href"):
        return link["href"]
    og = soup.find("meta", attrs={"property": "og:url"})
    if og and og.get("content"):
        return og["content"]
    return None


def _load_prior(path: str) -> dict:
    prior = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    r = json.loads(line)
                    prior[r["article_id"]] = r
    return prior


def _html_files(raw_dir: str) -> list[str]:
    if not os.path.isdir(raw_dir):
        raise SystemExit(
            f"no raw HTML directory at {raw_dir} — run the scraper first; "
            f"re-extraction cannot invent pages that were never fetched")
    files = sorted(f for f in os.listdir(raw_dir) if f.endswith(".html"))
    if not files:
        raise SystemExit(f"no saved HTML in {raw_dir}; run the scraper first")
    return files


# --------------------------------------------------------------------------
# adapters
# --------------------------------------------------------------------------
class _RU:
    """Dataclass records, module-level path constants."""
    module_name: str
    jsonl_attr = "OUT_JSONL"
    raw_attr = "RAW_DIR"

    def __init__(self, **_):
        self.M = __import__(self.module_name)

    def paths(self):
        return getattr(self.M, self.jsonl_attr), getattr(self.M, self.raw_attr)

    def rebuild(self, stem, html, prior):
        return dc_asdict(self.M.extract_record(self._ref(stem, html, prior), html))


class Ria(_RU):
    name, module_name = "ria", "scrape_ria"
    jsonl_attr, raw_attr = "OUTPUT_JSONL", "RAW_HTML_DIR"

    def _ref(self, stem, html, prior):
        url = (prior or {}).get("url") or _canonical_url(html)
        m = self.M.ARTICLE_RE.search(url) if url else None
        if not m:
            raise RuntimeError(f"cannot reconstruct ArticleRef for {stem}")
        return self.M.ArticleRef(m.group(0), m.group("id"), m.group("date"))


class TheInsider(_RU):
    name, module_name = "theinsider", "scrape_theinsider"

    def _ref(self, stem, html, prior):
        url = (prior or {}).get("url") or _canonical_url(html)
        if not url:
            raise RuntimeError(f"cannot recover url for {stem}")
        m = self.M.ARTICLE_RE.search(url)
        if not m:
            raise RuntimeError(f"{stem}: {url} is not an /opinions/ article url")
        return self.M.Ref(url=url, article_id=m.group(2), author_slug=m.group(1),
                          card_author=(prior or {}).get("author"),
                          card_date=(prior or {}).get("date"))


class NG(_RU):
    name, module_name = "ng", "scrape_ng_vision"

    def _ref(self, stem, html, prior):
        if prior:
            return self.M.Ref(url=prior["url"], article_id=prior["article_id"],
                              date=prior["date"])
        # raw filename is "<YYYY-MM-DD>_<article_id>.html"
        date, _, aid = stem.partition("_")
        url = _canonical_url(html)
        if not (aid and url):
            raise RuntimeError(f"cannot reconstruct Ref for {stem}")
        return self.M.Ref(url=url, article_id=aid, date=date)


class Holod(_RU):
    name, module_name = "holod", "scrape_holod"

    def _ref(self, stem, html, prior):
        if prior:
            # The listing card is a legitimate byline/reading-time source; carry
            # it through so re-extraction is never worse than the original run.
            return self.M.Ref(url=prior["url"], article_id=prior["article_id"],
                              date=prior["date"], author=prior.get("author"),
                              reading_time=prior.get("stated_reading_time"),
                              tag_interview=False)
        url = _canonical_url(html)
        ref = self.M._ref_from_url(url) if url else None
        if ref is None:
            raise RuntimeError(f"{stem}: not an in-scope /opinions/ url ({url})")
        return ref


class _TR:
    """Dict records. Raw store is <raw_dir>/<SOURCE>/.

    RECOVERY: the TR scrapers write raw HTML per article AS IT IS FETCHED, but
    the JSONL only when a run FINISHES. A crash therefore strands real,
    already-paid-for pages with no JSONL row. Those are recovered here by
    reading the url out of the page's own canonical/og:url, so a crashed run
    costs re-extraction, never a re-fetch.
    """
    module_name: str

    def __init__(self, out=None, raw_dir=None):
        self.M = __import__(self.module_name)
        self._out = out or default_out(self.M.SOURCE)
        self._raw = os.path.join(raw_dir or default_raw_root(), self.M.SOURCE)

    def paths(self):
        return self._out, self._raw

    def _url(self, stem, html, prior):
        """(url, recovered_from_html). The JSONL row wins when it exists."""
        if prior and prior.get("url"):
            return prior["url"], False
        url = _canonical_url(html)
        if not url:
            raise RuntimeError(
                f"{stem}: no JSONL row and no canonical/og:url in the saved page"
                f" -- cannot recover the article url")
        if self.M.scope_link(url) is None:
            raise RuntimeError(f"{stem}: recovered url out of scope ({url})")
        return url, True

    def rebuild(self, stem, html, prior):
        url, recovered = self._url(stem, html, prior)
        aid = prior["article_id"] if prior else self.M.scope_link(url)
        rec = self.M.build_record(url, aid, self.M.extract_article(html, url))
        if recovered:
            rec["_recovered_from_raw"] = True
        return rec


class Cumhuriyet(_TR):
    name, module_name = "cumhuriyet", "scrape_cumhuriyet"


class Yenicag(_TR):
    name, module_name = "yenicag", "scrape_yenicag"


class Odatv(_TR):
    name, module_name = "odatv", "scrape_odatv"


class Sabah(_TR):
    name, module_name = "sabah", "scrape_sabah"

    def rebuild(self, stem, html, prior):
        # Raw files are keyed by uid ("YYYY-MM-DD__slug"). scope_link derives
        # (slug, date_iso, uid) from the url path alone, so a crashed Sabah run
        # is fully recoverable. extract_article then cross-checks that date
        # against the page meta and fails loud on mismatch.
        url, recovered = self._url(stem, html, prior)
        scoped = self.M.scope_link(url)
        if scoped is None:
            raise RuntimeError(f"{stem}: {url} is not an in-scope Sabah column")
        slug, date_iso, uid = scoped
        if prior:
            date_iso = prior.get("date", date_iso)
            uid = prior.get("uid", uid)
        rec = self.M.build_record(
            url, prior["article_id"] if prior else slug, uid,
            self.M.extract_article(html, url, date_iso, slug))
        if recovered:
            rec["_recovered_from_raw"] = True
        return rec


ADAPTERS = {a.name: a for a in
            [Ria, TheInsider, NG, Holod, Cumhuriyet, Sabah, Yenicag, Odatv]}


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------
def _diff(old, new):
    if not old:
        return ["<new>"]
    return sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k))


def _tally(changed):
    t = {}
    for fields in changed.values():
        for f in fields:
            t[f] = t.get(f, 0) + 1
    return dict(sorted(t.items(), key=lambda kv: -kv[1]))


def reextract(source, dry_run=False, allow_failures=False, out=None, raw_dir=None):
    ad = ADAPTERS[source](out=out, raw_dir=raw_dir)
    jsonl_path, raw_html_dir = ad.paths()
    prior = _load_prior(jsonl_path)
    files = _html_files(raw_html_dir)

    records, failed, changed = [], [], {}
    for fn in files:
        stem = fn[:-5]
        html = open(os.path.join(raw_html_dir, fn), encoding="utf-8").read()
        p = prior.get(stem)
        if p is None:
            # A raw filename need not equal article_id: NG prefixes the date
            # ("<YYYY-MM-DD>_<id>.html") and Sabah keys by uid. Match every form,
            # or the file reports as <new> and the diff says nothing.
            tail = stem.split("_", 1)[1] if "_" in stem else None
            p = next((r for r in prior.values()
                      if r["article_id"] == stem
                      or r.get("uid") == stem
                      or (tail and r["article_id"] == tail)), None)
        try:
            rec = ad.rebuild(stem, html, p)
        except Exception as exc:
            failed.append({"id": stem, "error": f"{type(exc).__name__}: {exc}"})
            print(f"  FAILED {stem}: {exc}")
            continue
        leaked = BANNED & set(rec)
        if leaked:
            raise SystemExit(
                f"{source}: outlet-level field(s) {sorted(leaked)} are back on "
                f"the record schema — they belong in sources.csv (see SOURCES.md)")
        d = _diff(p, rec)
        if d:
            changed[stem] = d
        records.append(rec)

    stats = {"source": source, "raw_files": len(files), "reextracted": len(records),
             "failed": len(failed), "changed": len(changed),
             "fields_changed": _tally(changed)}
    print(json.dumps(stats, ensure_ascii=False, indent=2))

    if failed and not allow_failures:
        raise SystemExit(
            f"{source}: {len(failed)} article(s) failed — nothing written. "
            f"Fix them, or pass --allow-failures to write the {len(records)} "
            f"that succeeded.")
    if dry_run:
        print("  DRY RUN — nothing written.")
        return stats

    tmp = jsonl_path + ".tmp"
    os.makedirs(os.path.dirname(jsonl_path) or ".", exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    had_prev = os.path.exists(jsonl_path)
    if had_prev:
        shutil.copy2(jsonl_path, jsonl_path + ".bak")
    os.replace(tmp, jsonl_path)
    print(f"  wrote {len(records)} records -> {jsonl_path}"
          + (" (previous kept as .bak)" if had_prev else ""))
    return stats


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--source", choices=sorted(ADAPTERS))
    g.add_argument("--all", action="store_true", help="every source with saved HTML")
    ap.add_argument("--dry-run", action="store_true", help="report the diff, write nothing")
    ap.add_argument("--allow-failures", action="store_true",
                    help="write the records that succeeded even if some failed")
    ap.add_argument("--out", help="TR only: override the JSONL path")
    ap.add_argument("--raw-dir", default=None,
                    help="TR only: raw store root "
                         f"(default: {default_raw_root()})")
    args = ap.parse_args()

    results = []
    for src in (sorted(ADAPTERS) if args.all else [args.source]):
        print(f"\n=== {src} ===")
        try:
            results.append(reextract(src, args.dry_run, args.allow_failures,
                                     out=args.out, raw_dir=args.raw_dir))
        except SystemExit as exc:
            if not args.all:
                raise
            print(f"  SKIPPED: {exc}")

    if args.all and results:
        print("\n=== TOTAL ===")
        print(json.dumps({"sources": len(results),
                          "reextracted": sum(r["reextracted"] for r in results),
                          "changed": sum(r["changed"] for r in results),
                          "failed": sum(r["failed"] for r in results)}, indent=2))


if __name__ == "__main__":
    sys.exit(main())

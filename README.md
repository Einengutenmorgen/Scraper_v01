# RIA Novosti — "Аналитика" section scraper

Single-source corpus collector for **https://ria.ru/ria-novosti-analitika/**.
One module of a per-source system; deliberately **not** generalized.

The output feeds a hand-labeled corpus for narrative/framing annotation. The
operator does not read Russian, so the scraper's primary job is to capture
**language-independent quality signals** (length, paragraphing, sentence
density, signed-vs-institutional byline) that let article quality be judged
without reading the body.

## Install

```bash
pip install -r requirements.txt
python -m playwright install chromium      # only needed for METHOD="browser"
```

## Run

```bash
# collect 40 EXTRA articles beyond the initial page (operator's "i"), default method
python ria_analitika_scraper.py 40

# force the direct-HTTP load-more path instead of the browser
python ria_analitika_scraper.py 40 --method http
```

Or from Python:

```python
from ria_analitika_scraper import run
run(extra_articles=40, method="browser")
```

## Output (written to `./output/`)

- `ria_analitika.jsonl` — one JSON record per article, all fields below.
- `raw_html/<article_id>.html` — raw fetched HTML per article, so extraction can
  be **re-tuned and re-run offline without re-scraping**.
- `run_summary.json` (also printed) — URLs discovered, extracted, failed, and the
  **word_count / paragraph_count distribution** (min/median/max). This is the
  no-Russian-needed sanity check: a healthy analytics harvest skews long and
  multi-paragraph. A low median word_count means the section or the extraction is
  wrong — the summary prints a loud warning in that case.
- `discovered_endpoint.txt` — (browser method) the real load-more request URL(s)
  observed at runtime, so the faster `http` method can be confirmed/tuned.

### Per-article record fields

Content: `url`, `article_id`, `date`, `title`, `body`, `content`
(= `title + "\n\n" + body`, matching the annotation tool's unified `$content`
field), `byline`.

Quality signals (computed structurally): `char_count`, `word_count`,
`paragraph_count` (all body blocks), `prose_paragraph_count` (excludes bullet /
numbered / sub-header lines, so structured *доклады* don't look more narrative
than they are), `mean_paragraph_len`, `sentence_count`, `has_byline`.

`has_byline` is True only when a byline value actually looks like a person's
name ("Имя Фамилия"); RIA rubric/agency labels such as "Аналитика" or
'МИА "Россия сегодня"' map to institutional, not to a fake signed byline.

The run summary also reports `low_content_count` / `low_content_ids`: records
under ~150 words, which are almost always photo/video/infographic stubs from the
section feed rather than essays. They are flagged, not dropped — apply your own
floor before annotation.

### Re-extracting without re-scraping

Raw HTML is saved per article, so after tuning extraction you can rebuild the
whole corpus offline:

```bash
python reextract.py    # reads output/raw_html/*.html, rewrites JSONL + summary
```

Fixed axis tags: `source=ria_novosti`, `section=analitika`,
`orientation=state_aligned`, `factuality_tier=disinfo_prone`,
`genre=analysis_essay`.

## How collection works (discovery findings)

The section shows ~20 cards and an **"Ещё 20 материалов"** button. This is
**not** URL pagination — it is a JavaScript, cursor-based ("load items older than
ID X") XHR loader, standard for the Россия Сегодня CMS. `robots.txt` disallows
every query-string URL (`/*/*?*`) and `/services/`, which is where that loader
request lives.

Two collection methods:

- **`browser`** (default) — headless Chromium (Playwright) clicks the button and,
  on the first click, **intercepts and records the real load-more endpoint** to
  `discovered_endpoint.txt`. Reliable regardless of the exact endpoint shape, and
  it empirically documents the true endpoint on your first run.
- **`http`** — direct GET to the inferred `…/more.html?id=<cursor>&date=<cursor>`
  endpoint. Faster, but the exact shape could not be verified from the build
  environment (see the DISCOVERY NOTES block at the top of the module). It
  **fails loudly** if the first request returns no new cards rather than silently
  returning only page 1. Confirm the shape from `discovered_endpoint.txt`, then
  use this path for speed.

Both methods deduplicate by article ID, stop cleanly at end-of-section, and
**raise** on unexpected structures / HTTP errors — the "quietly returns page 1"
failure mode is explicitly guarded against.

## Extraction

Body text is extracted with **trafilatura** tuned for RIA (`favor_precision`,
tables/comments/links off) so the heavy tag-lists, related-article rails, "Ещё"
widgets, share blocks and comment scaffolding do **not** inflate the body or the
word counts. Artifact removal happens at extraction, not downstream. Byline
parsing tries RIA's signed-author markup first and falls back to the
institutional "РИА Новости".

## Tests

Local, no network required (ria.ru is unreachable from some sandboxes):

```bash
python test_extraction.py        # body pruning, byline, signals
python test_collector.py         # Playwright load-more loop: target/exhaust/fail-loud
python test_http_collector.py    # http load-more: paginate/dedup/exhaust/fail-loud
python test_run_integration.py   # JSONL + raw-HTML + summary wiring
```

## Verification against known articles

The three reference essays named in the spec were confirmed live to be long,
multi-paragraph analytics with the expected byline mix — use them as a
known-good benchmark on your first real run:

| ID         | Title                                                                | Byline                                       | Approx. length      |
| ---------- | -------------------------------------------------------------------- | -------------------------------------------- | ------------------- |
| 2081087907 | Мир после Ирана                                         | Александр Яковенко (signed) | ~2,500–2,800 words |
| 2050229360 | Перепрошивание Украины в анти-Россию | РИА Новости (institutional)        | ~7,000–8,000 words |
| 2015308036 | Эпоха Путина                                              | Александр Яковенко (signed) | ~8,000–9,000 words |

After a real run, confirm these IDs in the JSONL show thousands of chars and
many paragraphs. Tiny counts ⇒ extraction is grabbing the wrong node; retune
`_TRAFILATURA_KWARGS` against the saved `raw_html/<id>.html` and re-run
extraction only.

```
```
# scrape_holod.py — Holod.media /opinions/ ("Мнения и интервью")

**Discovery surface:** `https://holod.media/opinions/` (listing) + article pages;
optionally the WordPress REST API.

**Pagination ACTUALLY FOUND / used:** two paths, REST-preferred.
- **WP REST API (preferred):** `GET /wp-json/wp/v2/categories?slug=opinions` to
  resolve the category id, then `GET /wp-json/wp/v2/posts?categories=<id>&per_page=20&page=N&_embed`.
  Gives structured fields (link, slug, date, author via `_embedded`, terms). WP
  returns HTTP 404 past the last page — used as the clean end signal.
  **Could NOT be verified from the build sandbox** (the fetch tool obeys
  robots.txt, which disallows `/wp-json/`), so the code TRIES it and verifies it
  returns opinions posts; if anything fails it falls back to HTML.
- **HTML fallback:** `/opinions/page/N/`. If page 2 returns no new links while a
  "Посмотреть больше" button exists on page 1, the loader is likely
  `admin-ajax.php` and the code **fails loud** telling you to enable the REST API
  (or add a Playwright fallback) rather than returning page 1 only.

**Scope:** ONLY `/opinions/`. `/longrids/` is explicitly excluded (out of scope),
as are other sections — enforced in `_ref_from_url`.

**IDs / dates:** `article_id` = slug; `date` from the URL path.
`stated_reading_time` (int minutes) parsed from "N минут чтения" on the card or
article page.

**Genre mixing (interviews):** every record has `suspected_interview`. Detected
STRUCTURALLY, not by comprehension: (1) a tag/breadcrumb anchor exactly
"Интервью" (metadata signal, checked first, incl. WP `_embedded` terms), then
(2) ≥30% of body paragraphs opening with a dialogue dash `—`. `genre` is set to
`"interview"` when true, else `"opinion_essay"`. Interviews are FLAGGED, never
dropped; the count is in the run summary.

**Columnist series:** the run summary reports `articles_per_author` so a dominant
recurring column (e.g. "Что волнует военкоров", Иван Филиппов) is visible. These
are kept as legitimate opinion pieces.

**trafilatura config:** `favor_precision=True`, tables/comments/links off,
`target_language="ru"`, plus a line filter for Holod's heavy donation CTAs
(`нам очень нужна ваша помощь`, `регулярные пожертвования`, `криптовалют`,
`банковской карт`, `подпиш…`) so they don't inflate `word_count`.

**Fail-loud:** HTTP errors, empty first listing, and the HTML `/page/2/`-empty
case above.

**Run:** `python scrape_holod.py 15`. Outputs `output/holod_opinions.jsonl`,
`output/raw_html/holod/<slug>.html`, `output/holod_run_summary.json`.


# scrape_ng_vision.py — Nezavisimaya Gazeta /vision/

**Discovery surface:** `https://www.ng.ru/vision/` (listing) + article pages.

**Pagination ACTUALLY FOUND:** classic numbered pagination via query param
`?PAGEN_1=<N>`. Page 1 is the bare `/vision/` URL; N=2,3,… afterwards. The last
page is detected dynamically from the highest `PAGEN_1=` in the pagination
control (was ~137 at time of writing — **not** hardcoded). Collection also stops
when a page yields no new `/vision/` links.

**Scope / the sidebar hazard:** the `/vision/` listing renders a large sidebar of
OTHER NG sections (`/politics/`, `/economics/`, `/world/`, `/ideas/`,
`/monitoring/`, …). Harvesting is restricted to the
`/vision/YYYY-MM-DD/<slug>.html` pattern ONLY, and `collect()` **asserts** every
collected URL contains `/vision/`, raising otherwise. Verified in `test_ng.py`
that sidebar links are excluded.

**IDs / dates:** `article_id` = filename stem (e.g. `6_9542_contact`); `date` is
parsed directly from the URL path (not on-page rendering). `subtitle` = the deck,
`author` = the byline.

**trafilatura config:** `favor_precision=True`, tables/comments/links/images off,
`target_language="ru"`, plus a post-extraction line filter that drops
donation/subscription/related-rail boilerplate (`подпиш…`, `пожертвован…`,
`поддержите`, `читайте также`, …) so it never inflates `word_count`.

**Fail-loud:** raises on HTTP errors, on a first page with zero `/vision/` links
(structure changed), and on any non-`/vision/` URL leaking into the set.

**Run:** `python scrape_ng_vision.py 15` (target_articles; default 60). This is a
deep archive — respect the target and rate limiting; it does not harvest all 137
pages unless asked. Outputs `output/ng_opinions.jsonl`,
`output/raw_html/ng/<date>_<id>.html`, `output/ng_run_summary.json`.


# scrape_theinsider.py — The Insider /opinions

**Discovery surface:** `https://theins.ru/opinions` (listing) + article pages.

**Pagination ACTUALLY FOUND:** a "Загрузить ещё" (load-more) button on a Next.js
site — infinite-scroll, JS-driven. The exact data route (a JSON/RSC endpoint) is
**not verifiable from the build sandbox** (JS-executed, and the sandbox proxy
blocks the domain). So collection uses **Playwright**: it clicks the button, reads
the load-more RESPONSE as the end signal, and records the observed request URLs to
`output/discovered_endpoint_theins.txt`. Once that file shows the confirmed
endpoint + params, a faster direct-HTTP path can be wired (prefer it then).

End-of-section logic (hardened against the "silently returns page 1" failure):
- new cards rendered → keep going;
- button gone, no request fired, an **empty** response body, or a response
  repeating only already-seen ids → **clean stop**;
- a **non-empty** response that yields no new cards on two consecutive clicks →
  **fail loud** (cursor/injection anomaly, or a payload we could not parse — never
  assume "empty" and return a short list).

**IDs / dates / author:** `article_id` = trailing numeric id in
`/opinions/<author-slug>/<numeric-id>`. The author-slug is in the URL path and is
used as a fallback (`humanize_slug`); the rendered Cyrillic name (e.g.
"Георгий Чижов") is preferred and sets `has_byline=True`. Dates are Russian
strings ("21 июля 2026 г.") parsed to ISO with an explicit month map, from the
article page with the card date as fallback. `dates_missing` is reported in the
summary.

**trafilatura config:** `favor_precision=True`, tables/comments/links off,
`target_language="ru"`, plus a line filter for The Insider's heavy inline
donation CTAs (`поддержите нас`, `нам очень нужна ваша помощь`,
`регулярные пожертвования`, `подпиш…`) so they don't inflate `word_count`.

**Quirks:** author name lives in the URL slug (Latin) but is rendered in Cyrillic
on the page — both captured. No stated reading time on this source
(`stated_reading_time` = null). An RSS feed exists at `/feed` but is site-wide,
not opinions-only — not used for harvesting.

**Run:** `python scrape_theinsider.py 15`. Requires Playwright + Chromium
(`python -m playwright install chromium`). Outputs
`output/theinsider_opinions.jsonl`, `output/raw_html/theinsider/<id>.html`,
`output/theinsider_run_summary.json`.


# KuKi corpus — Turkish opinion scrapers

Four **independent** per-source scrapers for Turkish columnist/opinion sections,
feeding the existing KuKi corpus contract. No shared abstraction across the four
files — duplication is deliberate and matches the RU convention
(`scrape_theinsider.py`, `scrape_ng_vision.py`, `scrape_holod.py`).

| File | Source | orientation | factuality_tier | collection |
|------|--------|-------------|-----------------|-----------|
| `scrape_cumhuriyet.py` | cumhuriyet | secular_kemalist_opposition | high_factuality | JS load-more (HTTP replay *or* Playwright) |
| `scrape_sabah.py` | sabah | state_aligned | mixed | single-page `arsiv?getall=true` |
| `scrape_yenicag.py` | yenicag | ultranationalist_opposition | disinfo_prone | BilginPro `?sayfa=N` |
| `scrape_odatv.py` | odatv | nationalist_alternative | disinfo_prone | BilginPro `?sayfa=N` + author allow-list |

All four: `section=yazarlar`, `genre=opinion_column`.

## Corpus contract (identical to RIA / opinion trio)

Every JSONL record carries:

```
url, article_id, date (ISO 8601), source, section, orientation,
factuality_tier, genre, title, subtitle, body,
content,                 # = title + "\n\n" + body  (the $content LS field)
author, has_byline,
char_count, word_count, paragraph_count,
prose_paragraph_count, mean_paragraph_len, sentence_count,
stated_reading_time
```

`content` merges title + body with a `\n\n` separator — the exact field the
Label Studio `<View>` renders. Sabah adds a `uid` (`YYYY-MM-DD__slug`) because
its `article_id` (the title-slug) is not globally unique on its own.

Raw HTML for every article is written to the raw store
(`raw_store/<source>/<id>.html`) for re-extraction, same as the RU scrapers.

## Requirements

```
pip install trafilatura lxml requests
pip install playwright        # only if driving Cumhuriyet's load-more via browser
python -m playwright install chromium
pip install pytest            # tests only
```

## Run commands

First-run mode is `--smoke` (target = 15 articles/author). `--full` comes later.

```bash
# Cumhuriyet — HTTP load-more (preferred). Pass --endpoint once captured live.
python scrape_cumhuriyet.py --smoke --authors ozgur-mumcu,cigdem-toker \
    --endpoint "https://www.cumhuriyet.com.tr/yazarlar/{author}/daha-fazla?page={page}"
# Cumhuriyet — Playwright fallback (no stable HTTP endpoint)
python scrape_cumhuriyet.py --smoke --playwright --authors ozgur-mumcu

# Sabah — single-page archive (NB hyphen-free slugs)
python scrape_sabah.py --smoke --authors melihaltinok,ardic,donat

# Yeniçağ — numbered pagination
python scrape_yenicag.py --smoke --authors yavuz-selim-demirag,murat-agirel

# Oda TV — ALLOW-LIST driven (see note below)
python scrape_odatv.py --smoke --authors odatv_authors_allowlist.txt
```

## Opinion-isolating entry points (site-structure research)

Genre fit comes from WHERE we harvest, not a content proxy — this is the front
gate; the `genre_stance_filter` remains the semantic gate. Full research in
`sources/tr_opinion_entrypoints.md`. New options per source:

```bash
# Cumhuriyet — institutional opinion STREAMS lead the defaults now
#   (olaylar-ve-gorusler, olaylarin-ardindaki-gercek, cumhuriyet, konuk-yazarlar).
# Auto-discover columnists = master roster MINUS sport/lifestyle category pages:
python scrape_cumhuriyet.py --full --discover
# Full historical depth (bypasses the JS load-more) via the posts sitemap:
python scrape_cumhuriyet.py --full --sitemap

# Sabah — discover authors from the on-target CATEGORY hubs
#   (sabah, site, perspektif, bolgeler; excludes spor/gunaydin), and paginate
#   the full archive (getall?page=N, not the 20-item window):
python scrape_sabah.py --full --discover

# Yeniçağ — curated allow-list is the default; add the near-pure guest stream:
python scrape_yenicag.py --full --konuk-kalem

# Oda TV — the curated allow-list IS the gate (no opinion-only section exists):
python scrape_odatv.py --full --authors odatv_authors_allowlist.txt
```

Notes: Cumhuriyet's 4 collective streams are in `DEFAULT_AUTHORS`, so a plain
`--smoke` already samples them. Sabah `--discover` reads real slugs off the hubs
(hyphenation varies) and fixes the earlier ~20/author cap. Yeniçağ/Oda TV have
flat, un-categorised rosters, so they keep per-author allow-lists (drop-lists in
the research doc).

`--authors` accepts either a comma-separated list of slugs or a path to a file
(one slug per line, `#` comments allowed). `--out` and `--raw-dir` are
configurable; defaults are `out/<source>.jsonl` and `raw_store/`. Each run also
writes `<out>.summary.json`.

The `--authors` defaults in each file are **example slugs** for the smoke run —
replace them with the operator's confirmed in-scope roster for a full harvest.

## Run summary

Every run prints (and saves as JSON) an operator-readable summary with the
`word_count` and `paragraph_count` distribution (min / median / max + histogram
bins) so corpus quality is verifiable without reading Turkish. A healthy
columnist harvest skews long; a **low median triggers an explicit WARNING** —
section or extraction is probably wrong. Every record with `word_count < 150` is
listed under `low_content_ids`.

## Turkish-specific handling (all scrapers)

- **Casing**: never naive `.lower()`. `tr_lower()` uses an explicit map
  (`I→ı`, `İ→i`, `Ş→ş`, `Ğ→ğ`, `Ç→ç`, `Ö→ö`, `Ü→ü`) for every dedup key and
  string comparison. Round-trips are unit-tested.
- **Dedup**: by `article_id` first; normalized-URL only as a fallback.
- **Dates**: ISO in meta tags preferred where present; Turkish long-form
  (`24 Temmuz 2026`) parsed via an explicit `TR_MONTHS` map (Ocak…Aralık) — no
  reliance on system locale. Unparseable dates **fail loud**.
- **Extraction**: trafilatura with `include_tables=False`, narrowed extraction,
  and a per-source `strip_residual_artifacts` + boundary-marker truncation.
  Artifacts are fixed **at extraction**, never downstream, so artifact-inflated
  word counts never misrepresent real content.

## Confirmed endpoints

- **Cumhuriyet author page** (confirmed live):
  `https://www.cumhuriyet.com.tr/yazarlar/{author}`; article URLs
  `/yazarlar/{author}/{title-slug}-{numericID}`. Plain `requests` with a
  minimal header set hits a **consent/anti-bot redirect loop**
  (`TooManyRedirects`) — the scraper now sends a full browser header set, primes
  session cookies from the homepage, and passes a `Referer`. If a request still
  loops it fails loud with guidance to use `--playwright`.
- **Cumhuriyet load-more**: *live-only* — capture the "Daha Fazla Yazı Göster"
  XHR in the operator env and pass it via `--endpoint` (a `{author}`/`{page}`
  template). The confirmed value is written to
  `discovered_endpoint_cumhuriyet.txt`. If no stable HTTP endpoint exists, use
  `--playwright`. Choice must be recorded here after the first live run.
- **Sabah** (confirmed live): `https://www.sabah.com.tr/yazarlar/{author}/arsiv/getall`
  — whole archive in one response, no pagination. The `getall` is a **path
  segment**, not a `?getall=true` query. Sabah author slugs are **hyphen-free**
  (`melihaltinok`, `ardic`, `donat`) — unlike the hyphenated slugs everywhere
  else. Article URLs: `/yazarlar/{author}/{YYYY}/{MM}/{DD}/{title-slug}`.
- **Yeniçağ**: `https://www.yenicaggazetesi.com/yazarlar/{author}?sayfa=N`
  (canonical `.com`; the `.com.tr` mirror is rejected — we assert we never cross
  domains).
- **Oda TV**: `https://www.odatv.com/yazarlar/{author}?sayfa=N`.

## Per-source known issues / design notes

### Cumhuriyet
Cleanest source; no paywall on columns. Byline reliably in `meta-articleAuthor`.
Load-more strategy (HTTP replay vs Playwright) is an engineering choice made on
measured performance in the operator env — HTTP preferred if the endpoint is
stable/fast. End-of-author: an empty fragment **or** an all-seen fragment is a
clean stop; a 4xx/5xx or a load-more click that injects no new IDs while the
button is still present fails loud.

### Sabah
0. **Title vs author** — some Sabah columns (e.g. Engin Ardıç) put the *author
   name* in the `<h1>`. Title is therefore taken from `og:title` first (then
   `<title>`, then the URL title-slug), and the code guarantees `title !=
   author`; author comes from `meta-articleAuthor`. Markdown emphasis markers,
   `*******` dividers, "read more" teasers, and duplicate paragraphs are
   stripped from the body.
1. **Cloudflare** — `fetch()` uses a realistic browser header set and retries
   with exponential backoff; if still blocked it raises `CloudflareBlock`
   (fail loud with the block reason — never a silent empty return).
2. **Paragraph structure** — the spec anticipated `<br><br>`-separated bodies.
   Live confirmation (2026): current Sabah columns are `<p>`-structured with NO
   `<br><br>`, so segmentation uses trafilatura markdown (identical to the other
   three scrapers). A legacy `<br><br>` / `*******` splitter is retained and
   auto-selected only when a body actually contains `<br><br>`. Either way, a
   footer/nav filter drops labels like "Veri Politikası" / "İş İlanları", and
   the offline tests assert `paragraph_count` is non-degenerate for BOTH the
   `<p>` and `<br>` layouts.
3. Date is parsed from the URL path and **cross-checked** against
   `meta-datePublished`; a mismatch fails loud.
4. Body-node detection tries known BilginPro/Sabah class names, then falls back
   to a heuristic (the element with the most direct-child `<br>` nodes) so the
   `<br>` splitter still works if Sabah's markup class names change.

### Yeniçağ
BilginPro, server-rendered. **Confirmed live (corrected from the original
spec):** the `<h1>` is the **article title**, not the author. The author is the
"**{Author} yazdı…**" byline in `og:description` (or `<meta
property="article:author">`); title comes from `og:title`/`<h1>`, and the code
guarantees `title != author`. The publication **date comes from `<meta
property="article:published_time">`** (ISO) or `<time datetime>` — NOT the
`.date` masthead span, which shows *today* and previously poisoned every date.
Body cleaning strips the "Diğer Yazarlar" rail, KVKK/çerez notices, the masthead
date line, "Yazıyı Paylaş"/"Etiketler" nav, markdown emphasis markers, and
de-duplicates the occasional doubled column body. End-detection stops on an
empty or all-seen page (never infinite-loops); 4xx/5xx fails loud. Register is
L4-dense nationalist (Turan / beka / Saray / FETÖ / "Londra'nın atlıları").

### Oda TV
Same BilginPro template as Yeniçağ, kept as a **separate file** by convention.
Same confirmed-live extraction contract: `<h1>` = title; **date** from `<meta
property="article:published_time">` / `.post-info-bar__publish_date` (never the
masthead); **author** from the "{Author} yazdı…" byline — note `article:author`
here is the institutional "Odatv", so the byline (falling back to the URL
author-slug) is authoritative. Body cleaning drops the "Soner Yalçın yazdı…"
byline line, "En Çok Okunanlar/İzlenenler" rails, cookie/KVKK, masthead date,
emphasis markers, and duplicate paragraphs.

**Author-level genre mixing**: the roster mixes political columnists (Soner
Yalçın, Nihat Genç, Müyesser Yıldız — the L3/L4 payload) with gastronomy / TV /
health columnists (GastrOda etc.). Genre is **per-author, not per-section**, so
the scraper takes an explicit **author allow-list** (`--authors` or
`odatv_authors_allowlist.txt`) — a source-selection decision made by the
operator, *not* an auto-classification done per-article downstream. Only listed
slugs are harvested.

## Source provenance / copyright

Sabah and Yeniçağ (and Oda TV) carry hard "kesinlikle kullanılamaz" /
all-rights-reserved copyright notices. Harvest here is for an **internal
research corpus** only; raw HTML is retained solely for re-extraction and the
merged `content` feeds annotation. Redistribution is out of scope.

## Tests (offline, no network)

```bash
python -m pytest tests/ -q
```

Fixture-backed coverage (all pure logic is offline-testable; live smoke tests
run in the operator env with normal egress):

- **link scoping** — accept real article URLs, reject author-roots, other
  sections, cross-domain and the Yeniçağ `.com.tr` mirror;
- **Turkish date parsing** — ISO-meta preference + long-form month map + fail
  loud;
- **Turkish casing round-trips** — `İ/ı/Ş/ş/Ğ/ğ/Ç/Ö/Ü`, and divergence from
  naive `.lower()`;
- **extraction / boilerplate boundaries** — İlgili Konular, Yasal Uyarı, Diğer
  Yazarlar, En Çok Okunanlar, KVKK/çerez rails stripped; author-vs-title
  correction on BilginPro;
- **pagination end-detection** — empty-page stop, all-seen-page stop, continue,
  and the domain-crossing fail-loud path;
- **Sabah `<br>` splitter** — `paragraph_count` is not degenerate (≥ 4 on the
  multi-paragraph fixture, not 1).

## Live-only steps (cannot verify offline — flagged in each run summary)

- Cumhuriyet load-more endpoint shape / whether Playwright is required.
- Sabah Cloudflare behavior under the chosen header set.
- Yeniçağ / Oda TV true page-count per author and the exact empty-page response.


# Genre / stance-presence corpus filter

An LLM-judge filter that decides which articles enter the annotation corpus,
built to **avoid selecting on the dependent variable**. The downstream corpus is
labeled for narrative roles, frames and persuasion (the KuKi codebook); if the
entry filter were sensitive to those, prevalence estimates would be contaminated.
So this filter judges **only form and register**, never stance *content*.

## The construct (decomposed, anti-circular)

Three facets, scored **separately** so a judge can't collapse them onto a vague
"quality" axis and so disagreement is diagnosable per facet:

- **A — Genre**: `analysis_opinion` vs `reported_news` vs `wire_or_listing` vs `other`.
- **B — Stance presence (0–3)**: does the text advance an interpretive claim of
  its *own*, beyond neutral event reporting? **Presence, not content** — never
  whether the stance is convincing, warranted, or which side it takes.
- **C — Discourse development (0–3)**: how developed is the connected
  argumentative/expository text.

The judge prompt (`rubric.py`) **never** names frames, persuasion, propaganda,
roles (hero/villain/victim), coded language, the topic, or the political side.
A test (`test_no_codebook_leakage_in_prompt`) fails the build if any of that
vocabulary appears in the prompt — even in negation, because naming the DV primes
the judge.

## Stated combination rule (not inside the judge)

Judges only score facets. The keep/drop decision is applied afterwards to the
**aggregated** facets, by an explicit rule you choose (`aggregate.py`):

- `genre_only` — keep `analysis_opinion` regardless of stance/discourse. Most
  conservative about the stance↔persuasion correlation, since it never conditions
  on stance at all.
- `genre_discourse` — keep `analysis_opinion` with developed discourse.
- `genre_stance_disc` *(default)* — keep `analysis_opinion` with stance ≥2 **and**
  discourse ≥2.

Cross-judge aggregation: genre by plurality (ties / weak plurality → *contested*);
stance & discourse by ordinal median. **Contested items are never auto-kept** —
they become `borderline` and route to a human. Every raw judge vote is retained
per article for audit.

> Note on facet B: stance-*presence* is register-level and upstream of the
> codebook, but it is mildly correlated with persuasion prevalence. If you want
> zero conditioning on anything stance-adjacent, run `--rule genre_only` and keep
> B/C only as recorded diagnostics.

## Workflow

```bash
pip install -r requirements.txt
export OPENROUTER_API_KEY=sk-or-...

# 1. Draw the stratified human-gold sample (blind sheet, no judge scores)
python stratify_gold.py --in output/ria_analitika.jsonl --n 60 --bins 4 \
    --out-csv gold_sheet.csv --out-jsonl gold_sheet.jsonl
#    -> a human fills gold_genre / gold_stance / gold_discourse / gold_keep

# 2. Run the 5-judge panel over the corpus (cached; safe to re-run/resume)
python run_filter.py --in output/ria_analitika.jsonl \
    --out output/filter_decisions.jsonl --rule genre_stance_disc

# 3. Validate judges against gold; pick the final 3
python validate_judges.py --gold gold_sheet.jsonl \
    --votes output/filter_decisions.jsonl --final 3

# 4. Re-run step 2 with just the 3 chosen models
python run_filter.py --in output/ria_analitika.jsonl \
    --out output/filter_decisions.jsonl \
    --models anthropic/claude-3.5-sonnet openai/gpt-4o google/gemini-2.5-pro
```

## Judge panel

`DEFAULT_JUDGE_MODELS` in `judges.py` lists 5 candidates from different labs
(diverse labs → ensemble isn't shared-bias). **Verify the model slugs against the
live OpenRouter catalog before running** — slugs change. Requests go to
`https://openrouter.ai/api/v1/chat/completions` at `temperature=0`, with JSON
mode and tolerant fallback parsing.

Outputs are cached on disk keyed by `(model, prompt_version, article_id)`, so
adding a judge, resuming, or re-running only pays for uncomputed cells. Bump
`PROMPT_VERSION` in `rubric.py` to invalidate the cache when the rubric changes.

## Validation metrics (`metrics.py`, dependency-free)

- Genre: Cohen's κ + accuracy (nominal).
- Stance / discourse: quadratic-weighted κ (ordinal 0–3).
- Ordinal Krippendorff's α for multi-rater reliability.
- Keep/drop decision: precision / recall / F1 vs `gold_keep`, per judge and for
  the ensemble.

Judges are ranked by combined gold agreement; the top-N is a **starting point**
for the final panel — sanity-check lab diversity before committing.

## Tests

```bash
python tests/test_pipeline.py   # no API needed (mock judges)
```

Covers: prompt-leakage guard, tolerant JSON parsing, all three combination rules,
plurality / tie→contested / all-judges-failed handling, stratified-sample
coverage + determinism, the agreement metrics, and an end-to-end 5-judge mock
panel.

## What this does NOT do

It labels a `decision` per article; it does not delete anything. Filtering the
corpus by that label is a downstream step you control. It is a *silver* gate
validated against your human gold — not a replacement for the two-annotator gold
pass.

# Source registry (`sources.csv`)

## Why this file exists

Outlet-level judgements are **not** stored on article records. They are
properties of the OUTLET, asserted once by a human at corpus-design time — not
observations derived from any individual text. Denormalising them onto every
record makes an assumption look like a measurement, and makes it expensive to
revise (a re-typing of one outlet would require rewriting every record from it).

Records therefore carry only:

* **collection provenance** — `url`, `article_id`, `date`, `source`, `section`
  (facts about where the article came from; unrecoverable if dropped), and
* **text-derived fields** — `title`, `subtitle`, `body`, `content`, `author`,
  `has_byline`, the structural signals (`char_count`, `word_count`,
  `paragraph_count`, `prose_paragraph_count`, `mean_paragraph_len`,
  `sentence_count`), `stated_reading_time`, and per-source structural flags
  (`suspected_interview` on Holod, `uid` on Sabah).

Everything else joins on `source` at analysis time.

## Removed from records (2026-08)

| Field | Why removed |
|---|---|
| `orientation` | Outlet-level human judgement, constant per source. |
| `factuality_tier` | Same. Values were placeholders pending the source-typing sheet. |
| `genre` | Outlet-level *assumption* about the section — and genre is precisely what `genre_stance_filter` is built to decide. Storing it pre-judges the gate. |

Guards are in `test_extraction.py` and `test_run_integration.py`: any of these
three reappearing in a record fails the test loudly.

`genre` on Holod was formally text-derived (`"interview"` / `"opinion_essay"`),
but it was a genre *verdict*. The raw structural signal it was computed from
(`suspected_interview` — tag presence, Q/A ratio, dash-initial paragraph ratio)
is kept, so nothing is lost and the verdict is left to the filter.

## Columns

* `source` — join key; matches the `source` field on every record.
* `country`, `lang` — RU/TR, ru/tr.
* `orientation` — the political-position axis of the two-axis design matrix.
* `factuality_tier` — **currently blank for all sources, deliberately.** These
  are placeholders until the MBFC-analogue source-typing sheet is finished.
  Blank is honest; a placeholder value would read as a finding.
* `expected_genre` — documentation of what the harvested section *should*
  contain. **Not a label.** Never join this onto records before filtering; it
  exists so a reader can see the design intent, and so a filter result that
  contradicts it is visible as a signal about the section.

## Usage

```python
import csv, json

sources = {r["source"]: r for r in csv.DictReader(open("sources.csv"))}
recs = [json.loads(l) for l in open("out/cumhuriyet.jsonl") if l.strip()]
for r in recs:
    meta = sources[r["source"]]      # orientation / factuality_tier / country / lang
```

The join belongs in analysis code, or in the corpus merge step — never in a
scraper.

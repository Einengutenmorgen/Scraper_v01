#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Local verification of extraction + signal logic against a synthetic RIA-shaped
article. ria.ru is not reachable from the build sandbox, so this proves the
extraction PLUMBING (body pruning, byline, signals) on controlled HTML."""
import scrape_ria as R

ESSAY_PARAS = [
    "Похоже, что нынешняя война на Ближнем Востоке становится точкой слома "
    "имперской стратегии администрации. Это первое длинное предложение эссе, "
    "которое задаёт тон всему последующему аналитическому тексту автора.",
    "Во втором абзаце автор развивает мысль о том, как меняется расстановка сил. "
    "Он приводит исторические параллели и объясняет, почему прежний порядок "
    "больше не работает так, как раньше работал в предыдущие десятилетия.",
    "Третий абзац посвящён экономическим последствиям. Здесь много сложных "
    "многосоставных предложений. Автор рассуждает о нефти, санкциях и логистике. "
    "Каждое утверждение подкрепляется рассуждением, а не просто фактом.",
    "В заключение автор формулирует прогноз. Мир после этих событий будет иным. "
    "Он призывает читателя задуматься о будущем и о роли своей страны в нём.",
]

# Synthetic RIA-ish article: real essay body wrapped in the exact junk the
# extractor must prune — nav, related rail, tag list, share block, comments, a table.
def build_article_html(signed: bool) -> str:
    author_block = (
        '<div class="article__author-name"><a href="/authors/yakovenko">'
        'Александр Яковенко</a></div>' if signed else ""
    )
    meta_author = "Александр Яковенко" if signed else "РИА Новости"
    paras = "\n".join(f'<div class="article__text">{p}</div>' for p in ESSAY_PARAS)
    return f"""<!DOCTYPE html><html lang="ru"><head>
<meta charset="utf-8">
<meta name="author" content="{meta_author}">
<meta property="og:title" content="Мир после Ирана">
<title>Мир после Ирана - РИА Новости</title></head>
<body>
<header class="header"><nav class="menu"><a href="/">Главная</a>
<a href="/politics/">Политика</a><a href="/economy/">Экономика</a></nav></header>

<main>
<div class="article">
  <h1 class="article__title">Мир после Ирана</h1>
  {author_block}
  <div class="article__body">
    {paras}
  </div>
</div>

<!-- JUNK that must NOT inflate the body -->
<div class="article__tags"><a href="/tag/iran">Иран</a>
  <a href="/tag/usa">США</a><a href="/tag/tramp">Трамп</a></div>

<div class="share"><a>Поделиться ВКонтакте</a><a>Telegram</a><a>Одноклассники</a></div>

<aside class="article__aside">
  <div class="list list-related"><h3>Ещё материалы</h3>
    <div class="list-item"><a class="list-item__title"
       href="https://ria.ru/20260101/foo-2099999999.html">Совсем другая статья один</a></div>
    <div class="list-item"><a class="list-item__title"
       href="https://ria.ru/20260102/bar-2099999998.html">Совсем другая статья два</a></div>
    <div class="list-item"><a class="list-item__title"
       href="https://ria.ru/20260103/baz-2099999997.html">Совсем другая статья три</a></div>
  </div>
</aside>

<table class="data"><tr><td>Показатель</td><td>Значение</td></tr>
  <tr><td>ВВП</td><td>100500</td></tr></table>

<section class="comments"><h3>Комментарии</h3>
  <div class="comment">Первый коммент от читателя, не относится к тексту.</div>
  <div class="comment">Второй коммент, тоже мусор для корпуса.</div></section>
</main>

<footer class="footer">© РИА Новости 2026. Все права защищены.</footer>
</body></html>"""


def run_case(signed: bool):
    ref = R.ArticleRef(url="https://ria.ru/20260317/iran-2081087907.html",
                       article_id="2081087907", date="20260317")
    html = build_article_html(signed=signed)
    rec = R.extract_record(ref, html)
    print(f"\n--- signed={signed} ---")
    print("title       :", rec.title)
    print("byline      :", rec.byline, "| has_byline:", rec.has_byline)
    print("date        :", rec.date)
    print("char_count  :", rec.char_count)
    print("word_count  :", rec.word_count)
    print("paragraphs  :", rec.paragraph_count)
    print("mean_par_len:", rec.mean_paragraph_len)
    print("sentences   :", rec.sentence_count)
    print("content[:60]:", repr(rec.content[:60]))
    print("provenance  :", rec.source, rec.section)

    # Assertions: junk pruned, essay kept.
    body = rec.body
    assert rec.title == "Мир после Ирана", rec.title
    for junk in ["Поделиться", "Комментарии", "Первый коммент", "Показатель",
                 "Совсем другая статья", "Главная", "права защищены", "Иран</a>"]:
        assert junk not in body, f"JUNK LEAKED INTO BODY: {junk!r}"
    # related-article ids must NOT appear anywhere in the body
    for bad_id in ["2099999999", "2099999998", "2099999997"]:
        assert bad_id not in body
    assert rec.paragraph_count == len(ESSAY_PARAS), \
        f"expected {len(ESSAY_PARAS)} paras, got {rec.paragraph_count}"
    assert rec.word_count > 80, rec.word_count
    assert rec.content.startswith(rec.title)
    if signed:
        assert rec.has_byline and rec.byline == "Александр Яковенко"
    else:
        assert not rec.has_byline and rec.byline == "РИА Новости"
    # axis tags fixed
    assert (rec.source, rec.section) == ("ria_novosti", "analitika")
    # Outlet-level judgements must NOT be stored per record (they belong in
    # sources.csv). Fail loud if any of them creeps back into the schema.
    for banned in ("orientation", "factuality_tier", "genre"):
        assert not hasattr(rec, banned), (
            f"{banned!r} is an OUTLET-level field and must not be on a record")
    print("  PASSED")


def test_byline_classifier():
    # The exact 2096556188 bug: rubric label "Аналитика" must NOT be a byline.
    assert not R._looks_like_person("Аналитика")
    assert not R._looks_like_person('МИА "Россия сегодня"')
    assert not R._looks_like_person("РИА Новости")
    assert not R._looks_like_person("Спутник")
    assert not R._looks_like_person("")
    # Real signed authors must still be recognised.
    assert R._looks_like_person("Александр Яковенко")
    assert R._looks_like_person("Виктория Никифорова")
    assert R._looks_like_person("Петр Акопов")
    # An article whose only "author" slot holds the rubric => institutional.
    html = ('<html><head><meta name="author" content="Аналитика">'
            '<title>Доклад</title></head><body>'
            '<h1 class="article__title">Доклад</h1>'
            '<div class="article__text">Первый абзац доклада с достаточной длиной '
            'для того чтобы считаться прозой, а не заголовком раздела.</div>'
            '</body></html>')
    ref = R.ArticleRef("https://ria.ru/20260603/doklad-2096556188.html",
                       "2096556188", "20260603")
    rec = R.extract_record(ref, html)
    assert rec.byline == "РИА Новости" and rec.has_byline is False, \
        (rec.byline, rec.has_byline)
    print("  byline classifier: 'Аналитика' -> institutional (bug fixed);"
          " real names still signed")


def test_prose_paragraph_count():
    # A доклад-style body: prose paragraphs + numbered sub-headers + bullets.
    body = "\n".join([
        "Это первый содержательный абзац доклада, состоящий из нескольких "
        "предложений и явно являющийся прозой, а не заголовком.",
        "1.1. Раздел первый",                      # sub-header
        "Второй прозаический абзац с нормальной длиной и завершающей точкой.",
        "- первый пункт списка",                   # bullet
        "- второй пункт списка",                   # bullet
        "2) Ещё один нумерованный заголовок",      # numbered header
        "Заключительный абзац текста доклада, тоже проза с точкой в конце.",
    ])
    s = R.compute_signals(body)
    assert s["paragraph_count"] == 7, s["paragraph_count"]
    assert s["prose_paragraph_count"] == 3, s["prose_paragraph_count"]
    print(f"  prose paragraphs: total={s['paragraph_count']} "
          f"prose={s['prose_paragraph_count']} (list/headers excluded)")


if __name__ == "__main__":
    run_case(signed=True)
    run_case(signed=False)
    test_byline_classifier()
    test_prose_paragraph_count()
    print("\nALL EXTRACTION ASSERTIONS PASSED")

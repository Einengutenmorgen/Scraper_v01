#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression test for the Holod byline.

THE BUG (found 2026-08, smoke run showed author="—" on all 15 articles):
extract_record did `author = ref.author or author`, so the WordPress
`_embedded.author` account name -- a CMS user, sometimes a bare "—" placeholder
-- overwrote the correct byline read from the article's "Автор:" block.

The article page is authoritative; the WP account is a fallback only. Fixture
mirrors the real DOM (verified against saved holod.media pages).
"""
import scrape_holod as H

INFO = ('<div class="article__footer"><div class="article__info">'
        '<div class="article__info-item"><span>Автор:</span><span>{author}</span></div>'
        '<div class="article__info-item"><span>Редактор:</span><span>Юлия Дудкина</span></div>'
        '<div class="article__info-item"><span>Фото:</span><span>Холод</span></div>'
        '</div></div>')

PARA = ("Это достаточно длинный абзац текста статьи, который содержит много слов "
        "и предложений, чтобы алгоритм извлечения основного содержания счёл его "
        "основным текстом, а не навигацией или подписью. ")


def page(author="Юрий Белят"):
    return ("<html><head><title>t</title>"
            '<meta property="og:title" content="Заголовок статьи">'
            "</head><body><article><h1>Заголовок статьи</h1>"
            + "".join(f"<p>{PARA}</p>" for _ in range(4))
            + INFO.format(author=author)
            + "<span>7 минут чтения</span></article></body></html>")


def ref(author=None):
    return H.Ref(url="https://holod.media/2026/01/01/test-slug/",
                 article_id="test-slug", date="2026-01-01", author=author)


def main():
    # 1. the page byline is read at all
    _, _, a, _ = H._extract_meta(page())
    assert a == "Юрий Белят", a
    print("  page byline read from article__info  OK")

    # 2. the editor/photo rows must NOT be mistaken for the author
    assert a != "Юлия Дудкина" and a != "Холод", a
    print("  Редактор/Фото rows not used as byline  OK")

    # 3. THE REGRESSION: a WP placeholder must not beat the page byline
    rec = H.extract_record(ref(author="—"), page())
    assert rec.author == "Юрий Белят", (
        f"WP account name won over the page byline: {rec.author!r} "
        f"-- precedence is inverted again")
    assert rec.has_byline is True
    print("  WP '—' placeholder does NOT override page byline  OK")

    # 4. a real WP account name also must not beat the page byline
    rec = H.extract_record(ref(author="Виктор Билан"), page())
    assert rec.author == "Юрий Белят", rec.author
    print("  WP account name does NOT override page byline  OK")

    # 5. WP name IS used when the page has no info block
    bare = page().replace(INFO.format(author="Юрий Белят"), "")
    rec = H.extract_record(ref(author="Виктор Билан"), bare)
    assert rec.author == "Виктор Билан", rec.author
    print("  WP account used as FALLBACK when page has no byline  OK")

    # 6. placeholders never become a byline
    rec = H.extract_record(ref(author="—"), bare)
    assert rec.author is None and rec.has_byline is False, rec.author
    for junk in ["—", "-", " ", "·", "", "–"]:
        assert H._clean_name(junk) is None, junk
    assert H._clean_name("Юрий Белят") == "Юрий Белят"
    print("  dash/punctuation placeholders rejected by _clean_name  OK")

    # 7. extraction must not depend on how many articles ran before it
    #    (trafilatura deduplicate=False -- see the note in scrape_holod.py)
    assert H._TRAFI["deduplicate"] is False, (
        "deduplicate=True re-enables trafilatura's process-global LRU, which "
        "silently deletes repeated paragraphs from later articles")
    first = H.extract_record(ref(), page()).word_count
    for _ in range(6):
        H.extract_record(ref(), page())
    last = H.extract_record(ref(), page()).word_count
    assert first == last > 0, (
        f"word_count drifted across repeated extractions: {first} -> {last}")
    print(f"  extraction order-independent ({first} words, stable over 8 runs)  OK")

    print("\nHOLOD BYLINE + DEDUP REGRESSION TEST PASSED")


if __name__ == "__main__":
    main()

"""Р-78: TG-style HTML → MAX elements, and prepare_text by parse_mode (plan 2026-10-22 Р5; QA R5-28, R5-29)."""

import pytest
from pymax.formatting.markdown import Formatter

from app.max import frames
from app.max.html_text import HtmlTextError, html_to_elements, utf16_len

SAMPLES = ["a_b_c", "token=3mbQ_wto_fO_Dcu1", "my_file_name.pdf", "**x**", "__x__", "~~x~~", "`x`", "```x```",
           "# x", "> x", "[x](https://a.b/c_d)", "https://example.com/a_b*c~d", "Привет, мир 👋 _x_"]


@pytest.mark.parametrize("text", SAMPLES)
def test_no_parse_mode_is_byte_for_byte(text):
    assert frames.prepare_text(text, None) == (text, [])


@pytest.mark.parametrize("text", SAMPLES)
def test_markdown_is_pymax_formatter(text):
    clean, elements = Formatter.format_markdown(text)
    assert frames.prepare_text(text, "markdown") == (clean, list(elements))
    assert frames.prepare_text(text, "Markdown")[0] == clean


def test_every_tag_of_the_subset():
    text = ('<b>b</b><strong>s</strong><i>i</i><em>e</em><u>u</u><ins>n</ins><s>s</s><strike>k</strike><del>d</del>'
            '<code>c</code><pre>p</pre><a href="https://max.ru/a_b">l</a><blockquote>q</blockquote>')
    plain, elements = html_to_elements(text)
    assert plain == "bsieunskdcplq"
    assert [(e["type"], e["from"], e["length"]) for e in elements] == [
        ("STRONG", 0, 1), ("STRONG", 1, 1), ("EMPHASIZED", 2, 1), ("EMPHASIZED", 3, 1), ("UNDERLINE", 4, 1),
        ("UNDERLINE", 5, 1), ("STRIKETHROUGH", 6, 1), ("STRIKETHROUGH", 7, 1), ("STRIKETHROUGH", 8, 1),
        ("MONOSPACED", 9, 1), ("CODE", 10, 1), ("LINK", 11, 1), ("QUOTE", 12, 1)]
    assert elements[11]["url"] == "https://max.ru/a_b"


def test_utf16_offsets_entities_br_and_nesting():
    plain, elements = html_to_elements("👋 Привет <b>мир &amp; <i>a_b</i></b><br>&lt;x&gt; <BR/>end")
    assert plain == "👋 Привет мир & a_b\n<x> \nend"
    assert utf16_len("👋") == 2
    assert [(e["type"], e["from"], e["length"]) for e in elements] == [("STRONG", 10, 9), ("EMPHASIZED", 16, 3)]
    plain, elements = html_to_elements('<pre><code class="language-py">x_y = 1</code></pre>')
    assert (plain, [(e["type"], e["length"]) for e in elements]) == ("x_y = 1", [("CODE", 7)])
    assert html_to_elements("a < b, c > d, a_b") == ("a < b, c > d, a_b", [])


@pytest.mark.parametrize("text,match", [("<span>x</span>", "<span> is not supported"),
                                        ('<span class="tg-spoiler">x</span>', "not supported"),
                                        ("<b>x", "unclosed"), ("x</b>", "does not close"),
                                        ("<b><i>x</b></i>", "does not close"), ("<a>x</a>", "needs href")])
def test_refused_html(text, match):
    with pytest.raises(HtmlTextError, match=match):
        html_to_elements(text)
    with pytest.raises(frames.TextMarkupError, match="parse_mode html"):
        frames.prepare_text(text, "html")


def test_html_elements_are_pymax_elements_as_the_frame_carries_them():
    text, elements = frames.prepare_text('<b>a</b> <a href="https://x.ru">b</a>', "html")
    assert text == "a b"
    assert frames.dump_elements(elements) == [{"type": "STRONG", "from": 0, "length": 1},
                                              {"type": "LINK", "from": 2, "length": 1,
                                               "attributes": {"url": "https://x.ru"}}]
    with pytest.raises(frames.TextMarkupError):
        frames.prepare_text("x", "markdownv2")
    assert frames.prepare_text(None, "html") == (None, []) and frames.prepare_text("", None) == (None, [])

"""Telegram-style HTML → MAX text + elements (plan 2026-10-22 Р5 Р-78). No PyMax here.

Agents come with the habit of TG's parse_mode="html": the same subset of tags becomes MAX's
text elements (the web client's types), the rest is refused before any write:

    b, strong → STRONG          i, em → EMPHASIZED        u, ins → UNDERLINE
    s, strike, del → STRIKETHROUGH                        code → MONOSPACED
    pre → CODE (a <code> right inside <pre> is part of it, as TG writes code blocks)
    a href → LINK (url)         blockquote → QUOTE        br → a line break

Entities (&amp; &lt; …) are decoded; offsets (from, length) count UTF-16 code units, as
MAX and PyMax's markdown formatter do. An unknown tag, an unclosed or stray closing tag,
an <a> without href → HtmlTextError.
"""

from html.parser import HTMLParser

TAGS = {
    "b": "STRONG", "strong": "STRONG",
    "i": "EMPHASIZED", "em": "EMPHASIZED",
    "u": "UNDERLINE", "ins": "UNDERLINE",
    "s": "STRIKETHROUGH", "strike": "STRIKETHROUGH", "del": "STRIKETHROUGH",
    "code": "MONOSPACED",
    "pre": "CODE",
    "a": "LINK",
    "blockquote": "QUOTE",
}
ALLOWED = sorted([*TAGS, "br"])


class HtmlTextError(ValueError):
    pass


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


class _Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.pos = 0  # UTF-16 units written so far
        self.open: list[tuple[str, int, str | None, bool]] = []  # (tag, start, url, skipped)
        self.elements: list[dict] = []

    def _add(self, text: str) -> None:
        self.parts.append(text)
        self.pos += utf16_len(text)

    def handle_data(self, data: str) -> None:
        self._add(data)

    def handle_startendtag(self, tag: str, attrs) -> None:
        if tag == "br":
            self._add("\n")
            return
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "br":
            self._add("\n")
            return
        if tag not in TAGS:
            raise HtmlTextError(f"<{tag}> is not supported; tags: {', '.join(ALLOWED)}")
        url = None
        if tag == "a":
            url = dict(attrs).get("href")
            if not url:
                raise HtmlTextError("<a> needs href")
        skipped = tag == "code" and bool(self.open) and self.open[-1][0] == "pre"
        self.open.append((tag, self.pos, url, skipped))

    def handle_endtag(self, tag: str) -> None:
        if tag == "br":
            return
        if not self.open or self.open[-1][0] != tag:
            expected = f"</{self.open[-1][0]}>" if self.open else "no closing tag"
            raise HtmlTextError(f"</{tag}> does not close an open tag (expected {expected})")
        _, start, url, skipped = self.open.pop()
        length = self.pos - start
        if skipped or length <= 0:
            return
        element = {"type": TAGS[tag], "from": start, "length": length}
        if url is not None:
            element["url"] = url
        self.elements.append(element)


def html_to_elements(text: str) -> tuple[str, list[dict]]:
    """(plain text, [{type, from, length, url?}]) ordered by start; raises HtmlTextError."""
    parser = _Parser()
    parser.feed(text)
    parser.close()
    if parser.open:
        raise HtmlTextError(f"unclosed tags: {', '.join(f'<{t}>' for t, *_ in parser.open)}")
    elements = sorted(parser.elements, key=lambda e: (e["from"], -e["length"]))
    return "".join(parser.parts), elements

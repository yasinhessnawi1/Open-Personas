"""Email-reply parsing (Spec C5, D-C5-4) — extract the NEW content from a reply.

The one real per-platform complexity email adds: a reply arrives tangled with its quoted
history (``On … wrote:`` / ``-----Original Message-----`` / ``>`` blocks), a signature, and
client disclaimers — and the persona must read what the user *wrote*, not the whole thread.
Two tiers (D-C5-4):

- **Primary — Postmark ``StrippedTextReply``** (server-side quote-strip): trusted as-is when
  present (zero dependency).
- **Fallback — ``mail-parser-reply``** (pure-regex, no ML — chosen over the discontinued,
  scikit-learn-heavy ``talon``): the raw ``TextBody`` (or HTML→text) run through
  ``EmailReplyParser().read(text).latest_reply``, which strips quotes + signature +
  disclaimers across client quoting formats. Exercised explicitly across formats in the
  tests — the safety net proven, not just declared.

A parse miss degrades to "the persona sees a little quoted tail", never a crash. Owned
surface — stdlib + ``mail-parser-reply`` only; api-free.
"""

from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser

from mailparser_reply import EmailReplyParser

__all__ = ["extract_new_content", "html_to_text"]

# Block-level tags whose boundaries become line breaks when flattening HTML to text.
_BLOCK_TAGS = frozenset(
    {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"}
)
_MULTI_BLANK = re.compile(r"\n{3,}")
# English covers our v1 sender base; mail-parser-reply's English patterns also catch the
# structural ``On … wrote:`` / ``-----Original Message-----`` quote markers.
_LANGUAGES = ["en"]


class _TextExtractor(HTMLParser):
    """Flatten HTML to text — block tags become newlines, other markup is dropped."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_startendtag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def text(self) -> str:
        return "".join(self._parts)


def html_to_text(html: str) -> str:
    """A minimal, dependency-free HTML→text flatten (stdlib ``html.parser``).

    Block tags become line breaks; entities are unescaped; runs of blank lines collapse.
    Good enough to hand to the reply parser when only an HTML body is present (the common
    case carries a ``text/plain`` alternative we prefer).
    """
    extractor = _TextExtractor()
    extractor.feed(html)
    extractor.close()
    return _MULTI_BLANK.sub("\n\n", unescape(extractor.text())).strip()


def _strip_reply(text: str) -> str:
    """Strip quoted history + signature + disclaimers via mail-parser-reply (the fallback)."""
    return str(EmailReplyParser(languages=_LANGUAGES).read(text).latest_reply).strip()


def extract_new_content(
    *,
    stripped_text_reply: str | None = None,
    text_body: str | None = None,
    html_body: str | None = None,
) -> str:
    """Extract the user's new message from an inbound reply (D-C5-4).

    Precedence: Postmark's server-side ``StrippedTextReply`` (trusted as-is) → the raw
    ``TextBody`` through the mail-parser-reply fallback → the HTML body flattened then
    through the fallback → ``""`` (an empty/attachment-only inbound). The fallback is what
    the cross-format tests exercise (quotes + signature stripped across Gmail / Outlook /
    Apple / mobile / bottom-post).
    """
    if stripped_text_reply and stripped_text_reply.strip():
        return stripped_text_reply.strip()
    if text_body and text_body.strip():
        return _strip_reply(text_body)
    if html_body and html_body.strip():
        return _strip_reply(html_to_text(html_body))
    return ""

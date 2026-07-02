"""Email-reply parsing (Spec C5, D-C5-4) — criterion 3, across client quoting formats.

These tests deliberately feed RAW ``TextBody`` with **no** ``StrippedTextReply`` so the
``mail-parser-reply`` FALLBACK runs — the safety net proven, not just declared (the case
where Postmark's server-side strip is absent/imperfect). Each asserts the new content is
kept and both the quoted history AND the signature are gone.
"""

from __future__ import annotations

import pytest
from persona_connectors.email.parsing import extract_new_content, html_to_text

_NEW = "Hi Astrid, what's the deadline for the deposit filing?"
_SIGNATURE = "--\nBob Smith\nSenior Engineer, Acme\n+47 123 45 678"
_QUOTED_MARKER = "The deadline is next Friday"  # a unique phrase from the quoted history


_GMAIL = f"""{_NEW}

Thanks,
Bob

On Mon, Jan 1, 2026 at 10:00 AM Astrid <astrid@personas.app> wrote:
> {_QUOTED_MARKER}.
> Let me know if you need anything else.

{_SIGNATURE}
"""

_OUTLOOK = f"""{_NEW}

-----Original Message-----
From: Astrid <astrid@personas.app>
Sent: Monday, January 1, 2026 10:00 AM
To: Bob <bob@x.com>
Subject: Re: Deposit

{_QUOTED_MARKER}.
"""

_APPLE = f"""{_NEW}

On Jan 1, 2026, at 10:00 AM, Astrid <astrid@personas.app> wrote:

> {_QUOTED_MARKER}.

{_SIGNATURE}
"""


@pytest.mark.parametrize(
    ("name", "body"), [("gmail", _GMAIL), ("outlook", _OUTLOOK), ("apple", _APPLE)]
)
def test_fallback_strips_quotes_and_signature_across_clients(name: str, body: str) -> None:
    """The mail-parser-reply fallback keeps the new content, drops quote + signature."""
    result = extract_new_content(text_body=body)
    assert _NEW in result, f"{name}: new content lost"
    assert _QUOTED_MARKER not in result, f"{name}: quoted history leaked in"
    assert "Senior Engineer" not in result, f"{name}: signature leaked in"


def test_plain_bottom_post_reply_without_quote_is_returned_whole() -> None:
    """A plain reply with no quoted history (mobile/bottom-post) is returned intact."""
    body = "Yes, next Friday works. I'll send the form over."
    assert extract_new_content(text_body=body) == body


def test_stripped_text_reply_is_primary_and_used_as_is() -> None:
    """When Postmark supplies ``StrippedTextReply`` (server-side strip), it is trusted as-is
    — the fallback is not invoked (the raw body with its quote is ignored)."""
    result = extract_new_content(
        stripped_text_reply="  Just the new bit.  ",
        text_body=f"Just the new bit.\n\nOn ... wrote:\n> {_QUOTED_MARKER}",
    )
    assert result == "Just the new bit."


def test_html_only_body_is_flattened_then_stripped() -> None:
    """An HTML-only email is flattened to text then reply-parsed."""
    html = (
        f"<div>{_NEW}</div>"
        "<div><br></div>"
        "<blockquote>On Mon, Jan 1, 2026 Astrid wrote:<br>"
        f"{_QUOTED_MARKER}.</blockquote>"
    )
    result = extract_new_content(html_body=html)
    assert _NEW in result
    assert _QUOTED_MARKER not in result


def test_empty_or_attachment_only_inbound_yields_empty() -> None:
    assert extract_new_content() == ""
    assert extract_new_content(stripped_text_reply="   ", text_body="") == ""


def test_html_to_text_unescapes_and_breaks_blocks() -> None:
    assert html_to_text("<p>Hello&amp;bye</p><p>Line 2</p>") == "Hello&bye\nLine 2"
    assert html_to_text("A<br>B") == "A\nB"

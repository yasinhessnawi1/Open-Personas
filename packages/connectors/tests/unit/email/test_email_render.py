"""Outbound email rendering (Spec C5, Group C) — author-affordance From, Re: subject, headers."""

from __future__ import annotations

from persona.schema.origination import PersonaIdentityTag
from persona_connectors.email.render import (
    originated_subject,
    render_from,
    reply_subject,
    thread_headers,
)

_ASTRID = PersonaIdentityTag(persona_id="astrid", display_name="Astrid", visual_ref=None)


def test_render_from_puts_the_persona_in_the_display_name() -> None:
    assert (
        render_from(_ASTRID, address="inbound@x.com") == "Astrid via Open Persona <inbound@x.com>"
    )


def test_render_from_rfc_quotes_a_display_name_with_a_comma() -> None:
    """A comma in a display-name must be quoted or it reads as two addresses (formataddr)."""
    tag = PersonaIdentityTag(persona_id="p", display_name="Smith, John", visual_ref=None)
    rendered = render_from(tag, address="inbound@x.com")
    assert rendered == '"Smith, John via Open Persona" <inbound@x.com>'


def test_reply_subject_adds_a_single_re() -> None:
    assert reply_subject("Deposit dispute") == "Re: Deposit dispute"


def test_reply_subject_is_idempotent_any_case() -> None:
    assert reply_subject("Re: Deposit") == "Re: Deposit"  # not doubled
    assert reply_subject("RE: Deposit") == "RE: Deposit"  # existing prefix preserved


def test_reply_subject_blank_degrades_to_bare_re() -> None:
    assert reply_subject("") == "Re:"
    assert reply_subject(None) == "Re:"


def test_originated_subject_names_the_persona() -> None:
    assert originated_subject(_ASTRID) == "A message from Astrid"


def test_thread_headers_emit_only_present_headers() -> None:
    assert list(thread_headers(references="<root@x>", in_reply_to="<r@x>")) == [
        ("In-Reply-To", "<r@x>"),
        ("References", "<root@x>"),
    ]
    assert list(thread_headers()) == []  # a fresh thread carries none
    assert list(thread_headers(references="<root@x>")) == [("References", "<root@x>")]

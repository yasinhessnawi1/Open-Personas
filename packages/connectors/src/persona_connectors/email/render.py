"""Outbound email rendering (Spec C5, Group C) — the persona tag → email presentation.

Email is the top render tier: ``supports_author_affordance=True`` (C1-D-6), so the persona
identity rides in the **``From`` display-name** (a dedicated "who is speaking" slot), not a
body prefix — the reader sees ``Astrid via Open Persona <inbound@…>`` in their client. Plus
the subject rules: a reply preserves a single ``Re:`` so the user's client threads it
(criterion 8), and an originated email gets a clear, name-identified subject (criterion 7).

Pure — stdlib only (``email.utils.formataddr`` for RFC-correct display-name quoting; the
absolute import resolves to the stdlib ``email`` module, not this ``persona_connectors.email``
package). api-free.
"""

from __future__ import annotations

from email.utils import formataddr
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from persona.schema.origination import PersonaIdentityTag

__all__ = [
    "originated_subject",
    "render_from",
    "render_system_from",
    "reply_subject",
    "thread_headers",
]

_SUFFIX = "via Open Persona"
_PRODUCT = "Open Persona"


def render_system_from(address: str) -> str:
    """The ``From`` for a system (bot-voice) email — the product identity, no persona."""
    return formataddr((_PRODUCT, address))


def render_from(persona: PersonaIdentityTag, *, address: str) -> str:
    """The author-affordance ``From`` — the persona's name in the display-name slot.

    RFC-correct via :func:`email.utils.formataddr` (quotes/escapes a display-name with
    commas/quotes), so ``Astrid`` sending from ``inbound@x`` renders
    ``Astrid via Open Persona <inbound@x>`` — the reader can always tell which persona
    speaks (criterion 8), on the shared inbound address (no per-persona address, D-C5-3).
    """
    return formataddr((f"{persona.display_name} {_SUFFIX}", address))


def reply_subject(original_subject: str | None) -> str:
    """A reply subject with exactly one ``Re:`` so the user's client threads it (criterion 8).

    Idempotent: an already-``Re:``-prefixed subject (any case, extra spaces) is returned
    unchanged rather than doubled (``Re: Re:``); a missing/blank subject degrades to a bare
    ``Re:``.
    """
    subject = (original_subject or "").strip()
    if not subject:
        return "Re:"
    if subject[:3].lower() == "re:":
        return subject
    return f"Re: {subject}"


def originated_subject(persona: PersonaIdentityTag) -> str:
    """A clear, name-identified subject for a C0-originated email (criterion 7)."""
    return f"A message from {persona.display_name}"


def thread_headers(
    *, references: str | None = None, in_reply_to: str | None = None
) -> Sequence[tuple[str, str]]:
    """The threading headers (``In-Reply-To`` / ``References``) as ``(Name, Value)`` pairs.

    Only non-empty headers are emitted, so a fresh (new-thread) originated email carries
    none. Passed straight to :meth:`PostmarkClient.send_email`'s ``headers``.
    """
    headers: list[tuple[str, str]] = []
    if in_reply_to:
        headers.append(("In-Reply-To", in_reply_to))
    if references:
        headers.append(("References", references))
    return headers

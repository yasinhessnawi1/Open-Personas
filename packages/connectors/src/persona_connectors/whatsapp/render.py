"""Outbound rendering for WhatsApp (Spec C4 T8) — `*bold*` name tag over the shared splitter.

Two pure concerns turn a persona reply into WhatsApp message(s):

- **Splitting (D-C4-3).** Twilio enforces a 1600-char hard cap on a WhatsApp body, so
  the reply is split at that budget with the shared, boundary-aware
  :func:`persona_connectors.domain.render.split_text` (D-C3-X-splitter) bound to the
  **code-point** measure (WhatsApp counts characters; ``encoding_sensitive=False`` — no
  GSM-7 segmentation, unlike SMS).
- **Rendering (C1-D-6).** :func:`render_outbound` lowers C0's semantic
  :class:`~persona.schema.origination.PersonaIdentityTag` to WhatsApp's **bold-prefix**
  render tier: a ``*Name*`` header (WhatsApp bold is a **single** asterisk) on the
  **first part only**. The body is sent **as-is** — a stray markdown char (``* _ ~``) is
  a cosmetic mis-render, acceptable v1 (the Discord precedent D-C3-6: no injection risk,
  the name is the unmistakable part and it is bold-wrapped).

Rendering (the `*bold*` wrap) is WhatsApp-specific and stays here; only the split + the
length measure are shared. Pure + api-free.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona_connectors._twilio.client import TWILIO_MAX_MESSAGE_CHARS
from persona_connectors.domain.render import codepoint_measure, split_text

if TYPE_CHECKING:
    from persona.schema.origination import PersonaIdentityTag

__all__ = ["WHATSAPP_SPLIT_BUDGET", "render_outbound"]

# WhatsApp's per-message split budget = Twilio's enforced 1600-char hard cap.
WHATSAPP_SPLIT_BUDGET = TWILIO_MAX_MESSAGE_CHARS


def render_outbound(
    persona: PersonaIdentityTag,
    text: str,
    *,
    budget: int = WHATSAPP_SPLIT_BUDGET,
) -> list[str]:
    """Render a persona reply to WhatsApp message part(s) (D-C4-3 / C1-D-6).

    Lowers the semantic name tag to the bold-prefix tier: a ``*Name*`` header on the
    first part only. Splits the plaintext against the budget (the first part reserving
    room for the header), body sent as-is.

    Args:
        persona: The originating persona's identity tag (the SEMANTIC tag, C1-D-6).
        text: The plain reply body.
        budget: The per-message code-point budget (defaults to Twilio's 1600 cap).

    Returns:
        The ordered WhatsApp message strings to send (at least one — a header-only
        message when the body is empty).
    """
    header = f"*{persona.display_name}*"
    reserve = codepoint_measure(header) + 1  # the header + the newline before the body
    first_budget = max(1, budget - reserve)

    body_chunks = split_text(
        text, budget=budget, first_budget=first_budget, measure=codepoint_measure
    )
    if not body_chunks:
        return [header]
    return [
        f"{header}\n{chunk}" if index == 0 else chunk for index, chunk in enumerate(body_chunks)
    ]

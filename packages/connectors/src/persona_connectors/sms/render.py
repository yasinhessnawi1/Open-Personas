"""SMS encoding + rendering (Spec C4 T11) — the GSM-7/UCS-2 segment matrix + the cap.

SMS is the **plain-prefix floor** (C1-D-6): a persona reply is tagged ``Name: body`` —
no rich formatting (the SMS channel has none). Two encoding facts drive cost (C4-R-4):

- **GSM-7** (the default 7-bit alphabet): **160** chars in a single SMS, **153** per
  segment once a message concatenates (a 6-byte UDH eats 7 chars). Ten **extension**
  characters (``^ { } [ ] ~ \\ | €`` + form-feed) cost **2** septets each.
- **UCS-2** (used the moment ANY character is outside GSM-7): **70** single / **67** per
  concatenated segment; an astral char (most emoji) is **2** UTF-16 code units.
- **The contagion rule:** a single non-GSM-7 character forces the WHOLE message to
  UCS-2 — halving the budget. :func:`gsm7_encodable` decides per whole message.

A long reply is **capped** at ``max_segments`` and otherwise **truncated with an ASCII
``...`` continuation** (criterion 9 — bound the cost; D-C4-3). The marker is ASCII on
purpose: the ``…`` glyph is itself non-GSM-7 and would trip the contagion it exists to
avoid. The boundary-aware cut reuses the shared :func:`~persona_connectors.domain.render.
split_text` with the encoding-correct measure. Pure + api-free.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona_connectors.domain.render import split_text

if TYPE_CHECKING:
    from persona.schema.origination import PersonaIdentityTag

    from persona_connectors.domain.render import LengthMeasure

__all__ = ["gsm7_encodable", "render_outbound", "segment_count"]

# The GSM 03.38 basic alphabet (code points 0x00–0x7F, ESC 0x1B excluded) — one septet each.
_GSM7_BASIC = frozenset(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ"
    "ÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿"
    "abcdefghijklmnopqrstuvwxyzäöñüà"
)
# The GSM 03.38 extension table — each is ESC + char, so it costs TWO septets.
_GSM7_EXTENSION = frozenset("\f^{}\\[~]|€")

_GSM7_SINGLE, _GSM7_CONCAT = 160, 153
_UCS2_SINGLE, _UCS2_CONCAT = 70, 67

# ASCII continuation marker (stays GSM-7 — never the … glyph, which is UCS-2).
_CONTINUATION = "..."


def gsm7_encodable(text: str) -> bool:
    """Whether ``text`` is entirely GSM-7 (basic + extension) — else it is UCS-2 (contagion)."""
    return all(ch in _GSM7_BASIC or ch in _GSM7_EXTENSION for ch in text)


def _gsm7_units(text: str) -> int:
    """GSM-7 septet count — extension characters count as two."""
    return sum(2 if ch in _GSM7_EXTENSION else 1 for ch in text)


def _ucs2_units(text: str) -> int:
    """UCS-2 length in UTF-16 code units — an astral char (emoji) is two."""
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)


def _encoding(text: str) -> tuple[LengthMeasure, int, int]:
    """The (measure, single-budget, concat-budget) for ``text``'s required encoding."""
    if gsm7_encodable(text):
        return (_gsm7_units, _GSM7_SINGLE, _GSM7_CONCAT)
    return (_ucs2_units, _UCS2_SINGLE, _UCS2_CONCAT)


def segment_count(text: str) -> int:
    """How many SMS segments ``text`` bills as (the per-message cost driver, C4-R-4)."""
    measure, single, concat = _encoding(text)
    units = measure(text)
    if units <= single:
        return 1
    return -(-units // concat)  # ceil division into concatenated segments


def render_outbound(
    persona: PersonaIdentityTag,
    text: str,
    *,
    max_segments: int = 3,
) -> str:
    """Render a persona reply to a single SMS body (plain-prefix tier + the segment cap).

    Tags the body ``Name: text`` (the plain-prefix floor). If it fits within
    ``max_segments`` it is returned whole (Twilio auto-concatenates + bills per segment);
    otherwise it is **truncated at a natural boundary with an ASCII ``...``** so the cost
    is bounded (criterion 9). The continuation marker stays GSM-7, never re-triggering the
    UCS-2 contagion.

    Args:
        persona: The originating persona's identity tag (C1-D-6 semantic tag).
        text: The plain reply body.
        max_segments: The per-message segment cap (default 3). ``1`` keeps a reply to a
            single segment (the cheapest, terse-over-SMS option).

    Returns:
        The single SMS body to send (one Twilio ``send_message``).
    """
    body = f"{persona.display_name}: {text}"
    measure, single, concat = _encoding(body)
    limit = single if max_segments <= 1 else max_segments * concat
    if measure(body) <= limit:
        return body
    budget = max(1, limit - measure(_CONTINUATION))
    chunks = split_text(body, budget=budget, measure=measure)
    head = chunks[0] if chunks else body
    return f"{head}{_CONTINUATION}"

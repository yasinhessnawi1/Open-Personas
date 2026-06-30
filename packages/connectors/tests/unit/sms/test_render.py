"""SMS encoding + rendering (Spec C4 T11) — the GSM-7/UCS-2 segment matrix + the cap.

SMS is the plain-prefix floor (no rich formatting): a ``Name: body`` tag. The
load-bearing facts (C4-R-4):
- **GSM-7** packs 7 bits/char → **160** single / **153** per concatenated segment;
  extension chars (caret/braces/brackets/tilde/backslash/pipe/euro + form-feed) cost
  **2** septets each.
- **UCS-2** (any non-GSM-7 char) → **70** single / **67** per concatenated segment; an
  astral char (emoji) is 2 UTF-16 units.
- **The contagion rule:** ONE non-GSM-7 character forces the WHOLE message to UCS-2
  (160→70 budget) — the easy thing to get subtly wrong, proven explicitly here.
- **The cap:** a configurable max-segment cap then **truncate-with-continuation**
  (an ASCII ``...`` marker, NOT the ``…`` glyph — that glyph is itself non-GSM-7 and
  would trigger the contagion it is meant to avoid).
"""

from __future__ import annotations

from persona.schema.origination import PersonaIdentityTag
from persona_connectors.sms.render import gsm7_encodable, render_outbound, segment_count


def _tag(name: str = "Astrid") -> PersonaIdentityTag:
    return PersonaIdentityTag(persona_id="p1", display_name=name, visual_ref=None)


# --- encoding detection ---


def test_gsm7_encodable_accepts_the_basic_set_rejects_others() -> None:
    assert gsm7_encodable("Hello, world! 123")
    assert gsm7_encodable("café")  # é IS in the GSM-7 basic set
    assert gsm7_encodable("Pris: 50€")  # € is the GSM-7 extension (2 septets)
    assert not gsm7_encodable("naïve")  # ï is NOT in GSM-7 → UCS-2
    assert not gsm7_encodable("smart ’quotes’")  # curly apostrophe → UCS-2
    assert not gsm7_encodable("emoji 😀")  # astral → UCS-2
    assert not gsm7_encodable("你好")  # CJK → UCS-2


# --- the GSM-7 segment matrix (160 / 153) ---


def test_gsm7_single_vs_multi_segment_boundaries() -> None:
    assert segment_count("a" * 160) == 1  # exactly one segment
    assert segment_count("a" * 161) == 2  # 161 → 2 segments (153 each once concatenated)
    assert segment_count("a" * 306) == 2  # 2 × 153
    assert segment_count("a" * 307) == 3  # spills into a third


def test_gsm7_extension_char_costs_two_septets() -> None:
    assert segment_count("€" * 80) == 1  # 80 × 2 = 160 septets → still one segment
    assert segment_count("€" * 81) == 2  # 162 septets → two


# --- the UCS-2 segment matrix (70 / 67) ---


def test_ucs2_single_vs_multi_segment_boundaries() -> None:
    assert segment_count("中" * 70) == 1  # 70 UCS-2 units → one segment
    assert segment_count("中" * 71) == 2  # 71 → two (67 each)
    assert segment_count("中" * 134) == 2  # 2 × 67
    assert segment_count("中" * 135) == 3  # spills into a third


def test_astral_emoji_is_two_ucs2_units() -> None:
    assert segment_count("😀" * 35) == 1  # 70 units exactly → single UCS-2 segment
    assert segment_count("😀" * 36) == 2  # 72 units → two


# --- the contagion rule (the one to prove explicitly) ---


def test_one_non_gsm7_char_forces_whole_message_to_ucs2() -> None:
    body = "a" * 152 + "😀"  # 152 GSM-7 chars + one emoji
    assert not gsm7_encodable(body)  # the whole message is UCS-2 now
    # 152 + 2 (astral) = 154 UCS-2 units → ceil(154 / 67) = 3 segments (the research example)
    assert segment_count(body) == 3
    # without the emoji it would have been a single GSM-7 segment:
    assert segment_count("a" * 152) == 1


# --- render: plain-prefix tier + the cap ---


def test_render_short_reply_is_plain_name_prefix() -> None:
    assert render_outbound(_tag("Astrid"), "Hi there") == "Astrid: Hi there"


def test_render_within_cap_is_not_truncated() -> None:
    body = "word " * 50  # ~250 GSM-7 chars → 2 segments, under a 3-cap
    out = render_outbound(_tag("Bo"), body.strip(), max_segments=3)
    assert out == f"Bo: {body.strip()}"
    assert segment_count(out) <= 3


def test_render_over_cap_truncates_with_ascii_continuation_within_cap() -> None:
    body = "word " * 500  # way over any small cap
    out = render_outbound(_tag("Cy"), body, max_segments=2)
    assert out.endswith("...")  # ASCII continuation, not the … glyph
    assert gsm7_encodable(out)  # the marker did NOT flip it to UCS-2
    assert segment_count(out) <= 2  # bounded — no runaway segmentation
    assert out.startswith("Cy: ")


def test_render_truncation_marker_stays_gsm7_for_a_gsm7_body() -> None:
    out = render_outbound(_tag("Dot"), "x" * 5000, max_segments=1)
    assert out.endswith("...")
    assert segment_count(out) == 1  # capped to a single segment

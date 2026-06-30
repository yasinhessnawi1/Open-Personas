"""WhatsApp outbound rendering (Spec C4 T8) — `*bold*` name tag over the shared splitter.

WhatsApp sits at the bold-prefix render tier (C1-D-6): a ``*Name*`` header (WhatsApp
bold is a single asterisk) on the FIRST split part only, body sent as-is (a stray
markdown char is a cosmetic mis-render, acceptable v1 — the Discord precedent). The
boundary-aware split is the shared ``split_text`` bound to the code-point measure
(WhatsApp counts characters; ``encoding_sensitive=False``), budgeted at Twilio's
1600-char hard cap.
"""

from __future__ import annotations

from persona.schema.origination import PersonaIdentityTag
from persona_connectors.whatsapp.render import WHATSAPP_SPLIT_BUDGET, render_outbound


def _tag(name: str = "Astrid") -> PersonaIdentityTag:
    return PersonaIdentityTag(persona_id="p1", display_name=name, visual_ref=None)


def test_bold_name_header_on_a_single_part() -> None:
    parts = render_outbound(_tag("Astrid"), "Hello there")
    assert parts == ["*Astrid*\nHello there"]


def test_empty_body_is_header_only() -> None:
    assert render_outbound(_tag("Cy"), "") == ["*Cy*"]


def test_long_text_splits_header_on_first_part_only_within_budget() -> None:
    body = " ".join(["word"] * 1000)  # ~5000 chars → multiple 1600-char parts
    parts = render_outbound(_tag("Bo"), body)
    assert len(parts) >= 2
    assert parts[0].startswith("*Bo*\n")
    assert all(not p.startswith("*Bo*") for p in parts[1:])  # header first part only
    assert all(len(p) <= WHATSAPP_SPLIT_BUDGET for p in parts)  # every part within the cap
    # the body is preserved across the parts (modulo the header + whitespace trimming)
    rejoined = (parts[0].removeprefix("*Bo*\n") + " " + " ".join(parts[1:])).split()
    assert rejoined == body.split()


def test_budget_is_twilios_1600_cap() -> None:
    assert WHATSAPP_SPLIT_BUDGET == 1600

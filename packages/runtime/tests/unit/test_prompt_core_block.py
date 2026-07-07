"""The K9 core-memory block in the prompt (Spec K9, T7; K9-D-10/D-11).

Pins acceptance-7's prompt surface: the block is present in every prompt when set, and an
absent (None/empty) block renders NOTHING — byte-identical to the pre-K9 prompt (K9-D-11).
"""

from __future__ import annotations

from persona.schema.persona import Persona, PersonaIdentity
from persona_runtime.prompt import PromptBuilder, RetrievedContext

_HEADER = "What you carry about this person across your time together:"


def _persona() -> Persona:
    return Persona(
        persona_id="astrid",
        identity=PersonaIdentity(
            name="Astrid",
            role="assistant",
            background="Helpful.",
            constraints=["Be kind."],
        ),
    )


def test_core_block_is_rendered_when_present() -> None:
    ctx = RetrievedContext(core_block="Yasin is a builder in Norway; prefers terse answers.")
    system = (
        PromptBuilder()
        .build(_persona(), ctx, history=[], skill_index="", user_message="hi", max_tokens=8000)[0]
        .content
    )
    assert _HEADER in system
    assert "Yasin is a builder in Norway" in system


def test_absent_core_block_is_byte_identical() -> None:
    b = PromptBuilder()
    with_none = b.build(
        _persona(),
        RetrievedContext(core_block=None),
        history=[],
        skill_index="",
        user_message="hi",
        max_tokens=8000,
    )[0].content
    with_empty = b.build(
        _persona(),
        RetrievedContext(core_block=""),
        history=[],
        skill_index="",
        user_message="hi",
        max_tokens=8000,
    )[0].content
    baseline = b.build(
        _persona(),
        RetrievedContext(),
        history=[],
        skill_index="",
        user_message="hi",
        max_tokens=8000,
    )[0].content
    assert with_none == baseline  # None ⇒ nothing rendered
    assert with_empty == baseline  # empty ⇒ nothing rendered (K9-D-11)
    assert _HEADER not in baseline

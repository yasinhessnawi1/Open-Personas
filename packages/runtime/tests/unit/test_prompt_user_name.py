"""Unit tests for the K6 name-in-prompt seam (Spec K6, K6-D-6).

The persona addresses the user by their real name when set, and — the regression
gate — a nameless turn (``user_name`` ``None``/empty, or any caller that omits it)
is BYTE-IDENTICAL to the pre-K6 prompt (null-safe, no behaviour change).
"""

from __future__ import annotations

import pytest
from persona.schema.persona import Persona, PersonaIdentity
from persona_runtime.prompt import PromptBuilder, PromptMode, RetrievedContext


def _persona() -> Persona:
    return Persona(
        persona_id="astrid",
        identity=PersonaIdentity(
            name="Astrid",
            role="Norwegian tenancy law assistant",
            background="Knows husleieloven.",
            constraints=["Never give binding advice."],
        ),
        tools=[],
    )


@pytest.fixture
def builder() -> PromptBuilder:
    return PromptBuilder()


def _system_text(builder: PromptBuilder, **kw: object) -> str:
    msgs = builder.build(
        _persona(),
        RetrievedContext(),
        history=[],
        skill_index="",
        user_message="hi",
        max_tokens=8000,
        **kw,  # type: ignore[arg-type]
    )
    assert msgs[0].role == "system"
    content = msgs[0].content
    assert isinstance(content, str)
    return content


def test_name_is_spoken_when_set(builder: PromptBuilder) -> None:
    sys = _system_text(builder, user_name="Ada Lovelace")
    assert "You are speaking with Ada Lovelace." in sys


def test_name_line_sits_just_after_identity_and_above_constraints(
    builder: PromptBuilder,
) -> None:
    sys = _system_text(builder, user_name="Ada Lovelace")
    assert sys.index("You are Astrid") < sys.index("You are speaking with Ada Lovelace")
    assert sys.index("You are speaking with Ada Lovelace") < sys.index("You must NOT")


def test_none_name_is_byte_identical_to_omitting_it(builder: PromptBuilder) -> None:
    # The regression gate: the un-wired / nameless path must not change the prompt.
    omitted = _system_text(builder)
    explicit_none = _system_text(builder, user_name=None)
    assert omitted == explicit_none
    assert "speaking with" not in omitted


def test_empty_name_renders_nothing_and_is_byte_identical(builder: PromptBuilder) -> None:
    baseline = _system_text(builder)
    empty = _system_text(builder, user_name="")
    assert empty == baseline
    assert "speaking with" not in empty


def test_name_is_spoken_on_the_voice_channel_too(builder: PromptBuilder) -> None:
    # All channels, one integration — the name line is not mode-gated (K6-D-6).
    sys = _system_text(builder, user_name="Ada", mode=PromptMode.VOICE)
    assert "You are speaking with Ada." in sys

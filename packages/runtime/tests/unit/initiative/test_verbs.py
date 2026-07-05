"""Unit tests — the initiative-verb family (Spec A5, T10; the one seam's floors).

Deterministic: the cue net admits-only, the confirm/decline floors are
precision-biased whole-message matches, the pending resolution consults the
LEDGER id only (no pending ⇒ a stray "yes" moves nothing), and the dial
interpreter is conservative (garbage/error/null ⇒ the dial never moves).
"""

from __future__ import annotations

from typing import Any

import pytest
from persona.backends.types import ChatResponse, TokenUsage
from persona_runtime.initiative.verbs import (
    InitiativeVerb,
    ModelInitiativeVerbInterpreter,
    detect_initiative_cue,
    is_clean_decline,
    resolve_verb_for_pending,
)


class TestCueNet:
    @pytest.mark.parametrize(
        "message",
        [
            "stop suggesting things",
            "please ask me first before doing stuff",
            "you can act on your own for small things",
            "slutt å foreslå ting",
        ],
    )
    def test_admits_initiative_talk(self, message: str) -> None:
        assert detect_initiative_cue(message) is True

    def test_ordinary_chat_not_admitted(self) -> None:
        assert detect_initiative_cue("what's the weather in Bergen tomorrow?") is False


class TestDeclineFloor:
    @pytest.mark.parametrize("reply", ["no", "No thanks.", "leave it", "nei takk", "لا"])
    def test_clean_declines(self, reply: str) -> None:
        assert is_clean_decline(reply) is True

    @pytest.mark.parametrize(
        "reply",
        ["no but move it to 9", "not sure", "no way that's great news", "yes"],
    )
    def test_carrying_instructions_or_other_content_is_not_a_decline(self, reply: str) -> None:
        assert is_clean_decline(reply) is False


class TestPendingResolution:
    def test_clean_yes_confirms_the_ledger_pending(self) -> None:
        assert resolve_verb_for_pending("yes", "n-1") == (
            InitiativeVerb.CONFIRM_PROPOSAL,
            "n-1",
        )

    def test_clean_no_declines(self) -> None:
        assert resolve_verb_for_pending("no thanks", "n-1") == (
            InitiativeVerb.DECLINE_PROPOSAL,
            "n-1",
        )

    def test_no_pending_means_a_stray_yes_moves_nothing(self) -> None:
        assert resolve_verb_for_pending("yes", None) is None

    def test_ambiguity_falls_through(self) -> None:
        assert resolve_verb_for_pending("yes but only if it's cheap", "n-1") is None


class _StubBackend:
    def __init__(self, content: str = "", *, raises: bool = False) -> None:
        self._content = content
        self._raises = raises

    async def chat(self, messages: Any, **kwargs: Any) -> ChatResponse:  # noqa: ANN401, ARG002
        if self._raises:
            msg = "provider down"
            raise RuntimeError(msg)
        return ChatResponse(
            content=self._content,
            model="scripted",
            provider="anthropic",
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            latency_ms=1.0,
        )


class TestDialInterpreter:
    @pytest.mark.asyncio
    async def test_clear_instruction_yields_the_verb(self) -> None:
        interpreter = ModelInitiativeVerbInterpreter(_StubBackend('{"verb": "dial_off"}'))
        assert await interpreter.interpret("stop suggesting things") is InitiativeVerb.DIAL_OFF

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "content",
        ['{"verb": null}', '{"verb": "confirm_proposal"}', "I think they mean off?", ""],
    )
    async def test_null_unknown_or_garbage_moves_nothing(self, content: str) -> None:
        interpreter = ModelInitiativeVerbInterpreter(_StubBackend(content))
        assert await interpreter.interpret("hmm suggestions") is None

    @pytest.mark.asyncio
    async def test_backend_error_moves_nothing(self) -> None:
        interpreter = ModelInitiativeVerbInterpreter(_StubBackend(raises=True))
        assert await interpreter.interpret("stop suggesting") is None

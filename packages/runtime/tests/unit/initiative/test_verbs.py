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

#: Every unambiguous turn-off the interpreter handles without the model (also pinned to
#: fire the cue, so none of them can be lost before the interpreter sees it).
_TURN_OFF_PHRASES = [
    "stop suggesting things",
    "Stop suggesting things.",
    "  stop suggesting things  ",
    "please stop suggesting stuff",
    "stop suggesting",
    "stop bringing things up",
    "stop making suggestions please",
    "turn off suggestions",
    "turn off the initiative",
    "turn suggestions off",
    "no more suggestions!",
    "disable initiative",
    "stop the suggestions",
    "slutt å foreslå ting",
    "ingen flere forslag",
]


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

    @pytest.mark.parametrize(
        "message",
        [
            "what would you suggest for dinner?",
            "Which would you suggest, the blue or the green?",
            "can you suggest a restaurant near Grunerlokka?",
            "Could you suggest some books on Norse history?",
            "any suggestions for the weekend?",
            "suggest a gift for my mum",
            "do you have any suggestions for a birthday card?",
            "what do you suggest for lunch?",
            "do you have suggestions for a gift?",
        ],
    )
    def test_a_plain_request_for_suggestions_is_not_a_dial_cue(self, message: str) -> None:
        """Re-review MEDIUM-A: asking FOR a suggestion must not wait on the dial model."""
        assert detect_initiative_cue(message) is False

    @pytest.mark.parametrize(
        "message",
        [
            "what would you suggest for dinner?",
            "Can you suggest a restaurant?",
            "any suggestions?",
            "suggest me a book",
        ],
    )
    def test_the_reviews_plain_requests_stay_uncued(self, message: str) -> None:
        assert detect_initiative_cue(message) is False

    def test_every_dial_and_turn_off_phrase_still_fires(self) -> None:
        phrases = [
            *_TURN_OFF_PHRASES,
            "stop suggesting things",
            "please ask me first before doing stuff",
            "you can act on your own for small things",
            "slutt å foreslå ting",
            "hmm suggestions",
            "maybe fewer suggestions?",
            "stop suggesting that restaurant",
            "don't stop suggesting things",
            "no more suggestions about dinner, but keep the rest",
            "ask me first",
            "can you stop suggesting things",
            "could you suggest less often?",
            "could you suggest fewer things?",
            # Third review MEDIUM-1: request-shaped dial commands (recall over precision).
            "Can you suggest things without me asking?",
            "Could you suggest things more?",
            "Any suggestions should only come when I ask.",
            "can you suggest things only when I ask",
            "Suggest something only when I ask",
            "could you suggest things unless I ask",
            "can you suggest things until I asked otherwise?",
            "could you suggest less often?",
            "any suggestions more often would be welcome",
            "can you suggest things only on Sundays",
            "how often can you suggest things?",
        ]
        assert [p for p in phrases if not detect_initiative_cue(p)] == []


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
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages: Any, **kwargs: Any) -> ChatResponse:  # noqa: ANN401, ARG002
        self.calls.append(kwargs)
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
        assert await interpreter.interpret("maybe fewer suggestions?") is None


class _SlowBackend(_StubBackend):
    """A reasoning head that thinks for longer than the turn may wait; it records whether
    the wait was cancelled (so the test asserts the cancellation, not the clock)."""

    def __init__(self, content: str = "") -> None:
        super().__init__(content)
        self.cancelled = False
        self.completed = False

    async def chat(self, messages: Any, **kwargs: Any) -> ChatResponse:  # noqa: ANN401
        import asyncio

        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        self.completed = True
        return await super().chat(messages, **kwargs)


class TestTheDialVerbWaitsBoundedly:
    """Re-review MEDIUM-A: the interpret call sits in front of the persona's answer, so
    it has a timeout (a setting, default 4 seconds); a timeout reads as no verb, logged.
    The clear turn-off is deterministic, so it never waits."""

    def test_the_timeout_is_a_setting(self) -> None:
        from persona.initiative import InitiativeSettings

        assert InitiativeSettings().verb_timeout_seconds == 8.0  # max seen 5.8 s

    @pytest.mark.asyncio
    async def test_a_slow_model_times_out_to_no_verb_and_is_logged(self) -> None:
        """Third review LOW-4: the returned value and the cancelled call, not wall time."""
        from loguru import logger

        seen: list[str] = []
        sink = logger.add(lambda m: seen.append(str(m.record["message"])), level="WARNING")
        slow = _SlowBackend('{"verb": "dial_off"}')
        try:
            interpreter = ModelInitiativeVerbInterpreter(slow, timeout_seconds=0.05)
            assert await interpreter.interpret("maybe fewer suggestions?") is None
        finally:
            logger.remove(sink)
        assert slow.cancelled is True
        assert slow.completed is False
        assert any("timed out" in m for m in seen)

    @pytest.mark.asyncio
    async def test_a_clear_turn_off_never_waits(self) -> None:
        slow = _SlowBackend("")
        interpreter = ModelInitiativeVerbInterpreter(slow, timeout_seconds=0.05)
        assert await interpreter.interpret("stop suggesting things") is InitiativeVerb.DIAL_OFF
        assert (slow.cancelled, slow.completed) == (False, False)  # the model was never asked


class TestTheDialVerbsOnTheMidTier:
    """Review MEDIUM-2: on the mid tier the head is a reasoning model, which spent the old
    60-token cap reasoning, so every verb parsed to None and "stop suggesting things" did
    nothing. The cap is a setting (default 1024), and an unambiguous turn-off moves the
    dial without the model at all."""

    @pytest.mark.asyncio
    async def test_the_cap_is_the_setting_default_or_the_injected_value(self) -> None:
        from persona.initiative import InitiativeSettings

        assert InitiativeSettings().verb_max_output_tokens == 1024
        default = _StubBackend('{"verb": null}')
        await ModelInitiativeVerbInterpreter(default).interpret("hmm suggestions")
        assert default.calls[0]["max_tokens"] == 1024
        injected = _StubBackend('{"verb": null}')
        await ModelInitiativeVerbInterpreter(injected, max_output_tokens=300).interpret(
            "hmm suggestions"
        )
        assert injected.calls[0]["max_tokens"] == 300

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "message",
        [
            *_TURN_OFF_PHRASES,
        ],
    )
    async def test_an_unambiguous_turn_off_needs_no_model(self, message: str) -> None:
        silent = _StubBackend("")  # a reasoning head that ran out of tokens: no content
        assert await ModelInitiativeVerbInterpreter(silent).interpret(message) is (
            InitiativeVerb.DIAL_OFF
        )
        assert silent.calls == []
        failing = _StubBackend(raises=True)
        assert await ModelInitiativeVerbInterpreter(failing).interpret(message) is (
            InitiativeVerb.DIAL_OFF
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "message",
        [
            "stop suggesting that restaurant",
            "don't stop suggesting things",
            "that was a good suggestion",
            "can you turn off the lights",
            "no more suggestions about dinner, but keep the rest",
            "ask me first",
        ],
    )
    async def test_anything_else_goes_to_the_model(self, message: str) -> None:
        backend = _StubBackend("")  # empty content parses to nothing: the dial stays
        assert await ModelInitiativeVerbInterpreter(backend).interpret(message) is None
        assert len(backend.calls) == 1

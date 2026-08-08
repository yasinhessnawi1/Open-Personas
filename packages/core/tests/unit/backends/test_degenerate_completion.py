"""Garbage must never reach the user in the persona's voice (R9-090).

A persona asked for a task's status answered with roughly 4000 tokens of salad,
served by the free-tier model sitting last in both free chains, so it served
exactly when the first two rate-limited. ``completion_tokens`` was the 4096 output
cap: it ran until truncation. That is worse than an error, because an error says
the product is busy while this says the persona is broken, and it bills a full-cap
generation to say it.

Two paths, two different honest answers, both pinned here:

- **Non-streaming** can fail over, because nothing has been shown yet. The next
  model in the chain answers instead. It does NOT retry the same model, since a
  repetition collapse is a property of the model and not of the moment.
- **Streaming** cannot: yielded text cannot be recalled. The honest intervention
  is to stop digging, so the stream is cut where the collapse becomes certain.

The false-positive tests carry the real weight. A guard that eats good replies is
worse than the defect, so the threshold is checked against the shapes most likely
to look repetitive: long lists, code, and the healthy-prose ratios measured from
production.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from persona.backends.degenerate import DEGENERATE_MIN_WORDS, is_degenerate_repetition
from persona.backends.errors import AllModelsFailedError
from persona.backends.multi_model import MultiModelChatBackend
from persona.backends.types import ChatResponse, StreamChunk, TokenUsage

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from persona.schema.conversation import ConversationMessage

#: The collapse: one phrase cycling to the output cap.
_COLLAPSED = "We need to check the the the " * 400
#: Healthy prose measured from production sits at 0.70 to 0.81 distinct/total.
_HEALTHY = " ".join(f"word{i % 700}" for i in range(1200))


def _response(content: str, *, model: str = "m1") -> ChatResponse:
    return ChatResponse(
        content=content,
        tool_calls=[],
        usage=TokenUsage(prompt_tokens=10, completion_tokens=4096, total_tokens=4106),
        model=model,
        provider="openrouter",
        latency_ms=1.0,
    )


class _Backend:
    """A backend that returns a scripted body, and counts how often it was asked."""

    def __init__(self, body: str, *, name: str) -> None:
        self._body = body
        self.model_name = name
        self.provider_name = "openrouter"
        self.calls = 0
        self.supports_native_tools = False
        self.supports_vision = False

    async def chat(self, _messages: list[ConversationMessage], **_kw: object) -> ChatResponse:
        self.calls += 1
        return _response(self._body, model=self.model_name)

    async def chat_stream(
        self, _messages: list[ConversationMessage], **_kw: object
    ) -> AsyncIterator[StreamChunk]:
        self.calls += 1
        for word in self._body.split(" "):
            yield StreamChunk(delta=word + " ", is_final=False)
        yield StreamChunk(delta="", is_final=True)


# ----- the detector itself -------------------------------------------------


def test_a_collapsed_completion_is_detected() -> None:
    assert is_degenerate_repetition(_COLLAPSED) is True


def test_healthy_prose_is_not() -> None:
    assert is_degenerate_repetition(_HEALTHY) is False


@pytest.mark.parametrize(
    ("label", "text"),
    [
        (
            "a long numbered list",
            "\n".join(f"{i}. Item number {i} about topic {i}" for i in range(200)),
        ),
        (
            "source code",
            "\n".join(f"    self.field_{i} = compute(value_{i}, index={i})" for i in range(300)),
        ),
        ("a short repetitive reply", "yes yes yes yes absolutely yes"),
        ("empty", ""),
    ],
    ids=["numbered-list", "source-code", "short-repetitive", "empty"],
)
def test_legitimate_text_is_never_flagged(label: str, text: str) -> None:
    """The load-bearing guard: eating a good reply is worse than the defect.

    Lists and code are the shapes most likely to look repetitive to a naive
    measure, and a short answer is ALLOWED to repeat itself.
    """
    assert is_degenerate_repetition(text) is False, f"{label} was wrongly flagged"


def test_short_text_is_exempt_however_repetitive() -> None:
    """Below the floor the ratio means nothing, so it must not be consulted."""
    just_under = " ".join(["same"] * (DEGENERATE_MIN_WORDS - 1))
    assert is_degenerate_repetition(just_under) is False


# ----- the non-streaming path: fail over -----------------------------------


@pytest.mark.asyncio
async def test_a_collapsed_completion_falls_over_to_the_next_model() -> None:
    """THE regression: the user gets the second model's answer, not the salad."""
    bad, good = (
        _Backend(_COLLAPSED, name="nemotron-bad"),
        _Backend("Here is the status.", name="ok"),
    )
    wrapper = MultiModelChatBackend([bad, good])  # type: ignore[list-item]

    response = await wrapper.chat([])

    assert response.content == "Here is the status."
    assert good.calls == 1


@pytest.mark.asyncio
async def test_the_collapsed_model_is_not_retried() -> None:
    """A collapse is a property of the MODEL, not of the moment.

    Retrying would pay a second full-cap generation to receive the same garbage,
    which is exactly the cost this guard exists to stop. Contrast an EMPTY
    completion, which IS a flake and does retry the same backend once.
    """
    bad, good = _Backend(_COLLAPSED, name="nemotron-bad"), _Backend("Fine.", name="ok")
    await MultiModelChatBackend([bad, good]).chat([])  # type: ignore[list-item]

    assert bad.calls == 1, "the collapsed model was asked again"


@pytest.mark.asyncio
async def test_the_failure_names_the_model_without_quoting_the_garbage() -> None:
    """Diagnosis needs to know WHICH model collapsed, and the user needs no salad.

    With no healthy model left the wrapper exhausts, as it does for any other
    provider failure. What matters is that the attempt record carries the class
    and the model, so the next operator pass can see which model to drop, and
    that none of the collapsed text rides along into the error.
    """
    bad = _Backend(_COLLAPSED, name="nemotron-bad")
    with pytest.raises(AllModelsFailedError) as caught:
        await MultiModelChatBackend([bad]).chat([])  # type: ignore[list-item]

    attempts = str(caught.value.context["attempts_json"])
    assert "DegenerateCompletionError" in attempts
    assert "nemotron-bad" in attempts
    assert "the the the" not in str(caught.value)


@pytest.mark.asyncio
async def test_the_fallback_log_says_how_badly_it_collapsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operator pass that drops a bad model reads this line to find it.

    loguru binds stderr at import, so the module logger is monkeypatched rather
    than captured; that is what the fallback path actually calls.
    """
    from persona.backends import multi_model

    lines: list[str] = []

    class _Recorder:
        def warning(self, template: str, **kw: object) -> None:
            lines.append(template + " " + " ".join(f"{k}={v}" for k, v in kw.items()))

        def __getattr__(self, _name: str) -> object:
            return lambda *_a, **_kw: None

    monkeypatch.setattr(multi_model, "_LOG", _Recorder())
    bad, good = _Backend(_COLLAPSED, name="nemotron-bad"), _Backend("Fine.", name="ok")
    await MultiModelChatBackend([bad, good]).chat([])  # type: ignore[list-item]

    joined = " ".join(lines)
    assert "nemotron-bad" in joined
    assert "DegenerateCompletionError" in joined


# ----- the streaming path: cut the stream ----------------------------------


@pytest.mark.asyncio
async def test_a_collapsing_stream_is_cut_short() -> None:
    """Streamed text cannot be recalled, so the honest answer is to stop.

    The user still sees a wrong reply -- that cannot be undone once it is on
    screen -- but a short one instead of thousands of tokens, and we stop paying
    for the rest of the generation.
    """
    bad = _Backend(_COLLAPSED, name="nemotron-bad")
    delivered = [c.delta async for c in MultiModelChatBackend([bad]).chat_stream([])]  # type: ignore[list-item]

    emitted_words = len("".join(delivered).split())
    assert emitted_words < len(_COLLAPSED.split()), "the stream ran to the end"
    assert emitted_words >= DEGENERATE_MIN_WORDS, "cut before the measure was meaningful"


@pytest.mark.asyncio
async def test_a_healthy_stream_is_delivered_whole() -> None:
    """The guard must be invisible to every normal reply."""
    good = _Backend(_HEALTHY, name="ok")
    delivered = [c.delta async for c in MultiModelChatBackend([good]).chat_stream([])]  # type: ignore[list-item]

    assert "".join(delivered).split() == _HEALTHY.split()

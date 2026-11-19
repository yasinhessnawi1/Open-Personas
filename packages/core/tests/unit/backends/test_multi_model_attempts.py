"""Each call carries its own attempt history, same-backend retries included (R9-229).

The shared ledger listed one record per backend that FELL THROUGH and nothing for a
retry on the same backend, so a primary that returned a 429 and then answered looked
like a clean first answer to the latency tracker. ``ServedCall.attempts`` lists every
try of the call in order, the serving one last, with the timing and any usage a failed
try reported; ``fell_through()`` is the old ledger's per-backend shape, for the TurnLog.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from persona.backends.errors import RateLimitError
from persona.backends.multi_model import MultiModelChatBackend
from persona.backends.types import (
    BackendAttempt,
    ChatResponse,
    ServedCall,
    StreamChunk,
    TokenUsage,
)
from persona.schema.content import ImageContent, TextContent
from persona.schema.conversation import ConversationMessage

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

#: A sentinel the error text carries; built at run time so no credential-shaped literal
#: sits in the source. It must never reach an attempt record.
_SECRET = "-".join(("r9", "229", "sentinel", "never", "log", "me"))
_OK = TokenUsage(prompt_tokens=3, completion_tokens=2, total_tokens=5)
_WASTED = TokenUsage(prompt_tokens=900, completion_tokens=0, total_tokens=900)


def _transient() -> RateLimitError:
    """A short rate limit: retried once on the same backend, then falls through."""
    return RateLimitError(
        f"429 for key {_SECRET}", context={"status_code": "429", "retry_after_s": "0.01"}
    )


class _Member:
    """Plays a script of outcomes: an exception, ``"empty"`` (a usage-only reply), or ``"ok"``."""

    supports_native_tools = False

    def __init__(
        self, provider: str, model: str, script: list[object], *, vision: bool = False
    ) -> None:
        self.provider_name = provider
        self.model_name = model
        self.supports_vision = vision
        self.script = list(script)

    def _next(self) -> object:
        outcome = self.script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def chat(self, messages: Any, **_: Any) -> ChatResponse:  # noqa: ANN401, ARG002
        outcome = self._next()
        return ChatResponse(
            content="" if outcome == "empty" else "ok",
            usage=_WASTED if outcome == "empty" else _OK,
            model=self.model_name,
            provider=self.provider_name,
            latency_ms=1.0,
        )

    async def chat_stream(self, messages: Any, **_: Any) -> AsyncIterator[StreamChunk]:  # noqa: ANN401, ARG002
        outcome = self._next()
        if outcome == "empty":
            yield StreamChunk(delta="", is_final=True, usage=_WASTED)
            return
        yield StreamChunk(delta="ok")
        yield StreamChunk(delta="", is_final=True, usage=_OK)


def _text() -> list[ConversationMessage]:
    return [ConversationMessage(role="user", content="hi", created_at=datetime.now(UTC))]


def _image() -> list[ConversationMessage]:
    return [
        ConversationMessage(
            role="user",
            content=[
                TextContent(text="what is this?"),
                ImageContent(workspace_path="uploads/a.png", media_type="image/png"),
            ],
            created_at=datetime.now(UTC),
        )
    ]


async def _served(chain: MultiModelChatBackend, *, stream: bool, image: bool = False) -> ServedCall:
    messages = _image() if image else _text()
    if stream:
        chunks = [c async for c in chain.chat_stream(messages)]
        served = chunks[-1].served
    else:
        served = (await chain.chat(messages)).served
    assert served is not None
    return served


def _shape(attempts: tuple[BackendAttempt, ...]) -> list[tuple[int, str, str | None, bool]]:
    return [(a.index, a.model, a.error_class, a.retry) for a in attempts]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.asyncio
async def test_a_clean_primary_answer_is_one_attempt(*, stream: bool) -> None:
    chain = MultiModelChatBackend([_Member("deepseek", "ds", ["ok"]), _Member("openai", "g", [])])
    served = await _served(chain, stream=stream)
    assert _shape(served.attempts) == [(0, "ds", None, False)]
    assert served.first_attempt is True
    assert served.fell_through() == ()


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.asyncio
async def test_a_same_backend_retry_is_in_the_history(*, stream: bool) -> None:
    """R9-229: the retry that answered is not a clean first answer."""
    chain = MultiModelChatBackend(
        [_Member("deepseek", "ds", [_transient(), "ok"]), _Member("openai", "g", [])]
    )
    served = await _served(chain, stream=stream)
    assert _shape(served.attempts) == [(0, "ds", "RateLimitError", False), (0, "ds", None, True)]
    assert served.first_attempt is False
    # A retry on the backend that answered is not a fallback.
    assert served.fell_through() == ()


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.asyncio
async def test_a_fallback_after_a_retry_projects_one_record_per_backend(*, stream: bool) -> None:
    chain = MultiModelChatBackend(
        [
            _Member("deepseek", "ds", [_transient(), _transient()]),
            _Member("openai", "g", ["ok"]),
        ]
    )
    served = await _served(chain, stream=stream)
    assert _shape(served.attempts) == [
        (0, "ds", "RateLimitError", False),
        (0, "ds", "RateLimitError", True),
        (1, "g", None, False),
    ]
    # The ledger's shape: one record per backend that fell through, retried flag set.
    assert _shape(served.fell_through()) == [(0, "ds", "RateLimitError", True)]
    assert [a.status_code for a in served.fell_through()] == [429]
    assert served.first_attempt is False


@pytest.mark.asyncio
async def test_three_fallbacks_project_in_chain_order() -> None:
    no_retry = RateLimitError("q", context={"status_code": "429", "reason": "insufficient_quota"})
    chain = MultiModelChatBackend(
        [
            _Member("a", "m0", [no_retry]),
            _Member("b", "m1", [no_retry]),
            _Member("c", "m2", [no_retry]),
            _Member("d", "m3", ["ok"]),
        ]
    )
    served = await _served(chain, stream=True)
    assert [(a.index, a.provider) for a in served.fell_through()] == [(0, "a"), (1, "b"), (2, "c")]
    assert (served.provider, served.model) == ("d", "m3")


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.asyncio
async def test_a_failed_attempt_keeps_the_usage_it_reported(*, stream: bool) -> None:
    """D1: an empty completion still cost tokens; the history carries them, uncharged."""
    chain = MultiModelChatBackend(
        [_Member("deepseek", "ds", ["empty", "empty"]), _Member("openai", "g", ["ok"])]
    )
    served = await _served(chain, stream=stream)
    assert [a.usage for a in served.attempts] == [_WASTED, _WASTED, None]


@pytest.mark.asyncio
async def test_the_index_is_the_chain_position_on_an_image_request() -> None:
    """R9-227: the skipped non-vision primary keeps index 0; the vision model is 1."""
    chain = MultiModelChatBackend(
        [_Member("deepseek", "ds", []), _Member("openai", "g", ["ok"], vision=True)]
    )
    served = await _served(chain, stream=True, image=True)
    assert _shape(served.attempts) == [(1, "g", None, False)]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.asyncio
async def test_the_history_carries_class_names_never_error_text(*, stream: bool) -> None:
    """D-20-9 privacy: an error message (here holding a key) never reaches the history."""
    chain = MultiModelChatBackend(
        [_Member("deepseek", "ds", [_transient(), _transient()]), _Member("openai", "g", ["ok"])]
    )
    served = await _served(chain, stream=stream)
    assert _SECRET not in served.model_dump_json()
    assert "429 for key" not in served.model_dump_json()


@pytest.mark.asyncio
async def test_every_attempt_is_timed() -> None:
    chain = MultiModelChatBackend(
        [_Member("deepseek", "ds", [_transient(), "ok"]), _Member("openai", "g", [])]
    )
    served = await _served(chain, stream=True)
    assert all(a.elapsed_ms >= 0.0 for a in served.attempts)


def test_a_hand_built_served_call_with_no_history_counts_as_a_first_attempt() -> None:
    assert ServedCall(provider="openai", model="g").first_attempt is True


def _try(usage: TokenUsage | None, *, error: str | None = "EmptyCompletionError") -> BackendAttempt:
    return BackendAttempt(index=0, provider="vendor", model="m", error_class=error, usage=usage)


def test_charged_failed_tries_are_the_failed_tries_that_reported_tokens() -> None:
    """D-ORCH-11-18-s: only a failed try that reported tokens (or an actual) is charged.

    A provider that sends no usage gets a ``TokenUsage(0, 0, 0)`` from the backend, so a
    try with usage present but zero tokens and no actual reported nothing.
    """
    zero = _try(TokenUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0))
    none = _try(None)
    prompt_only = _try(TokenUsage(prompt_tokens=5, completion_tokens=0, total_tokens=5))
    completion_only = _try(TokenUsage(prompt_tokens=0, completion_tokens=3, total_tokens=3))
    actual_only = _try(
        TokenUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0, cost_usd=0.001)
    )
    served_try = _try(TokenUsage(prompt_tokens=9, completion_tokens=9, total_tokens=18), error=None)
    served = ServedCall(
        provider="vendor",
        model="m",
        attempts=(zero, none, prompt_only, completion_only, actual_only, served_try),
    )
    assert served.charged_failed_tries() == (prompt_only, completion_only, actual_only)
    assert [a.reported_tokens for a in (zero, none, prompt_only, actual_only)] == [
        False,
        False,
        True,
        True,
    ]

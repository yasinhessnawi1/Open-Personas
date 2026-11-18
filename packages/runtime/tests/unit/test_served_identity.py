"""Unit tests for :func:`persona_runtime.routing.served_identity` (R9-223).

The one resolution the text loop's TurnLog attribution (Spec M2, D-M2-2), the voice
meter and turn log, the agentic step meter and the background usage collector share:
the served ``(provider, model)`` of ONE call, read from that call's own
``ServedCall`` (``ChatResponse.served`` / ``StreamChunk.served``), never from state on
the chain every conversation on the tier shares. Driven through a REAL
:class:`MultiModelChatBackend` whose primary genuinely raises inside ``chat_stream``.

Also :func:`persona_runtime.routing.first_token_sample_model` (R9-226, R9-229): which
model a first-token latency sample belongs to, and when there is none.
"""

# Test doubles keep the loose signatures of the protocols they stand in for.
# ruff: noqa: ANN401, ARG002
from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from _fakes import ScriptedBackend, ScriptedRound  # type: ignore[import-not-found]
from persona.backends.errors import RateLimitError
from persona.backends.multi_model import MultiModelChatBackend
from persona.backends.types import StreamChunk, TokenUsage
from persona.schema.conversation import ConversationMessage
from persona_runtime.routing import first_token_sample_model, served_identity

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from persona.backends.types import ServedCall


class _RateLimitedBackend:
    """A backend whose stream raises a 429 ``fail`` times before answering."""

    supports_native_tools = False
    supports_vision = False

    def __init__(self, *, provider: str, model: str, fail: int = 99, retry: bool = False) -> None:
        self.provider_name = provider
        self.model_name = model
        self.fail = fail
        self.retry = retry
        self.stream_calls = 0

    async def chat_stream(
        self, messages: list[ConversationMessage], **_kwargs: Any
    ) -> AsyncIterator[StreamChunk]:
        self.stream_calls += 1
        if self.fail > 0:
            self.fail -= 1
            context = {"provider": self.provider_name}
            if self.retry:
                context["retry_after_s"] = "0.01"
            raise RateLimitError("scripted 429", context=context)
        yield StreamChunk(delta="hi")
        yield StreamChunk(
            delta="",
            is_final=True,
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )


async def _served(backend: MultiModelChatBackend) -> ServedCall:
    message = ConversationMessage(role="user", content="hi", created_at=datetime.now(UTC))
    chunks = [chunk async for chunk in backend.chat_stream([message])]
    served = chunks[-1].served
    assert served is not None
    return served


def test_a_bare_backend_is_its_own_served_model_and_owns_its_sample() -> None:
    backend = ScriptedBackend(
        [ScriptedRound(text="x")], provider_name="nvidia", model_name="nvidia/llama"
    )
    assert served_identity(backend, None) == ("nvidia", "nvidia/llama")
    assert first_token_sample_model(backend, None) == "nvidia/llama"


@pytest.mark.asyncio
async def test_a_chain_whose_primary_answers_names_the_primary() -> None:
    primary = ScriptedBackend(
        [ScriptedRound(text="ok")], provider_name="nvidia", model_name="nvidia/llama"
    )
    fallback = ScriptedBackend([], provider_name="anthropic", model_name="claude-sonnet-4-6")
    chain = MultiModelChatBackend([primary, fallback], max_retries_per_backend=0)
    served = await _served(chain)
    assert served_identity(chain, served) == ("nvidia", "nvidia/llama")
    assert first_token_sample_model(chain, served) == "nvidia/llama"


@pytest.mark.asyncio
async def test_a_chain_whose_primary_fails_names_the_fallback_and_gives_no_sample() -> None:
    primary = _RateLimitedBackend(provider="nvidia", model="nvidia/llama")
    fallback = ScriptedBackend(
        [ScriptedRound(text="ok")], provider_name="anthropic", model_name="claude-sonnet-4-6"
    )
    chain = MultiModelChatBackend([primary, fallback], max_retries_per_backend=0)  # type: ignore[list-item]
    served = await _served(chain)
    # The chain still names its primary by contract; the call names who served it.
    assert (chain.provider_name, chain.model_name) == ("nvidia", "nvidia/llama")
    assert served_identity(chain, served) == ("anthropic", "claude-sonnet-4-6")
    assert first_token_sample_model(chain, served) is None


@pytest.mark.asyncio
async def test_a_primary_that_answered_on_a_retry_gives_no_sample() -> None:
    primary = _RateLimitedBackend(provider="nvidia", model="nvidia/llama", fail=1, retry=True)
    fallback = ScriptedBackend([], provider_name="anthropic", model_name="claude-sonnet-4-6")
    chain = MultiModelChatBackend([primary, fallback])  # type: ignore[list-item]
    served = await _served(chain)
    assert primary.stream_calls == 2
    assert served_identity(chain, served) == ("nvidia", "nvidia/llama")
    assert first_token_sample_model(chain, served) is None

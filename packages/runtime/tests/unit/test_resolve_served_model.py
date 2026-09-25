"""Unit tests for :func:`persona_runtime.routing.resolve_served_model` (R9-214).

The one resolution the text loop's TurnLog attribution (Spec M2, D-M2-2) and the
voice turn log share. Driven through a REAL :class:`MultiModelChatBackend` whose
primary genuinely raises inside ``chat_stream``, so the wrapper's own classifier and
attempt ledger do the work; nothing about the ledger is hand-forced except in the
defensive exhaustion case, which the wrapper never reaches without raising.

Also :func:`persona_runtime.routing.first_token_sample_model` (R9-226), its sibling:
which model a first-token latency sample belongs to, and when there is none.
"""

# Test doubles keep the loose signatures of the protocols they stand in for.
# ruff: noqa: ANN401, ARG002
from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from _fakes import ScriptedBackend, ScriptedRound  # type: ignore[import-not-found]
from persona.backends.errors import RateLimitError
from persona.backends.multi_model import AttemptRecord, MultiModelChatBackend
from persona.schema.conversation import ConversationMessage
from persona_runtime.routing import first_token_sample_model, resolve_served_model

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from persona.backends import StreamChunk


class _RateLimitedBackend:
    """A backend whose stream raises a 429 before its first chunk, as a real one does."""

    supports_native_tools = False
    supports_vision = False

    def __init__(self, *, provider: str, model: str) -> None:
        self.provider_name = provider
        self.model_name = model
        self.stream_calls = 0

    async def chat_stream(
        self, messages: list[ConversationMessage], **_kwargs: Any
    ) -> AsyncIterator[StreamChunk]:
        self.stream_calls += 1
        raise RateLimitError("scripted 429", context={"provider": self.provider_name})
        yield  # pragma: no cover - makes this an async generator


async def _drain(backend: MultiModelChatBackend) -> str:
    text = ""
    message = ConversationMessage(role="user", content="hi", created_at=datetime.now(UTC))
    async for chunk in backend.chat_stream([message]):
        text += chunk.delta
    return text


def test_a_bare_backend_is_its_own_served_model() -> None:
    backend = ScriptedBackend([], provider_name="nvidia", model_name="nvidia/llama")

    assert resolve_served_model(backend) == ("nvidia", "nvidia/llama")


@pytest.mark.asyncio
async def test_a_chain_whose_primary_answers_names_the_primary() -> None:
    primary = ScriptedBackend(
        [ScriptedRound(text="hello")], provider_name="nvidia", model_name="nvidia/llama"
    )
    fallback = ScriptedBackend([], provider_name="anthropic", model_name="claude-sonnet-4-6")
    wrapper = MultiModelChatBackend([primary, fallback], max_retries_per_backend=0)

    assert await _drain(wrapper) == "hello"

    assert resolve_served_model(wrapper) == ("nvidia", "nvidia/llama")
    assert fallback.chat_stream_calls == 0


@pytest.mark.asyncio
async def test_a_chain_whose_primary_fails_names_the_fallback_that_answered() -> None:
    primary = _RateLimitedBackend(provider="nvidia", model="nvidia/llama")
    fallback = ScriptedBackend(
        [ScriptedRound(text="hello")], provider_name="anthropic", model_name="claude-sonnet-4-6"
    )
    wrapper = MultiModelChatBackend([primary, fallback], max_retries_per_backend=0)

    assert await _drain(wrapper) == "hello"

    # The primary was genuinely tried, and the wrapper still names it by contract:
    # the helper is what turns that into the model that actually answered.
    assert primary.stream_calls == 1
    assert (wrapper.provider_name, wrapper.model_name) == ("nvidia", "nvidia/llama")
    assert resolve_served_model(wrapper) == ("anthropic", "claude-sonnet-4-6")


def test_a_ledger_as_long_as_the_chain_falls_back_to_the_wrappers_own_identity() -> None:
    class _Child:
        def __init__(self, provider: str, model: str) -> None:
            self.provider_name = provider
            self.model_name = model

    class _ExhaustedWrapper:
        provider_name = "nvidia"
        model_name = "nvidia/llama"
        backends = [_Child("nvidia", "nvidia/llama")]
        last_attempts = [
            AttemptRecord(
                provider="nvidia",
                model="nvidia/llama",
                last_error_class="RateLimitError",
                last_error_status_code=429,
                retried_same_model=False,
            )
        ]

    assert resolve_served_model(_ExhaustedWrapper()) == ("nvidia", "nvidia/llama")  # type: ignore[arg-type]


# ----- first_token_sample_model (R9-226) -------------------------------------


def test_a_bare_backend_owns_its_first_token_sample() -> None:
    backend = ScriptedBackend([], provider_name="nvidia", model_name="nvidia/llama")

    assert first_token_sample_model(backend) == "nvidia/llama"


@pytest.mark.asyncio
async def test_a_chain_whose_primary_answered_first_time_samples_the_primary() -> None:
    primary = ScriptedBackend(
        [ScriptedRound(text="hello")], provider_name="nvidia", model_name="nvidia/llama"
    )
    fallback = ScriptedBackend([], provider_name="anthropic", model_name="claude-sonnet-4-6")
    wrapper = MultiModelChatBackend([primary, fallback], max_retries_per_backend=0)

    assert await _drain(wrapper) == "hello"

    assert first_token_sample_model(wrapper) == "nvidia/llama"


@pytest.mark.asyncio
async def test_a_chain_a_fallback_answered_yields_no_sample_for_either_model() -> None:
    # The time to the fallback's first token includes the primary's failure, and the
    # chain keeps no per-attempt timing to take it out, so neither model gets a sample.
    primary = _RateLimitedBackend(provider="nvidia", model="nvidia/llama")
    fallback = ScriptedBackend(
        [ScriptedRound(text="hello")], provider_name="anthropic", model_name="claude-sonnet-4-6"
    )
    wrapper = MultiModelChatBackend([primary, fallback], max_retries_per_backend=0)

    assert await _drain(wrapper) == "hello"

    assert primary.stream_calls == 1
    assert resolve_served_model(wrapper) == ("anthropic", "claude-sonnet-4-6")
    assert first_token_sample_model(wrapper) is None

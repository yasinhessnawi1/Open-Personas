"""The chain names the model that served each call, on the call itself (R9-223, R9-227).

A tier's :class:`MultiModelChatBackend` is one object that every conversation on the
tier shares. The served identity used to be read back afterwards from a ledger stored on
that object, so a second call could reset it before the first call read it, and on an
image request the ledger's length no longer indexed the chain. The identity now rides
the call: ``ChatResponse.served`` and ``StreamChunk.served`` name the sub-backend that
actually answered, so nothing has to be read back from shared state.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from persona.backends.errors import RateLimitError
from persona.backends.multi_model import MultiModelChatBackend
from persona.backends.types import ChatResponse, ServedCall, StreamChunk, TokenUsage
from persona.schema.content import ImageContent, TextContent
from persona.schema.conversation import ConversationMessage

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_USAGE = TokenUsage(prompt_tokens=3, completion_tokens=2, total_tokens=5)


def _rate_limited() -> RateLimitError:
    # A monetary rate limit falls straight through to the next backend (no retry sleep).
    return RateLimitError("429", context={"status_code": "429", "reason": "insufficient_quota"})


class _Member:
    """A chain member: fails ``fail`` times, then answers; may wait on a gate first."""

    supports_native_tools = False

    def __init__(
        self,
        provider: str,
        model: str,
        *,
        fail: int = 0,
        vision: bool = False,
        gate: asyncio.Event | None = None,
        chunks: list[StreamChunk] | None = None,
    ) -> None:
        self.provider_name = provider
        self.model_name = model
        self.supports_vision = vision
        self.fail = fail
        self.gate = gate
        self.chunks = chunks

    async def _maybe_fail(self) -> None:
        if self.gate is not None:
            await self.gate.wait()
        if self.fail > 0:
            self.fail -= 1
            raise _rate_limited()

    async def chat(self, messages: Any, **_: Any) -> ChatResponse:  # noqa: ANN401, ARG002
        await self._maybe_fail()
        return ChatResponse(
            content="ok",
            usage=_USAGE,
            model=f"echo-{self.model_name}",
            provider=self.provider_name,
            latency_ms=1.0,
        )

    async def chat_stream(self, messages: Any, **_: Any) -> AsyncIterator[StreamChunk]:  # noqa: ANN401, ARG002
        await self._maybe_fail()
        for chunk in self.chunks or [
            StreamChunk(delta="hel"),
            StreamChunk(delta="lo"),
            StreamChunk(delta="", is_final=True, usage=_USAGE),
        ]:
            yield chunk


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


def _identity(served: ServedCall | None) -> tuple[str, str] | None:
    return None if served is None else (served.provider, served.model)


async def _drain(
    chain: MultiModelChatBackend, messages: list[ConversationMessage]
) -> list[StreamChunk]:
    return [chunk async for chunk in chain.chat_stream(messages)]


@pytest.mark.asyncio
async def test_a_primary_answer_names_the_primary_on_the_response() -> None:
    chain = MultiModelChatBackend(
        [_Member("deepseek", "deepseek-chat"), _Member("anthropic", "sonnet")]
    )
    response = await chain.chat(_text())
    assert _identity(response.served) == ("deepseek", "deepseek-chat")


@pytest.mark.asyncio
async def test_a_fallback_answer_names_the_fallback_on_the_response() -> None:
    chain = MultiModelChatBackend(
        [_Member("deepseek", "deepseek-chat", fail=1), _Member("anthropic", "sonnet")]
    )
    response = await chain.chat(_text())
    # The configured identity, not the provider's echo (the price tables key on it).
    assert _identity(response.served) == ("anthropic", "sonnet")
    assert response.model == "echo-sonnet"


@pytest.mark.asyncio
async def test_every_streamed_chunk_names_the_fallback_that_answered() -> None:
    chain = MultiModelChatBackend(
        [_Member("deepseek", "deepseek-chat", fail=1), _Member("anthropic", "sonnet")]
    )
    chunks = await _drain(chain, _text())
    assert len(chunks) == 3
    assert [_identity(c.served) for c in chunks] == [("anthropic", "sonnet")] * 3


@pytest.mark.asyncio
async def test_held_back_chunks_released_at_commit_carry_the_served_model() -> None:
    """A usage chunk sent before the first payload is held, then released: still stamped."""
    usage_first = [
        StreamChunk(delta="", usage=_USAGE),
        StreamChunk(delta="  "),
        StreamChunk(delta="hi", is_final=True),
    ]
    chain = MultiModelChatBackend([_Member("openai", "gpt-4o", chunks=usage_first)])
    chunks = await _drain(chain, _text())
    assert [c.usage for c in chunks] == [_USAGE, None, None]
    assert all(_identity(c.served) == ("openai", "gpt-4o") for c in chunks)


@pytest.mark.asyncio
async def test_exactly_one_usage_chunk_reaches_the_caller() -> None:
    """Stamping copies chunks; it must never duplicate the one that carries usage."""
    chain = MultiModelChatBackend(
        [_Member("deepseek", "deepseek-chat", fail=1), _Member("anthropic", "sonnet")]
    )
    chunks = await _drain(chain, _text())
    assert sum(1 for c in chunks if c.usage is not None) == 1


@pytest.mark.asyncio
async def test_an_image_request_names_the_vision_model_not_the_skipped_primary() -> None:
    """R9-227: the walk skips a non-vision primary without a failure to record."""
    chain = MultiModelChatBackend(
        [_Member("deepseek", "deepseek-chat"), _Member("openai", "gpt-4o", vision=True)]
    )
    response = await chain.chat(_image())
    assert _identity(response.served) == ("openai", "gpt-4o")
    chain_stream = MultiModelChatBackend(
        [_Member("deepseek", "deepseek-chat"), _Member("openai", "gpt-4o", vision=True)]
    )
    chunks = await _drain(chain_stream, _image())
    assert all(_identity(c.served) == ("openai", "gpt-4o") for c in chunks)


@pytest.mark.asyncio
async def test_an_image_request_whose_first_vision_model_fails_names_the_second() -> None:
    chain = MultiModelChatBackend(
        [
            _Member("deepseek", "deepseek-chat"),
            _Member("openai", "gpt-4o", vision=True, fail=1),
            _Member("anthropic", "sonnet", vision=True),
        ]
    )
    response = await chain.chat(_image())
    assert _identity(response.served) == ("anthropic", "sonnet")


@pytest.mark.asyncio
async def test_two_interleaved_calls_on_one_chain_each_name_their_own_model() -> None:
    """R9-223: call A falls back; call B, on the same object, starts and ends meanwhile.

    Deterministic: A's fallback waits on a gate that B opens after it finishes, so B's
    whole call runs between A's primary failure and A's first chunk.
    """
    gate = asyncio.Event()
    primary = _Member("deepseek", "deepseek-chat", fail=1)
    fallback = _Member("anthropic", "sonnet", gate=gate)
    chain = MultiModelChatBackend([primary, fallback])

    a_task = asyncio.create_task(_drain(chain, _text()))
    while primary.fail:  # A's primary has failed; A now waits at the gate
        await asyncio.sleep(0)
    b_chunks = await _drain(chain, _text())  # B: the primary answers
    gate.set()
    a_chunks = await a_task

    assert {_identity(c.served) for c in a_chunks} == {("anthropic", "sonnet")}
    assert {_identity(c.served) for c in b_chunks} == {("deepseek", "deepseek-chat")}


@pytest.mark.asyncio
async def test_a_call_that_commits_mid_stream_does_not_rename_another_streams_chunks() -> None:
    """R9-223: A has committed to the fallback and is still streaming when B commits."""
    chain = MultiModelChatBackend(
        [_Member("deepseek", "deepseek-chat", fail=1), _Member("anthropic", "sonnet")]
    )
    a_stream = chain.chat_stream(_text())
    a_first = await anext(a_stream)  # A: primary failed, the fallback is speaking
    b_chunks = await _drain(chain, _text())  # B: the primary answers, start to end
    a_rest = [chunk async for chunk in a_stream]

    assert {_identity(c.served) for c in [a_first, *a_rest]} == {("anthropic", "sonnet")}
    assert {_identity(c.served) for c in b_chunks} == {("deepseek", "deepseek-chat")}


@pytest.mark.asyncio
async def test_two_interleaved_non_streaming_calls_each_name_their_own_model() -> None:
    gate = asyncio.Event()
    primary = _Member("deepseek", "deepseek-chat", fail=1)
    fallback = _Member("anthropic", "sonnet", gate=gate)
    chain = MultiModelChatBackend([primary, fallback])

    a_task = asyncio.create_task(chain.chat(_text()))
    while primary.fail:
        await asyncio.sleep(0)
    b_response = await chain.chat(_text())
    gate.set()
    a_response = await a_task

    assert _identity(a_response.served) == ("anthropic", "sonnet")
    assert _identity(b_response.served) == ("deepseek", "deepseek-chat")


@pytest.mark.asyncio
async def test_a_bare_backend_leaves_served_unset() -> None:
    """Only a chain stamps; a bare backend is its own served identity (callers know it)."""
    bare = _Member("openai", "gpt-4o")
    response = await bare.chat(_text())
    assert response.served is None
    chunks = [c async for c in bare.chat_stream(_text())]
    assert all(c.served is None for c in chunks)


def test_served_is_optional_and_frozen() -> None:
    served = ServedCall(provider="openai", model="gpt-4o")
    chunk = StreamChunk(delta="x", served=served)
    assert chunk.served == served
    assert StreamChunk(delta="x").served is None
    with pytest.raises(ValueError, match="frozen"):
        served.model = "other"  # type: ignore[misc]

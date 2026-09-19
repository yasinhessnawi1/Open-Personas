"""A leaked tool call must not reach the user, live OR on reopen (R9-157).

The provider boundary strips the markup, so the text the loop streams and the
text it writes to episodic are the same text. These tests drive the REAL
``OpenAICompatibleBackend`` (its SDK mocked at the transport) through the REAL
``ConversationLoop`` and a REAL ``Toolbox``, because the value of the fix is
exactly that the composition agrees end to end: a scripted fake backend would
prove nothing here (it never runs the filter).
"""

# ruff: noqa: ANN401, SLF001 (mocks use Any return types; tests access private attrs)

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from persona.backends.config import BackendConfig
from persona.backends.openai_compat import OpenAICompatibleBackend
from pydantic import SecretStr

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_loop import _conv, _make_loop  # type: ignore[import-not-found]

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

# Verbatim from production (GitHub issues #10 and #13), re-pointed at the
# ``echo`` tool the loop's default toolbox actually holds.
LEAK_PAREN_DELTAS = [
    "Let me check what's actually on the books.\n<tool",
    '_call>echo(message: "on the books")',
]
LEAK_MANGLED = (
    'Let me check the clock so we know exactly when "an hour from now" is.'
    '<tool_call>datetime tool_call: </arg_value><arg_key>tool": "mcp_search", '
    '"args": {"query": "schedule a one-time task or reminder", "top_k": 5}}'
)

MARKUP = ("<tool_call>", "<arg_key>", "<arg_value>", "</arg_value>", "</tool_call>")


def _stream_chunk(*, content: str = "", usage: Any | None = None) -> Any:
    chunk = MagicMock()
    delta = MagicMock()
    delta.content = content
    delta.tool_calls = []
    delta.reasoning_content = None
    delta.reasoning = None
    choice = MagicMock()
    choice.delta = delta
    chunk.choices = [choice]
    chunk.usage = usage
    return chunk


def _leaky_backend(rounds: list[list[str]]) -> tuple[OpenAICompatibleBackend, AsyncMock]:
    """A real OpenRouter/GLM backend whose transport replays scripted rounds."""
    backend = OpenAICompatibleBackend(
        BackendConfig(
            provider="openrouter",  # type: ignore[arg-type]
            model="z-ai/glm-5.3",
            api_key=SecretStr("test-key"),
        )
    )
    remaining = list(rounds)

    async def _create(**_kwargs: Any) -> AsyncIterator[Any]:
        pieces = remaining.pop(0) if remaining else ["Nothing more to add."]

        async def _gen() -> AsyncIterator[Any]:
            for piece in pieces:
                yield _stream_chunk(content=piece)

        return _gen()

    create = AsyncMock(side_effect=_create)
    return backend, create


def _install(backend: OpenAICompatibleBackend, create: AsyncMock) -> Any:
    return patch.object(
        backend._openai.chat.completions,  # type: ignore[union-attr]
        "create",
        new=create,
    )


@pytest.mark.asyncio
async def test_converted_call_executes_and_the_prose_is_clean() -> None:
    # Round 2 leaks too, and the loop persists the FINAL round's text, so the
    # reopened assertion below lands on text that really carried markup.
    backend, create = _leaky_backend(
        [LEAK_PAREN_DELTAS, ["The schedule is in good order:", "</arg_value> All set."]]
    )
    loop, stores, _writer = _make_loop(backend)  # type: ignore[arg-type]

    with _install(backend, create):
        chunks = [c async for c in loop.turn(_conv(0), "what is on the books?")]

    live = "".join(c.delta for c in chunks)
    for marker in MARKUP:
        assert marker not in live, f"{marker} leaked into the live stream"
    assert "Let me check what's actually on the books." in live
    assert "The schedule is in good order:" in live
    assert "All set." in live

    # The leaked call became a REAL dispatch through the normal path.
    assert create.await_count == 2, "the recovered call must trigger a tool round"
    reprompt = create.await_args_list[-1].kwargs["messages"]
    assert any("echoed: on the books" in str(m.get("content", "")) for m in reprompt), (
        "the recovered call never ran: no tool result reached the re-prompt"
    )

    # R9-157: what a reopened conversation reads is what streamed.
    persisted = stores["episodic"].writes[-1][0].text
    for marker in MARKUP:
        assert marker not in persisted, f"{marker} survived into the persisted record"
    assert "The schedule is in good order:" in persisted
    assert "All set." in persisted


@pytest.mark.asyncio
async def test_mangled_fragment_never_reaches_the_user_or_the_record() -> None:
    backend, create = _leaky_backend([[LEAK_MANGLED]])
    loop, stores, _writer = _make_loop(backend)  # type: ignore[arg-type]

    with _install(backend, create):
        chunks = [c async for c in loop.turn(_conv(0), "did you book it?")]

    live = "".join(c.delta for c in chunks)
    for marker in MARKUP:
        assert marker not in live
    assert "mcp_search" not in live
    assert 'exactly when "an hour from now" is.' in live
    # Unrecoverable: nothing is dispatched, so the turn ends in one round.
    assert create.await_count == 1

    persisted = stores["episodic"].writes[-1][0].text
    for marker in MARKUP:
        assert marker not in persisted
    assert "mcp_search" not in persisted

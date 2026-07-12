"""M2-T3 — OpenRouter response-side actuals in openai_compat (D-M2-3).

Pins, with scripted payloads (no SDK reach, no network):

* ``_usage_cost_usd`` fail-open parse matrix (present / absent / malformed /
  negative / decimal-string / bool / NaN / inf),
* ALL FIVE ``TokenUsage`` construction sites' dispositions —
  openai-path streaming usage (captures, OR-gated), openai-path zero-usage
  fallback (never sets), openai-path non-streaming parse (captures, OR-gated),
  anthropic streaming (never parses ``cost``), anthropic non-streaming parse
  (never parses ``cost``),
* the ``extra_body`` usage-accounting opt-in on BOTH openai-path payload
  builders, including D-20-3 precedence (an operator-supplied key — even
  ``"usage"`` itself — WINS over the M2 opt-in), and byte-identical requests
  for non-OpenRouter providers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from persona.backends.config import BackendConfig
from persona.backends.openai_compat import (
    OpenAICompatibleBackend,
    _parse_anthropic_response,
    _usage_cost_usd,
)
from persona.schema.conversation import ConversationMessage
from pydantic import SecretStr

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from persona.backends.types import StreamChunk


def _config(
    provider: str,
    *,
    model: str = "test-model",
    extra_body: dict[str, object] | None = None,
) -> BackendConfig:
    return BackendConfig(
        provider=provider,  # type: ignore[arg-type]
        model=model,
        api_key=SecretStr("test-key"),
        extra_body=extra_body,  # type: ignore[arg-type]
    )


def _user(text: str) -> ConversationMessage:
    return ConversationMessage(role="user", content=text, created_at=datetime.now(UTC))


async def _aiter(chunks: list[object]) -> AsyncIterator[object]:
    for c in chunks:
        yield c


def _text_chunk(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        usage=None,
        choices=[SimpleNamespace(delta=SimpleNamespace(content=text, tool_calls=None))],
    )


def _usage_chunk(*, prompt: int = 10, completion: int = 5, **extra: object) -> SimpleNamespace:
    """The final streaming usage chunk; pass ``cost=...`` to script the actual."""
    usage = SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion, **extra)
    return SimpleNamespace(usage=usage, choices=[])


def _openai_response(
    *, prompt: int = 10, completion: int = 5, **usage_extra: object
) -> SimpleNamespace:
    """A minimal non-streaming chat.completions response."""
    usage = SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion, **usage_extra)
    message = SimpleNamespace(content="hello", tool_calls=[])
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        model="test-model",
        usage=usage,
    )


async def _collect_stream(backend: OpenAICompatibleBackend) -> list[StreamChunk]:
    return [c async for c in backend.chat_stream([_user("hi")])]


class TestUsageCostParser:
    """The fail-open parse matrix (never raises, never guesses)."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (0.00042, 0.00042),
            (3, 3.0),
            (Decimal("0.000123"), 0.000123),
            ("0.0000123", 0.0000123),  # decimal-string (the documented OR shape)
            (0, 0.0),  # a :free route's actual IS 0.0
            (0.0, 0.0),
        ],
    )
    def test_valid_values_parse(self, raw: object, expected: float) -> None:
        assert _usage_cost_usd(SimpleNamespace(cost=raw)) == pytest.approx(expected)

    @pytest.mark.parametrize(
        "raw",
        [
            None,
            True,  # bool is not a payment
            False,
            -0.5,  # negative is not a payment
            "-1",
            "wat",  # malformed string
            "nan",  # non-finite would poison cost_cents downstream
            "inf",
            {},  # wrong type
            [0.1],
        ],
    )
    def test_invalid_values_are_none(self, raw: object) -> None:
        assert _usage_cost_usd(SimpleNamespace(cost=raw)) is None

    def test_absent_attribute_is_none(self) -> None:
        assert _usage_cost_usd(SimpleNamespace()) is None

    def test_none_usage_object_is_none(self) -> None:
        assert _usage_cost_usd(None) is None


class TestStreamingCaptureSite:
    """Site 1/5: the openai-path streaming usage chunk (OR-gated capture)."""

    @pytest.mark.asyncio
    async def test_openrouter_stream_captures_cost(self) -> None:
        backend = OpenAICompatibleBackend(_config("openrouter"))
        chunks = [_text_chunk("hi"), _usage_chunk(cost=0.00042)]
        with patch.object(
            backend._openai.chat.completions,  # type: ignore[union-attr]  # noqa: SLF001
            "create",
            new=AsyncMock(return_value=_aiter(chunks)),  # type: ignore[arg-type]
        ):
            collected = await _collect_stream(backend)
        final = next(c for c in collected if c.is_final)
        assert final.usage is not None
        assert final.usage.cost_usd == pytest.approx(0.00042)
        assert final.usage.total_tokens == 15

    @pytest.mark.asyncio
    async def test_non_openrouter_stream_ignores_a_cost_field(self) -> None:
        # Gate: only OpenRouter's usage accounting is trusted (D-M2-3).
        backend = OpenAICompatibleBackend(_config("deepseek"))
        chunks = [_text_chunk("hi"), _usage_chunk(cost=0.00042)]
        with patch.object(
            backend._openai.chat.completions,  # type: ignore[union-attr]  # noqa: SLF001
            "create",
            new=AsyncMock(return_value=_aiter(chunks)),  # type: ignore[arg-type]
        ):
            collected = await _collect_stream(backend)
        final = next(c for c in collected if c.is_final)
        assert final.usage is not None
        assert final.usage.cost_usd is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_cost", ["wat", -1, "nan"])
    async def test_openrouter_malformed_cost_fails_open(self, bad_cost: object) -> None:
        backend = OpenAICompatibleBackend(_config("openrouter"))
        chunks = [_text_chunk("hi"), _usage_chunk(cost=bad_cost)]
        with patch.object(
            backend._openai.chat.completions,  # type: ignore[union-attr]  # noqa: SLF001
            "create",
            new=AsyncMock(return_value=_aiter(chunks)),  # type: ignore[arg-type]
        ):
            collected = await _collect_stream(backend)
        final = next(c for c in collected if c.is_final)
        assert final.usage is not None
        assert final.usage.cost_usd is None  # tokens still parsed; turn unharmed
        assert final.usage.total_tokens == 15

    @pytest.mark.asyncio
    async def test_openrouter_absent_cost_is_none(self) -> None:
        backend = OpenAICompatibleBackend(_config("openrouter"))
        chunks = [_text_chunk("hi"), _usage_chunk()]  # no cost attr at all
        with patch.object(
            backend._openai.chat.completions,  # type: ignore[union-attr]  # noqa: SLF001
            "create",
            new=AsyncMock(return_value=_aiter(chunks)),  # type: ignore[arg-type]
        ):
            collected = await _collect_stream(backend)
        final = next(c for c in collected if c.is_final)
        assert final.usage is not None
        assert final.usage.cost_usd is None


class TestZeroUsageFallbackSite:
    """Site 2/5: the openai-path zero-usage fallback never sets cost_usd."""

    @pytest.mark.asyncio
    async def test_no_usage_chunks_yields_zero_usage_no_cost(self) -> None:
        backend = OpenAICompatibleBackend(_config("openrouter"))
        chunks = [_text_chunk("hi")]  # provider never sent a usage chunk
        with patch.object(
            backend._openai.chat.completions,  # type: ignore[union-attr]  # noqa: SLF001
            "create",
            new=AsyncMock(return_value=_aiter(chunks)),  # type: ignore[arg-type]
        ):
            collected = await _collect_stream(backend)
        final = next(c for c in collected if c.is_final)
        assert final.usage is not None
        assert final.usage.total_tokens == 0
        assert final.usage.cost_usd is None


class TestNonStreamingCaptureSite:
    """Site 3/5: ``_parse_openai_response`` (OR-gated capture)."""

    @pytest.mark.asyncio
    async def test_openrouter_chat_captures_decimal_string_cost(self) -> None:
        backend = OpenAICompatibleBackend(_config("openrouter"))
        with patch.object(
            backend._openai.chat.completions,  # type: ignore[union-attr]  # noqa: SLF001
            "create",
            new=AsyncMock(return_value=_openai_response(cost="0.0007")),
        ):
            response = await backend.chat([_user("hi")])
        assert response.usage.cost_usd == pytest.approx(0.0007)
        assert response.usage.total_tokens == 15

    @pytest.mark.asyncio
    async def test_non_openrouter_chat_ignores_a_cost_field(self) -> None:
        backend = OpenAICompatibleBackend(_config("deepseek"))
        with patch.object(
            backend._openai.chat.completions,  # type: ignore[union-attr]  # noqa: SLF001
            "create",
            new=AsyncMock(return_value=_openai_response(cost=0.5)),
        ):
            response = await backend.chat([_user("hi")])
        assert response.usage.cost_usd is None


class _FakeAnthropicStream:
    """Mimics ``anthropic.AsyncMessageStream`` minimally (harness precedent)."""

    def __init__(self, events: list[object], final_message: object) -> None:
        self._events = events
        self._final_message = final_message

    async def __aenter__(self) -> _FakeAnthropicStream:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def __aiter__(self) -> AsyncIterator[object]:
        async def gen() -> AsyncIterator[object]:
            for ev in self._events:
                yield ev

        return gen()

    async def get_final_message(self) -> object:
        return self._final_message


class TestAnthropicSitesUntouched:
    """Sites 4+5/5: the anthropic-SDK paths never parse a ``cost`` field.

    They are not OpenRouter-reachable; a bogus ``cost`` attribute on their
    usage objects must be ignored (cost_usd stays None by construction).
    """

    @pytest.mark.asyncio
    async def test_anthropic_stream_ignores_cost(self) -> None:
        backend = OpenAICompatibleBackend(_config("anthropic"))
        text_event = SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="text_delta", text="Hello"),
        )
        final_msg = SimpleNamespace(
            usage=SimpleNamespace(input_tokens=4, output_tokens=2, cost=0.99),
        )
        fake_stream = _FakeAnthropicStream([text_event], final_msg)
        with patch.object(
            backend._anthropic.messages,  # type: ignore[union-attr]  # noqa: SLF001
            "stream",
            new=MagicMock(return_value=fake_stream),
        ):
            collected = await _collect_stream(backend)
        final = next(c for c in collected if c.is_final)
        assert final.usage is not None
        assert final.usage.total_tokens == 6
        assert final.usage.cost_usd is None  # bogus cost ignored

    def test_anthropic_parse_ignores_cost(self) -> None:
        response = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="hello")],
            usage=SimpleNamespace(input_tokens=4, output_tokens=2, cost=0.99),
            model="claude-x",
        )
        parsed = _parse_anthropic_response(response, "anthropic", use_native_tools=True)
        assert parsed.usage.total_tokens == 6
        assert parsed.usage.cost_usd is None


class TestExtraBodyOptIn:
    """The D-M2-3 opt-in on BOTH payload builders + D-20-3 precedence."""

    @staticmethod
    def _captured_kwargs_chat(
        backend: OpenAICompatibleBackend,
    ) -> tuple[AsyncMock, object]:
        mock = AsyncMock(return_value=_openai_response())
        return mock, patch.object(
            backend._openai.chat.completions,  # type: ignore[union-attr]  # noqa: SLF001
            "create",
            new=mock,
        )

    @pytest.mark.asyncio
    async def test_openrouter_chat_opts_in(self) -> None:
        backend = OpenAICompatibleBackend(_config("openrouter"))
        mock, ctx = self._captured_kwargs_chat(backend)
        with ctx:  # type: ignore[attr-defined]
            await backend.chat([_user("hi")])
        assert mock.call_args.kwargs["extra_body"] == {"usage": {"include": True}}

    @pytest.mark.asyncio
    async def test_openrouter_stream_opts_in(self) -> None:
        backend = OpenAICompatibleBackend(_config("openrouter"))
        mock = AsyncMock(return_value=_aiter([_usage_chunk(cost=0.1)]))
        with patch.object(
            backend._openai.chat.completions,  # type: ignore[union-attr]  # noqa: SLF001
            "create",
            new=mock,  # type: ignore[arg-type]
        ):
            await _collect_stream(backend)
        assert mock.call_args.kwargs["extra_body"] == {"usage": {"include": True}}

    @pytest.mark.asyncio
    async def test_operator_extra_body_keys_are_merged(self) -> None:
        backend = OpenAICompatibleBackend(
            _config("openrouter", extra_body={"transforms": ["middle-out"]})
        )
        mock, ctx = self._captured_kwargs_chat(backend)
        with ctx:  # type: ignore[attr-defined]
            await backend.chat([_user("hi")])
        assert mock.call_args.kwargs["extra_body"] == {
            "usage": {"include": True},
            "transforms": ["middle-out"],
        }

    @pytest.mark.asyncio
    async def test_operator_usage_key_wins_over_the_opt_in(self) -> None:
        # D-20-3 precedence: the configured pass-through wins on collision —
        # an explicit operator override can disable usage accounting.
        backend = OpenAICompatibleBackend(
            _config("openrouter", extra_body={"usage": {"include": False}})
        )
        mock, ctx = self._captured_kwargs_chat(backend)
        with ctx:  # type: ignore[attr-defined]
            await backend.chat([_user("hi")])
        assert mock.call_args.kwargs["extra_body"] == {"usage": {"include": False}}

    @pytest.mark.asyncio
    async def test_non_openrouter_extra_body_unchanged(self) -> None:
        backend = OpenAICompatibleBackend(_config("deepseek", extra_body={"foo": 1}))
        mock, ctx = self._captured_kwargs_chat(backend)
        with ctx:  # type: ignore[attr-defined]
            await backend.chat([_user("hi")])
        assert mock.call_args.kwargs["extra_body"] == {"foo": 1}  # no usage key added

    @pytest.mark.asyncio
    async def test_non_openrouter_without_extra_body_sends_none(self) -> None:
        # Byte-identical requests for non-OR providers (no extra_body at all).
        backend = OpenAICompatibleBackend(_config("deepseek"))
        mock, ctx = self._captured_kwargs_chat(backend)
        with ctx:  # type: ignore[attr-defined]
            await backend.chat([_user("hi")])
        assert "extra_body" not in mock.call_args.kwargs

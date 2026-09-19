"""A model's tool-call channel must never leak into the text channel.

GLM-class models served through OpenRouter sometimes write their tool call as
TEXT in the content stream (an XML-ish ``<tool_call>`` block, sometimes with
``<arg_key>`` / ``<arg_value>`` tags) instead of populating the structured
``tool_calls`` field. Two real productions leaks, both from the chat UI:

1. ``<tool_call>schedule_introspect(scope: "all", days_ahead: 7)The schedule
   is in good order:``, well formed enough to recover, and the prose ran
   straight on after the closing paren.
2. ``<tool_call>datetime tool_call: </arg_value><arg_key>tool": "mcp_search",
   "args": {...}}``, mangled beyond recovery; must be stripped, never shown.

The fix lives at the provider boundary so BOTH the streaming chat loop and
the non-streaming agentic loop get it, and so the text that is persisted is
the same text that streamed (R9-157: live and reopened must agree).
"""

# ruff: noqa: ANN401, SLF001 (mocks use Any return types; tests access private attrs)

from __future__ import annotations

from collections.abc import AsyncIterator  # noqa: TC003 (used at runtime in helpers)
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from persona.backends._text_tool_calls import (
    TEXT_TOOL_CALL_OPEN,
    TextToolCallFilter,
    strip_text_tool_calls,
)
from persona.backends.config import BackendConfig
from persona.backends.openai_compat import OpenAICompatibleBackend
from persona.backends.types import StreamChunk, ToolSpec
from persona.schema.conversation import ConversationMessage  # noqa: TC001
from pydantic import SecretStr

# The two fragments captured from production (issues #10 and #13).
LEAK_PAREN = (
    "Let me check what's actually on the books rather than trusting memory.\n"
    '<tool_call>schedule_introspect(scope: "all", days_ahead: 7)'
    "The schedule is in good order:"
)
LEAK_MANGLED = (
    'Let me check the clock so we know exactly when "an hour from now" is.'
    '<tool_call>datetime tool_call: </arg_value><arg_key>tool": "mcp_search", '
    '"args": {"query": "schedule a one-time task or reminder at a specific time", '
    '"top_k": 5}}'
)
# GLM's own native tool-call serialisation, leaked whole.
LEAK_GLM_TAGS = (
    "One moment.\n"
    "<tool_call>datetime\n"
    "<arg_key>timezone</arg_key>\n"
    '<arg_value>"Europe/Oslo"</arg_value>\n'
    "</tool_call>\n"
    "There we go."
)

KNOWN = frozenset({"schedule_introspect", "datetime", "mcp_search"})


def _user(text: str) -> ConversationMessage:
    return ConversationMessage(role="user", content=text, created_at=datetime.now(UTC))


def _config(provider: str = "openrouter", model: str = "z-ai/glm-5.3") -> BackendConfig:
    return BackendConfig(
        provider=provider,  # type: ignore[arg-type]
        model=model,
        api_key=SecretStr("test-key"),
    )


async def _async_iter(items: list[Any]) -> AsyncIterator[Any]:
    for x in items:
        yield x


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


# -----------------------------------------------------------------------------
# The detector itself
# -----------------------------------------------------------------------------


class TestStripTextToolCalls:
    def test_paren_shape_converts_to_a_real_call(self) -> None:
        text, calls = strip_text_tool_calls(
            LEAK_PAREN, known_tool_names=KNOWN, provider="openrouter", model="z-ai/glm-5.3"
        )
        assert TEXT_TOOL_CALL_OPEN not in text
        assert "schedule_introspect(scope" not in text
        assert len(calls) == 1
        assert calls[0].name == "schedule_introspect"
        assert calls[0].args == {"scope": "all", "days_ahead": 7}
        # The prose on both sides survives.
        assert "rather than trusting memory" in text
        assert "The schedule is in good order:" in text

    def test_glm_arg_tag_shape_converts_to_a_real_call(self) -> None:
        text, calls = strip_text_tool_calls(
            LEAK_GLM_TAGS, known_tool_names=KNOWN, provider="openrouter", model="z-ai/glm-5.3"
        )
        assert "<arg_key>" not in text
        assert "<arg_value>" not in text
        assert TEXT_TOOL_CALL_OPEN not in text
        assert len(calls) == 1
        assert calls[0].name == "datetime"
        assert calls[0].args == {"timezone": "Europe/Oslo"}
        assert "One moment." in text
        assert "There we go." in text

    def test_json_shape_converts_to_a_real_call(self) -> None:
        text, calls = strip_text_tool_calls(
            'Checking.<tool_call>{"name": "datetime", "arguments": {"timezone": "UTC"}}'
            "</tool_call> done.",
            known_tool_names=KNOWN,
            provider="openrouter",
            model="z-ai/glm-5.3",
        )
        assert len(calls) == 1
        assert calls[0].name == "datetime"
        assert calls[0].args == {"timezone": "UTC"}
        assert "<" not in text

    def test_mangled_shape_is_stripped_not_shown(self) -> None:
        text, calls = strip_text_tool_calls(
            LEAK_MANGLED, known_tool_names=KNOWN, provider="openrouter", model="z-ai/glm-5.3"
        )
        assert TEXT_TOOL_CALL_OPEN not in text
        assert "<arg_key>" not in text
        assert "</arg_value>" not in text
        assert "mcp_search" not in text
        # Not recoverable: the args are not attached to a parseable call.
        assert calls == []
        assert 'Let me check the clock so we know exactly when "an hour from now" is.' in text

    def test_unknown_tool_name_is_stripped_never_dispatched(self) -> None:
        text, calls = strip_text_tool_calls(
            "before <tool_call>not_a_real_tool(x: 1)</tool_call> after",
            known_tool_names=KNOWN,
            provider="openrouter",
            model="z-ai/glm-5.3",
        )
        assert calls == []
        assert "not_a_real_tool" not in text
        assert "before" in text
        assert "after" in text

    def test_wire_name_is_accepted_for_an_mcp_real_name(self) -> None:
        # The toolbox advertises the wire name; a model that echoes the REAL
        # colon name still resolves.
        text, calls = strip_text_tool_calls(
            '<tool_call>mcp:deepwiki:ask(question: "hi")</tool_call>',
            known_tool_names=frozenset({"mcp_deepwiki_ask"}),
            provider="openrouter",
            model="z-ai/glm-5.3",
        )
        assert len(calls) == 1
        assert calls[0].name == "mcp_deepwiki_ask"
        assert text.strip() == ""

    def test_half_arrived_opening_tag_is_dropped_not_shown(self) -> None:
        # The stream ended mid-tag (the provider cut off, or the round ended
        # right there). Never flash the half-written markup.
        f = TextToolCallFilter(known_tool_names=KNOWN, provider="openrouter", model="z-ai/glm-5.3")
        first, _ = f.feed("Checking the books.<tool_c")
        tail, calls = f.finish()
        assert first + tail == "Checking the books."
        assert calls == []

    def test_a_lone_trailing_angle_bracket_is_kept(self) -> None:
        f = TextToolCallFilter(known_tool_names=KNOWN, provider="openrouter", model="z-ai/glm-5.3")
        first, _ = f.feed("is 3 <")
        tail, _ = f.finish()
        assert first + tail == "is 3 <"

    def test_clean_prose_is_untouched(self) -> None:
        prose = "No markup here. 3 < 4 and a <b> tag-ish thing."
        text, calls = strip_text_tool_calls(
            prose, known_tool_names=KNOWN, provider="openrouter", model="z-ai/glm-5.3"
        )
        assert text == prose
        assert calls == []

    def test_orphan_arg_tags_without_an_opener_are_stripped(self) -> None:
        text, calls = strip_text_tool_calls(
            "Sure.</arg_value><arg_key>x</arg_key> Done.",
            known_tool_names=KNOWN,
            provider="openrouter",
            model="z-ai/glm-5.3",
        )
        assert "<arg_key>" not in text
        assert "</arg_value>" not in text
        assert calls == []
        # The key's VALUE goes with its tags, not left behind as stray words.
        assert text == "Sure. Done."


class TestChunkSplitTag:
    """The opening tag can land across two provider deltas."""

    @pytest.mark.parametrize("split_at", list(range(1, len(LEAK_PAREN))))
    def test_any_split_point_gives_the_same_answer(self, split_at: int) -> None:
        """EVERY split point, not a sample.

        A sampled range once skipped the split that lands right after the
        opening "<": the rest of the tag carries no "<" of its own, so emitting
        that one character leaked the whole tag downstream.
        """
        whole_text, whole_calls = strip_text_tool_calls(
            LEAK_PAREN, known_tool_names=KNOWN, provider="openrouter", model="z-ai/glm-5.3"
        )
        f = TextToolCallFilter(known_tool_names=KNOWN, provider="openrouter", model="z-ai/glm-5.3")
        out = ""
        calls = []
        for piece in (LEAK_PAREN[:split_at], LEAK_PAREN[split_at:]):
            t, c = f.feed(piece)
            out += t
            calls.extend(c)
        t, c = f.finish()
        out += t
        calls.extend(c)
        assert out == whole_text
        assert [(x.name, x.args) for x in calls] == [(x.name, x.args) for x in whole_calls]

    def test_a_delta_that_ends_on_the_bare_angle_bracket_holds_it(self) -> None:
        f = TextToolCallFilter(known_tool_names=KNOWN, provider="openrouter", model="z-ai/glm-5.3")
        out = ""
        calls = []
        for piece in ["Checking.<", "tool_call>", 'datetime(timezone: "UTC")', "Done."]:
            t, c = f.feed(piece)
            out += t
            calls.extend(c)
        t, c = f.finish()
        out += t
        calls.extend(c)
        assert out == "Checking.Done."
        assert [x.name for x in calls] == ["datetime"]

    def test_tag_split_exactly_mid_token(self) -> None:
        f = TextToolCallFilter(known_tool_names=KNOWN, provider="openrouter", model="z-ai/glm-5.3")
        out = ""
        calls = []
        for piece in ["Checking.<tool", "_call>datetime(timezone: ", '"UTC")', "Done."]:
            t, c = f.feed(piece)
            out += t
            calls.extend(c)
        t, c = f.finish()
        out += t
        calls.extend(c)
        assert out == "Checking.Done."
        assert len(calls) == 1
        assert calls[0].name == "datetime"
        assert calls[0].args == {"timezone": "UTC"}


# -----------------------------------------------------------------------------
# Wired into the provider boundary
# -----------------------------------------------------------------------------


class TestStreamingBackendStripsTheLeak:
    @pytest.mark.asyncio
    async def test_stream_converts_the_leaked_call_and_cleans_the_prose(self) -> None:
        backend = OpenAICompatibleBackend(_config())
        tools = [ToolSpec(name="schedule_introspect", description="x", parameters={})]
        # Chunked so `<tool_call>` spans two deltas.
        pieces = [
            "Let me check.\n<tool",
            '_call>schedule_introspect(scope: "all", days_ahead: 7)',
            "The schedule is in good order:",
        ]
        with patch.object(
            backend._openai.chat.completions,  # type: ignore[union-attr]
            "create",
            new=AsyncMock(return_value=_async_iter([_stream_chunk(content=p) for p in pieces])),
        ):
            collected: list[StreamChunk] = []
            async for c in backend.chat_stream([_user("hi")], tools=tools):
                collected.append(c)

        streamed = "".join(c.delta for c in collected)
        assert TEXT_TOOL_CALL_OPEN not in streamed
        assert "schedule_introspect(scope" not in streamed
        assert "Let me check." in streamed
        assert "The schedule is in good order:" in streamed
        deltas = [c.tool_call_delta for c in collected if c.tool_call_delta is not None]
        assert len(deltas) == 1
        assert deltas[0].name_delta == "schedule_introspect"
        assert deltas[0].call_id

    @pytest.mark.asyncio
    async def test_stream_strips_the_mangled_fragment(self) -> None:
        backend = OpenAICompatibleBackend(_config())
        tools = [ToolSpec(name="datetime", description="x", parameters={})]
        with patch.object(
            backend._openai.chat.completions,  # type: ignore[union-attr]
            "create",
            new=AsyncMock(return_value=_async_iter([_stream_chunk(content=LEAK_MANGLED)])),
        ):
            collected = [c async for c in backend.chat_stream([_user("hi")], tools=tools)]
        streamed = "".join(c.delta for c in collected)
        assert TEXT_TOOL_CALL_OPEN not in streamed
        assert "<arg_key>" not in streamed
        assert "mcp_search" not in streamed
        assert "an hour from now" in streamed
        assert [c.tool_call_delta for c in collected if c.tool_call_delta is not None] == []

    @pytest.mark.asyncio
    async def test_text_call_ids_do_not_collide_across_rounds(self) -> None:
        """Round 2 must not re-mint round 1's synthetic id (mirrors R9-046)."""
        backend = OpenAICompatibleBackend(_config())
        tools = [ToolSpec(name="datetime", description="x", parameters={})]

        async def _one_round() -> str:
            with patch.object(
                backend._openai.chat.completions,  # type: ignore[union-attr]
                "create",
                new=AsyncMock(
                    return_value=_async_iter(
                        [_stream_chunk(content='<tool_call>datetime(timezone: "UTC")')]
                    )
                ),
            ):
                collected = [c async for c in backend.chat_stream([_user("hi")], tools=tools)]
            deltas = [c.tool_call_delta for c in collected if c.tool_call_delta is not None]
            assert len(deltas) == 1
            return deltas[0].call_id

        first = await _one_round()
        second = await _one_round()
        assert first
        assert second
        assert first != second


class TestNonStreamingBackendStripsTheLeak:
    """The agentic loop calls ``chat()``, not ``chat_stream()``."""

    @pytest.mark.asyncio
    async def test_chat_converts_the_leaked_call(self) -> None:
        backend = OpenAICompatibleBackend(_config())
        tools = [ToolSpec(name="schedule_introspect", description="x", parameters={})]
        message = MagicMock()
        message.content = LEAK_PAREN
        message.tool_calls = []
        choice = MagicMock()
        choice.message = message
        choice.finish_reason = "stop"
        response = MagicMock()
        response.choices = [choice]
        response.usage = None
        response.model = "z-ai/glm-5.3"
        with patch.object(
            backend._openai.chat.completions,  # type: ignore[union-attr]
            "create",
            new=AsyncMock(return_value=response),
        ):
            result = await backend.chat([_user("hi")], tools=tools)
        assert TEXT_TOOL_CALL_OPEN not in result.content
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "schedule_introspect"
        # A pair-able id, not the empty string two leaked calls would share.
        assert result.tool_calls[0].call_id == "text-1"

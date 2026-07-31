"""The tool-message pairing invariant (R9-068).

A ``role="tool"`` message is only legal when the message before it is an
``assistant`` carrying the matching ``tool_calls``. Providers enforce this and
reject a violation with a 400 ("tool message has no preceding assistant tool
call"), which killed the turn AFTER the tool had already run — so the user got
a confident, wrong fallback answer rather than an error.

These tests drive the REAL ``ConversationLoop.turn`` through a real tool
dispatch and assert the invariant on the message array the loop actually hands
the backend on its re-prompt (``ScriptedBackend.last_stream_messages``). They
deliberately do NOT hand-construct a broken array and assert a validator
rejects it: that would prove a validator, not the loop.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _fakes import ScriptedBackend, ScriptedRound  # type: ignore[import-not-found]
from test_loop import _conv, _make_loop  # type: ignore[import-not-found]

if TYPE_CHECKING:
    from persona.schema.conversation import ConversationMessage

# The provider names whose tool results are formatted natively (role="tool").
# "openrouter" is the one that failed in production; "anthropic" is the fake's
# default and has the same native shape.
_NATIVE_PROVIDERS = ("openrouter", "anthropic")


def _assert_pairing_holds(messages: list[ConversationMessage]) -> None:
    """Every ``role="tool"`` message follows an assistant owning its call id."""
    for index, message in enumerate(messages):
        if message.role != "tool":
            continue
        assert index > 0, "a role='tool' message cannot be first in the array"
        previous = messages[index - 1]
        assert previous.role == "assistant", (
            f"messages[{index}] is role='tool' but messages[{index - 1}] is "
            f"role='{previous.role}' — this is the exact shape the provider "
            f"rejects with 'tool message has no preceding assistant tool call'"
        )
        call_id = message.metadata.get("tool_call_id", "")
        owned = {tc.call_id for tc in (previous.tool_calls or [])}
        assert call_id in owned, (
            f"messages[{index}] carries tool_call_id={call_id!r} but the "
            f"preceding assistant owns {owned!r}"
        )


@pytest.mark.parametrize("provider", _NATIVE_PROVIDERS)
@pytest.mark.asyncio
async def test_non_native_backend_never_emits_an_orphan_tool_message(provider: str) -> None:
    """The regression: native-format provider + non-native backend.

    ``supports_native_tools=False`` means the loop skips the assistant message
    that owns the tool_calls. Before R9-068 the tool result was still formatted
    natively (``role="tool"``) purely because the PROVIDER name was in the
    native list, leaving an orphan and 400-ing the turn.
    """
    backend = ScriptedBackend(
        [
            ScriptedRound(tool_name="echo", tool_args={"message": "ping"}),
            ScriptedRound(text="The tool said: echoed: ping"),
        ],
        provider_name=provider,
        supports_native_tools=False,
    )
    loop, _stores, _writer = _make_loop(backend)

    chunks = [c async for c in loop.turn(_conv(2), "use the echo tool")]

    assert backend.chat_stream_calls == 2, "the tool round must trigger a re-prompt"
    sent = backend.last_stream_messages or []
    # The re-prompt must carry the tool result in SOME form...
    assert any("echoed: ping" in (m.content or "") for m in sent), (
        "the tool result never reached the re-prompt at all"
    )
    # ...but never as an orphaned native tool message.
    assert not [m for m in sent if m.role == "tool"], (
        "a non-native backend must not emit role='tool' — there is no "
        "assistant.tool_calls for it to attach to"
    )
    _assert_pairing_holds(sent)
    # The turn still completes normally.
    assert chunks[-1].is_final is True


@pytest.mark.parametrize("provider", _NATIVE_PROVIDERS)
@pytest.mark.asyncio
async def test_native_backend_pairs_each_tool_result_with_its_assistant(provider: str) -> None:
    """The other direction: native backend DOES emit the pair, correctly ordered.

    Guards against "fixing" the orphan by simply never emitting ``role="tool"``,
    which would silently downgrade every native provider to the text shim.
    """
    backend = ScriptedBackend(
        [
            ScriptedRound(tool_name="echo", tool_args={"message": "ping"}),
            ScriptedRound(text="The tool said: echoed: ping"),
        ],
        provider_name=provider,
        supports_native_tools=True,
    )
    loop, _stores, _writer = _make_loop(backend)

    [c async for c in loop.turn(_conv(2), "use the echo tool")]

    sent = backend.last_stream_messages or []
    tool_messages = [m for m in sent if m.role == "tool"]
    assert tool_messages, "a native backend must still use the native tool format"
    _assert_pairing_holds(sent)


@pytest.mark.asyncio
async def test_shim_provider_is_unchanged_by_the_fix() -> None:
    """A genuinely shim provider keeps its existing text form (no regression)."""
    backend = ScriptedBackend(
        [
            ScriptedRound(tool_name="echo", tool_args={"message": "ping"}),
            ScriptedRound(text="done"),
        ],
        provider_name="ollama",
        supports_native_tools=False,
    )
    loop, _stores, _writer = _make_loop(backend)

    [c async for c in loop.turn(_conv(2), "use the echo tool")]

    sent = backend.last_stream_messages or []
    assert not [m for m in sent if m.role == "tool"]
    assert any("echoed: ping" in (m.content or "") for m in sent)
    _assert_pairing_holds(sent)

"""The ``voice turn ended`` line names the model that actually answered (R9-214).

Driven end to end: a REAL :class:`VoiceModelReplyProducer` inside a REAL
:class:`StreamingLoop`, over a REAL :class:`MultiModelChatBackend` whose primary
genuinely raises inside ``chat_stream``, so the chain's own classifier and ledger
decide who served. The assertion is on the log line itself, the thing an operator
reads, and the served pair is compared with equality.

Also pinned here: the billing meter is still fed the chain's primary (R9-221), and
the producer's reply rule matches the chain's commit rule (R9-033).
"""

# Test doubles keep the loose signatures of the protocols they stand in for.
# ruff: noqa: ANN401, ARG001, ARG002
from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from persona.backends import BackendConfig, StreamChunk, TokenUsage
from persona.backends.errors import ProviderError, RateLimitError
from persona.backends.multi_model import MultiModelChatBackend
from persona.backends.types import ToolCallDelta
from persona.history import ConversationHistoryManager
from persona.schema.chunks import PersonaChunk
from persona.schema.conversation import Conversation
from persona.schema.persona import Persona, PersonaIdentity, RoutingConfig
from persona.schema.tools import ToolCall, ToolResult
from persona.tools import Toolbox
from persona.tools.protocol import tool
from persona_runtime.prompt import PromptBuilder
from persona_runtime.router import Router
from persona_runtime.tier import TierConfig, TierRegistry
from persona_voice.loop import streaming as streaming_mod
from persona_voice.loop.streaming import AudioChunk, HeardReply, StreamingLoop, Transcript
from persona_voice.model import VoiceModelReplyProducer, VoiceTurnContext
from persona_voice.model.origination_gate import VoiceOriginationDecision
from persona_voice.model.reply_producer import _is_reply_payload
from persona_voice.session.state_machine import SessionLifecycleEvent, SessionStateMachine

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from persona_voice.logging import VoiceLog

pytestmark = pytest.mark.asyncio

_ENDED = re.compile(
    r"voice turn ended outcome=(?P<outcome>\w+) tokens=\d+ first_audio_ms=(?:\d+|None) "
    r"total_ms=\d+ provider=(?P<provider>\S+) model=(?P<model>\S+)"
)

_SPEAK = "speak"
_HANG = "hang"
_FAIL = "fail"
_CRASH = "crash"
_RATE_LIMIT = "rate_limit"
#: A round that is ONLY a tool call, keyed by the tool it calls.
_TOOL_ARGS = {
    "web_search": '{"query": "rights"}',
    "code_execution": '{"code": "x"}',
    "generate_image": '{"prompt": "a castle"}',
}


class _Backend:
    """A streaming backend double that plays one scripted behaviour per call."""

    supports_native_tools = False
    supports_vision = False

    def __init__(self, provider: str, model: str, behaviours: list[str]) -> None:
        self.provider_name = provider
        self.model_name = model
        self._behaviours = behaviours
        self.stream_calls = 0

    async def chat_stream(self, messages: list[Any], **_kwargs: Any) -> AsyncIterator[StreamChunk]:
        behaviour = self._behaviours[self.stream_calls]
        self.stream_calls += 1
        if behaviour == _FAIL:
            # A 400 with a status is SURFACE for the chain: it ends the turn, never falls back.
            raise ProviderError("scripted 400", context={"status_code": "400"})
        if behaviour == _CRASH:
            # Not a domain error: the loop treats it as a bug, before any reply was produced.
            raise RuntimeError("scripted bug")
        if behaviour == _RATE_LIMIT:
            raise RateLimitError("scripted 429", context={"provider": self.provider_name})
        if behaviour in _TOOL_ARGS:
            yield StreamChunk(
                delta="",
                tool_call_delta=ToolCallDelta(
                    call_id="c1", name_delta=behaviour, arguments_delta=_TOOL_ARGS[behaviour]
                ),
            )
        else:
            yield StreamChunk(delta=f"Hi from {self.model_name}. ")
        if behaviour == _HANG:
            await asyncio.sleep(3600)
        yield StreamChunk(
            delta="",
            is_final=True,
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )


class _SilentHangBackend(_Backend):
    """Never produces a reply chunk at all: the stall BEFORE any payload."""

    async def chat_stream(self, messages: list[Any], **_kwargs: Any) -> AsyncIterator[StreamChunk]:
        self.stream_calls += 1
        await asyncio.sleep(3600)
        yield StreamChunk(delta="")  # pragma: no cover - never reached


class _RateLimitedPrimary(_Backend):
    """A primary whose stream raises a 429 before its first chunk, as a real one does."""

    async def chat_stream(self, messages: list[Any], **_kwargs: Any) -> AsyncIterator[StreamChunk]:
        self.stream_calls += 1
        raise RateLimitError("scripted 429", context={"provider": self.provider_name})
        yield StreamChunk(delta="")  # pragma: no cover - makes this an async generator


@tool(name="web_search", description="Search the web.")
async def _web_search(query: str) -> ToolResult:
    return ToolResult(tool_name="web_search", content=f"results for {query}")


@tool(name="code_execution", description="Run code.")
async def _code_execution(code: str) -> ToolResult:
    msg = "a deferred tool must never run on the live voice path"
    raise RuntimeError(msg)


@tool(name="generate_image", description="Generate an image.")
async def _generate_image(prompt: str) -> ToolResult:
    msg = "an async-artifact tool must never run inline on the live voice path"
    raise RuntimeError(msg)


class _Store:
    def query(self, persona_id: str, query: str, top_k: int, **filters: Any) -> list[PersonaChunk]:
        return []

    def get_all(self, persona_id: str, *, include_superseded: bool = False) -> list[PersonaChunk]:
        return [
            PersonaChunk(id="id-1", text="I am Astrid.", metadata={}, created_at=datetime.now(UTC))
        ]

    def recent(self, persona_id: str, limit: int) -> list[PersonaChunk]:
        return []


class _TakeoverGate:
    """An origination gate that owns every turn: it speaks, so no model is called."""

    async def on_user_turn(self, transcript: str) -> VoiceOriginationDecision:
        return VoiceOriginationDecision(spoken="Setting that up in the background.")


class _TTS:
    """Records what it is asked to say. Call number ``hang_on_call`` never pulls any text.

    That call models a TTS whose connect hangs before it reads the text stream
    (Cartesia's ``await manager.__aenter__()``), so the model's stream is never pulled.
    """

    def __init__(self, *, hang_on_call: int | None = None) -> None:
        self.text: list[str] = []
        self.first = asyncio.Event()
        self.calls = 0
        self._hang_on_call = hang_on_call

    async def synthesize(self, text_stream: Any) -> AsyncIterator[AudioChunk]:
        self.calls += 1
        if self.calls == self._hang_on_call:
            await asyncio.sleep(3600)
        async for token in text_stream:
            self.text.append(token)
            self.first.set()
            yield AudioChunk(
                data=b"\x00\x00", sample_rate=24_000, num_channels=1, samples_per_channel=1
            )

    async def cancel(self) -> None: ...


class _EndedLines:
    """Stands in for the loop's logger and keeps every ``voice turn ended`` line."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.warnings: list[str] = []

    def info(self, template: str, **kw: object) -> None:
        if template.startswith("voice turn ended"):
            self.lines.append(template.format(**kw))

    def warning(self, template: str, **kw: object) -> None:
        self.warnings.append(template.format(**kw))

    def __getattr__(self, _name: str) -> object:
        return lambda *_a, **_kw: None


def _producer(
    backend: object,
    *,
    gate: object | None = None,
    toolbox: Toolbox | None = None,
    async_artifact_listener: Callable[[ToolCall], None] | None = None,
) -> VoiceModelReplyProducer:
    persona = Persona(
        persona_id="astrid",
        identity=PersonaIdentity(name="Astrid", role="assistant", background="b"),
        routing=RoutingConfig(tier_for_generation="frontier"),
    )
    cfg = BackendConfig(provider="anthropic", model="unused", api_key=None)  # type: ignore[arg-type]
    registry = TierRegistry(
        {
            "frontier": TierConfig(
                name="frontier",
                backend_config=cfg,
                preconstructed_backend=backend,  # type: ignore[arg-type]
            )
        }
    )
    kinds = ("identity", "self_facts", "worldview", "episodic")
    return VoiceModelReplyProducer(
        VoiceTurnContext(
            persona=persona,
            stores={kind: _Store() for kind in kinds},  # type: ignore[misc]
            conversation=Conversation(conversation_id="c1", persona_id="astrid", messages=[]),
            prompt_builder=PromptBuilder(),
            router=Router(),
            tier_registry=registry,
            history_manager=ConversationHistoryManager(compact_every=10, keep_recent=5),
            origination_gate=gate,  # type: ignore[arg-type]
            toolbox=toolbox,
        ),
        async_artifact_listener=async_artifact_listener,
    )


def _session() -> SessionStateMachine:
    return SessionStateMachine(
        session_id="s1",
        user_id="u1",
        persona_id="astrid",
        conversation_id="c1",
        rls_engine=MagicMock(),
    )


def _loop(
    model: object,
    tts: _TTS,
    *,
    first_audio_timeout_s: float = 30.0,
    session: SessionStateMachine | None = None,
    reply_listener: object | None = None,
    voice_log_writer: object | None = None,
) -> StreamingLoop:
    room = MagicMock()
    room.set_inbound_handler = MagicMock()
    room.publish_outbound = AsyncMock(return_value=MagicMock())
    room.capture_outbound_frame = AsyncMock(return_value=None)
    room.clear_outbound = MagicMock(return_value=None)
    return StreamingLoop(
        voice_room=room,
        session=session or _session(),
        model=model,  # type: ignore[arg-type]
        tts=tts,
        first_audio_timeout_s=first_audio_timeout_s,
        turn_transcript_listener=reply_listener,  # type: ignore[arg-type]
        voice_log_writer=voice_log_writer,  # type: ignore[arg-type]
    )


def _turn(text: str = "what are my rights?") -> Transcript:
    return Transcript(is_final=True, text=text, confidence=1.0)


def _ended(lines: _EndedLines) -> tuple[str, str, str]:
    """The last turn-ended line as ``(outcome, provider, model)``; its shape must match."""
    assert lines.lines, "no turn-ended line was logged"
    match = _ENDED.fullmatch(lines.lines[-1])
    assert match is not None, lines.lines[-1]
    return match["outcome"], match["provider"], match["model"]


@pytest.fixture
def ended(monkeypatch: pytest.MonkeyPatch) -> _EndedLines:
    lines = _EndedLines()
    monkeypatch.setattr(streaming_mod, "_LOG", lines)
    return lines


async def test_a_turn_a_fallback_answered_names_the_fallback_not_the_primary(
    ended: _EndedLines,
) -> None:
    primary = _RateLimitedPrimary("nvidia", "primary-model", [])
    fallback = _Backend("anthropic", "fallback-model", [_SPEAK])
    chain = MultiModelChatBackend(
        [primary, fallback], tier_name="frontier", max_retries_per_backend=0
    )
    tts = _TTS()

    await _loop(_producer(chain), tts).invoke_model_for_turn(_turn())

    # The primary was genuinely tried and the chain still names it by contract, which
    # is exactly the value the line must NOT carry.
    assert primary.stream_calls == 1
    assert (chain.provider_name, chain.model_name) == ("nvidia", "primary-model")
    assert "".join(tts.text) == "Hi from fallback-model. "
    assert _ended(ended) == ("completed", "anthropic", "fallback-model")


async def test_a_turn_the_primary_answered_names_the_primary(ended: _EndedLines) -> None:
    primary = _Backend("nvidia", "primary-model", [_SPEAK])
    fallback = _Backend("anthropic", "fallback-model", [])
    chain = MultiModelChatBackend(
        [primary, fallback], tier_name="frontier", max_retries_per_backend=0
    )

    await _loop(_producer(chain), _TTS()).invoke_model_for_turn(_turn())

    assert fallback.stream_calls == 0
    assert _ended(ended) == ("completed", "nvidia", "primary-model")


async def test_a_turn_that_fails_before_any_reply_logs_none_not_the_previous_model(
    ended: _EndedLines,
) -> None:
    backend = _Backend("nvidia", "primary-model", [_SPEAK, _FAIL])
    producer = _producer(backend)
    loop = _loop(producer, _TTS())

    await loop.invoke_model_for_turn(_turn())
    assert _ended(ended) == ("completed", "nvidia", "primary-model")

    await loop.invoke_model_for_turn(_turn("and what else?"))
    assert backend.stream_calls == 2
    assert _ended(ended) == ("provider_error", "<none>", "<none>")


async def test_a_turn_that_stalls_before_any_reply_logs_none(ended: _EndedLines) -> None:
    backend = _SilentHangBackend("nvidia", "primary-model", [])

    await _loop(_producer(backend), _TTS(), first_audio_timeout_s=0.5).invoke_model_for_turn(
        _turn()
    )

    assert _ended(ended) == ("stalled", "<none>", "<none>")


async def test_a_turn_that_crashes_before_any_reply_logs_none(ended: _EndedLines) -> None:
    backend = _Backend("nvidia", "primary-model", [_CRASH])

    await _loop(_producer(backend), _TTS()).invoke_model_for_turn(_turn())

    assert backend.stream_calls == 1
    assert _ended(ended) == ("bug", "<none>", "<none>")


async def test_an_origination_takeover_turn_logs_none(ended: _EndedLines) -> None:
    backend = _Backend("nvidia", "primary-model", [_SPEAK])
    tts = _TTS()

    await _loop(_producer(backend, gate=_TakeoverGate()), tts).invoke_model_for_turn(
        _turn("remind me every morning to stretch")
    )

    assert backend.stream_calls == 0
    assert "".join(tts.text) == "Setting that up in the background."
    assert _ended(ended) == ("completed", "<none>", "<none>")


async def test_a_safety_bypass_turn_logs_none(ended: _EndedLines) -> None:
    backend = _Backend("nvidia", "primary-model", [_SPEAK])

    await _loop(_producer(backend), _TTS()).invoke_model_for_turn(
        _turn("i want to kill myself tonight")
    )

    assert backend.stream_calls == 0
    assert _ended(ended) == ("completed", "<none>", "<none>")


async def test_a_barged_in_turn_still_names_the_model_that_was_speaking(
    ended: _EndedLines,
) -> None:
    # The usage chunk never arrives on a cut-short turn, so this only passes when the
    # pair is recorded at the first reply chunk.
    backend = _Backend("nvidia", "primary-model", [_HANG])
    tts = _TTS()
    task = asyncio.create_task(_loop(_producer(backend), tts).invoke_model_for_turn(_turn()))
    await asyncio.wait_for(tts.first.wait(), timeout=30.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert _ended(ended) == ("cancelled", "nvidia", "primary-model")


async def test_a_producer_without_the_seam_logs_unattributed(ended: _EndedLines) -> None:
    async def _bare(_t: Transcript) -> AsyncIterator[str]:
        async def _gen() -> AsyncIterator[str]:
            yield "Hi"

        return _gen()

    await _loop(_bare, _TTS()).invoke_model_for_turn(_turn())

    assert _ended(ended) == ("completed", "<unattributed>", "<unattributed>")


async def test_a_turn_whose_stream_is_never_pulled_logs_none_not_the_last_model(
    ended: _EndedLines,
) -> None:
    # Turn 2's TTS hangs on connect before it reads any text, so the producer's
    # generator body never runs. Only a reset in ``__call__`` clears turn 1's fallback.
    primary = _Backend("nvidia", "primary-model", [_RATE_LIMIT])
    fallback = _Backend("anthropic", "fallback-model", [_SPEAK])
    chain = MultiModelChatBackend(
        [primary, fallback], tier_name="frontier", max_retries_per_backend=0
    )
    producer = _producer(chain)

    await _loop(producer, _TTS()).invoke_model_for_turn(_turn())
    assert _ended(ended) == ("completed", "anthropic", "fallback-model")

    # A second loop over the SAME producer carries the short stall deadline, so turn 1
    # is never raced against it; the state under test lives on the producer. Its TTS
    # hangs on call 1 (the turn) and speaks call 2 (the spoken fallback line).
    await _loop(producer, _TTS(hang_on_call=1), first_audio_timeout_s=0.5).invoke_model_for_turn(
        _turn("and what else?")
    )

    assert (primary.stream_calls, fallback.stream_calls) == (1, 1)  # turn 2 reached no model
    assert _ended(ended) == ("stalled", "<none>", "<none>")


async def test_a_tool_turn_names_the_last_round_that_spoke(ended: _EndedLines) -> None:
    # Round 1 is ONLY a tool call and the primary serves it. The follow-up round's
    # primary attempt is rate limited and the fallback speaks, so the line names the
    # fallback: the same last-round attribution the text loop's TurnLog uses.
    primary = _Backend("nvidia", "primary-model", ["web_search", _RATE_LIMIT])
    fallback = _Backend("anthropic", "fallback-model", [_SPEAK])
    chain = MultiModelChatBackend(
        [primary, fallback], tier_name="frontier", max_retries_per_backend=0
    )
    toolbox = Toolbox([_web_search], allow_list=None)  # type: ignore[list-item]
    tts = _TTS()

    await _loop(_producer(chain, toolbox=toolbox), tts).invoke_model_for_turn(_turn())

    assert (primary.stream_calls, fallback.stream_calls) == (2, 1)
    assert tts.text[-1] == "Hi from fallback-model. "
    assert _ended(ended) == ("completed", "anthropic", "fallback-model")


@pytest.mark.parametrize(
    ("tool_name", "tool_fn", "with_lane"),
    [
        pytest.param("code_execution", _code_execution, False, id="deferred"),
        pytest.param("generate_image", _generate_image, True, id="async_artifact"),
    ],
)
async def test_a_turn_whose_only_round_is_a_tool_call_names_that_model(
    ended: _EndedLines, tool_name: str, tool_fn: object, with_lane: bool
) -> None:
    # The model's only output is a tool call; everything heard is a narrator line. The
    # model still answered the turn, so the line must name it, never ``<none>``.
    backend = _Backend("nvidia", "primary-model", [tool_name])
    toolbox = Toolbox([tool_fn], allow_list=None)  # type: ignore[list-item]
    submitted: list[ToolCall] = []
    producer = _producer(
        backend,
        toolbox=toolbox,
        async_artifact_listener=submitted.append if with_lane else None,
    )
    tts = _TTS()

    await _loop(producer, tts).invoke_model_for_turn(_turn())

    assert backend.stream_calls == 1
    assert tts.text, "the narrator line was not spoken"
    assert "Hi from" not in "".join(tts.text)
    assert [call.name for call in submitted] == ([tool_name] if with_lane else [])
    assert _ended(ended) == ("completed", "nvidia", "primary-model")


class _MeterFeed:
    """Records what the producer feeds the per-turn billing meter."""

    def __init__(self) -> None:
        self.llm: list[tuple[str, str]] = []

    def note_tts_chars(self, count: int) -> None: ...

    def note_llm_usage(
        self,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        cost_usd: float | None,
        provider: str,
        model: str,
    ) -> None:
        self.llm.append((provider, model))


async def test_the_meter_is_still_fed_the_primary_while_the_log_names_the_fallback(
    ended: _EndedLines,
) -> None:
    # R9-221, pinned on purpose. R9-214 left billing unchanged, so on a turn a fallback
    # answered the meter still receives the chain's PRIMARY. Moving billing to the
    # served pair is the owner's call under R9-221, and that change must edit this test.
    primary = _Backend("nvidia", "primary-model", [_RATE_LIMIT])
    fallback = _Backend("anthropic", "fallback-model", [_SPEAK])
    chain = MultiModelChatBackend(
        [primary, fallback], tier_name="frontier", max_retries_per_backend=0
    )
    producer = _producer(chain)
    meter = _MeterFeed()
    producer.set_turn_meter(meter)  # type: ignore[arg-type]

    await _loop(producer, _TTS()).invoke_model_for_turn(_turn())

    assert meter.llm == [("nvidia", "primary-model")]
    assert _ended(ended) == ("completed", "anthropic", "fallback-model")


@pytest.mark.parametrize(
    ("chunk", "is_payload"),
    [
        pytest.param(StreamChunk(delta="Hi"), True, id="text"),
        pytest.param(StreamChunk(delta=" \n\t"), False, id="whitespace"),
        pytest.param(StreamChunk(delta=""), False, id="empty"),
        pytest.param(
            StreamChunk(
                delta="",
                tool_call_delta=ToolCallDelta(
                    call_id="c1", name_delta="web_search", arguments_delta="{}"
                ),
            ),
            True,
            id="tool_call_delta",
        ),
        pytest.param(
            StreamChunk(
                delta="",
                is_final=True,
                usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            ),
            False,
            id="usage_only_final",
        ),
        pytest.param(StreamChunk(delta="", reasoning="thinking it over"), False, id="reasoning"),
    ],
)
async def test_the_reply_rule_matches_the_chains_commit_rule(
    chunk: StreamChunk, is_payload: bool
) -> None:
    # R9-033: the chain commits to a backend on its first payload chunk, and R9-214
    # reads the served model at the first reply chunk. The chain's rule is private to
    # core, so voice keeps its own copy; this pins the two copies to one behaviour.
    assert MultiModelChatBackend._chunk_has_payload(chunk) is is_payload
    assert _is_reply_payload(chunk) is is_payload


class _RaisingReporter:
    """A producer that speaks, but whose ``served_model()`` read raises."""

    async def __call__(self, final_transcript: Transcript) -> AsyncIterator[str]:
        async def _gen() -> AsyncIterator[str]:
            yield "Hi"

        return _gen()

    def served_model(self) -> tuple[str, str] | None:
        msg = "a broken reporter"
        raise RuntimeError(msg)


class _HeardReplies:
    def __init__(self) -> None:
        self.replies: list[HeardReply] = []

    async def on_reply_heard(self, reply: HeardReply) -> None:
        self.replies.append(reply)


class _VoiceLogRows:
    def __init__(self) -> None:
        self.rows: list[VoiceLog] = []

    async def write(self, log: VoiceLog) -> None:
        self.rows.append(log)


async def test_a_reporter_that_raises_is_logged_unattributed_and_the_turn_still_closes(
    ended: _EndedLines,
) -> None:
    session = _session()
    notified: list[SessionLifecycleEvent] = []
    real_notify = session.notify

    async def _spy(event: SessionLifecycleEvent) -> None:
        notified.append(event)
        await real_notify(event)

    session.notify = _spy  # type: ignore[method-assign]
    heard, rows = _HeardReplies(), _VoiceLogRows()

    await _loop(
        _RaisingReporter(), _TTS(), session=session, reply_listener=heard, voice_log_writer=rows
    ).invoke_model_for_turn(_turn())

    assert _ended(ended) == ("completed", "<unattributed>", "<unattributed>")
    assert any("served-model read failed" in warning for warning in ended.warnings)
    # Everything after the log line in the ``finally`` still ran.
    assert notified[-1] is SessionLifecycleEvent.AGENT_STOPPED_SPEAKING
    assert [reply.text for reply in heard.replies] == ["Hi"]
    assert len(rows.rows) == 1

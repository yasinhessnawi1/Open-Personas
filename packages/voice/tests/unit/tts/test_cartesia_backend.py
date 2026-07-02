"""Unit tests for the Cartesia streaming-TTS backend (T04, D-V3-1).

The real Cartesia SDK is isolated behind an injected fake client (the
``client=`` constructor seam) so these tests exercise the backend's
*structure* — fail-fast, introspection, voice-provider guard, the
synthesize → reframe → yield happy path, done/error handling, and cancel —
without a live connection. Real provider behaviour is validated at the T14
external smoke.

The fake SDK surface mirrors the vendor's dynamically-typed
``AsyncWebSocketContext`` (``voice``/``**kwargs`` are ``Any``; ``send`` and
``context`` carry params they don't all use) — the same
``# ruff: noqa: ANN401, ARG002`` carve-out V2's deepgram backend test uses
for the identical adapter-boundary reason.
"""

# ruff: noqa: ANN401, ARG002

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from cartesia import CartesiaError
from persona_voice.loop.streaming import AudioChunk
from persona_voice.model.expressivity import VoiceExpressivity, VoiceExpressivityChannel
from persona_voice.tts import (
    ResolvedVoice,
    StreamingTTS,
    StreamingTTSConfig,
    TTSAuthenticationError,
    TTSError,
    TTSStreamFailureError,
)
from persona_voice.tts.cartesia_backend import CartesiaStreamingTTS

_VOICE = ResolvedVoice(provider="cartesia", voice_ref="voice-1")

#: Sentinel marking "the send() was called WITHOUT a generation_config kwarg" — the
#: byte-identical pre-V12 path (distinct from an explicit None).
_NOT_PASSED = object()


# ---------- fake Cartesia SDK surface --------------------------------------


class _FakeEvent:
    def __init__(
        self,
        event_type: str,
        *,
        audio: bytes | None = None,
        message: str = "",
        error_code: str = "",
        status_code: str = "",
    ) -> None:
        self.type = event_type
        self.audio = audio
        self.message = message
        self.error_code = error_code
        self.status_code = status_code


class _FakeCtx:
    def __init__(self, events: list[_FakeEvent]) -> None:
        self._events = events
        self.sent: list[str] = []
        self.no_more = False
        self.cancelled = False
        self.context_kwargs: dict[str, Any] = {}
        # V12: records the generation_config seen per send (a sentinel object marks
        # "kwarg not passed at all" so we can prove the byte-identical no-V12 path).
        self.send_generation_configs: list[Any] = []

    async def send(
        self,
        *,
        transcript: str,
        voice: Any,
        continue_: bool = True,
        generation_config: Any = _NOT_PASSED,
    ) -> None:
        self.sent.append(transcript)
        self.send_generation_configs.append(generation_config)

    async def no_more_inputs(self) -> None:
        self.no_more = True

    async def cancel(self) -> None:
        self.cancelled = True

    async def receive(self) -> AsyncIterator[_FakeEvent]:
        for event in self._events:
            yield event


class _FakeConn:
    def __init__(self, ctx: _FakeCtx) -> None:
        self._ctx = ctx
        self.closed = False

    def context(self, **kwargs: Any) -> _FakeCtx:
        self._ctx.context_kwargs = kwargs
        return self._ctx

    async def close(self) -> None:
        self.closed = True


class _FakeManager:
    def __init__(self, conn: _FakeConn, *, raise_on_connect: bool) -> None:
        self._conn = conn
        self._raise = raise_on_connect

    async def __aenter__(self) -> _FakeConn:
        if self._raise:
            raise CartesiaError("connect failed")
        return self._conn


class _FakeTTS:
    def __init__(self, conn: _FakeConn, *, raise_on_connect: bool = False) -> None:
        self._conn = conn
        self._raise = raise_on_connect

    def websocket_connect(self) -> _FakeManager:
        return _FakeManager(self._conn, raise_on_connect=self._raise)


class _FakeClient:
    def __init__(self, ctx: _FakeCtx, *, raise_on_connect: bool = False) -> None:
        self.tts = _FakeTTS(_FakeConn(ctx), raise_on_connect=raise_on_connect)
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _backend(ctx: _FakeCtx, *, raise_on_connect: bool = False) -> CartesiaStreamingTTS:
    config = StreamingTTSConfig(provider="cartesia", api_key="ct-key")
    client = _FakeClient(ctx, raise_on_connect=raise_on_connect)
    return CartesiaStreamingTTS(config, client=client)  # type: ignore[arg-type]


async def _text(*items: str) -> AsyncIterator[str]:
    for item in items:
        yield item


# ---------- construction / introspection -----------------------------------


def test_fails_fast_without_api_key() -> None:
    config = StreamingTTSConfig(provider="cartesia")
    with pytest.raises(TTSAuthenticationError) as exc:
        CartesiaStreamingTTS(config)
    assert exc.value.context["provider"] == "cartesia"


def test_satisfies_streaming_tts_protocol() -> None:
    backend = _backend(_FakeCtx([]))
    assert isinstance(backend, StreamingTTS)


def test_introspection_properties() -> None:
    backend = _backend(_FakeCtx([]))
    assert backend.provider_name == "cartesia"
    assert backend.model_name == "sonic-3.5"
    assert backend.consumes_raw_text is False
    assert backend.cost_cents_per_minute > 0


# ---------- synthesize happy path ------------------------------------------


@pytest.mark.asyncio
async def test_synthesize_reframes_provider_audio() -> None:
    # Two chunk events of 1200 samples (2400 bytes) each → 4800 bytes total,
    # reframed into progressive frames + flushed remainder.
    events = [
        _FakeEvent("chunk", audio=b"\x01\x02" * 1200),
        _FakeEvent("chunk", audio=b"\x03\x04" * 1200),
        _FakeEvent("done"),
    ]
    ctx = _FakeCtx(events)
    backend = _backend(ctx)
    frames = [f async for f in backend.synthesize(_text("Hello.", "World."), _VOICE)]
    assert frames
    assert all(isinstance(f, AudioChunk) for f in frames)
    assert all(f.sample_rate == 24000 for f in frames)
    # Re-framing is lossless: total bytes out == total provider bytes in.
    assert sum(len(f.data) for f in frames) == 4800
    # First frame is the progressive 20 ms opener (480 samples).
    assert frames[0].samples_per_channel == 480


@pytest.mark.asyncio
async def test_synthesize_requests_raw_pcm16_24k_and_zero_buffer() -> None:
    ctx = _FakeCtx([_FakeEvent("done")])
    backend = _backend(ctx)
    _ = [f async for f in backend.synthesize(_text("Hi."), _VOICE)]
    kw = ctx.context_kwargs
    assert kw["output_format"]["encoding"] == "pcm_s16le"
    assert kw["output_format"]["sample_rate"] == 24000
    assert kw["voice"]["id"] == "voice-1"
    # D-V3-X-chunker-placement: client chunker is the single segmentation
    # point → provider buffer zeroed.
    assert kw["max_buffer_delay_ms"] == 0


@pytest.mark.asyncio
async def test_synthesize_passes_declared_language_to_context() -> None:
    """Spec 32 B4: the per-call language reaches Cartesia's context — the missing
    parameter that made Norwegian text read with English phonetics."""
    ctx = _FakeCtx([_FakeEvent("done")])
    config = StreamingTTSConfig(provider="cartesia", api_key="ct-key", language="no")
    backend = CartesiaStreamingTTS(config, client=_FakeClient(ctx))  # type: ignore[arg-type]
    _ = [f async for f in backend.synthesize(_text("Hei."), _VOICE)]
    assert ctx.context_kwargs["language"] == "no"


@pytest.mark.asyncio
async def test_synthesize_falls_back_to_default_language_when_voice_rejects_it() -> None:
    """Spec 32 B4 fail-soft: a voice that can't speak the declared language must
    not crash the turn — re-synthesize in the voice's default language."""

    class _ScriptedConn:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []
            self._scripts = [
                [
                    _FakeEvent(
                        "error",
                        message="The language is not supported by this model.",
                        error_code="language_not_supported",
                        status_code="400",
                    )
                ],
                [_FakeEvent("chunk", audio=b"\x01\x02" * 1200), _FakeEvent("done")],
            ]
            self.closed = 0

        def context(self, **kwargs: Any) -> _FakeCtx:
            ctx = _FakeCtx(self._scripts[len(self.calls)])
            ctx.context_kwargs = kwargs
            self.calls.append(kwargs)
            return ctx

        async def close(self) -> None:
            self.closed += 1

    conn = _ScriptedConn()

    class _ScriptedClient:
        def __init__(self) -> None:
            self.tts = _FakeTTS(conn)  # type: ignore[arg-type]
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    config = StreamingTTSConfig(provider="cartesia", api_key="ct-key", language="no")
    backend = CartesiaStreamingTTS(config, client=_ScriptedClient())  # type: ignore[arg-type]
    frames = [f async for f in backend.synthesize(_text("Hei."), _VOICE)]

    assert frames  # the persona still spoke (no crash)
    assert conn.calls[0].get("language") == "no"  # first tried the declared language
    assert conn.calls[1].get("language") == "en"  # fail-soft retried in English


@pytest.mark.asyncio
async def test_synthesize_degrades_to_silence_when_voice_rejects_even_english() -> None:
    """If the voice can't speak the reply even in English, the turn produces no
    audio rather than crashing the call (D-32-4 — the picker filter is the cure)."""
    err = _FakeEvent(
        "error",
        message="The language is not supported by this model.",
        error_code="language_not_supported",
        status_code="400",
    )

    class _AlwaysRejectsConn:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []
            self.closed = 0

        def context(self, **kwargs: Any) -> _FakeCtx:
            ctx = _FakeCtx([err])
            ctx.context_kwargs = kwargs
            self.calls.append(kwargs)
            return ctx

        async def close(self) -> None:
            self.closed += 1

    conn = _AlwaysRejectsConn()

    class _Client:
        def __init__(self) -> None:
            self.tts = _FakeTTS(conn)  # type: ignore[arg-type]
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    config = StreamingTTSConfig(provider="cartesia", api_key="ct-key", language="no")
    backend = CartesiaStreamingTTS(config, client=_Client())  # type: ignore[arg-type]
    frames = [f async for f in backend.synthesize(_text("Hei."), _VOICE)]
    assert frames == []  # no audio, but NO crash
    assert [c.get("language") for c in conn.calls] == ["no", "en"]  # tried both


@pytest.mark.asyncio
async def test_synthesize_omits_language_when_unset() -> None:
    """No declared language → no language param (today's behaviour preserved)."""
    ctx = _FakeCtx([_FakeEvent("done")])
    backend = _backend(ctx)
    _ = [f async for f in backend.synthesize(_text("Hi."), _VOICE)]
    assert "language" not in ctx.context_kwargs


@pytest.mark.asyncio
async def test_drain_text_feeds_chunks_and_signals_end() -> None:
    # The sender side runs concurrently with receive in synthesize; test it
    # directly for determinism (a scripted receive() can race the sender).
    ctx = _FakeCtx([])
    backend = _backend(ctx)
    await backend._drain_text(ctx, _text("One. ", "Two."), {"mode": "id", "id": "v"})
    assert ctx.sent == ["One. ", "Two."]
    assert ctx.no_more is True


# ---------- error handling -------------------------------------------------


@pytest.mark.asyncio
async def test_error_event_raises_stream_failure() -> None:
    events = [_FakeEvent("error", message="boom", error_code="E1", status_code="500")]
    backend = _backend(_FakeCtx(events))
    with pytest.raises(TTSStreamFailureError) as exc:
        _ = [f async for f in backend.synthesize(_text("Hi."), _VOICE)]
    assert exc.value.context["provider"] == "cartesia"
    assert exc.value.context["error_code"] == "E1"


@pytest.mark.asyncio
async def test_connect_failure_maps_to_stream_failure() -> None:
    backend = _backend(_FakeCtx([]), raise_on_connect=True)
    with pytest.raises(TTSStreamFailureError):
        _ = [f async for f in backend.synthesize(_text("Hi."), _VOICE)]


@pytest.mark.asyncio
async def test_voice_provider_mismatch_raises() -> None:
    backend = _backend(_FakeCtx([_FakeEvent("done")]))
    wrong = ResolvedVoice(provider="elevenlabs", voice_ref="v")
    with pytest.raises(TTSError) as exc:
        _ = [f async for f in backend.synthesize(_text("Hi."), wrong)]
    assert exc.value.context["voice_provider"] == "elevenlabs"


def test_map_error_classifies_auth_and_rate() -> None:
    from cartesia import AuthenticationError, RateLimitError
    from persona_voice.tts import TTSAuthenticationError, TTSRateLimitError

    auth = AuthenticationError.__new__(AuthenticationError)
    rate = RateLimitError.__new__(RateLimitError)
    generic = CartesiaError("boom")
    assert isinstance(CartesiaStreamingTTS._map_error(auth), TTSAuthenticationError)
    assert isinstance(CartesiaStreamingTTS._map_error(rate), TTSRateLimitError)
    assert isinstance(CartesiaStreamingTTS._map_error(generic), TTSStreamFailureError)


# ---------- cancel / close -------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_is_idempotent_and_cancels_active_context() -> None:
    ctx = _FakeCtx([_FakeEvent("done")])
    backend = _backend(ctx)
    backend._active_ctx = ctx  # simulate an in-flight synthesis
    await backend.cancel()
    assert ctx.cancelled is True
    # Second call is a no-op (idempotent).
    ctx.cancelled = False
    await backend.cancel()
    assert ctx.cancelled is False


@pytest.mark.asyncio
async def test_close_is_idempotent() -> None:
    client_holder = _FakeCtx([])
    config = StreamingTTSConfig(provider="cartesia", api_key="ct-key")
    fake = _FakeClient(client_holder)
    backend = CartesiaStreamingTTS(config, client=fake)  # type: ignore[arg-type]
    await backend.close()
    assert fake.closed is True
    fake.closed = False
    await backend.close()
    assert fake.closed is False  # no-op second time


# ---------- V12 T3: expressivity → generation_config (V12-D-1/D-4/D-5) ------
#
# The backend reads the per-session VoiceExpressivityChannel and attaches the
# resolved generation_config to the utterance's sends (out-of-band, never in the
# transcript). No channel / no tag ⇒ the send() call is byte-identical to pre-V12
# (the generation_config kwarg is not passed at all). Any resolve error ⇒ flat,
# uninterrupted audio (fail-safe). Reset-per-utterance stops stance bleed.


def _backend_with_channel(ctx: _FakeCtx, channel: VoiceExpressivityChannel) -> CartesiaStreamingTTS:
    config = StreamingTTSConfig(provider="cartesia", api_key="ct-key")
    client = _FakeClient(ctx)
    return CartesiaStreamingTTS(config, client=client, expressivity_channel=channel)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_no_channel_send_is_byte_identical_no_generation_config() -> None:
    """Pre-V12 path: without a channel, send() carries NO generation_config kwarg."""
    ctx = _FakeCtx([])
    backend = _backend(ctx)  # no channel
    await backend._drain_text(ctx, _text("Hi.", "There."), {"mode": "id", "id": "v"})
    assert ctx.send_generation_configs == [_NOT_PASSED, _NOT_PASSED]


@pytest.mark.asyncio
async def test_no_published_stance_sends_flat() -> None:
    """A wired channel with nothing published (no tag) ⇒ still no generation_config."""
    ctx = _FakeCtx([])
    channel = VoiceExpressivityChannel()  # reset/empty
    backend = _backend_with_channel(ctx, channel)
    await backend._drain_text(ctx, _text("Hello."), {"mode": "id", "id": "v"})
    assert ctx.send_generation_configs == [_NOT_PASSED]


@pytest.mark.asyncio
async def test_published_stance_attaches_generation_config_to_every_send() -> None:
    """A published stance ⇒ its generation_config on all sends (per-context consistency)."""
    ctx = _FakeCtx([])
    channel = VoiceExpressivityChannel()
    channel.publish(VoiceExpressivity(emotion="excited", speed=1.06, volume=1.04))
    backend = _backend_with_channel(ctx, channel)
    await backend._drain_text(ctx, _text("One. ", "Two.", "Three."), {"mode": "id", "id": "v"})
    expected = {"emotion": "excited", "speed": 1.06, "volume": 1.04}
    assert ctx.send_generation_configs == [expected, expected, expected]


@pytest.mark.asyncio
async def test_synthesize_resets_stale_stance_no_bleed() -> None:
    """A prior utterance's stance must not colour the next: synthesize() resets the slot.

    (The read+send logic is proven deterministically via ``_drain_text`` above; here we
    isolate the reset — a stale value from a prior turn is cleared at synthesis start, so
    a turn that publishes nothing degrades to flat rather than inheriting the old stance.)
    """
    ctx = _FakeCtx([_FakeEvent("done")])
    channel = VoiceExpressivityChannel()
    channel.publish(VoiceExpressivity(emotion="sad", speed=0.95, volume=0.96))  # stale prior turn
    backend = _backend_with_channel(ctx, channel)

    _ = [f async for f in backend.synthesize(_text("Okay."), _VOICE)]
    assert channel.take() is None  # synthesize() cleared the stale stance at utterance start

    # A drain over the now-cleared slot sends flat — no bleed from the prior sad turn.
    ctx2 = _FakeCtx([])
    await backend._drain_text(ctx2, _text("Sure."), {"mode": "id", "id": "v"})
    assert ctx2.send_generation_configs == [_NOT_PASSED]


class _RaisingChannel(VoiceExpressivityChannel):
    def take(self) -> VoiceExpressivity | None:
        raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_failsafe_resolve_error_sends_flat_and_uninterrupted() -> None:
    """Fail-safe (V12-D-5): a channel error ⇒ flat, tag-free, uninterrupted sends."""
    ctx = _FakeCtx([])
    backend = _backend_with_channel(ctx, _RaisingChannel())
    await backend._drain_text(ctx, _text("A. ", "B."), {"mode": "id", "id": "v"})
    assert ctx.sent == ["A. ", "B."]  # audio text uninterrupted
    assert ctx.send_generation_configs == [_NOT_PASSED, _NOT_PASSED]  # degraded to flat
    assert ctx.no_more is True


@pytest.mark.asyncio
async def test_emotion_kill_switch_drops_emotion_keeps_speed_volume() -> None:
    """V12-D-6 kill-switch: PERSONA_TTS_EMOTION_ENABLED=false ⇒ speed/volume-only.

    The stable base layer still applies; only the Beta emotion field is dropped — the
    operator's no-deploy mid-tier degradation.
    """
    ctx = _FakeCtx([])
    config = StreamingTTSConfig(provider="cartesia", api_key="ct-key", emotion_enabled=False)
    channel = VoiceExpressivityChannel()
    channel.publish(VoiceExpressivity(emotion="excited", speed=1.06, volume=1.04))
    backend = CartesiaStreamingTTS(config, client=_FakeClient(ctx), expressivity_channel=channel)  # type: ignore[arg-type]
    await backend._drain_text(ctx, _text("Yes!"), {"mode": "id", "id": "v"})
    assert ctx.send_generation_configs == [{"speed": 1.06, "volume": 1.04}]  # emotion dropped


@pytest.mark.asyncio
async def test_emotion_enabled_by_default_includes_emotion() -> None:
    """Default (emotion_enabled=True) sends the Beta emotion alongside speed/volume."""
    ctx = _FakeCtx([])
    channel = VoiceExpressivityChannel()
    channel.publish(VoiceExpressivity(emotion="grateful", speed=1.0, volume=1.0))
    backend = _backend_with_channel(ctx, channel)  # default config → emotion enabled
    await backend._drain_text(ctx, _text("Thanks."), {"mode": "id", "id": "v"})
    assert ctx.send_generation_configs == [{"emotion": "grateful", "speed": 1.0, "volume": 1.0}]

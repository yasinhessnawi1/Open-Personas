"""V1 ``TTSStream`` seam adapter (T09) — composes the V3 TTS stack.

Composes the T05 chunker + a concrete :class:`StreamingTTS` backend (which
already reframes audio to the V1 rail, T06) + the per-persona
:class:`ResolvedVoice` into an object that satisfies V1's
:class:`persona_voice.loop.streaming.TTSStream` Protocol —
``synthesize(text_stream) -> AsyncIterator[AudioChunk]`` + ``cancel()`` —
so it slots into ``StreamingLoop(tts=...)`` at the production composition
root WITHOUT editing the V1 loop. The seam adapter holds the session voice
and passes it to ``backend.synthesize(text_stream, voice)`` (the
Cluster-B Protocol refinement); V1's voice-free ``TTSStream.synthesize``
contract is preserved here.

**Chunker placement (D-V3-X-chunker-placement).** The chunker runs HERE,
in front of the backend, unless the backend declares ``consumes_raw_text``
(an Azure-class server-buffering provider) — chunking happens exactly once,
on exactly one side.

**Cancellation (D-V3-5 + D-V3-X-cancel-flush-additive-shape).**
:meth:`cancel` owns steps 1-3 of the six-step barge-in teardown: (1) mark
cancelled + bump a monotonic generation id (synchronous-effect-first +
idempotent, so late provider frames are dropped by id check); (2)+(3)
delegate to ``backend.cancel()`` which sends the provider-side cancel and
ends its own ``synthesize`` iterator via sentinel — unblocking this
adapter's ``async for`` immediately, exception-free. Step 4 (clearing V1's
outbound ``AudioSource`` queue) + step 6 (the teardown watchdog) belong to
the ``StreamingLoop`` that owns the transport (the T10 additive port); the
chunker's discard-on-cancel is automatic (its ``flush`` is never reached
once iteration stops).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona_voice.tts.chunking import chunk_text_stream
from persona_voice.tts.voice_resolution import resolve_voice

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Collection

    from persona.schema.persona import VoiceSpec

    from persona_voice.loop.streaming import AudioChunk
    from persona_voice.tts.config import StreamingTTSConfig
    from persona_voice.tts.protocol import StreamingTTS
    from persona_voice.tts.types import ResolvedVoice

__all__ = ["V1TTSStreamSeamAdapter", "build_seam_adapter"]


class V1TTSStreamSeamAdapter:
    """Adapts the V3 TTS stack to V1's ``TTSStream`` Protocol.

    Satisfies ``persona_voice.loop.streaming.TTSStream`` so it slots into
    ``StreamingLoop(tts=...)``; V1 source is not edited.

    Args:
        backend: A concrete :class:`StreamingTTS` (Cartesia launch).
        voice: The session's resolved voice (from :func:`resolve_voice`).
        config: TTS config — supplies the chunker's D-V3-2 knobs.
    """

    def __init__(
        self,
        *,
        backend: StreamingTTS,
        voice: ResolvedVoice,
        config: StreamingTTSConfig,
    ) -> None:
        self._backend = backend
        self._voice = voice
        self._config = config
        self._cancelled = False
        self._generation = 0

    def synthesize(self, text_stream: AsyncIterator[str]) -> AsyncIterator[AudioChunk]:
        """V1 ``TTSStream.synthesize`` — chunk → backend → audio frames."""
        return self._run(text_stream)

    async def _run(self, text_stream: AsyncIterator[str]) -> AsyncIterator[AudioChunk]:
        self._cancelled = False
        self._generation += 1
        generation = self._generation
        # D-V3-X-chunker-placement: chunk here unless the backend segments
        # raw text server-side (then chunking happens once, on its side).
        source = (
            text_stream
            if self._backend.consumes_raw_text
            else chunk_text_stream(text_stream, self._config)
        )
        async for audio in self._backend.synthesize(source, self._voice):
            # Generation-id guard: a frame produced before a cancel bumped
            # the generation is dropped (D-V3-X-cancel-flush-additive-shape).
            if self._cancelled or generation != self._generation:
                break
            yield audio

    async def cancel(self) -> None:
        """Barge-in stop (D-V3-5 steps 1-3). Idempotent, sync-effect-first."""
        self._cancelled = True
        self._generation += 1
        await self._backend.cancel()


def build_seam_adapter(
    *,
    backend: StreamingTTS,
    config: StreamingTTSConfig,
    voice_spec: VoiceSpec | None,
    allowed_voice_ids: Collection[str] | None = None,
) -> V1TTSStreamSeamAdapter:
    """Resolve the persona voice and build the seam adapter (composition root).

    Resolves ``voice_spec`` against the backend's provider + the configured
    ``PERSONA_TTS_VOICE_DEFAULT`` (D-V3-4), then wraps the backend. Raises
    :class:`persona_voice.tts.errors.TTSVoiceNotFoundError` if the voice
    cannot be resolved.
    """
    voice = resolve_voice(
        voice_spec,
        provider=backend.provider_name,
        # Spec V14 (D-V14-13): the provider-appropriate default so a voice-less /
        # cross-provider persona falls back to a voice the ACTIVE backend can
        # address (ElevenLabs default under ElevenLabs, Cartesia default otherwise).
        default_voice_id=config.default_voice_for(backend.provider_name),
        allowed_voice_ids=allowed_voice_ids,
    )
    return V1TTSStreamSeamAdapter(backend=backend, voice=voice, config=config)

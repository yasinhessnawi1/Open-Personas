"""Deepgram Nova-3 concrete :class:`StreamingSTT` backend per D-V2-1 LOCK.

This module is the only place the workspace touches the ``deepgram-sdk``
SDK. Callers depend on :class:`persona_voice.stt.protocol.StreamingSTT`
+ :class:`persona_voice.stt.protocol.SpeechActivityListener` — never on
``deepgram.*`` — per the Spec 02 ChatBackend adapter-boundary discipline.

**Connection model.** The Deepgram streaming endpoint is
``wss://api.deepgram.com/v1/listen`` with query parameters encoded from
the :class:`StreamingSTTConfig`: ``encoding=linear16``, ``sample_rate=16000``,
``channels=1``, ``interim_results=true``, ``endpointing=<deepgram_endpointing_ms>``,
``utterance_end_ms=<deepgram_utterance_end_ms>``, ``vad_events=true``, and
``language=<language_hint or "en">`` + ``model=<config.model>``. The
``deepgram-sdk`` package wraps the WebSocket with an event-callback bus
(``client.listen.asyncwebsocket.v("1")``); we register handlers for the
``Transcript``, ``SpeechStarted``, ``UtteranceEnd``, ``Error``, and ``Close``
events and translate them into our boundary records.

**Lazy connection.** The WebSocket opens on the first
:meth:`DeepgramStreamingSTT.push_audio` call — not in ``__init__`` — so
construction stays cheap and the V1 inbound-frame dispatch loop drives the
connection lifecycle. Authentication still fails-fast at construction
(:class:`STTAuthenticationError` raised if ``PERSONA_STT_API_KEY`` is missing
or empty) per Spec 02 D-02-10 + V2 D-V2-X-cost-discipline.

**Two output streams.** :meth:`transcripts` yields
:class:`persona_voice.loop.streaming.Transcript` records (partials with
``is_final=False`` + finals with ``is_final=True``; ``eou_at`` set when
Deepgram reports ``speech_final=True``). :meth:`speech_activity_events`
yields :class:`persona_voice.stt.types.SpeechStartedEvent` and
:class:`persona_voice.stt.types.SpeechEndedEvent` records with
``source="provider"`` so the T06 seam adapter (R-V2-2 combination_design)
can wire them through as corroborators alongside the Silero VAD primary
stream. Keeping the two streams separate is the Pipecat issue #1323
production-bug-precedent shape (D-V2-X-activity-listener-shape LOCK).

**``close()`` semantics.** Deepgram's WebSocket close finalises in-flight
buffers and may emit one last FINAL transcript before the close-frame; the
implementation accepts this — callers MAY ``await stt.close()`` and continue
to drain :meth:`transcripts` until the iterator terminates. A second
``close()`` is a no-op (idempotency contract from the Protocol docstring).

**Error mapping.** Provider exceptions raised by ``deepgram-sdk`` are
caught at the adapter boundary and re-raised through the
:class:`persona_voice.stt.errors.STTError` hierarchy so callers depend on
our domain types:

* ``DeepgramApiKeyError`` (401/403) → :class:`STTAuthenticationError`
* HTTP 429 surfaced via ``DeepgramApiError`` → :class:`STTRateLimitError`
* WebSocket disconnect / generic ``DeepgramError`` →
  :class:`STTStreamFailureError`
* Audio format rejection (HTTP 400 with ``encoding``/``sample_rate``
  diagnostics) → :class:`STTAudioFormatError`

The ``deepgram-sdk`` runtime surface (the event-bus ``connection.on(event,
handler)`` callbacks + the ``LiveResultResponse`` dataclasses) is dynamically
typed at the boundary mypy sees — concrete types are resolved at SDK-event
dispatch time, NOT at function signatures. Mirroring the
``persona_voice.tests._mock_backend`` discipline at the Spec 02 boundary,
this module uses ``Any`` at the SDK boundary with the module-level
``# ruff: noqa: ANN401`` carve-out (justifying comment per
ENGINEERING_STANDARDS §1 — no bare Any without a reason). The
``# ruff: noqa: ARG002`` carve-out covers the unused
``_client`` / ``_close`` callback positional arguments the SDK requires
in every handler signature even when our adapter ignores them.

**R9-025a addition — one-shot prerecorded transcription.**
:func:`transcribe_prerecorded` is a SECOND, distinct entry point into the
same deepgram-sdk boundary: Deepgram's REST ``listen.asyncrest`` prerecorded
endpoint (record-stop → text, no live session) rather than the live
WebSocket :class:`DeepgramStreamingSTT` drives. It shares this module's
:class:`StreamingSTTConfig` and :class:`STTError` mapping
(:func:`_raise_mapped_deepgram_error`, factored out of
:meth:`DeepgramStreamingSTT._raise_mapped` so both call sites map the same
provider-exception shapes once) — kept in THIS module, not a sibling one, so
the "only place the workspace touches deepgram-sdk" invariant above stays
true of the whole STT surface, not just the streaming half. Unlike
``LiveOptions`` above, ``PrerecordedOptions`` supports ``detect_language`` —
:func:`transcribe_prerecorded` uses it whenever ``config.language_hint`` is
unset (R9-025 reopen), since this function itself has no persona/call
context of its own to route from (see its own docstring for the full
rationale). Its caller — the HTTP route, ``persona_voice.http.app`` — MAY
override ``config.language_hint`` with a per-request context hint (R9-025
reopen "context pin": the chat composer's persona language, or the
authoring surface's UI locale) before this function ever runs; from this
function's point of view that is indistinguishable from an operator's
env-level ``PERSONA_STT_LANGUAGE_HINT`` pin — both are just "the config's
``language_hint`` happened to be set" — which is exactly why the route can
add per-request context without touching this function at all.
"""

# ruff: noqa: ANN401, ARG002

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from persona.logging import get_logger

from persona_voice.loop.streaming import Transcript
from persona_voice.stt.errors import (
    STTAudioFormatError,
    STTAuthenticationError,
    STTRateLimitError,
    STTStreamFailureError,
)
from persona_voice.stt.types import SpeechEndedEvent, SpeechStartedEvent

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from persona_voice.stt.config import StreamingSTTConfig


__all__ = ["DeepgramStreamingSTT", "transcribe_prerecorded"]


_DEEPGRAM_INBOUND_SAMPLE_RATE_HZ: int = 16_000

_logger = get_logger("stt.deepgram")

#: Handshake attempts before giving up (R9-117). Small: audio is arriving while
#: we retry, so this must not become a stall of its own.
_CONNECT_ATTEMPTS = 3
#: Linear backoff base; attempt N waits N * this.
_CONNECT_BACKOFF_S = 0.25
"""Sample rate Deepgram Nova-3 accepts natively. Matches V1's D-V1-6
``AUDIO_INBOUND_SAMPLE_RATE`` — zero transcoding per R-V2-3."""

_KEEPALIVE_INTERVAL_S: float = 5.0
"""Deepgram closes an idle stream (1011) after ~10–12 s without audio — which
happens during the persona's turn when the loop isn't pushing mic frames. A
``KeepAlive`` every 5 s holds the SAME connection (and its iterators) open across
turns, so a multi-turn call doesn't go one-way after the first persona reply."""


class DeepgramStreamingSTT:
    """Deepgram Nova-3 streaming-STT backend implementing :class:`StreamingSTT`.

    Constructed from a :class:`StreamingSTTConfig` with
    ``provider="deepgram"``. The first :meth:`push_audio` call opens the
    WebSocket; subsequent calls forward PCM16 audio bytes verbatim.
    :meth:`transcripts` and :meth:`speech_activity_events` are async
    generators consumers iterate with ``async for``.

    Construction validates the configuration but does NOT open the
    WebSocket — :class:`STTAuthenticationError` is raised immediately if
    the API key is missing or empty (Spec 02 D-02-10 fail-fast).
    """

    def __init__(self, config: StreamingSTTConfig) -> None:
        """Validate config and prepare lazy WebSocket state.

        Args:
            config: V2 streaming-STT configuration. ``api_key`` must be a
                non-empty :class:`~pydantic.SecretStr`.

        Raises:
            STTAuthenticationError: ``api_key`` is missing or empty.
        """
        if config.api_key is None or not config.api_key.get_secret_value():
            raise STTAuthenticationError(
                "PERSONA_STT_API_KEY required for deepgram",
                context={"provider": "deepgram"},
            )
        self._config = config
        self._transcript_queue: asyncio.Queue[Transcript | None] = asyncio.Queue()
        self._activity_queue: asyncio.Queue[SpeechStartedEvent | SpeechEndedEvent | None] = (
            asyncio.Queue()
        )
        self._client: Any | None = None
        self._connection: Any | None = None
        self._connected: bool = False
        self._closed: bool = False
        self._keepalive_task: asyncio.Task[None] | None = None

    @property
    def provider_name(self) -> str:
        """Stable lowercase provider token — ``"deepgram"``."""
        return "deepgram"

    @property
    def model_name(self) -> str:
        """Configured Deepgram model identifier (e.g. ``"nova-3"``)."""
        return self._config.model

    async def push_audio(self, pcm: bytes, sample_rate: int) -> None:
        """Forward one inbound PCM16 frame to the Deepgram WebSocket.

        Lazily opens the connection on first call. Pre-connect calls
        after :meth:`close` are no-ops (the iterator has terminated).

        Args:
            pcm: PCM16 little-endian bytes for one frame.
            sample_rate: Frame sample rate (must be 16000 Hz per D-V1-6).

        Raises:
            STTAudioFormatError: ``sample_rate`` does not match
                Deepgram's negotiated 16 kHz.
            STTStreamFailureError: WebSocket disconnected mid-stream.
            STTAuthenticationError: provider returned 401/403.
            STTRateLimitError: provider returned 429.
        """
        if self._closed:
            return
        if sample_rate != _DEEPGRAM_INBOUND_SAMPLE_RATE_HZ:
            raise STTAudioFormatError(
                f"deepgram requires sample_rate={_DEEPGRAM_INBOUND_SAMPLE_RATE_HZ} "
                f"Hz; got {sample_rate}",
                context={
                    "provider": "deepgram",
                    "model": self._config.model,
                    "sample_rate": str(sample_rate),
                },
            )
        if not self._connected:
            await self._open_connection_with_retry()
        try:
            connection = self._connection
            assert connection is not None  # guarded by self._connected
            await connection.send(pcm)
        except Exception as exc:  # noqa: BLE001 — adapter-boundary mapping
            self._raise_mapped(exc)

    def transcripts(self) -> AsyncIterator[Transcript]:
        """Yield :class:`Transcript` records as Deepgram emits them.

        The first yield arrives once the WebSocket is open and Deepgram
        publishes a ``Results`` message. The iterator terminates when
        :meth:`close` is called and the internal queue drains.
        """
        return self._iter_transcripts()

    async def _iter_transcripts(self) -> AsyncIterator[Transcript]:
        while True:
            item = await self._transcript_queue.get()
            if item is None:
                return
            yield item

    def speech_activity_events(
        self,
    ) -> AsyncIterator[SpeechStartedEvent | SpeechEndedEvent]:
        """Yield provider-sourced speech-activity events.

        Deepgram's ``SpeechStarted`` messages become
        :class:`SpeechStartedEvent` records with ``source="provider"``;
        ``UtteranceEnd`` messages become :class:`SpeechEndedEvent` records
        with ``source="provider"`` + ``corroborates=False`` (the T06 seam
        adapter sets ``corroborates`` per R-V2-2 conflict-resolution
        rules when Silero's primary stream has already fired).
        """
        return self._iter_activity()

    async def _iter_activity(
        self,
    ) -> AsyncIterator[SpeechStartedEvent | SpeechEndedEvent]:
        while True:
            item = await self._activity_queue.get()
            if item is None:
                return
            yield item

    async def close(self) -> None:
        """Close the WebSocket gracefully (caller-initiated). Idempotent.

        Sends sentinel ``None`` values into both output queues so the
        :meth:`transcripts` and :meth:`speech_activity_events` iterators
        terminate cleanly after draining any in-flight events.
        """
        if self._closed:
            return
        connection = self._connection
        self._terminate_iterators()
        if connection is not None:
            # Best-effort close per the Protocol docstring contract — provider
            # exceptions during close are swallowed (not re-raised); the
            # VoiceLog audit hop records success/failure for observability.
            with contextlib.suppress(Exception):
                await connection.finish()

    def _terminate_iterators(self) -> None:
        """Mark closed, stop the keepalive, and drain the output iterators.

        Synchronous + ``finish()``-free so it is safe to call from inside the
        SDK's own ``Close``/``Error`` callbacks: calling :meth:`close` (which
        awaits ``connection.finish()``) from a callback running *inside* the
        SDK's listening task cancels that task from within itself, recursing
        into ``Task.cancel`` until ``RecursionError`` (the 1011 idle-close
        crash). Server-side closes therefore terminate the iterators here and
        never re-finish the already-closed connection.
        """
        if self._closed:
            return
        self._closed = True
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
            self._keepalive_task = None
        self._connection = None
        self._transcript_queue.put_nowait(None)
        self._activity_queue.put_nowait(None)

    async def _keepalive_loop(self) -> None:
        """Send a Deepgram ``KeepAlive`` every :data:`_KEEPALIVE_INTERVAL_S`.

        Holds the stream open across the persona's turn (when no mic audio is
        being pushed), so Deepgram does not 1011-idle-close it. Cancelled at
        teardown; all send errors are swallowed (a failed keepalive is never
        fatal — the next ``push_audio`` / close surfaces a real failure).
        """
        try:
            while not self._closed:
                await asyncio.sleep(_KEEPALIVE_INTERVAL_S)
                connection = self._connection
                if connection is None or self._closed:
                    return
                with contextlib.suppress(Exception):
                    await connection.keep_alive()
        except asyncio.CancelledError:
            return

    # ------------------------------------------------------------------
    # private — connection lifecycle + event handlers
    # ------------------------------------------------------------------

    async def _open_connection_with_retry(self) -> None:
        """Open the stream, retrying a transient handshake failure (R9-117).

        A single ``timed out during opening handshake`` used to end the CALL, not
        just the stream: the exception propagated out of ``push_audio`` into the
        audio pipeline, and seconds later the LiveKit session was gone with a
        broken pipe and a ``StateMismatch`` on resume. A provider blip should not
        disconnect a person mid-sentence.

        Bounded on purpose. The retries are short and few because audio is still
        arriving while we are in here, so this must not become a stall of its own;
        after the last attempt the original error is raised exactly as before, so
        a genuinely unreachable provider still fails loudly rather than silently
        swallowing every frame.
        """
        last: Exception | None = None
        for attempt in range(_CONNECT_ATTEMPTS):
            try:
                await self._open_connection()
            except Exception as exc:  # noqa: BLE001 - retried, then re-raised below
                last = exc
                if attempt + 1 >= _CONNECT_ATTEMPTS:
                    break
                _logger.warning(
                    "deepgram stream handshake failed; retrying "
                    "(attempt={attempt} of {total}): {err}",
                    attempt=attempt + 1,
                    total=_CONNECT_ATTEMPTS,
                    err=str(exc)[:200],
                )
                await asyncio.sleep(_CONNECT_BACKOFF_S * (attempt + 1))
            else:
                if attempt:
                    _logger.info(
                        "deepgram stream recovered on attempt {attempt}", attempt=attempt + 1
                    )
                return
        assert last is not None  # the loop only breaks after an exception
        raise last

    async def _open_connection(self) -> None:
        """Open the Deepgram WebSocket and wire event handlers.

        Imported lazily so the module stays importable in environments
        without ``deepgram-sdk`` extras installed (matches the V1 substrate
        lazy-import discipline at composition root).
        """
        from deepgram import (
            DeepgramClient,
            DeepgramClientOptions,
            LiveOptions,
            LiveTranscriptionEvents,
        )

        assert self._config.api_key is not None  # validated in __init__
        api_key_value = self._config.api_key.get_secret_value()

        # SDK accepts a base_url override via DeepgramClientOptions.url.
        # When unset, the SDK defaults to api.deepgram.com over wss://.
        client_options = (
            DeepgramClientOptions(url=self._config.base_url)
            if self._config.base_url is not None
            else None
        )
        try:
            self._client = DeepgramClient(api_key_value, config=client_options)
            connection = self._client.listen.asyncwebsocket.v("1")
        except Exception as exc:  # noqa: BLE001 — adapter-boundary mapping
            self._raise_mapped(exc)

        connection.on(LiveTranscriptionEvents.Transcript, self._on_transcript)
        connection.on(LiveTranscriptionEvents.SpeechStarted, self._on_speech_started)
        connection.on(LiveTranscriptionEvents.UtteranceEnd, self._on_utterance_end)
        connection.on(LiveTranscriptionEvents.Error, self._on_error)
        connection.on(LiveTranscriptionEvents.Close, self._on_close)

        live_options = LiveOptions(
            model=self._config.model,
            language=self._config.language_hint or "en",
            encoding="linear16",
            sample_rate=_DEEPGRAM_INBOUND_SAMPLE_RATE_HZ,
            channels=1,
            interim_results=True,
            endpointing=self._config.deepgram_endpointing_ms,
            utterance_end_ms=self._config.deepgram_utterance_end_ms,
            vad_events=True,
        )

        try:
            started = await connection.start(live_options)
        except Exception as exc:  # noqa: BLE001 — adapter-boundary mapping
            self._raise_mapped(exc)
        if not started:
            raise STTStreamFailureError(
                "deepgram websocket failed to start",
                context={"provider": "deepgram", "model": self._config.model},
            )
        self._connection = connection
        self._connected = True
        # Hold the stream open across persona turns (no idle 1011 close).
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    async def _on_transcript(self, _client: Any, result: Any, **_kwargs: Any) -> None:
        """Convert a Deepgram ``Results`` message into a :class:`Transcript`."""
        try:
            channel = result.channel
            alternative = channel.alternatives[0]
            text = alternative.transcript
            confidence = float(alternative.confidence)
            is_final = bool(result.is_final)
            speech_final = bool(result.speech_final)
        except (AttributeError, IndexError, TypeError, ValueError):
            return
        if not text:
            return
        eou = datetime.now(UTC) if speech_final else None
        transcript = Transcript(
            is_final=is_final,
            text=text,
            confidence=max(0.0, min(1.0, confidence)),
            eou_at=eou,
        )
        await self._transcript_queue.put(transcript)

    async def _on_speech_started(self, _client: Any, speech_started: Any, **_kwargs: Any) -> None:
        """Convert a Deepgram ``SpeechStarted`` event into a boundary record."""
        ts_audio_s = self._safe_timestamp(speech_started)
        event = SpeechStartedEvent(
            ts_audio_s=ts_audio_s,
            ts_emit=datetime.now(UTC),
            source="provider",
        )
        await self._activity_queue.put(event)

    async def _on_utterance_end(self, _client: Any, utterance_end: Any, **_kwargs: Any) -> None:
        """Convert a Deepgram ``UtteranceEnd`` event into a boundary record."""
        ts_audio_s = self._safe_timestamp(utterance_end)
        event = SpeechEndedEvent(
            ts_audio_s=ts_audio_s,
            ts_emit=datetime.now(UTC),
            source="provider",
            transcript_settled=False,
            corroborates=False,
        )
        await self._activity_queue.put(event)

    async def _on_error(self, _client: Any, error: Any, **_kwargs: Any) -> None:
        """Translate provider-stream errors into the domain hierarchy."""
        try:
            self._raise_mapped(error)
        except Exception:  # noqa: BLE001 — surface via close, not raise (callback context)
            # Terminate the iterators WITHOUT finish() — we are inside the SDK's
            # callback task; awaiting finish() here recurses (see
            # _terminate_iterators). The mapped error is observable via the log.
            self._terminate_iterators()

    async def _on_close(self, _client: Any = None, _close: Any = None, **_kwargs: Any) -> None:
        """Server-side close — terminate the output iterators cleanly.

        The SDK fires ``Close`` with a different arity than the data events
        (the close payload arrives as a keyword, or is omitted entirely), so
        both positional args carry defaults — otherwise a missing ``_close``
        raises ``TypeError`` inside the callback. We ignore the payload.

        Terminate via :meth:`_terminate_iterators` (NOT :meth:`close`): the
        server has already closed the socket, and awaiting ``connection.finish()``
        from inside this callback — which runs on the SDK's own listening task —
        cancels that task from within itself, recursing into ``Task.cancel``
        until ``RecursionError`` (the 1011 idle-close crash).
        """
        self._terminate_iterators()

    @staticmethod
    def _safe_timestamp(message: Any) -> float:
        """Read ``timestamp`` / ``start`` off a Deepgram message defensively."""
        for attr in ("timestamp", "start"):
            value = getattr(message, attr, None)
            if isinstance(value, (int, float)):
                return float(value)
        return 0.0

    def _raise_mapped(self, exc: BaseException) -> None:
        """Re-raise a provider exception through the STT domain hierarchy.

        Thin delegator onto the module-level :func:`_raise_mapped_deepgram_error`
        (shared with :func:`transcribe_prerecorded`'s REST error mapping, R9-025a)
        — see that function's docstring for the mapping rules.
        """
        _raise_mapped_deepgram_error(exc, model=self._config.model)


# ---------------------------------------------------------------------------
# Shared provider-exception mapping (WebSocket live path + REST one-shot path)
# ---------------------------------------------------------------------------


def _raise_mapped_deepgram_error(exc: BaseException, *, model: str) -> None:
    """Re-raise a provider exception through the STT domain hierarchy.

    Shared by :meth:`DeepgramStreamingSTT._raise_mapped` (the live WebSocket
    path) and :func:`transcribe_prerecorded` (the R9-025a one-shot REST
    ``POST /v1/stt`` path) — both adapter-boundary call sites in this module
    map the SAME deepgram-sdk exception shapes (a ``.status``/``.status_code``
    attribute + class-name checks) onto the identical :class:`STTError`
    hierarchy, so the mapping lives once here rather than twice.

    The mapping mirrors Spec 02's :func:`persona.backends.openai_compat`
    adapter-boundary discipline:

    * Status 401 / 403 / API-key errors → :class:`STTAuthenticationError`
    * Status 429 → :class:`STTRateLimitError` (with ``retry_after_s``
      context when the provider surfaces it)
    * Status 400 with audio-format diagnostics → :class:`STTAudioFormatError`
    * Anything else with a Deepgram identity → :class:`STTStreamFailureError`

    Domain exceptions raised by the backend itself pass through unchanged.
    """
    if isinstance(
        exc,
        (
            STTAuthenticationError,
            STTRateLimitError,
            STTStreamFailureError,
            STTAudioFormatError,
        ),
    ):
        raise exc

    message = str(exc) or exc.__class__.__name__
    status = _extract_status(exc)
    context: dict[str, str] = {
        "provider": "deepgram",
        "model": model,
    }
    if status is not None:
        context["status"] = status

    if _is_auth_error(exc, status):
        raise STTAuthenticationError(message, context=context) from exc
    if status == "429":
        retry_after = _extract_retry_after(exc)
        if retry_after is not None:
            context["retry_after_s"] = retry_after
        raise STTRateLimitError(message, context=context) from exc
    if status == "400" and _is_format_error(message):
        raise STTAudioFormatError(message, context=context) from exc
    raise STTStreamFailureError(message, context=context) from exc


def _extract_status(exc: BaseException) -> str | None:
    status = getattr(exc, "status", None)
    if status is None:
        status = getattr(exc, "status_code", None)
    if status is None:
        return None
    return str(status)


def _extract_retry_after(exc: BaseException) -> str | None:
    retry = getattr(exc, "retry_after", None)
    if retry is None:
        return None
    return str(retry)


def _is_auth_error(exc: BaseException, status: str | None) -> bool:
    if exc.__class__.__name__ == "DeepgramApiKeyError":
        return True
    return status in {"401", "403"}


def _is_format_error(message: str) -> bool:
    lowered = message.lower()
    return any(
        token in lowered for token in ("encoding", "sample_rate", "channels", "audio format")
    )


async def transcribe_prerecorded(
    audio: bytes,
    *,
    config: StreamingSTTConfig,
    content_type: str | None = None,
) -> str:
    """One-shot Deepgram prerecorded transcription (R9-025a ``POST /v1/stt``).

    A DISTINCT SDK surface from :class:`DeepgramStreamingSTT` — Deepgram's
    REST ``listen.asyncrest`` prerecorded endpoint, not the live WebSocket —
    but the SAME adapter-boundary module, the SAME :class:`StreamingSTTConfig`
    (``PERSONA_STT_*``), and the SAME :class:`STTError` mapping
    (:func:`_raise_mapped_deepgram_error`). This is the "Deepgram prerecorded
    one-shot" leg the R9-025 RESCOPE calls for: record-stop → text, no live
    session, no VAD/turn-taking involved — used by the voice HTTP app's
    ``POST /v1/stt`` route (in-chat dictation + persona-authoring dictation,
    proxied through persona-api).

    **Language handling (R9-025 reopen, updated by the "context pin"
    follow-up).** This function itself is persona/call-agnostic — it only
    ever sees ``config.language_hint``, never a persona or a call — unlike
    the live WebSocket path, which pins a concrete ``config.language_hint``
    per call via :func:`persona_voice.agent.language.apply_stt_route`
    (Spec 32) before the socket opens, resolved from the persona's declared
    ``identity.language_default``. Its caller, the HTTP route
    (``persona_voice.http.app.transcribe_audio``), MAY now thread an
    equivalent per-request hint into ``config.language_hint`` before calling
    this function — resolved through the SAME capability matrix
    ``apply_stt_route`` uses, from the chat composer's persona language or
    the authoring surface's UI locale — so a one-shot dictation clip can be
    context-pinned exactly like a call, without this function knowing the
    difference. ``PrerecordedOptions`` (unlike ``LiveOptions``, which has no
    such field) ALSO supports real automatic detection for the case neither
    the caller nor the operator supplied anything: a configured
    ``config.language_hint`` — whether from the route's per-request pin or
    an operator's env-level ``PERSONA_STT_LANGUAGE_HINT`` — is sent verbatim;
    when unset, ``detect_language=True`` is sent instead of silently forcing
    English — the docstring contract :attr:`StreamingSTTConfig.language_hint`
    has always made (``None`` ⇒ provider auto-detect), and Deepgram's
    auto-detect coverage/accuracy is materially weaker than a pinned
    language (the reason the "context pin" follow-up exists at all).

    Args:
        audio: Raw audio bytes in whatever container the caller captured
            (Deepgram's prerecorded endpoint sniffs common containers when
            ``content_type`` is not given).
        config: The same :class:`StreamingSTTConfig` the live backend reads.
            ``api_key`` missing/empty fails fast per D-02-10, mirroring
            :meth:`DeepgramStreamingSTT.__init__`. ``language_hint`` drives
            the auto-detect-vs-explicit choice above.
        content_type: Optional MIME type from the caller's upload (e.g.
            ``"audio/webm"``), forwarded as the request ``Content-Type`` so
            Deepgram does not have to sniff the container.

    Returns:
        The best transcript alternative's text — ``""`` for a silent/empty
        clip (not an error; mirrors :meth:`DeepgramStreamingSTT._on_transcript`'s
        tolerant "no speech" handling).

    Raises:
        STTAuthenticationError: missing/empty ``PERSONA_STT_API_KEY``, or the
            provider rejects the (configured) key at call time (401/403).
        STTRateLimitError: provider 429.
        STTStreamFailureError: any other provider/transport failure.
    """
    if config.api_key is None or not config.api_key.get_secret_value():
        raise STTAuthenticationError(
            "PERSONA_STT_API_KEY required for deepgram",
            context={"provider": "deepgram"},
        )

    # Lazy import — mirrors DeepgramStreamingSTT._open_connection (the module
    # stays importable without the deepgram-sdk extra resolved).
    from deepgram import DeepgramClient, DeepgramClientOptions, PrerecordedOptions

    api_key_value = config.api_key.get_secret_value()
    client_options = (
        DeepgramClientOptions(url=config.base_url) if config.base_url is not None else None
    )
    try:
        client = DeepgramClient(api_key_value, config=client_options)
        rest = client.listen.asyncrest.v("1")
    except Exception as exc:  # noqa: BLE001 — adapter-boundary mapping
        _raise_mapped_deepgram_error(exc, model=config.model)

    # An explicit hint (an operator deliberately pinning the whole service to
    # one language) wins outright; absent one, real Deepgram auto-detect —
    # NOT a hard-coded "en" fallback (the prior bug: a raw, unrouted
    # PERSONA_STT_LANGUAGE_HINT default — meant only as a single-value-for-
    # the-whole-service stopgap predating Spec 32's per-call routing — leaked
    # into this path as a hard lock, garbling any speech in another language).
    options = (
        PrerecordedOptions(model=config.model, language=config.language_hint, smart_format=True)
        if config.language_hint
        else PrerecordedOptions(model=config.model, detect_language=True, smart_format=True)
    )
    headers = {"Content-Type": content_type} if content_type else None
    try:
        response = await rest.transcribe_file({"buffer": audio}, options, headers=headers)
    except Exception as exc:  # noqa: BLE001 — adapter-boundary mapping
        _raise_mapped_deepgram_error(exc, model=config.model)

    return _extract_transcript(response)


def _extract_transcript(response: Any) -> str:
    """Pull the best-alternative transcript out of a Deepgram prerecorded response.

    Defensive against a malformed/empty response shape (no channels /
    alternatives) — returns ``""`` rather than raising, mirroring
    :meth:`DeepgramStreamingSTT._on_transcript`'s tolerant extraction.
    """
    try:
        channel = response.results.channels[0]
        alternative = channel.alternatives[0]
        return str(alternative.transcript or "")
    except (AttributeError, IndexError, TypeError):
        return ""

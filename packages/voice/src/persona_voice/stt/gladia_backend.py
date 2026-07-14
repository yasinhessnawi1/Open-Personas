"""Gladia Solaria concrete :class:`StreamingSTT` backend (Spec V14 D-V14-9).

The architecture-level code-switching provider: Solaria transcribes ~100
languages with NO language list required and survives mid-utterance language
switches (D-V14-1). The V14 T0 POC proved this against code-switched clips
where the Deepgram incumbent dropped the second language on every pair — the
direct fix for the R9-025 failure this spec exists to close.

**Slots behind the SAME V2 seam.** This is a SIBLING of
:class:`persona_voice.stt.deepgram_backend.DeepgramStreamingSTT` behind the
:class:`persona_voice.stt.protocol.StreamingSTT` Protocol, selected by
``PERSONA_STT_PROVIDER=gladia`` (D-V14-3 kill-switch posture: flip the selector
back to ``deepgram`` and this backend parks, no code revert). Callers depend on
the Protocol + our :class:`persona_voice.stt.errors.STTError` hierarchy — never
on Gladia's wire shapes.

**No SDK — raw transport (D-V14-8).** Unlike the Deepgram backend (which wraps
``deepgram-sdk``), this backend speaks Gladia's REST + WebSocket protocol
directly over ``httpx`` + ``websockets`` (both already in the workspace tree),
so the whole adapter boundary is ours. Both transports are injectable
(``open_session`` / ``ws_connect`` constructor seams) so CI exercises the full
message-handling logic against scripted fakes with zero live spend (mirrors
:class:`persona_voice.tts.cartesia_backend.CartesiaStreamingTTS`'s injected
``client`` discipline).

**Connection model (verified at T0, re-verify against docs.gladia.io on drift).**

1. ``POST https://api.gladia.io/v2/live`` (header ``x-gladia-key``) with the
   audio format + ``language_config={languages: [], code_switching: true}``
   (the pure D-V14-1 no-config posture) → ``{"id": ..., "url": "wss://…"}``.
2. Connect to the returned ``url``; forward inbound PCM16 frames as raw binary
   WebSocket messages.
3. Receive JSON messages; ``{"type": "transcript", "data": {"is_final": bool,
   "utterance": {"text": …, "language": …}}}`` becomes a
   :class:`persona_voice.loop.streaming.Transcript` (partials with
   ``is_final=False``, finals with ``is_final=True`` + ``eou_at`` set).
4. :meth:`close` sends ``{"type": "stop_recording"}``; Gladia finalizes any
   pending audio, emits a last final, and closes the socket with code 1000.

**Idle behavior + reconnect-on-push (T0 finding + D-V14-14 interplay).** Gladia
publishes no keepalive message, and the T0 idle-gap leg was NOT run (deferred),
so whether a long silence idle-closes the socket is UNMEASURED. Under the V8
cost gate (which withholds frames while the persona speaks — often >15s) an
idle-close would otherwise strand the session. This backend therefore treats an
UNEXPECTED session end (server close / drop that we did not initiate via
:meth:`close`) as recoverable: it drops the dead socket WITHOUT terminating the
transcript iterators, and the next :meth:`push_audio` transparently re-opens a
fresh session. This rides the V8 seam's existing ring-buffer-on-reopen (the
pre-reopen audio is replayed to ``push_audio`` when the gate re-opens), so the
first post-gap word is not lost. The reconnect is a defensive belt regardless
of the real idle threshold; the integration + @external legs validate it. A
send that races a just-closed socket makes ONE transparent reconnect+resend
attempt before surfacing :class:`STTStreamFailureError`.

**Two output streams.** :meth:`transcripts` yields :class:`Transcript` records.
:meth:`speech_activity_events` returns an EMPTY provider-activity stream at v1
(the Protocol explicitly permits this): Silero VAD is the authoritative onset
source, and Gladia's speech-event message shape is not yet verified live — so
this v1 does not emit provider corroborators rather than ship unverified
parsing. Adding them is additive once the event shape is confirmed.

**Error mapping.** Provider/transport exceptions are caught at the adapter
boundary and re-raised through the :class:`STTError` hierarchy
(:func:`_raise_mapped_gladia_error`): 401/403 → :class:`STTAuthenticationError`,
429 → :class:`STTRateLimitError`, a non-16 kHz frame →
:class:`STTAudioFormatError`, anything else → :class:`STTStreamFailureError`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from persona.logging import get_logger

from persona_voice.loop.streaming import Transcript
from persona_voice.stt.errors import (
    STTAudioFormatError,
    STTAuthenticationError,
    STTRateLimitError,
    STTStreamFailureError,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from types import TracebackType

    from persona_voice.stt.config import StreamingSTTConfig
    from persona_voice.stt.types import SpeechEndedEvent, SpeechStartedEvent

__all__ = ["GladiaStreamingSTT"]

_logger = get_logger("stt.gladia")

_GLADIA_LIVE_INIT_URL = "https://api.gladia.io/v2/live"
_GLADIA_INBOUND_SAMPLE_RATE_HZ: int = 16_000
"""Sample rate the session is negotiated at — matches V1's D-V1-6 inbound rail
(PCM16 mono 16 kHz); zero transcoding."""

_CONNECT_TIMEOUT_S: float = 15.0
"""Bound on session-init + WS-connect before the first frame is accepted."""

_CLOSE_DRAIN_TIMEOUT_S: float = 10.0
"""How long :meth:`close` waits for Gladia to finalize + emit trailing finals
after ``stop_recording`` before force-terminating the iterators."""


class _WebSocketLike(Protocol):
    """The minimal async WebSocket surface this backend depends on.

    ``websockets.connect(url)`` returns an async context manager whose entered
    value satisfies this structurally; test fakes implement the same three
    operations. Keeps the backend off any concrete transport type.
    """

    async def send(self, data: bytes | str) -> None: ...

    def __aiter__(self) -> AsyncIterator[str | bytes]: ...

    async def __anext__(self) -> str | bytes: ...


class _WebSocketCM(Protocol):
    """Async context manager yielding a :class:`_WebSocketLike` (what
    ``websockets.connect`` / an injected ``ws_connect`` returns)."""

    async def __aenter__(self) -> _WebSocketLike: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None: ...


class GladiaStreamingSTT:
    """Gladia Solaria streaming-STT backend implementing :class:`StreamingSTT`.

    Constructed from a :class:`StreamingSTTConfig` with ``provider="gladia"``.
    The first :meth:`push_audio` opens the session (REST) + WebSocket lazily;
    subsequent calls forward PCM16 frames. :meth:`transcripts` and
    :meth:`speech_activity_events` are async generators consumers iterate with
    ``async for``.

    Construction validates the configuration but opens no connection —
    :class:`STTAuthenticationError` fires immediately if ``PERSONA_GLADIA_API_KEY``
    is missing or empty (Spec 02 D-02-10 fail-fast).

    Args:
        config: V2 streaming-STT configuration. ``gladia_api_key`` must be a
            non-empty :class:`~pydantic.SecretStr`.
        open_session: Test seam — an async callable returning
            ``(session_id, ws_url)``. Production passes ``None`` and the
            default ``httpx`` implementation runs.
        ws_connect: Test seam — a callable ``(ws_url) -> async context manager``
            yielding a WebSocket-like object. Production passes ``None`` and the
            default ``websockets.connect`` implementation runs.
    """

    def __init__(
        self,
        config: StreamingSTTConfig,
        *,
        open_session: Callable[[], Awaitable[tuple[str, str]]] | None = None,
        ws_connect: Callable[[str], _WebSocketCM] | None = None,
    ) -> None:
        if config.gladia_api_key is None or not config.gladia_api_key.get_secret_value():
            raise STTAuthenticationError(
                "PERSONA_GLADIA_API_KEY required for gladia",
                context={"provider": "gladia"},
            )
        self._config = config
        self._open_session = open_session or self._default_open_session
        self._ws_connect = ws_connect or self._default_ws_connect
        self._transcript_queue: asyncio.Queue[Transcript | None] = asyncio.Queue()
        # v1 emits no provider-side activity events (Silero is authoritative);
        # the queue exists only to deliver the terminating sentinel so the
        # activity iterator ends cleanly at close (empty-stream contract).
        self._activity_queue: asyncio.Queue[SpeechStartedEvent | SpeechEndedEvent | None] = (
            asyncio.Queue()
        )
        self._ws: _WebSocketLike | None = None
        self._session_task: asyncio.Task[None] | None = None
        self._connected: asyncio.Event = asyncio.Event()
        self._connect_error: BaseException | None = None
        self._closed: bool = False
        self._terminated: bool = False

    @property
    def provider_name(self) -> str:
        """Stable lowercase provider token — ``"gladia"``."""
        return "gladia"

    @property
    def model_name(self) -> str:
        """Configured Gladia model identifier (e.g. ``"solaria-1"``)."""
        return self._config.gladia_model

    async def push_audio(self, pcm: bytes, sample_rate: int) -> None:
        """Forward one inbound PCM16 frame to the Gladia WebSocket.

        Lazily opens the session + socket on first call. Pre/post-close calls
        are no-ops. On a send that races a just-closed socket, makes ONE
        transparent reconnect+resend attempt (the idle-close mitigation) before
        surfacing the failure.

        Args:
            pcm: PCM16 little-endian bytes for one frame.
            sample_rate: Frame sample rate (must be 16000 Hz per D-V1-6).

        Raises:
            STTAudioFormatError: ``sample_rate`` is not 16 kHz.
            STTStreamFailureError: session/socket could not be (re)established
                or the frame could not be delivered.
            STTAuthenticationError: provider rejected the key (401/403).
            STTRateLimitError: provider returned 429.
        """
        if self._closed:
            return
        if sample_rate != _GLADIA_INBOUND_SAMPLE_RATE_HZ:
            raise STTAudioFormatError(
                f"gladia requires sample_rate={_GLADIA_INBOUND_SAMPLE_RATE_HZ} Hz; "
                f"got {sample_rate}",
                context={
                    "provider": "gladia",
                    "model": self._config.gladia_model,
                    "sample_rate": str(sample_rate),
                },
            )
        await self._ensure_connected()
        ws = self._ws
        assert ws is not None  # guaranteed by _ensure_connected (else it raised)
        try:
            await ws.send(pcm)
        except Exception as first_exc:  # noqa: BLE001 — adapter-boundary recovery + mapping
            # The socket died between _ensure_connected and this send (idle-close
            # / drop). Drop it and make ONE transparent reconnect+resend attempt
            # (rides the V8 ring-buffer replay); surface the failure only if that
            # also fails.
            self._drop_session()
            if self._closed:
                return
            try:
                await self._ensure_connected()
                reconnected = self._ws
                assert reconnected is not None
                await reconnected.send(pcm)
            except (
                STTAuthenticationError,
                STTRateLimitError,
                STTStreamFailureError,
                STTAudioFormatError,
            ):
                raise
            except Exception:  # noqa: BLE001 — map the ORIGINAL send failure
                self._raise_mapped(first_exc)

    def transcripts(self) -> AsyncIterator[Transcript]:
        """Yield :class:`Transcript` records as Gladia emits them.

        Partials (``is_final=False``) then finals (``is_final=True`` with
        ``eou_at`` set). The iterator terminates when :meth:`close` drains the
        internal queue. An unexpected mid-stream session end does NOT terminate
        this iterator (the backend reconnects on the next frame).
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
        """Provider-side speech-activity stream — EMPTY at v1.

        The Protocol permits an empty stream; Silero VAD is the authoritative
        onset source and Gladia's speech-event shape is not yet verified live,
        so this v1 emits no provider corroborators. The iterator yields nothing
        and terminates cleanly at :meth:`close`.
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
        """Close the session gracefully (caller-initiated). Idempotent.

        Sends ``{"type": "stop_recording"}`` so Gladia finalizes pending audio
        and emits a trailing final, waits (bounded) for the receiver to drain +
        observe the server's 1000 close, then terminates both iterators via
        sentinels. A second call is a no-op.
        """
        if self._closed:
            return
        self._closed = True
        ws = self._ws
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.send(json.dumps({"type": "stop_recording"}))
        task = self._session_task
        if task is not None and not task.done():
            with contextlib.suppress(TimeoutError, asyncio.CancelledError, Exception):
                await asyncio.wait_for(asyncio.shield(task), timeout=_CLOSE_DRAIN_TIMEOUT_S)
        self._terminate_iterators()

    # ------------------------------------------------------------------
    # private — connection lifecycle
    # ------------------------------------------------------------------

    async def _ensure_connected(self) -> None:
        """Open the session + socket if not already live; block until ready.

        Idempotent while connected. On a prior unexpected end (``self._ws`` was
        cleared without :meth:`close`), starts a fresh receiver. Raises the
        mapped connect error if session init / WS connect failed.
        """
        if self._ws is not None:
            return
        if self._closed:
            raise STTStreamFailureError(
                "gladia backend is closed",
                context={"provider": "gladia", "model": self._config.gladia_model},
            )
        if self._session_task is None or self._session_task.done():
            self._connected = asyncio.Event()
            self._connect_error = None
            self._session_task = asyncio.create_task(self._run_session())
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=_CONNECT_TIMEOUT_S)
        except TimeoutError as exc:
            self._drop_session()
            raise STTStreamFailureError(
                "gladia session did not connect within the timeout",
                context={"provider": "gladia", "model": self._config.gladia_model},
            ) from exc
        if self._connect_error is not None:
            err = self._connect_error
            self._raise_mapped(err)
        if self._ws is None:
            raise STTStreamFailureError(
                "gladia session connected without a live socket",
                context={"provider": "gladia", "model": self._config.gladia_model},
            )

    async def _run_session(self) -> None:
        """Open session + socket, publish readiness, then pump inbound messages.

        Runs as a background task. On any failure before readiness, records the
        exception (surfaced by :meth:`_ensure_connected`) and unblocks the
        waiter. On end: if WE closed, terminate the iterators; otherwise clear
        the socket so the next :meth:`push_audio` reconnects (idle mitigation).
        """
        try:
            _session_id, ws_url = await self._open_session()
            async with self._ws_connect(ws_url) as ws:
                self._ws = ws
                self._connected.set()
                async for raw in ws:
                    self._handle_message(raw)
        except Exception as exc:  # noqa: BLE001 — adapter boundary; surfaced via _connect_error
            if self._ws is None:
                # Failed before readiness — hand the error to the awaiting caller.
                self._connect_error = exc
                self._connected.set()
            else:
                _logger.warning(
                    "gladia session ended unexpectedly ({err}); next frame will reconnect",
                    err=repr(exc)[:200],
                )
        finally:
            if self._closed:
                self._terminate_iterators()
            else:
                # Unexpected end — allow a transparent reconnect on next push.
                self._ws = None
                self._connected.clear()

    def _handle_message(self, raw: str | bytes) -> None:
        """Parse one inbound WS message into a :class:`Transcript` (best-effort).

        Non-transcript messages and malformed payloads are ignored (defensive,
        mirrors Deepgram's tolerant extraction). Binary frames are not expected
        for our config and are skipped.
        """
        if isinstance(raw, bytes):
            return
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return
        if not isinstance(msg, dict) or msg.get("type") != "transcript":
            return
        data = msg.get("data") or {}
        utterance = data.get("utterance") or {}
        text = utterance.get("text") or ""
        if not text:
            return
        is_final = bool(data.get("is_final"))
        raw_conf = utterance.get("confidence")
        confidence = float(raw_conf) if isinstance(raw_conf, (int, float)) else 1.0
        transcript = Transcript(
            is_final=is_final,
            text=text,
            confidence=max(0.0, min(1.0, confidence)),
            eou_at=datetime.now(UTC) if is_final else None,
        )
        self._transcript_queue.put_nowait(transcript)

    def _drop_session(self) -> None:
        """Discard the current (dead) socket + receiver without terminating the
        iterators — the next :meth:`push_audio` re-opens (idle mitigation)."""
        self._ws = None
        self._connected.clear()
        task = self._session_task
        self._session_task = None
        if task is not None and not task.done():
            task.cancel()

    def _terminate_iterators(self) -> None:
        """Push terminating sentinels into both output queues. Idempotent.

        Guarded by ``self._terminated`` so the two paths that call it (the
        :meth:`close` caller and the receiver's own ``finally`` when we closed)
        cannot enqueue duplicate sentinels.
        """
        if self._terminated:
            return
        self._terminated = True
        self._transcript_queue.put_nowait(None)
        self._activity_queue.put_nowait(None)

    def _raise_mapped(self, exc: BaseException) -> None:
        """Re-raise a provider/transport exception through the STT hierarchy."""
        _raise_mapped_gladia_error(exc, model=self._config.gladia_model)

    # ------------------------------------------------------------------
    # private — default (production) transports; overridden in tests
    # ------------------------------------------------------------------

    async def _default_open_session(self) -> tuple[str, str]:
        """Open a Gladia live session via REST; return ``(session_id, ws_url)``.

        Sends the pure D-V14-1 posture: an EMPTY ``languages`` list with
        ``code_switching: true`` (no language told to the model). Lazy
        ``httpx`` import mirrors the Deepgram backend's lazy-import discipline.
        """
        import httpx

        assert self._config.gladia_api_key is not None  # validated in __init__
        key = self._config.gladia_api_key.get_secret_value()
        init_url = self._config.base_url or _GLADIA_LIVE_INIT_URL
        body: dict[str, Any] = {
            "encoding": "wav/pcm",
            "sample_rate": _GLADIA_INBOUND_SAMPLE_RATE_HZ,
            "bit_depth": 16,
            "channels": 1,
            "model": self._config.gladia_model,
            "language_config": {"languages": [], "code_switching": True},
            "messages_config": {
                "receive_partial_transcripts": True,
                "receive_final_transcripts": True,
            },
        }
        try:
            async with httpx.AsyncClient(timeout=self._config.request_timeout_s) as client:
                resp = await client.post(
                    init_url,
                    headers={"x-gladia-key": key, "Content-Type": "application/json"},
                    json=body,
                )
        except httpx.HTTPError as exc:
            raise STTStreamFailureError(
                "gladia session init transport error",
                context={"provider": "gladia", "model": self._config.gladia_model},
            ) from exc
        _raise_for_gladia_status(resp.status_code, model=self._config.gladia_model)
        payload = resp.json()
        session_id = str(payload.get("id", ""))
        ws_url = str(payload.get("url", ""))
        if not ws_url:
            raise STTStreamFailureError(
                "gladia session init returned no websocket url",
                context={"provider": "gladia", "model": self._config.gladia_model},
            )
        return session_id, ws_url

    def _default_ws_connect(self, ws_url: str) -> _WebSocketCM:
        """Connect to the Gladia live WebSocket via ``websockets`` (lazy import)."""
        import websockets

        # ``websockets.connect`` returns an async context manager whose entered
        # value satisfies :class:`_WebSocketLike` structurally.
        return websockets.connect(ws_url)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Shared provider-exception mapping (session-init REST + WS transport)
# ---------------------------------------------------------------------------


def _raise_for_gladia_status(status: int, *, model: str) -> None:
    """Map an HTTP status from Gladia session init onto the STT hierarchy."""
    context = {"provider": "gladia", "model": model, "status": str(status)}
    if status in (401, 403):
        raise STTAuthenticationError("gladia rejected the api key", context=context)
    if status == 429:
        raise STTRateLimitError("gladia rate limit", context=context)
    if status >= 400:
        raise STTStreamFailureError("gladia session init failed", context=context)


def _raise_mapped_gladia_error(exc: BaseException, *, model: str) -> None:
    """Re-raise a provider/transport exception through the STT hierarchy.

    Domain exceptions pass through unchanged; anything else becomes a
    :class:`STTStreamFailureError` (the canonical reconnect target for a
    mid-stream drop).
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
    raise STTStreamFailureError(message, context={"provider": "gladia", "model": model}) from exc

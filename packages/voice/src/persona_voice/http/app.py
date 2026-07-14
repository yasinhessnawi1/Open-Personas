"""persona-voice HTTP app — the ``POST /v1/voice/token`` endpoint (spec V1 T04).

The endpoint is the **only** HTTP route the voice service exposes at v0.1.
LiveKit Server handles all WebRTC signaling, peer connections, and media
transport internally (D-V1-1 branch (A) + D-V1-3); persona-voice's job is to
mint a Room access token after verifying:

1. The caller's IdP JWT is valid (via the extracted ``make_jwt_verifier``).
2. The caller owns the requested persona (ownership check — defense-in-depth
   on top of the session-bound RLS engine T06 adds).

A passing call returns ``{token, room_name, livekit_url}`` — the client uses
these to join the LiveKit Room directly. Failures fail-closed: missing or
invalid JWT → 401; persona not visible to the caller → 404 (RLS-shape, never
leaks whether the persona exists for another tenant).

Ownership check is intentionally minimal at v0.1: a single ``SELECT`` against
``personas WHERE id = :pid AND owner_id = :uid``. The RLS-scoped engine T06
ships for the audio loop is a different concern (per-session lifecycle vs.
per-request). When persona-voice grows additional HTTP routes (post-V1), the
two patterns may consolidate.

**R9-025a — one-shot ``POST /v1/tts`` + ``POST /v1/stt``.** Two REST
primitives sitting beside the realtime call stack, NOT inside it: no LiveKit
Room, no session, no turn-taking. ``POST /v1/tts`` synthesises ``{text,
voice_id?}`` into a playable WAV clip by feeding the SAME cached
``CartesiaStreamingTTS`` instance ``GET /v1/voices`` warms a single-chunk
text stream and collecting the PCM16 output (D-V1-6 rail, wrapped in a WAV
header for direct ``<audio>`` playback); an omitted/``null`` ``voice_id``
(R9-025 reopen leg A) falls back to ``PERSONA_TTS_VOICE_DEFAULT`` — the
proxy's voiceless-persona contract, see :class:`TTSRequest`. ``POST
/v1/stt`` transcribes an uploaded audio clip via Deepgram's prerecorded REST
endpoint (:func:`persona_voice.stt.deepgram_backend.transcribe_prerecorded`
— a distinct SDK surface from the live WebSocket backend); an optional
``language`` field (R9-025 reopen — context-pinned dictation) pins the
request through the same capability matrix the live call pipeline uses,
instead of leaning on Deepgram's own limited-coverage ``detect_language``.
Both routes share
``GET /v1/voices``'s auth posture (any signed-in user via
:func:`get_current_user`; no persona scoping here — persona-api's proxy
resolves persona → voice_id / ownership before calling through,
server-to-server) and bound their inputs (``_TTS_TEXT_MAX_CHARS`` /
``_STT_AUDIO_MAX_BYTES`` → 413). Provider failures map to a clean 502 (or,
for a credential/config problem, 503 — mirroring persona-api's
``ImageGenUnavailableError`` precedent) with a FIXED vocabulary reason,
never the provider's raw payload.
"""

from __future__ import annotations

import io
import uuid
import wave

# NOTE: `Request`, `Awaitable`, `Callable`, `UploadFile` are RUNTIME imports
# (NOT under TYPE_CHECKING) because FastAPI resolves dependency / route
# signatures via ``get_type_hints`` at startup — with ``from __future__
# import annotations`` every annotation is a string, so every name in a
# dependency's signature must be importable at runtime or FastAPI mis-reads
# the params (e.g. treating ``request: Request`` as a query parameter). Same
# pattern as persona-api ``auth/deps.py``.
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, cast

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from persona.auth.jwt_verifier import AuthenticatedUser, make_jwt_verifier
from persona.credits import require_credits as _require_credits_core
from persona.errors import AuthenticationError, CreditsExhaustedError
from persona.language_capability import default_capability_registry
from persona.logging import get_logger
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine, create_engine, event, text

from persona_voice.config import VoiceConfig
from persona_voice.stt.errors import (
    STTAudioFormatError,
    STTAuthenticationError,
    STTError,
    STTRateLimitError,
    STTStreamFailureError,
)
from persona_voice.tokens.issuer import RoomAccessToken, mint_room_access_token
from persona_voice.tts.audio import OUTBOUND_SAMPLE_RATE
from persona_voice.tts.errors import (
    TTSAudioFormatError,
    TTSAuthenticationError,
    TTSError,
    TTSRateLimitError,
    TTSStreamFailureError,
)
from persona_voice.tts.types import ResolvedVoice, VoiceCatalogueEntry

if TYPE_CHECKING:
    from persona_voice.stt.config import StreamingSTTConfig
    from persona_voice.tts.catalogue import VoiceCatalogue
    from persona_voice.tts.config import StreamingTTSConfig
    from persona_voice.tts.protocol import StreamingTTS

__all__ = ["build_app", "create_app", "get_voice_config"]

_logger = get_logger("voice.http")

#: R9-025a bounded sizes — a client-input-shaped 413, distinct from a
#: provider/config failure (502/503). ~4k chars is comfortably above a long
#: chat reply; ~10MB covers several minutes of compressed dictation audio.
_TTS_TEXT_MAX_CHARS = 4000
_STT_AUDIO_MAX_BYTES = 10 * 1024 * 1024

#: R9-025 reopen — context-pinned dictation language shape gate: a base
#: 2-3 letter ISO-639-1/639-2-ish code with an optional BCP-47 region/script
#: subtag (``"en"``, ``"no"``, ``"en-US"``, ``"nb-NO"``). A shape-invalid
#: value 422s before it ever reaches the capability registry or Deepgram; a
#: well-formed but UNRECOGNIZED code still resolves (fail-soft to English,
#: same as every other :mod:`persona.language_capability` consumer).
_LANGUAGE_HINT_PATTERN = r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{1,8})?$"

# Sentinel distinguishing "catalogue not yet built" from "built, but None
# (TTS unconfigured)" on app.state.
_UNSET: object = object()


def _get_voice_catalogue(request: Request) -> VoiceCatalogue | None:
    """The app-scoped voice catalogue (Spec V6 C2, D-V6-E4), built lazily.

    The Cartesia launch backend (``load_streaming_tts``) conforms to the
    :class:`VoiceCatalogue` Protocol (D-V3-3). Built on first ``GET /v1/voices``
    and cached on ``app.state`` so a token-only deployment boots without TTS
    configured. Construction failure (no ``PERSONA_TTS_API_KEY``) caches + returns
    ``None`` → the endpoint returns an empty list (the selector degrades to the
    persona's existing / the global-default voice). Tests override by setting
    ``app.state.voice_catalogue`` directly.
    """
    cached = getattr(request.app.state, "voice_catalogue", _UNSET)
    if cached is not _UNSET:
        return cast("VoiceCatalogue | None", cached)

    catalogue: VoiceCatalogue | None
    try:
        from persona_voice.tts._factory import load_streaming_tts
        from persona_voice.tts.config import StreamingTTSConfig

        backend = load_streaming_tts(StreamingTTSConfig())
        catalogue = cast("VoiceCatalogue", backend)
    except Exception:  # noqa: BLE001 — unconfigured TTS must not break voice-list
        catalogue = None
    request.app.state.voice_catalogue = catalogue
    return catalogue


def _prewarm_catalogue(app: FastAPI) -> None:
    """Build the voice catalogue + walk it once in the background (D-V6-E4 perf).

    Both ``GET /v1/voices`` and the persona-create voice auto-pick walk the full
    provider catalogue (~30s) on a cold cache, which can exceed a caller timeout.
    Warming it off the event loop at startup means the first real request is
    served from the cached list, not a 30s walk. Fail-soft: unconfigured TTS or a
    walk error just leaves the lazy path intact (the catalogue stays usable).
    """
    import asyncio

    try:
        from persona_voice.tts._factory import load_streaming_tts
        from persona_voice.tts.config import StreamingTTSConfig

        backend = load_streaming_tts(StreamingTTSConfig())
    except Exception:  # noqa: BLE001 — unconfigured TTS must not break startup
        app.state.voice_catalogue = None
        return
    app.state.voice_catalogue = backend

    async def _walk() -> None:
        try:
            await cast("VoiceCatalogue", backend).list_voices(limit=1)
        except Exception:  # noqa: BLE001 — a failed warm just defers to the lazy walk
            return

    # Keep a reference so the fire-and-forget task is not garbage-collected.
    app.state.catalogue_warm_task = asyncio.create_task(_walk())


class TokenRequest(BaseModel):
    """Body for ``POST /v1/voice/token``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    persona_id: str
    conversation_id: str


class TokenResponse(BaseModel):
    """Result returned to the client."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    token: str
    room_name: str
    livekit_url: str


class VoiceListResponse(BaseModel):
    """``GET /v1/voices`` result (Spec V6 C2).

    Carries the catalogue ``provider`` so the voice-selector can set the
    persona's full ``VoiceSpec`` (``{provider, voice_id}``); ``provider`` is
    ``None`` (and ``voices`` empty) when TTS is unconfigured.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str | None
    voices: list[VoiceCatalogueEntry]


class TTSRequest(BaseModel):
    """Body for ``POST /v1/tts`` (R9-025a one-shot synthesis).

    ``voice_id`` is the provider-scoped catalogue handle (a
    :class:`persona_voice.tts.types.VoiceCatalogueEntry.voice_id` /
    :class:`persona.schema.persona.CatalogueVoice.voice_id`) — the voice
    service is persona-agnostic, so the caller (persona-api's proxy) resolves
    a persona to a voice_id server-side before calling here.

    ``voice_id`` is OPTIONAL (R9-025 reopen leg A): omit it (or send ``null``)
    to synthesize with this service's configured default voice
    (``PERSONA_TTS_VOICE_DEFAULT`` — the SAME D-V3-4 fallback the realtime call
    stack's :func:`persona_voice.tts.voice_resolution.resolve_voice` already
    applies to a voice-less persona). This is the explicit contract the proxy's
    voiceless-persona fallback relies on — a persona with no configured voice is
    no longer a local 503, it rides through to this default. ``503
    no_voice_configured`` still fires when there is truly no voice to use
    (neither an explicit id nor a configured default).

    ``provider`` is OPTIONAL (Spec V14 D-V14-13): the provider the ``voice_id``
    is addressed to (the persona's stored ``identity.voice.provider``). When it
    is present AND does not match the active TTS backend's provider (e.g. a
    Cartesia voice under an ElevenLabs-active deployment before the auto-remap
    re-picks it), the ``voice_id`` is DROPPED and synthesis falls back to the
    active provider's default voice — sending the wrong-provider id to the
    backend would 4xx. ``None`` ⇒ trust the id (today's behavior, backward
    compatible).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(min_length=1)
    voice_id: str | None = Field(default=None, min_length=1)
    provider: str | None = Field(default=None, min_length=1)


class STTResponse(BaseModel):
    """Result of ``POST /v1/stt`` (R9-025a one-shot prerecorded transcription)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    transcript: str


def get_voice_config(request: Request) -> VoiceConfig:
    """Provide the active :class:`VoiceConfig` (overridable in tests)."""
    cfg = getattr(request.app.state, "voice_config", None)
    if cfg is None:
        msg = "voice_config not configured on app.state"
        raise RuntimeError(msg)
    assert isinstance(cfg, VoiceConfig)
    return cfg


def _bearer_token(request: Request) -> str:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise AuthenticationError("missing or malformed Authorization header")
    token = header.removeprefix("Bearer ").strip()
    if not token:
        raise AuthenticationError("empty bearer token")
    return token


async def _disabled_verify(_token: str) -> AuthenticatedUser:
    """Community has no auth wall; this stand-in is never actually called.

    It exists only so the ``get_verify_token`` dependency resolves without
    building a JWT verifier (which would require a configured secret).
    Fail-closed if ever invoked.
    """
    raise AuthenticationError("token verification is disabled in the community edition")


def get_verify_token(request: Request) -> Callable[[str], Awaitable[AuthenticatedUser]]:
    """Active token verifier — overridable on ``app.state.verify_token`` for tests.

    Spec 33 (D-33-X-voice-edition): the community edition is no-auth, so we return
    a disabled stand-in rather than building a JWT verifier that would demand a
    secret (``get_current_user`` returns a fixed local owner instead).

    Cloud edition verifies the same way persona-api does (shared
    ``make_jwt_verifier`` — D-V1-X-jwt-verifier-extraction): RS256 against a
    Clerk-dashboard PEM in ``PERSONA_VOICE_JWT_PUBLIC_KEY`` (production; the SAME
    posture ``packages/api/run-local.sh`` already wires for local dev — see that
    script's "Spec V6: persona-voice service" section), or HS256 against a shared
    secret in ``PERSONA_VOICE_JWT_SECRET`` for a lighter dev/test setup. Both are
    documented in ``.env.example``.

    R9-025 reopen (leg A): a cloud-edition deployment with NEITHER key configured
    used to crash ``make_jwt_verifier`` with an unhandled ``ValueError`` at
    dependency-construction time — every request 500'd with no diagnosable signal
    (the months-silent failure this reopen exists to fix). That is a server
    misconfiguration, not a caller-specific problem, but it still must fail
    *closed* (no request should ever get through unverified) — so it is now
    surfaced as the SAME 401 ``authentication_error`` shape any bad bearer gets,
    logged at WARNING with the construction failure's reason.
    """
    verifier = getattr(request.app.state, "verify_token", None)
    if verifier is not None:
        return verifier  # type: ignore[no-any-return]
    cfg = get_voice_config(request)
    if not cfg.is_cloud:
        return _disabled_verify
    try:
        return make_jwt_verifier(cfg)
    except ValueError as exc:
        _logger.warning(
            "voice auth misconfigured — verification unavailable: {err}", err=str(exc)[:200]
        )
        raise AuthenticationError(
            "voice service authentication is not configured", context={"reason": str(exc)[:160]}
        ) from exc


async def get_current_user(
    request: Request,
    verify: Callable[[str], Awaitable[AuthenticatedUser]] = Depends(get_verify_token),
) -> AuthenticatedUser:
    """Authenticate the request; return the user or raise ``AuthenticationError``.

    Spec 33: community is no-auth — return the fixed local owner with no bearer
    token (mirrors persona-api's ``CommunityOwnerResolver``). Cloud verifies the
    bearer JWT, unchanged. A test-injected ``app.state.verify_token`` always wins.
    """
    cfg = get_voice_config(request)
    if not cfg.is_cloud and getattr(request.app.state, "verify_token", None) is None:
        return AuthenticatedUser(id=cfg.community_owner_id, email=cfg.community_owner_email)
    return await verify(_bearer_token(request))


def _require_credits(request: Request, *, user_id: str) -> None:
    """Pre-flight credit gate for ``POST /v1/voice/token`` (D-19-X-voice-token-credit-gate).

    Mirrors the persona-api chat 402 contract (D-11-12): raises
    :class:`CreditsExhaustedError` when ``balance <= 0`` so the LiveKit Room
    token is never minted for a user out of credits. The check runs at token
    issue (call-start); per-turn deductions during the call are a separate
    concern. Tests can override via ``app.state.require_credits`` to skip the
    DB hop entirely (same pattern as ``owns_persona``).
    """
    # Spec 33 (D-33-X-voice-edition): community is unmetered — no credit gate.
    if not get_voice_config(request).is_cloud:
        return
    override = getattr(request.app.state, "require_credits", None)
    if override is not None:
        override(user_id=user_id)
        return
    engine = getattr(request.app.state, "ownership_engine", None)
    if engine is None:
        msg = "ownership_engine not configured on app.state"
        raise RuntimeError(msg)
    _require_credits_core(rls_engine=engine, user_id=user_id)


def _check_persona_ownership(
    request: Request,
    *,
    persona_id: str,
    user_id: str,
) -> None:
    """Verify the user owns ``persona_id`` against the personas table.

    Raises ``HTTPException(404)`` if the persona is not visible to the user —
    the same shape persona-api uses to avoid leaking persona existence across
    tenants. In tests the app state can expose an ``owns_persona`` override
    so the DB hop is skipped entirely.
    """
    # Spec 33 (D-33-X-voice-edition): community is single-owner — the one local
    # owner owns every persona, so there is nothing to cross-tenant-check.
    if not get_voice_config(request).is_cloud:
        return
    override = getattr(request.app.state, "owns_persona", None)
    if override is not None:
        if not override(persona_id=persona_id, user_id=user_id):
            raise HTTPException(status_code=404, detail="persona not found")
        return
    engine = getattr(request.app.state, "ownership_engine", None)
    if engine is None:
        msg = "ownership_engine not configured on app.state"
        raise RuntimeError(msg)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT 1 FROM personas WHERE id = :pid AND owner_id = :uid"),
            {"pid": persona_id, "uid": user_id},
        ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="persona not found")


# personas + credits are RLS-FORCED, so the shared HTTP ownership engine must
# scope every connection to the request's user — otherwise RLS filters every row
# and the ownership SELECT (above) and persona.credits.require_credits both see
# nothing → a false 404 at POST /v1/voice/token. This is persona-api's
# request-scoped pattern (a ContextVar, because one engine serves concurrent
# requests), NOT make_session_rls_engine's per-session baked user_id.
_rls_user_id: ContextVar[str] = ContextVar("voice_rls_user_id", default="")
_SET_RLS_SQL = "SELECT set_config('app.current_user_id', %s, false)"


def _make_ownership_engine(url: str) -> Engine:
    """Shared engine whose connections RLS-scope to ``_rls_user_id`` on checkout."""
    engine = create_engine(url, pool_size=5, pool_pre_ping=True)

    @event.listens_for(engine, "checkout")
    def _scope_to_request_user(
        dbapi_conn: Any,  # noqa: ANN401 — psycopg3 dynamic connection type
        _record: Any,  # noqa: ANN401
        _proxy: Any,  # noqa: ANN401
    ) -> None:
        cursor = dbapi_conn.cursor()
        try:
            cursor.execute(_SET_RLS_SQL, (_rls_user_id.get(),))
        finally:
            cursor.close()

    return engine


# ---------------------------------------------------------------------------
# R9-025a — one-shot TTS/STT helpers
# ---------------------------------------------------------------------------


def _get_tts_backend(request: Request) -> StreamingTTS | None:
    """The cached one-shot-capable TTS backend, or ``None`` if unconfigured.

    ``CartesiaStreamingTTS`` conforms to BOTH ``VoiceCatalogue`` (listing,
    what :func:`_get_voice_catalogue` types it as) and ``StreamingTTS``
    (synthesis) — reusing that function's cached ``app.state.voice_catalogue``
    instance for ``POST /v1/tts`` means the route opens no new provider
    client and shares the startup prewarm. This IS "reuse the existing
    backend class" (R9-025a), in the most literal sense: the same object.
    """
    catalogue = _get_voice_catalogue(request)
    if catalogue is None:
        return None
    return cast("StreamingTTS", catalogue)


def _get_tts_stream_config(request: Request) -> StreamingTTSConfig:
    """The active :class:`StreamingTTSConfig` — overridable via ``app.state.tts_stream_config``.

    Mirrors :func:`_get_stt_config`'s override convention (tests inject a
    config directly; production reads fresh from ``PERSONA_TTS_*`` env vars —
    cheap, no I/O). ``POST /v1/tts`` reads only ``voice_default`` from it (the
    R9-025 reopen leg A voiceless-caller fallback, D-V3-4) — catalogue/backend
    construction stays on its own cached path (:func:`_get_voice_catalogue`).
    """
    cfg = getattr(request.app.state, "tts_stream_config", None)
    if cfg is not None:
        return cast("StreamingTTSConfig", cfg)
    from persona_voice.tts.config import StreamingTTSConfig

    return StreamingTTSConfig()


async def _synthesize_once(backend: StreamingTTS, *, text: str, voice_id: str) -> bytes:
    """Feed ``text`` through the streaming backend as a single chunk; collect PCM16.

    No LiveKit session, no chunker, no barge-in — the backend's ``synthesize``
    is driven exactly as the realtime call stack drives it (a text stream +
    a :class:`ResolvedVoice`), just with a one-item stream and the output
    collected instead of framed onto a transport.
    """

    async def _one_chunk() -> AsyncIterator[str]:
        yield text

    resolved = ResolvedVoice(provider=backend.provider_name, voice_ref=voice_id)
    pcm = bytearray()
    async for chunk in backend.synthesize(_one_chunk(), resolved):
        pcm.extend(chunk.data)
    return bytes(pcm)


def _pcm16_to_wav(pcm: bytes, *, sample_rate: int, channels: int = 1) -> bytes:
    """Wrap headerless PCM16LE mono bytes (the D-V1-6 outbound rail) in a WAV container.

    A one-shot REST response needs only a header — no resampling/transcoding,
    since the rail already matches WAV's most common PCM shape. WAV (not raw
    PCM) so a browser ``<audio>`` element can play the response directly with
    no client-side decoding.
    """
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)  # PCM16
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def _tts_error_reason(exc: TTSError) -> str:
    """Fixed-vocabulary reason for a 502 ``tts_provider_error`` body.

    Never interpolates ``str(exc)`` (which may carry the provider's raw
    message) into a response — provider payloads stay server-side (logged),
    never echoed to the caller verbatim.
    """
    if isinstance(exc, TTSRateLimitError):
        return "provider_rate_limited"
    if isinstance(exc, TTSAudioFormatError):
        return "provider_audio_format_error"
    if isinstance(exc, TTSStreamFailureError):
        return "provider_stream_failed"
    return "provider_error"


def _get_stt_config(request: Request) -> StreamingSTTConfig:
    """The active STT config — overridable via ``app.state.stt_config`` (tests).

    Mirrors :func:`get_voice_config`'s override convention. Falls back to the
    env-driven :class:`StreamingSTTConfig` (``PERSONA_STT_*``) — the SAME
    config the realtime call stack's Deepgram backend reads.
    """
    cfg = getattr(request.app.state, "stt_config", None)
    if cfg is not None:
        return cast("StreamingSTTConfig", cfg)
    from persona_voice.stt.config import StreamingSTTConfig

    return StreamingSTTConfig()


def _get_stt_transcriber(request: Request) -> Callable[..., Awaitable[str]]:
    """The active one-shot transcriber — overridable via ``app.state.transcribe_audio``.

    Mirrors the ``owns_persona`` / ``require_credits`` test-seam convention
    already used throughout this module. Defaults to
    :func:`persona_voice.stt.deepgram_backend.transcribe_prerecorded` — the
    D-V2-1 LOCK launch provider's one-shot REST leg (no dispatch/factory: a
    single provider implements the one-shot path today).
    """
    override = getattr(request.app.state, "transcribe_audio", None)
    if override is not None:
        return cast("Callable[..., Awaitable[str]]", override)
    from persona_voice.stt.deepgram_backend import transcribe_prerecorded

    return transcribe_prerecorded


def _stt_error_reason(exc: STTError) -> str:
    """Fixed-vocabulary reason for a 502 ``stt_provider_error`` body.

    See :func:`_tts_error_reason` — same rationale, mirrored for STT.
    """
    if isinstance(exc, STTRateLimitError):
        return "provider_rate_limited"
    if isinstance(exc, STTAudioFormatError):
        return "provider_audio_format_error"
    if isinstance(exc, STTStreamFailureError):
        return "provider_stream_failed"
    return "provider_error"


def build_app(config: VoiceConfig) -> FastAPI:
    """Build the persona-voice FastAPI app.

    The app holds the active :class:`VoiceConfig` on ``app.state.voice_config``.
    Production callers also attach an ``ownership_engine`` (a SQLAlchemy
    Engine bound to the ``persona_app`` RLS-scoped role) before serving
    requests. Tests can override ``owns_persona`` instead to skip the DB hop.
    """
    # Spec V6 A0 (D-V6-X-agent-worker) — the dev/operator-pass-grade in-process
    # agent launcher, built BEFORE the app so the lifespan can warm it. When
    # enabled, the token endpoint spawns the agent that joins the call's Room and
    # becomes the persona. Default-off keeps token-only deploys + tests unaffected.
    launcher = None
    if config.agent_inprocess:
        from persona_voice.agent import InProcessAgentLauncher

        launcher = InProcessAgentLauncher(config)

    @asynccontextmanager
    async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # Startup: BLOCK until the bge embedder cold-load (~tens of seconds on
        # CPU) finishes, off the event loop. uvicorn holds incoming requests
        # until lifespan-startup completes, so the first call waits a few seconds
        # then runs warm — instead of its first turn hanging on memory recall
        # (the V6 operator-pass finding). `on_event("startup")` did NOT fire
        # under the factory; the lifespan always does.
        if launcher is not None:
            await launcher.warm()
        # Warm the voice catalogue off the loop so the first GET /v1/voices (and
        # the persona-create voice auto-pick) is served from cache, not a ~30s
        # full-catalogue walk (D-V6-E4).
        _prewarm_catalogue(_app)
        yield
        if launcher is not None:
            await launcher.aclose()

    app = FastAPI(title="persona-voice", version="0.1.0", lifespan=_lifespan)
    app.state.voice_config = config
    app.state.agent_launcher = launcher
    if config.database_url:
        app.state.ownership_engine = _make_ownership_engine(config.database_url)

    # CORS — the browser calls POST /v1/voice/token + GET /v1/voices cross-origin
    # (mirrors persona-api). Bearer auth (no cookies) → allow_credentials=False.
    if config.cors_origins_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.cors_origins_list,
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # (the agent launcher is built above + warmed/closed via the lifespan.)

    @app.exception_handler(AuthenticationError)
    async def _auth_error_handler(_req: Request, exc: AuthenticationError) -> object:
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=401,
            content={"error": "authentication_error", "detail": str(exc)},
            headers={"WWW-Authenticate": "Bearer"},
        )

    @app.exception_handler(CreditsExhaustedError)
    async def _credits_error_handler(_req: Request, exc: CreditsExhaustedError) -> object:
        # Mirrors the persona-api 402 contract (D-11-12) so the web client's
        # existing credits_exhausted handling works for voice too.
        from fastapi.responses import JSONResponse

        payload: dict[str, object] = {
            "error": "credits_exhausted",
            "detail": exc.message or "insufficient credits",
        }
        if exc.context:
            payload["context"] = exc.context
        return JSONResponse(status_code=402, content=payload)

    @app.post("/v1/voice/token", response_model=TokenResponse)
    async def issue_voice_token(
        body: TokenRequest,
        request: Request,
        user: AuthenticatedUser = Depends(get_current_user),
    ) -> TokenResponse:
        # RLS-scope this request's DB access (personas + credits are RLS-FORCED)
        # to the authenticated user, so the ownership + credit checks see the
        # caller's rows instead of an RLS-empty result → a false 404.
        _rls_user_id.set(user.id)
        _check_persona_ownership(request, persona_id=body.persona_id, user_id=user.id)
        _require_credits(request, user_id=user.id)
        cfg = get_voice_config(request)
        session_id = uuid.uuid4().hex
        token: RoomAccessToken = mint_room_access_token(
            api_key=cfg.livekit_api_key.get_secret_value(),
            api_secret=cfg.livekit_api_secret.get_secret_value(),
            livekit_url=cfg.livekit_url,
            session_id=session_id,
            user_id=user.id,
            persona_id=body.persona_id,
            conversation_id=body.conversation_id,
            ttl_s=cfg.livekit_token_ttl_s,
        )
        # Spec V6 A0 — launch the agent into the call's Room (dev path only;
        # default-off). The user joins ``room_name``; the agent joins the same
        # Room and becomes the persona. Fire-and-forget — a failed launch never
        # blocks the token response (the launcher catches + logs).
        launcher = getattr(request.app.state, "agent_launcher", None)
        if launcher is not None:
            launcher.launch(
                session_id=session_id,
                user_id=user.id,
                persona_id=body.persona_id,
                conversation_id=body.conversation_id,
            )
        return TokenResponse(
            token=token.token,
            room_name=token.room_name,
            livekit_url=token.livekit_url,
        )

    @app.get("/v1/voices", response_model=VoiceListResponse)
    async def list_voices(
        request: Request,
        _user: AuthenticatedUser = Depends(get_current_user),
        language: str | None = Query(default=None),
    ) -> VoiceListResponse:
        """List the provider voice catalogue for the voice-selector (Spec V6 C2).

        Auth'd (any signed-in user) — voices are non-sensitive, not user-scoped
        (D-V6-E4). Returns an empty list when TTS is unconfigured or the provider
        fetch fails, so the selector degrades gracefully to the persona's
        existing / the global-default voice rather than erroring.

        ``language`` (Spec 32) — when given, only voices that speak that language
        are returned, so an author cannot pick a voice the persona's declared
        language can't be spoken in (the root cause of a call-time
        ``language_not_supported``). The raw code is normalized through the
        capability registry (``nb`` → the served ``no``); an unrecognized
        language falls back to English voices.
        """
        catalogue = _get_voice_catalogue(request)
        if catalogue is None:
            return VoiceListResponse(provider=None, voices=[])
        # Normalize the declared language to the provider's voice-language code
        # (the catalogue filters on the voice's primary language).
        provider_language = (
            default_capability_registry().resolve_tts(language).code
            if language is not None and language.strip() != ""
            else None
        )
        try:
            entries = await catalogue.list_voices(language=provider_language, limit=200)
        except Exception:  # noqa: BLE001 — a provider/network error → empty list
            return VoiceListResponse(provider=catalogue.provider_name, voices=[])
        return VoiceListResponse(provider=catalogue.provider_name, voices=list(entries))

    @app.post("/v1/tts")
    async def synthesize_speech(
        body: TTSRequest,
        request: Request,
        _user: AuthenticatedUser = Depends(get_current_user),
    ) -> Response:
        """One-shot synthesis (R9-025a): ``{text, voice_id?}`` → a playable WAV clip.

        Auth matches ``GET /v1/voices`` (any signed-in user; persona-agnostic
        — persona-api's proxy resolves persona → voice_id before calling
        here). No LiveKit session; reuses the SAME cached backend instance
        ``GET /v1/voices`` warms. ``text`` bounded to
        :data:`_TTS_TEXT_MAX_CHARS` → 413. Missing/rejected provider
        credentials → 503 (mirrors persona-api's ``ImageGenUnavailableError``
        precedent: a deployment/config problem, not a transient one);
        any other provider failure → 502 with a fixed-vocabulary reason.

        ``voice_id`` omitted/``null`` (R9-025 reopen leg A): falls back to
        ``PERSONA_TTS_VOICE_DEFAULT`` — the proxy's voiceless-persona contract
        (see :class:`TTSRequest`). ``503 no_voice_configured`` when there is
        no id AND no configured default.
        """
        if len(body.text) > _TTS_TEXT_MAX_CHARS:
            raise HTTPException(
                status_code=413,
                detail={
                    "error": "tts_text_too_long",
                    "detail": "text exceeds the synthesis size limit",
                },
            )
        backend = _get_tts_backend(request)
        if backend is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "tts_unavailable",
                    "detail": "text-to-speech is not configured",
                },
            )
        active_provider = backend.provider_name
        voice_id = body.voice_id
        # Spec V14 (D-V14-13): a stored voice addressed to a DIFFERENT provider
        # than the active backend cannot be used — drop it and fall back to the
        # active provider's default (sending a Cartesia id to ElevenLabs 4xxs).
        if voice_id is not None and body.provider is not None and body.provider != active_provider:
            _logger.warning(
                "tts voice provider {vp!r} != active backend {ap!r}; using the "
                "provider default until the voice is re-picked",
                vp=body.provider,
                ap=active_provider,
            )
            voice_id = None
        if voice_id is None:
            # D-V14-13: the provider-appropriate default (ElevenLabs default under
            # ElevenLabs, Cartesia default otherwise), not the generic Cartesia one.
            voice_id = _get_tts_stream_config(request).default_voice_for(active_provider)
            if not voice_id:
                raise HTTPException(
                    status_code=503,
                    detail={
                        "error": "tts_unavailable",
                        "detail": "no voice specified and no default voice is configured",
                        "reason": "no_voice_configured",
                    },
                )
        try:
            pcm = await _synthesize_once(backend, text=body.text, voice_id=voice_id)
        except TTSAuthenticationError as exc:
            _logger.warning("tts one-shot synthesis unavailable: {err}", err=repr(exc)[:200])
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "tts_unavailable",
                    "detail": "text-to-speech is not configured",
                },
            ) from exc
        except TTSError as exc:
            _logger.warning("tts one-shot synthesis failed: {err}", err=repr(exc)[:200])
            raise HTTPException(
                status_code=502,
                detail={"error": "tts_provider_error", "reason": _tts_error_reason(exc)},
            ) from exc
        if not pcm:
            # The backend's own fail-soft path (e.g. a language the voice can't
            # speak even in the English fallback) can legitimately yield zero
            # audio for the realtime call; a one-shot caller asked for speech
            # and got silence, which is honest to surface as a provider error
            # rather than a 200 with an empty/silent clip.
            raise HTTPException(
                status_code=502,
                detail={"error": "tts_provider_error", "reason": "no_audio_produced"},
            )
        wav_bytes = _pcm16_to_wav(pcm, sample_rate=OUTBOUND_SAMPLE_RATE)
        return Response(content=wav_bytes, media_type="audio/wav")

    @app.post("/v1/stt", response_model=STTResponse)
    async def transcribe_audio(
        request: Request,
        audio: UploadFile = File(...),
        language: str | None = Form(
            default=None,
            min_length=2,
            max_length=16,
            pattern=_LANGUAGE_HINT_PATTERN,
        ),
        _user: AuthenticatedUser = Depends(get_current_user),
    ) -> STTResponse:
        """One-shot prerecorded transcription (R9-025a): record-stop → text.

        Auth matches ``GET /v1/voices``. Bounded to
        :data:`_STT_AUDIO_MAX_BYTES` → 413. Missing/rejected provider
        credentials → 503; any other provider failure → 502 with a
        fixed-vocabulary reason (never the provider payload verbatim).

        ``language`` (R9-025 reopen — context-pinned dictation): an optional
        caller-supplied hint — the chat composer sends the conversation
        persona's ``identity.language_default``; the persona-authoring mic
        sends the active UI locale. Shape-invalid → 422
        (:data:`_LANGUAGE_HINT_PATTERN`), before any provider/config work.
        A shape-valid hint is resolved through the SAME
        :class:`persona.language_capability.CapabilityRegistry` matrix the
        live call pipeline's ``apply_stt_route`` (Spec 32) pins per call —
        so ``"nb"``/``"nn"``/region-tagged variants collapse onto the
        identical provider-served codes a persona's declared language
        resolves to on a call (never a raw, unmapped code reaching
        Deepgram), fail-soft to English for anything unrecognized (never a
        422 for a well-formed-but-unknown code). Overrides any env-level
        ``PERSONA_STT_LANGUAGE_HINT`` default for this one request. Omitted
        → unchanged 7647699 behavior (``config.language_hint`` from env,
        auto-``detect_language`` when falsy).
        """
        data = await audio.read()
        if len(data) > _STT_AUDIO_MAX_BYTES:
            raise HTTPException(
                status_code=413,
                detail={
                    "error": "stt_audio_too_large",
                    "detail": "audio exceeds the transcription size limit",
                },
            )
        config = _get_stt_config(request)
        if language:
            route = default_capability_registry().resolve_stt(language)
            config = config.model_copy(update={"language_hint": route.code, "model": route.model})
        transcriber = _get_stt_transcriber(request)
        try:
            transcript = await transcriber(data, config=config, content_type=audio.content_type)
        except STTAuthenticationError as exc:
            _logger.warning("stt one-shot transcription unavailable: {err}", err=repr(exc)[:200])
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "stt_unavailable",
                    "detail": "speech-to-text is not configured",
                },
            ) from exc
        except STTError as exc:
            _logger.warning("stt one-shot transcription failed: {err}", err=repr(exc)[:200])
            raise HTTPException(
                status_code=502,
                detail={"error": "stt_provider_error", "reason": _stt_error_reason(exc)},
            ) from exc
        return STTResponse(transcript=transcript)

    return app


def create_app(config: VoiceConfig | None = None) -> FastAPI:
    """Zero-arg app factory for ``uvicorn --factory`` (reads VoiceConfig from env).

    Mirrors ``persona_api.app.create_app``: build the env-driven config and wire
    the app. The token-only deployment, the catalogue, and (when
    ``PERSONA_VOICE_AGENT_INPROCESS=true``) the dev agent launcher are all set up
    inside :func:`build_app`.
    """
    return build_app(config or VoiceConfig())

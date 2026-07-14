"""Voice proxy routes (R9-025a): thin authed fronts over persona-voice's REST primitives.

persona-api does not talk to Cartesia/Deepgram directly — it proxies to the
separate persona-voice service (mirrors ``voice_assignment_service.py``'s
build-time catalogue fetch + ``routes/connectors.py``'s link-initiation
proxy, the two existing "call out to a sibling HTTP service" precedents):

* ``POST /v1/personas/{persona_id}/tts`` — resolves THE PERSONA's configured
  voice server-side (``identity.voice`` in its stored YAML — the same field
  the build-time voice auto-pick writes, Issue 1 / ``persona_service.set_voice``),
  ownership-checked (RLS via :func:`persona_service.get_persona`, 404 on
  cross-tenant), then proxies ``{text}`` + the resolved ``voice_id`` to
  persona-voice's ``POST /v1/tts``. Returns the WAV bytes verbatim.

  **Voiceless-persona fallback (R9-025 reopen leg A).** A persona with no
  ``identity.voice`` (never auto-picked, or auto-picked before TTS was
  configured — see ``voice_assignment_service``) is no longer an immediate
  local 503: ``voice_id`` rides through as ``null``, and persona-voice
  resolves it against its own ``PERSONA_TTS_VOICE_DEFAULT`` (the SAME D-V3-4
  fallback the realtime call stack already applies to a voice-less persona —
  see ``persona_voice.tts.voice_resolution.resolve_voice``). The 503
  ``no_voice_configured`` shape still fires, but now only when persona-voice
  itself has nothing to fall back to (or is unreachable/unconfigured) — never
  merely because THIS persona hasn't picked a voice yet.
* ``POST /v1/stt`` — owner-scoped only (no persona; dictation has no
  persona context during authoring), audio passthrough to persona-voice's
  ``POST /v1/stt``. Returns ``{transcript}``. An optional ``language`` form
  field (R9-025 reopen — context-pinned dictation) rides along verbatim —
  shape-validated here, resolved server-side by persona-voice.

Both routes forward the caller's OWN verified bearer to persona-voice (which
authorises any signed-in user for these routes, the SAME posture as
``GET /v1/voices``) — no service credential is introduced, mirroring
``routes/connectors.py``'s ``initiate_link`` reasoning verbatim.

**Fail-soft gating (D-C6-0-mirror).** Gated on ``PERSONA_VOICE_SERVICE_URL``
exactly like the existing voice-service call sites: unset, unreachable, a
timeout, or an unexpected non-2xx upstream ALL collapse to
:class:`persona_api.errors.VoiceServiceUnavailableError` → 503
``voice_unavailable`` (never a leaked 500, never an upstream status/payload
leak) — the web treats this as "feature absent" and hides the read-aloud /
mic-dictation affordances. The ONE upstream status preserved verbatim is 413
(the caller's text/audio was too large): a client-actionable, common-enough
case (a long assistant reply; a long dictation clip) that collapsing it into
"unavailable" would mislead the user into thinking voice is broken rather
than that this one input was too big.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import httpx
import yaml
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from persona.schema.persona import Persona
from pydantic import BaseModel, ConfigDict, Field

from persona_api.auth import AuthenticatedUser, get_current_user
from persona_api.errors import VoiceServiceUnavailableError
from persona_api.middleware.rate_limit import rate_limit
from persona_api.services import persona_service

if TYPE_CHECKING:
    from persona.schema.persona import CatalogueVoice

router = APIRouter(prefix="/v1", tags=["voice"])

# One-shot synthesis / transcription can legitimately take a few seconds
# (Cartesia synthesis of up to ~4k chars; Deepgram prerecorded transcription
# of up to ~10MB audio) — matches StreamingTTSConfig/StreamingSTTConfig's
# own request_timeout_s default (60.0) so the proxy hop never times out
# before the voice service's own provider call would.
_PROXY_TIMEOUT = httpx.Timeout(60.0)

# R9-025 reopen — context-pinned dictation language: same shape gate as
# persona-voice's own field (persona_voice.http.app._LANGUAGE_HINT_PATTERN)
# — mirrored, not imported, so this proxy never grows a Python dependency on
# the sibling service. Rejecting a malformed hint HERE (before any upstream
# call) is the cheapest possible gate, matching this module's existing
# "fail fast before any network/DB hop" discipline. The actual language
# RESOLUTION (vocabulary/fail-soft matching) stays server-side at
# persona-voice, which owns the Deepgram-facing capability matrix — this
# proxy only validates shape and forwards the raw value verbatim.
_LANGUAGE_HINT_PATTERN = r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{1,8})?$"


class PersonaTTSRequest(BaseModel):
    """Body for ``POST /v1/personas/{persona_id}/tts``. The persona's
    configured voice is resolved server-side — the caller supplies only the
    text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(min_length=1)


class STTProxyResponse(BaseModel):
    """Result of ``POST /v1/stt`` (transcript passthrough from persona-voice)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    transcript: str


def _voice_service_base(request: Request) -> str:
    """The configured persona-voice base URL, or raise the fail-soft 503.

    Checked FIRST (before any DB hop) in both routes below — the cheapest
    possible gate, mirroring ``routes/connectors.py``'s ``initiate_link``.
    """
    base = str(getattr(request.app.state.config, "voice_service_url", "") or "").rstrip("/")
    if not base:
        raise VoiceServiceUnavailableError(
            "the voice service is not configured", context={"reason": "not_configured"}
        )
    return base


def _forwarded_auth_headers(request: Request) -> dict[str, str]:
    """Forward the caller's OWN verified bearer — no service credential introduced.

    persona-voice re-verifies the same token and only requires ANY signed-in
    user (the ``GET /v1/voices`` posture) — mirrors
    ``voice_assignment_service._fetch_catalogue`` + ``connectors.initiate_link``.
    """
    authorization = request.headers.get("Authorization")
    return {"Authorization": authorization} if authorization else {}


def _passthrough_413_or_unavailable(resp: httpx.Response, *, context: dict[str, str]) -> None:
    """Map a non-2xx upstream response to the right failure shape.

    413 (the caller's input was too large) passes through as ITS OWN 413
    with a single clean body — persona-voice already sanitises this into
    ``{"error": ..., "detail": ...}`` (never a raw provider payload), so
    unwrapping FastAPI's one ``HTTPException(detail=...)`` envelope and
    re-raising is safe. Everything else collapses to the uniform fail-soft
    503 (never an upstream status/payload leak).
    """
    if resp.status_code == 413:
        detail: dict[str, Any] | None = None
        try:
            data = resp.json()
        except ValueError:
            data = None
        if isinstance(data, dict) and isinstance(data.get("detail"), dict):
            detail = cast("dict[str, Any]", data["detail"])
        raise HTTPException(
            status_code=413,
            detail=detail or {"error": "input_too_large", "detail": "input exceeds the size limit"},
        )
    raise VoiceServiceUnavailableError(
        "the voice service could not complete the request", context=context
    )


def _extract_persona_voice(persona_row: dict[str, object]) -> CatalogueVoice | None:
    """Extract ``identity.voice`` from a persona row's stored YAML.

    Mirrors ``routes/imagegen.py``'s ``_extract_visual_style`` shape exactly.
    ``None`` when the persona has no configured voice (never auto-picked, or
    picked before TTS was configured) — the caller maps this to the SAME
    fail-soft 503 a fully-unconfigured deployment gets (context distinguishes
    the reason for diagnosability; the caller sees one honest signal).
    """
    yaml_str = str(persona_row.get("yaml", ""))
    if not yaml_str.strip():
        return None
    raw = yaml.safe_load(yaml_str)
    if not isinstance(raw, dict):
        return None
    persona = Persona.model_validate(raw)
    return persona.identity.voice


@router.post(
    "/personas/{persona_id}/tts",
    dependencies=[Depends(rate_limit("default"))],
)
async def post_persona_tts(
    persona_id: str,
    body: PersonaTTSRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),  # noqa: ARG001 — RLS via contextvar
) -> Response:
    """Read-aloud: synthesise ``text`` in THE PERSONA's configured voice.

    Ownership: :func:`persona_service.get_persona` is RLS-scoped — a
    cross-tenant ``persona_id`` surfaces as :class:`PersonaNotFoundError`
    (→ 404 via the existing handler), never a leak. Fails soft to 503 when
    the voice service is unconfigured/unreachable; 413 passes through when
    the text is too long.

    R9-025 reopen leg A: a persona with no configured voice is no longer an
    immediate local 503 — ``voice_id`` rides through as ``null`` and
    persona-voice resolves its own default (see the module docstring's
    "Voiceless-persona fallback" section). 503 ``no_voice_configured`` is
    still reachable, but only when persona-voice itself has no default.
    """
    base = _voice_service_base(request)
    persona_row = persona_service.get_persona(
        rls_engine=request.app.state.rls_engine, persona_id=persona_id
    )
    voice = _extract_persona_voice(persona_row)
    voice_id = voice.voice_id if voice is not None else None
    # Spec V14 (D-V14-13): forward the stored voice's PROVIDER so persona-voice
    # can drop a voice addressed to a different provider than its active backend
    # (a Cartesia voice under an ElevenLabs deployment) and fall back to the
    # active provider's default — instead of 4xx-ing on the wrong-provider id.
    # ``None`` when the persona has no configured voice (rides through to the
    # service default, unchanged).
    voice_provider = voice.provider if voice is not None else None

    try:
        async with httpx.AsyncClient(timeout=_PROXY_TIMEOUT) as client:
            resp = await client.post(
                f"{base}/v1/tts",
                headers=_forwarded_auth_headers(request),
                json={"text": body.text, "voice_id": voice_id, "provider": voice_provider},
            )
    except httpx.HTTPError as exc:
        raise VoiceServiceUnavailableError(
            "the voice service is unreachable", context={"persona_id": persona_id}
        ) from exc

    if resp.status_code != httpx.codes.OK:
        _passthrough_413_or_unavailable(resp, context={"persona_id": persona_id})
    return Response(content=resp.content, media_type="audio/wav")


@router.post("/stt", dependencies=[Depends(rate_limit("default"))])
async def post_stt(
    request: Request,
    audio: UploadFile = File(...),
    language: str | None = Form(
        default=None,
        min_length=2,
        max_length=16,
        pattern=_LANGUAGE_HINT_PATTERN,
    ),
    user: AuthenticatedUser = Depends(get_current_user),  # noqa: ARG001 — auth wall; owner-scoped, no persona
) -> STTProxyResponse:
    """In-chat + authoring dictation: transcribe an uploaded audio clip.

    Owner-scoped only (``get_current_user`` sets the RLS contextvar; no
    persona is involved — dictation runs before a persona necessarily
    exists, e.g. the author wizard's description field). Fails soft to 503
    when the voice service is unconfigured/unreachable; 413 passes through
    when the audio is too large.

    ``language`` (R9-025 reopen — context-pinned dictation): an optional
    ISO-639-1-ish hint, forwarded verbatim to persona-voice's
    ``POST /v1/stt``, which resolves it through
    ``persona.language_capability`` (the SAME matrix the live call pipeline
    pins per call) instead of leaning on Deepgram's own limited-coverage
    ``detect_language``. Shape-invalid → 422 here, before any upstream call
    (same validation posture as persona-voice's own field — belt-and-
    suspenders, cheapest-gate-first). The web client supplies this from
    already-loaded context: the chat composer's persona
    ``identity.language_default``, or the authoring surface's active UI
    locale — never a persona lookup here (this route stays persona-agnostic,
    per the docstring above). Omitted → unchanged detect-fallback (7647699).
    """
    base = _voice_service_base(request)
    data = await audio.read()

    try:
        async with httpx.AsyncClient(timeout=_PROXY_TIMEOUT) as client:
            resp = await client.post(
                f"{base}/v1/stt",
                headers=_forwarded_auth_headers(request),
                files={
                    "audio": (
                        audio.filename or "audio",
                        data,
                        audio.content_type or "application/octet-stream",
                    )
                },
                data={"language": language} if language else None,
            )
    except httpx.HTTPError as exc:
        raise VoiceServiceUnavailableError("the voice service is unreachable", context={}) from exc

    if resp.status_code != httpx.codes.OK:
        _passthrough_413_or_unavailable(resp, context={})
    try:
        payload = resp.json()
    except ValueError as exc:
        raise VoiceServiceUnavailableError(
            "the voice service returned a malformed response", context={}
        ) from exc
    if not isinstance(payload, dict):
        raise VoiceServiceUnavailableError(
            "the voice service returned a malformed response", context={}
        )
    return STTProxyResponse(transcript=str(payload.get("transcript", "")))


__all__ = ["PersonaTTSRequest", "STTProxyResponse", "router"]

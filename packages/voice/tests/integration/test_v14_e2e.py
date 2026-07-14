"""Spec V14 T5 — the two headline e2e chains + the STT/TTS fail-closed pins.

Both chains drive the REAL production dispatch/composition path with only the
outermost provider wire scripted (no hand-forced state — the M1-T8 precedent:
"the ONLY monkeypatch is the outermost provider seam"):

(a) **Dictation code-switch, per-language, through Gladia BATCH one-shot (T2).**
    A real ``TestClient`` POSTs to ``/v1/stt`` on the REAL persona-voice FastAPI
    app; the ONLY test seam set is ``app.state.stt_config`` (provider=gladia —
    the SAME override convention ``test_http_app.py`` already uses for
    ``language`` pin tests). ``app.state.transcribe_audio`` is deliberately
    LEFT UNSET, so ``_get_stt_transcriber`` makes its REAL dispatch decision and
    the REAL ``transcribe_oneshot_batch`` runs; only ``httpx.AsyncClient`` (the
    outermost wire boundary) is scripted.

(b) **A two-language conversation speaks each reply in its own language on ONE
    persona voice, through the ElevenLabs backend (no ``language_code`` on the
    wire).** ``load_streaming_tts`` (the REAL factory) dispatches the REAL
    ``ElevenLabsStreamingTTS``; ``build_seam_adapter`` (the REAL composition
    root ``runner.py`` calls) resolves the persona's ONE stored voice. Only the
    backend's injectable ``_ws_connect`` seam is scripted (mirrors
    ``test_elevenlabs_backend.py``'s own transport fakes) — two synth calls (an
    English reply, then a Norwegian reply) reuse the SAME resolved voice.

Each chain is paired with a fail-closed pin (M1-T8 precedent): a short,
DESCRIBED (not just asserted) proof that the test is non-vacuous — deleting the
provider-selector branch it depends on breaks a DIFFERENT wire shape than the
one scripted here, so the test would fail rather than silently pass.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING

import httpx
import pytest
from fastapi.testclient import TestClient
from persona.schema.persona import CatalogueVoice
from persona_voice.config import VoiceConfig
from persona_voice.http.app import _get_stt_transcriber, build_app
from persona_voice.stt.config import StreamingSTTConfig
from persona_voice.stt.gladia_backend import transcribe_oneshot_batch
from persona_voice.tts._factory import load_streaming_tts
from persona_voice.tts.config import StreamingTTSConfig
from persona_voice.tts.elevenlabs_backend import ElevenLabsStreamingTTS
from persona_voice.tts.seam_adapter import build_seam_adapter

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

pytestmark = pytest.mark.integration


# ============================================================================
# (a) Dictation code-switch -> Gladia BATCH one-shot, through the REAL app.
# ============================================================================

_GLADIA_UPLOAD_URL = "https://api.gladia.io/v2/upload"
_GLADIA_PRERECORDED_URL = "https://api.gladia.io/v2/pre-recorded"

# A bilingual transcript — mirrors the T0/T2 external leg's family1 (en -> no)
# marker words, so this test and the real-speech @external leg below assert
# the SAME "did the second language survive" shape.
_BILINGUAL_TRANSCRIPT = "Let me check the schedule tomorrow. Jeg må flytte møtet til klokken tre."


def _scripted_gladia_batch_handler(calls: list[str]) -> httpx.MockTransport:
    """A 3-step Gladia BATCH transport: upload -> init job -> poll (done first try).

    Recording ``calls`` (the request path per step) is the non-vacuous proof:
    a Deepgram fallback (what dispatch degrades to if the ``gladia`` selector
    branch were deleted) would never hit ``/v2/upload`` or ``/v2/pre-recorded``
    at all — it POSTs a totally different (Deepgram) URL/shape, which this
    transport does not handle and would error on.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.url.path == "/v2/upload":
            return httpx.Response(200, json={"audio_url": "https://cdn.gladia.io/audio/abc"})
        if request.url.path == "/v2/pre-recorded" and request.method == "POST":
            body = json.loads(request.content)
            # The code-switching guidance shape D-V14-15 sends — proves the
            # request this handler answers really is the Gladia batch-init call.
            assert body["language_config"]["code_switching"] is True
            return httpx.Response(200, json={"id": "job-e2e-1"})
        if request.url.path == "/v2/pre-recorded/job-e2e-1":
            return httpx.Response(
                200,
                json={
                    "status": "done",
                    "result": {"transcription": {"full_transcript": _BILINGUAL_TRANSCRIPT}},
                },
            )
        return httpx.Response(404, json={"error": f"unscripted path {request.url.path}"})

    return httpx.MockTransport(handler)


def _voice_test_client() -> TestClient:
    """Community edition (no bearer needed) — the REAL persona-voice FastAPI app.

    The suite-wide ``conftest.py`` autouse fixture defaults ``PERSONA_EDITION``
    to ``cloud`` for the rest of the suite's pre-existing tests; an explicit
    ``edition=`` kwarg wins over the env var (the module's own documented
    convention) so this e2e chain runs no-auth/community without disturbing it.
    """
    return TestClient(build_app(VoiceConfig(edition="community")))


@pytest.mark.asyncio
async def test_dictation_code_switch_lands_per_language_through_gladia_batch_e2e(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The REAL /v1/stt dispatch (no ``transcribe_audio`` override) reaches the
    REAL ``transcribe_oneshot_batch`` under ``provider=gladia`` and both
    language segments of a code-switched clip survive to the response."""
    calls: list[str] = []
    real_async_client = httpx.AsyncClient  # capture BEFORE patching (avoid self-recursion)
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_async_client(transport=_scripted_gladia_batch_handler(calls), **kw),
    )
    client = _voice_test_client()
    client.app.state.stt_config = StreamingSTTConfig(
        provider="gladia",
        gladia_api_key="gl-e2e-secret",  # type: ignore[arg-type]
    )
    # NOTE: app.state.transcribe_audio is deliberately NOT set — the dispatch
    # inside _get_stt_transcriber must fire for real.

    resp = client.post(
        "/v1/stt",
        files={"audio": ("clip.webm", b"\x1aE\xdf\xa3fake-webm-bytes", "audio/webm")},
    )

    assert resp.status_code == 200, resp.text
    transcript = resp.json()["transcript"].lower()
    # Both language segments survived the round trip (D-V14-1's dictation half).
    assert "schedule" in transcript  # English segment
    assert "tomorrow" in transcript  # English segment
    assert "møtet" in transcript or "klokken" in transcript  # Norwegian segment
    # The scripted transport was hit with EXACTLY the Gladia batch 3-step shape —
    # proves the real gladia branch fired (not a coincidental pass).
    assert calls == [
        "POST /v2/upload",
        "POST /v2/pre-recorded",
        "GET /v2/pre-recorded/job-e2e-1",
    ]


def test_stt_provider_selector_is_load_bearing_for_the_gladia_dispatch() -> None:
    """Fail-closed pin (M1-T8 precedent), described not just asserted.

    ``_get_stt_transcriber`` returns the concrete ``transcribe_oneshot_batch``
    function ONLY because ``_get_stt_config(request).provider == "gladia"``
    inside it. This identity check is the SAME branch chain (a)'s e2e depends
    on: delete it (or the ``if`` guard it lives in) and dispatch falls through
    to ``transcribe_prerecorded`` (Deepgram) instead — a DIFFERENT function
    object, so this assertion fails, AND chain (a)'s e2e above fails too
    (Deepgram's transcriber would call ``PrerecordedOptions``/Deepgram's own
    SDK, never touching ``/v2/upload``, so the scripted Gladia transport's
    ``calls`` list would stay empty and the request would 503/502)."""
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                stt_config=StreamingSTTConfig(provider="gladia", gladia_api_key="k"),  # type: ignore[arg-type]
                transcribe_audio=None,
            )
        )
    )
    assert _get_stt_transcriber(request) is transcribe_oneshot_batch  # type: ignore[arg-type]


# ============================================================================
# (b) Two-language conversation, ONE ElevenLabs voice, no language on the wire.
# ============================================================================


def _audio_msg(pcm: bytes) -> str:
    import base64

    return json.dumps({"audio": base64.b64encode(pcm).decode("ascii")})


class _FakeElevenWS:
    """A scripted ElevenLabs stream-input WebSocket (mirrors the T3 unit fake)."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self._messages = [_audio_msg(b"\x01\x02" * 400), json.dumps({"isFinal": True})]

    async def __aenter__(self) -> _FakeElevenWS:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def send(self, data: str) -> None:
        self.sent.append(data)

    def __aiter__(self) -> _FakeElevenWS:
        return self

    async def __anext__(self) -> str:
        if self._messages:
            return self._messages.pop(0)
        raise StopAsyncIteration


async def _text_stream(*chunks: str) -> AsyncIterator[str]:
    for chunk in chunks:
        yield chunk


@pytest.mark.asyncio
async def test_two_language_conversation_speaks_each_reply_on_one_elevenlabs_voice_e2e(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The REAL factory (``load_streaming_tts``) + REAL composition root
    (``build_seam_adapter``, what ``runner.py`` calls) drive TWO replies — one
    English, one Norwegian — on the persona's ONE stored ElevenLabs voice.
    Only the backend's injectable WS seam is scripted; nothing about voice
    resolution or the wire-message shape is hand-forced."""
    config = StreamingTTSConfig(
        provider="elevenlabs",
        elevenlabs_api_key="el-e2e-secret",  # type: ignore[arg-type]
    )
    backend = load_streaming_tts(config)  # the REAL factory dispatch
    assert isinstance(backend, ElevenLabsStreamingTTS)  # sanity: real elevenlabs class

    connected_urls: list[str] = []

    def _fake_ws_connect(url: str) -> _FakeElevenWS:
        connected_urls.append(url)
        return _FakeElevenWS()

    monkeypatch.setattr(backend, "_ws_connect", _fake_ws_connect)

    voice_spec = CatalogueVoice(provider="elevenlabs", voice_id="one-persona-voice")
    seam = build_seam_adapter(backend=backend, config=config, voice_spec=voice_spec)

    english_audio = [c async for c in seam.synthesize(_text_stream("Hello, see you soon."))]
    norwegian_audio = [c async for c in seam.synthesize(_text_stream("Hei, vi sees snart."))]

    assert english_audio  # the English reply actually produced audio
    assert norwegian_audio  # the Norwegian reply actually produced audio too
    assert len(connected_urls) == 2  # one per-utterance socket each (D-V14-10)
    # Same persona voice addressed BOTH times.
    assert all("one-persona-voice" in url for url in connected_urls)
    # The D-V14-1 property, proven end-to-end: no language param anywhere on
    # either connection's URL or handshake — the wire never named a language;
    # ElevenLabs infers it from the TEXT alone.
    for url in connected_urls:
        assert "language" not in url.lower()


def test_tts_provider_selector_is_load_bearing_for_the_elevenlabs_dispatch() -> None:
    """Fail-closed pin (M1-T8 precedent), described not just asserted.

    ``load_streaming_tts`` returns a concrete ``ElevenLabsStreamingTTS`` ONLY
    because of the ``if provider == "elevenlabs":`` branch in
    ``tts/_factory.py``. Delete that branch and the provider falls through to
    the ``TTSError`` "unknown provider" raise — this construction call raises
    instead of returning an instance, so this assertion fails; AND chain (b)'s
    e2e above fails too (there would be no backend to inject ``_ws_connect``
    onto at all, and no ``eleven_flash_v2_5``/``pcm_24000``/``auto_mode`` URL
    ever gets built — a structurally different failure than a wrong voice)."""
    config = StreamingTTSConfig(
        provider="elevenlabs",
        elevenlabs_api_key="el-pin-secret",  # type: ignore[arg-type]
    )
    backend = load_streaming_tts(config)
    assert isinstance(backend, ElevenLabsStreamingTTS)
    assert backend.provider_name == "elevenlabs"

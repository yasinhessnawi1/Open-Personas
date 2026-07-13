"""Unit tests for the persona-voice HTTP app (spec V1 T04).

Mounts the FastAPI app with a fake JWT verifier and a fake ``owns_persona``
override so the suite needs neither Clerk nor a database. Tests cover the
auth seam, the ownership check, the happy-path response shape, and the
failure modes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient
from jose import jwt
from persona.auth.jwt_verifier import AuthenticatedUser
from persona.errors import AuthenticationError, CreditsExhaustedError
from persona_voice.config import VoiceConfig
from persona_voice.http.app import build_app
from persona_voice.loop.streaming import AudioChunk
from persona_voice.stt.config import StreamingSTTConfig
from persona_voice.stt.errors import STTAuthenticationError, STTStreamFailureError
from persona_voice.tts.config import StreamingTTSConfig
from persona_voice.tts.errors import TTSAuthenticationError, TTSStreamFailureError
from persona_voice.tts.types import ResolvedVoice, VoiceCatalogueEntry
from pydantic import SecretStr


def _build_test_client(
    *,
    owns_persona_result: bool = True,
    credits_balance: int = 100,
) -> TestClient:
    cfg = VoiceConfig(
        livekit_url="ws://localhost:7880",
        livekit_api_key=SecretStr("lk_key_test"),
        livekit_api_secret=SecretStr("very-very-long-test-secret-for-hs256-signing"),
        jwt_secret=SecretStr("s3cret"),
        jwt_algorithms="HS256",
    )
    app = build_app(cfg)

    async def _fake_verify(token: str) -> AuthenticatedUser:
        if token == "good":
            return AuthenticatedUser(id="user_test", email="a@x.test")
        raise AuthenticationError("bad token")

    app.state.verify_token = _fake_verify

    def _owns(*, persona_id: str, user_id: str) -> bool:  # noqa: ARG001
        return owns_persona_result

    app.state.owns_persona = _owns

    def _require_credits(*, user_id: str) -> None:  # noqa: ARG001
        if credits_balance <= 0:
            raise CreditsExhaustedError(
                "Your free credits are used up.",
                context={"balance": str(credits_balance)},
            )

    app.state.require_credits = _require_credits
    return TestClient(app)


def test_token_endpoint_requires_bearer() -> None:
    client = _build_test_client()
    resp = client.post(
        "/v1/voice/token",
        json={"persona_id": "p1", "conversation_id": "c1"},
    )
    assert resp.status_code == 401
    assert resp.json()["error"] == "authentication_error"
    assert resp.headers["WWW-Authenticate"] == "Bearer"


def test_token_endpoint_rejects_invalid_bearer() -> None:
    client = _build_test_client()
    resp = client.post(
        "/v1/voice/token",
        headers={"Authorization": "Bearer wrong"},
        json={"persona_id": "p1", "conversation_id": "c1"},
    )
    assert resp.status_code == 401


def test_token_endpoint_404_when_persona_not_owned() -> None:
    client = _build_test_client(owns_persona_result=False)
    resp = client.post(
        "/v1/voice/token",
        headers={"Authorization": "Bearer good"},
        json={"persona_id": "p_other_tenant", "conversation_id": "c1"},
    )
    # RLS-shape: never leaks whether the persona exists for another tenant.
    assert resp.status_code == 404


def test_token_endpoint_happy_path_returns_signed_token() -> None:
    client = _build_test_client()
    resp = client.post(
        "/v1/voice/token",
        headers={"Authorization": "Bearer good"},
        json={"persona_id": "p_astrid", "conversation_id": "c_42"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"token", "room_name", "livekit_url"}
    assert body["livekit_url"] == "ws://localhost:7880"
    assert body["room_name"].startswith("persona:")
    # The minted token decodes against our test signing secret.
    decoded = jwt.decode(
        body["token"],
        "very-very-long-test-secret-for-hs256-signing",
        algorithms=["HS256"],
        options={"verify_aud": False},
    )
    assert decoded["sub"] == "user_test"
    assert decoded["video"]["room"] == body["room_name"]
    assert decoded["video"]["roomJoin"] is True


def test_token_endpoint_402_when_credits_exhausted() -> None:
    """Mirrors the persona-api chat 402 contract (D-11-12 / D-19-X-voice-token-credit-gate).

    The voice token must NOT be minted when the caller is out of credits —
    otherwise the LiveKit Room joins succeed and the deduct-per-turn path
    can't recover the wasted signaling round-trip. Per-turn deductions during
    the call are a separate concern (not asserted here).
    """
    client = _build_test_client(credits_balance=0)
    resp = client.post(
        "/v1/voice/token",
        headers={"Authorization": "Bearer good"},
        json={"persona_id": "p_astrid", "conversation_id": "c_42"},
    )
    assert resp.status_code == 402
    body = resp.json()
    assert body["error"] == "credits_exhausted"
    assert body["context"]["balance"] == "0"


def test_token_endpoint_200_when_credits_positive() -> None:
    """Balance > 0 lets the mint proceed (the deduct happens per-turn, not here)."""
    client = _build_test_client(credits_balance=1)
    resp = client.post(
        "/v1/voice/token",
        headers={"Authorization": "Bearer good"},
        json={"persona_id": "p_astrid", "conversation_id": "c_42"},
    )
    assert resp.status_code == 200


def test_token_endpoint_rejects_body_with_extra_fields() -> None:
    """The body schema has ``extra='forbid'`` so unknown fields are rejected
    (defense-in-depth: keeps clients from accidentally smuggling state).
    """
    client = _build_test_client()
    resp = client.post(
        "/v1/voice/token",
        headers={"Authorization": "Bearer good"},
        json={"persona_id": "p", "conversation_id": "c", "owner_id": "spoofed"},
    )
    assert resp.status_code == 422


def test_community_edition_mints_token_with_no_auth() -> None:
    """Spec 33 (D-33-X-voice-edition): community voice is no-auth, no-credits.

    No bearer header, no JWT secret, no ownership/credits overrides — the token
    is minted for the fixed local owner. Note: no ``app.state.verify_token``
    override is set, so the community no-auth path is exercised end-to-end.
    """
    cfg = VoiceConfig(
        edition="community",
        livekit_url="ws://localhost:7880",
        livekit_api_key=SecretStr("lk_key_test"),
        livekit_api_secret=SecretStr("very-very-long-test-secret-for-hs256-signing"),
    )
    client = TestClient(build_app(cfg))
    resp = client.post(
        "/v1/voice/token",
        json={"persona_id": "p_astrid", "conversation_id": "c_42"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"token", "room_name", "livekit_url"}
    decoded = jwt.decode(
        body["token"],
        "very-very-long-test-secret-for-hs256-signing",
        algorithms=["HS256"],
        options={"verify_aud": False},
    )
    assert decoded["sub"] == "local-owner"


def test_voice_config_reads_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSONA_VOICE_LIVEKIT_URL", "wss://lk.test")
    monkeypatch.setenv("PERSONA_VOICE_LIVEKIT_API_KEY", "ak_env")
    monkeypatch.setenv("PERSONA_VOICE_LIVEKIT_API_SECRET", "as_env_super_long_secret")
    monkeypatch.setenv("PERSONA_VOICE_JWT_SECRET", "env_secret")
    monkeypatch.setenv("PERSONA_VOICE_JWT_ALGORITHMS", "HS256,RS256")
    cfg = VoiceConfig()
    assert cfg.livekit_url == "wss://lk.test"
    assert cfg.livekit_api_key.get_secret_value() == "ak_env"
    assert cfg.livekit_api_secret.get_secret_value() == "as_env_super_long_secret"
    assert cfg.jwt_secret is not None
    assert cfg.jwt_secret.get_secret_value() == "env_secret"
    # Comma-separated list parsed by the computed property.
    assert cfg.jwt_algorithms_list == ["HS256", "RS256"]


# ---- dev auth posture, real (non-overridden) verify path (R9-025 reopen leg A) ---
#
# Every other test in this file sets ``app.state.verify_token`` to a fake, which
# ALWAYS wins over the real ``get_verify_token``/``make_jwt_verifier`` path (see
# that function's docstring) — so none of them ever exercise the actual
# cfg-driven cloud-edition verifier. These tests build the app with NO override,
# so the real path runs: proving both documented dev postures (RS256 against a
# PEM — the run-local.sh / prod posture; HS256 against a shared secret — the
# lighter dev alternative) actually verify a real signed token end-to-end, and
# that a misconfigured cloud edition fails CLOSED with a clean 401 rather than
# an unhandled construction-time crash.


def _build_cloud_app(
    *,
    jwt_secret: SecretStr | None = None,
    jwt_public_key: SecretStr | None = None,
    jwt_algorithms: str = "HS256",
) -> TestClient:
    cfg = VoiceConfig(
        edition="cloud",
        livekit_url="ws://localhost:7880",
        livekit_api_key=SecretStr("lk_key_test"),
        livekit_api_secret=SecretStr("very-very-long-test-secret-for-hs256-signing"),
        jwt_secret=jwt_secret,
        jwt_public_key=jwt_public_key,
        jwt_algorithms=jwt_algorithms,
    )
    app = build_app(cfg)
    # No app.state.verify_token override — the REAL get_verify_token path runs.
    return TestClient(app)


def _rsa_keypair() -> tuple[str, str]:
    """A throwaway RSA keypair — mirrors ``test_jwt_verifier.py``'s own helper."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    pub = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return priv, pub


def test_cloud_edition_rs256_dev_posture_verifies_a_real_token() -> None:
    """The documented dev/prod posture: RS256 against a Clerk-dashboard-shaped
    PEM in ``PERSONA_VOICE_JWT_PUBLIC_KEY`` — the SAME mechanism
    ``packages/api/run-local.sh`` already wires for local dev (reading
    ``.secrets/clerk-jwt-public.pem``) and Fly sets as a secret in prod."""
    priv, pub = _rsa_keypair()
    client = _build_cloud_app(jwt_public_key=SecretStr(pub), jwt_algorithms="RS256")
    token = jwt.encode({"sub": "user_rs256"}, priv, algorithm="RS256")
    resp = client.get("/v1/voices", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200


def test_cloud_edition_hs256_dev_posture_verifies_a_real_token() -> None:
    """The documented lighter-weight dev alternative: a shared secret in
    ``PERSONA_VOICE_JWT_SECRET`` (no PEM to source)."""
    client = _build_cloud_app(jwt_secret=SecretStr("dev-shared-secret"), jwt_algorithms="HS256")
    token = jwt.encode({"sub": "user_hs256"}, "dev-shared-secret", algorithm="HS256")
    resp = client.get("/v1/voices", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200


def test_cloud_edition_unconfigured_jwt_401s_cleanly_instead_of_crashing() -> None:
    """R9-025 reopen: before this fix, cloud edition with NEITHER
    ``PERSONA_VOICE_JWT_PUBLIC_KEY`` nor ``PERSONA_VOICE_JWT_SECRET`` set made
    ``make_jwt_verifier`` raise an unhandled ``ValueError`` at
    dependency-construction time — every request 500'd, with no signal telling
    an operator what to fix. It must still fail CLOSED (no caller gets
    through), but now as the same diagnosable 401 shape a bad bearer gets."""
    client = _build_cloud_app()  # no jwt_secret, no jwt_public_key configured
    resp = client.get("/v1/voices", headers={"Authorization": "Bearer whatever"})
    assert resp.status_code == 401
    assert resp.json()["error"] == "authentication_error"


# ---------- GET /v1/voices (spec V6 C2) -------------------------------------


class _FakeCatalogue:
    """A VoiceCatalogue stub returning one entry (no provider/network)."""

    @property
    def provider_name(self) -> str:
        return "cartesia"

    async def list_voices(
        self,
        *,
        gender: object = None,  # noqa: ARG002
        language: object = None,  # noqa: ARG002
        limit: int | None = None,  # noqa: ARG002
    ) -> tuple[VoiceCatalogueEntry, ...]:
        return (
            VoiceCatalogueEntry(
                voice_id="v_clara",
                name="Clara",
                gender="feminine",
                language="en",
                description="warm & professional",
                preview_url="https://cdn.test/clara.mp3",
            ),
        )


def test_voices_endpoint_requires_bearer() -> None:
    client = _build_test_client()
    resp = client.get("/v1/voices")
    assert resp.status_code == 401


def test_voices_endpoint_returns_catalogue_with_preview_url() -> None:
    client = _build_test_client()
    client.app.state.voice_catalogue = _FakeCatalogue()
    resp = client.get("/v1/voices", headers={"Authorization": "Bearer good"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["provider"] == "cartesia"
    assert len(data["voices"]) == 1
    assert data["voices"][0]["voice_id"] == "v_clara"
    assert data["voices"][0]["gender"] == "feminine"
    # preview_url is passed through for the voice-selector's hear-before-choosing.
    assert data["voices"][0]["preview_url"] == "https://cdn.test/clara.mp3"


class _RecordingCatalogue(_FakeCatalogue):
    """Records the `language` the endpoint forwards to the catalogue filter."""

    def __init__(self) -> None:
        self.seen_language: object = "UNSET"

    async def list_voices(
        self,
        *,
        gender: object = None,  # noqa: ARG002
        language: object = None,
        limit: int | None = None,  # noqa: ARG002
    ) -> tuple[VoiceCatalogueEntry, ...]:
        self.seen_language = language
        return ()


def test_voices_endpoint_normalizes_and_filters_by_language() -> None:
    """Spec 32 — `?language=nb` filters voices to the served `no` code so an
    author can't pick a voice the persona's declared language can't speak."""
    client = _build_test_client()
    catalogue = _RecordingCatalogue()
    client.app.state.voice_catalogue = catalogue
    resp = client.get(
        "/v1/voices", params={"language": "nb"}, headers={"Authorization": "Bearer good"}
    )
    assert resp.status_code == 200
    assert catalogue.seen_language == "no"  # nb normalized to the served Norwegian code


def test_voices_endpoint_no_language_filter_when_omitted() -> None:
    client = _build_test_client()
    catalogue = _RecordingCatalogue()
    client.app.state.voice_catalogue = catalogue
    resp = client.get("/v1/voices", headers={"Authorization": "Bearer good"})
    assert resp.status_code == 200
    assert catalogue.seen_language is None  # no filter → all voices


def test_voices_endpoint_returns_empty_when_tts_unconfigured() -> None:
    client = _build_test_client()
    # Simulate the no-PERSONA_TTS_API_KEY path: catalogue resolves to None.
    client.app.state.voice_catalogue = None
    resp = client.get("/v1/voices", headers={"Authorization": "Bearer good"})
    assert resp.status_code == 200
    assert resp.json() == {"provider": None, "voices": []}


# ---------- POST /v1/tts (R9-025a one-shot synthesis) -----------------------


class _FakeTTSBackend:
    """A ``StreamingTTS``-conforming double for ``POST /v1/tts`` route tests.

    Also satisfies ``VoiceCatalogue`` (a bare ``provider_name`` + unused
    ``list_voices``) since ``_get_tts_backend`` reuses the SAME
    ``app.state.voice_catalogue`` seam ``GET /v1/voices`` tests already use.
    """

    def __init__(
        self,
        *,
        chunks: list[bytes] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._chunks = [b"\x01\x00" * 100] if chunks is None else chunks
        self._error = error
        self.seen_texts: list[str] = []
        self.seen_voice_refs: list[str] = []

    @property
    def provider_name(self) -> str:
        return "cartesia"

    async def list_voices(self, **_kwargs: object) -> tuple[VoiceCatalogueEntry, ...]:
        return ()

    async def synthesize(
        self, text_stream: AsyncIterator[str], voice: ResolvedVoice
    ) -> AsyncIterator[AudioChunk]:
        self.seen_voice_refs.append(voice.voice_ref)
        async for chunk in text_stream:
            self.seen_texts.append(chunk)
        if self._error is not None:
            raise self._error
        for data in self._chunks:
            yield AudioChunk(
                data=data, sample_rate=24000, num_channels=1, samples_per_channel=len(data) // 2
            )


def test_tts_endpoint_requires_bearer() -> None:
    client = _build_test_client()
    resp = client.post("/v1/tts", json={"text": "hello", "voice_id": "v1"})
    assert resp.status_code == 401


def test_tts_endpoint_503_when_tts_unconfigured() -> None:
    client = _build_test_client()
    client.app.state.voice_catalogue = None
    resp = client.post(
        "/v1/tts",
        headers={"Authorization": "Bearer good"},
        json={"text": "hello", "voice_id": "v1"},
    )
    assert resp.status_code == 503
    assert resp.json()["detail"]["error"] == "tts_unavailable"


def test_tts_endpoint_returns_playable_wav_audio() -> None:
    client = _build_test_client()
    backend = _FakeTTSBackend(chunks=[b"\x01\x00" * 500])
    client.app.state.voice_catalogue = backend
    resp = client.post(
        "/v1/tts",
        headers={"Authorization": "Bearer good"},
        json={"text": "Hello there.", "voice_id": "v_clara"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "audio/wav"
    # Canonical RIFF/WAVE header — a browser <audio> element can play this
    # directly with no client-side decoding.
    assert resp.content[:4] == b"RIFF"
    assert resp.content[8:12] == b"WAVE"
    assert len(resp.content) > 44  # header + actual PCM payload
    assert backend.seen_texts == ["Hello there."]
    assert backend.seen_voice_refs == ["v_clara"]


def test_tts_endpoint_reuses_the_cached_catalogue_backend_instance() -> None:
    """R9-025a's "reuse the existing backend class": the SAME object GET
    /v1/voices warms is the one POST /v1/tts synthesizes through — no new
    provider client, no duplicate connections."""
    client = _build_test_client()
    backend = _FakeTTSBackend()
    client.app.state.voice_catalogue = backend
    client.post(
        "/v1/tts",
        headers={"Authorization": "Bearer good"},
        json={"text": "hi", "voice_id": "v1"},
    )
    voices_resp = client.get("/v1/voices", headers={"Authorization": "Bearer good"})
    assert voices_resp.status_code == 200
    # Still the exact same instance on app.state — nothing replaced it.
    assert client.app.state.voice_catalogue is backend


def test_tts_endpoint_413_when_text_too_long() -> None:
    client = _build_test_client()
    client.app.state.voice_catalogue = _FakeTTSBackend()
    resp = client.post(
        "/v1/tts",
        headers={"Authorization": "Bearer good"},
        json={"text": "x" * 4001, "voice_id": "v1"},
    )
    assert resp.status_code == 413
    assert resp.json()["detail"]["error"] == "tts_text_too_long"


def test_tts_endpoint_rejects_body_with_extra_fields() -> None:
    client = _build_test_client()
    client.app.state.voice_catalogue = _FakeTTSBackend()
    resp = client.post(
        "/v1/tts",
        headers={"Authorization": "Bearer good"},
        json={"text": "hi", "voice_id": "v1", "owner_id": "spoofed"},
    )
    assert resp.status_code == 422


# ---- voiceless-caller fallback (R9-025 reopen leg A) ------------------------


def test_tts_endpoint_uses_the_configured_default_when_voice_id_is_omitted() -> None:
    """The proxy's voiceless-persona contract: no ``voice_id`` key at all."""
    client = _build_test_client()
    backend = _FakeTTSBackend()
    client.app.state.voice_catalogue = backend
    client.app.state.tts_stream_config = StreamingTTSConfig(voice_default="v_default")
    resp = client.post(
        "/v1/tts",
        headers={"Authorization": "Bearer good"},
        json={"text": "hi"},
    )
    assert resp.status_code == 200, resp.text
    assert backend.seen_voice_refs == ["v_default"]


def test_tts_endpoint_uses_the_configured_default_when_voice_id_is_explicit_null() -> None:
    """The EXACT shape the api proxy sends for a voiceless persona: ``voice_id: null``."""
    client = _build_test_client()
    backend = _FakeTTSBackend()
    client.app.state.voice_catalogue = backend
    client.app.state.tts_stream_config = StreamingTTSConfig(voice_default="v_default")
    resp = client.post(
        "/v1/tts",
        headers={"Authorization": "Bearer good"},
        json={"text": "hi", "voice_id": None},
    )
    assert resp.status_code == 200, resp.text
    assert backend.seen_voice_refs == ["v_default"]


def test_tts_endpoint_explicit_voice_id_wins_over_the_default() -> None:
    client = _build_test_client()
    backend = _FakeTTSBackend()
    client.app.state.voice_catalogue = backend
    client.app.state.tts_stream_config = StreamingTTSConfig(voice_default="v_default")
    resp = client.post(
        "/v1/tts",
        headers={"Authorization": "Bearer good"},
        json={"text": "hi", "voice_id": "v_explicit"},
    )
    assert resp.status_code == 200, resp.text
    assert backend.seen_voice_refs == ["v_explicit"]


def test_tts_endpoint_503_no_voice_configured_when_no_id_and_no_default() -> None:
    client = _build_test_client()
    backend = _FakeTTSBackend()
    client.app.state.voice_catalogue = backend
    client.app.state.tts_stream_config = StreamingTTSConfig(voice_default=None)
    resp = client.post(
        "/v1/tts",
        headers={"Authorization": "Bearer good"},
        json={"text": "hi"},
    )
    assert resp.status_code == 503
    body = resp.json()["detail"]
    assert body["error"] == "tts_unavailable"
    assert body["reason"] == "no_voice_configured"
    # The backend was never called — nothing to synthesize without a voice.
    assert backend.seen_voice_refs == []


def test_tts_endpoint_503_on_authentication_error_never_leaks_provider_payload() -> None:
    client = _build_test_client()
    client.app.state.voice_catalogue = _FakeTTSBackend(
        error=TTSAuthenticationError(
            "raw provider secret leak: sk-cartesia-abc123", context={"provider": "cartesia"}
        )
    )
    resp = client.post(
        "/v1/tts",
        headers={"Authorization": "Bearer good"},
        json={"text": "hi", "voice_id": "v1"},
    )
    assert resp.status_code == 503
    body = resp.json()
    assert body["detail"]["error"] == "tts_unavailable"
    assert "sk-cartesia-abc123" not in resp.text  # never the raw provider payload


def test_tts_endpoint_502_on_provider_stream_failure_with_fixed_reason() -> None:
    client = _build_test_client()
    client.app.state.voice_catalogue = _FakeTTSBackend(
        error=TTSStreamFailureError(
            "raw provider secret leak: sk-cartesia-abc123", context={"provider": "cartesia"}
        )
    )
    resp = client.post(
        "/v1/tts",
        headers={"Authorization": "Bearer good"},
        json={"text": "hi", "voice_id": "v1"},
    )
    assert resp.status_code == 502
    body = resp.json()
    assert body["detail"]["error"] == "tts_provider_error"
    assert body["detail"]["reason"] == "provider_stream_failed"
    assert "sk-cartesia-abc123" not in resp.text


def test_tts_endpoint_502_when_no_audio_produced() -> None:
    """The backend's own fail-soft "no audio" path (e.g. an unsupported
    language even in English fallback) surfaces as a provider error here —
    a one-shot caller asked for speech, silence is not a 200."""
    client = _build_test_client()
    client.app.state.voice_catalogue = _FakeTTSBackend(chunks=[])
    resp = client.post(
        "/v1/tts",
        headers={"Authorization": "Bearer good"},
        json={"text": "hi", "voice_id": "v1"},
    )
    assert resp.status_code == 502
    assert resp.json()["detail"]["reason"] == "no_audio_produced"


# ---------- POST /v1/stt (R9-025a one-shot prerecorded transcription) ------


class _FakeTranscriber:
    """A callable double matching ``transcribe_prerecorded``'s signature.

    Installed on ``app.state.transcribe_audio`` — the same test-seam
    convention as ``owns_persona`` / ``require_credits`` elsewhere in this
    file (see :func:`persona_voice.http.app._get_stt_transcriber`).
    """

    def __init__(self, *, transcript: str = "hello world", error: Exception | None = None) -> None:
        self._transcript = transcript
        self._error = error
        self.calls: list[dict[str, object]] = []

    async def __call__(
        self,
        audio: bytes,
        *,
        config: StreamingSTTConfig,
        content_type: str | None = None,
    ) -> str:
        self.calls.append({"audio": audio, "config": config, "content_type": content_type})
        if self._error is not None:
            raise self._error
        return self._transcript


def test_stt_endpoint_requires_bearer() -> None:
    client = _build_test_client()
    resp = client.post("/v1/stt", files={"audio": ("clip.wav", b"RIFF....", "audio/wav")})
    assert resp.status_code == 401


def test_stt_endpoint_returns_transcript() -> None:
    client = _build_test_client()
    transcriber = _FakeTranscriber(transcript="hello there")
    client.app.state.transcribe_audio = transcriber
    resp = client.post(
        "/v1/stt",
        headers={"Authorization": "Bearer good"},
        files={"audio": ("clip.webm", b"\x00\x01fake-audio-bytes", "audio/webm")},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"transcript": "hello there"}
    assert transcriber.calls[0]["content_type"] == "audio/webm"
    assert transcriber.calls[0]["audio"] == b"\x00\x01fake-audio-bytes"


def test_stt_endpoint_413_when_audio_too_large() -> None:
    client = _build_test_client()
    client.app.state.transcribe_audio = _FakeTranscriber()
    oversized = b"\x00" * (10 * 1024 * 1024 + 1)
    resp = client.post(
        "/v1/stt",
        headers={"Authorization": "Bearer good"},
        files={"audio": ("clip.wav", oversized, "audio/wav")},
    )
    assert resp.status_code == 413
    assert resp.json()["detail"]["error"] == "stt_audio_too_large"


def test_stt_endpoint_503_on_authentication_error_never_leaks_provider_payload() -> None:
    client = _build_test_client()
    client.app.state.transcribe_audio = _FakeTranscriber(
        error=STTAuthenticationError(
            "raw provider secret leak: dg-abc123", context={"provider": "deepgram"}
        )
    )
    resp = client.post(
        "/v1/stt",
        headers={"Authorization": "Bearer good"},
        files={"audio": ("clip.wav", b"fake-bytes", "audio/wav")},
    )
    assert resp.status_code == 503
    body = resp.json()
    assert body["detail"]["error"] == "stt_unavailable"
    assert "dg-abc123" not in resp.text


def test_stt_endpoint_502_on_provider_stream_failure_with_fixed_reason() -> None:
    client = _build_test_client()
    client.app.state.transcribe_audio = _FakeTranscriber(
        error=STTStreamFailureError(
            "raw provider secret leak: dg-abc123", context={"provider": "deepgram"}
        )
    )
    resp = client.post(
        "/v1/stt",
        headers={"Authorization": "Bearer good"},
        files={"audio": ("clip.wav", b"fake-bytes", "audio/wav")},
    )
    assert resp.status_code == 502
    body = resp.json()
    assert body["detail"]["error"] == "stt_provider_error"
    assert body["detail"]["reason"] == "provider_stream_failed"
    assert "dg-abc123" not in resp.text


# ---------- POST /v1/stt `language` (R9-025 reopen — context-pinned dictation) ----


def test_stt_endpoint_language_hint_pins_via_the_capability_registry() -> None:
    """A provided hint overrides the config's language_hint AND model —
    exactly `apply_stt_route`'s update shape (Spec 32 B3) — before the
    transcriber is ever called. No `language` in the response body; this
    asserts on what the transcriber actually received."""
    client = _build_test_client()
    transcriber = _FakeTranscriber(transcript="hallo der")
    client.app.state.transcribe_audio = transcriber
    resp = client.post(
        "/v1/stt",
        headers={"Authorization": "Bearer good"},
        files={"audio": ("clip.webm", b"fake-bytes", "audio/webm")},
        data={"language": "no"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"transcript": "hallo der"}
    sent_config = transcriber.calls[0]["config"]
    assert isinstance(sent_config, StreamingSTTConfig)
    assert sent_config.language_hint == "no"
    assert sent_config.model == "nova-3"


def test_stt_endpoint_language_hint_normalizes_bcp47_variants_like_a_call() -> None:
    """`"nb"` (Norwegian Bokmål) collapses to the provider-served `"no"` —
    the SAME D-32-X-norwegian-collapse-to-no mapping the live call pipeline
    applies to a persona's declared `identity.language_default` — proving
    this route reuses the Spec 32 vocabulary rather than piping a raw code
    straight to Deepgram."""
    client = _build_test_client()
    transcriber = _FakeTranscriber()
    client.app.state.transcribe_audio = transcriber
    resp = client.post(
        "/v1/stt",
        headers={"Authorization": "Bearer good"},
        files={"audio": ("clip.webm", b"fake-bytes", "audio/webm")},
        data={"language": "nb"},
    )
    assert resp.status_code == 200, resp.text
    sent_config = transcriber.calls[0]["config"]
    assert isinstance(sent_config, StreamingSTTConfig)
    assert sent_config.language_hint == "no"


def test_stt_endpoint_language_hint_fails_soft_to_english_when_unrecognized() -> None:
    """A shape-VALID but unrecognized code (e.g. not in the capability
    matrix) fails soft to English — same fail-soft contract as every other
    `persona.language_capability` consumer — never a 422 for this case
    (422 is reserved for shape-invalid input, tested separately below)."""
    client = _build_test_client()
    transcriber = _FakeTranscriber()
    client.app.state.transcribe_audio = transcriber
    resp = client.post(
        "/v1/stt",
        headers={"Authorization": "Bearer good"},
        files={"audio": ("clip.webm", b"fake-bytes", "audio/webm")},
        data={"language": "zz"},
    )
    assert resp.status_code == 200, resp.text
    sent_config = transcriber.calls[0]["config"]
    assert isinstance(sent_config, StreamingSTTConfig)
    assert sent_config.language_hint == "en"


def test_stt_endpoint_language_hint_overrides_a_stale_env_level_default() -> None:
    """The exact real-world bug this fix targets: an operator's env-level
    `PERSONA_STT_LANGUAGE_HINT` (here simulated via `app.state.stt_config`,
    the existing override seam) pins a stale language service-wide; a
    request-level hint must still win for THIS request."""
    client = _build_test_client()
    client.app.state.stt_config = StreamingSTTConfig(language_hint="no", model="nova-3")
    transcriber = _FakeTranscriber()
    client.app.state.transcribe_audio = transcriber
    resp = client.post(
        "/v1/stt",
        headers={"Authorization": "Bearer good"},
        files={"audio": ("clip.webm", b"fake-bytes", "audio/webm")},
        data={"language": "en"},
    )
    assert resp.status_code == 200, resp.text
    sent_config = transcriber.calls[0]["config"]
    assert isinstance(sent_config, StreamingSTTConfig)
    assert sent_config.language_hint == "en"


def test_stt_endpoint_omitted_language_leaves_config_untouched() -> None:
    """Absent `language` → the route never touches `config.language_hint` —
    the exact 7647699 behavior (env default / auto-detect) is preserved
    byte-for-byte. Proven here by installing a config the fake transcriber
    can inspect verbatim; `transcribe_prerecorded`'s own
    hint-vs-detect_language branch is covered by
    ``stt/test_deepgram_backend.py`` and is untouched by this fix."""
    client = _build_test_client()
    sentinel_config = StreamingSTTConfig(language_hint=None, model="nova-3")
    client.app.state.stt_config = sentinel_config
    transcriber = _FakeTranscriber()
    client.app.state.transcribe_audio = transcriber
    resp = client.post(
        "/v1/stt",
        headers={"Authorization": "Bearer good"},
        files={"audio": ("clip.webm", b"fake-bytes", "audio/webm")},
    )
    assert resp.status_code == 200, resp.text
    assert transcriber.calls[0]["config"] is sentinel_config


@pytest.mark.parametrize(
    "bad_language",
    [
        "1",  # too short / not letters
        "english",  # way over the 2-3 letter base-code shape
        "en_US",  # underscore, not the BCP-47 hyphen
        "12",  # digits only
        "e",  # below min_length
        "en-" + "x" * 20,  # region subtag over the bound
    ],
)
def test_stt_endpoint_422_on_shape_invalid_language(bad_language: str) -> None:
    client = _build_test_client()
    client.app.state.transcribe_audio = _FakeTranscriber()
    resp = client.post(
        "/v1/stt",
        headers={"Authorization": "Bearer good"},
        files={"audio": ("clip.webm", b"fake-bytes", "audio/webm")},
        data={"language": bad_language},
    )
    assert resp.status_code == 422, resp.text

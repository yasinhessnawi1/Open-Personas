"""Unit tests for the R9-025a voice proxy routes.

``POST /v1/personas/{persona_id}/tts`` + ``POST /v1/stt`` — thin authed
fronts over persona-voice's REST primitives (``packages/api/src/persona_api/
routes/voice.py``). Exercised at the HTTP boundary via ``TestClient`` over
``create_app`` WITHOUT the lifespan (mirrors ``test_mcp_oauth_routes.py``):
no real Postgres — ``persona_service.get_persona`` is monkeypatched (mirrors
``test_create_avatar_hook.py``'s module-attribute convention) and the
upstream persona-voice call is swapped for an ``httpx.MockTransport``
(mirrors ``test_connectors_api.py``'s ``_install_mock_httpx`` house
pattern). No real DB, no live provider spend.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from persona.errors import PersonaNotFoundError
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig
from persona_api.middleware.rate_limit import InMemoryRateLimitStore, RateLimiter
from persona_api.routes import voice as voice_routes

_DB_URLS: dict[str, str] = {
    "database_url": "postgresql+psycopg://super@localhost/persona_shell",
    "app_database_url": "postgresql+psycopg://persona_app@localhost/persona_shell",
}

_VOICE_URL = "http://voice.test"

_PERSONA_YAML_WITH_VOICE = """\
schema_version: "1.0"
identity:
  name: Astrid
  role: Norwegian tenancy law assistant
  background: Helps tenants understand husleieloven.
  voice:
    provider: cartesia
    voice_id: v_astrid
"""

_PERSONA_YAML_NO_VOICE = """\
schema_version: "1.0"
identity:
  name: Astrid
  role: Norwegian tenancy law assistant
  background: Helps tenants understand husleieloven.
"""


def _install_mock_httpx(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    """Swap ``httpx.AsyncClient`` for one whose transport is a MockTransport.

    The proxy route looks up ``httpx.AsyncClient`` at call time, so patching
    the module attribute routes its upstream POST into ``handler`` — no real
    network (house pattern, ``test_connectors_api.py``).
    """
    real = httpx.AsyncClient

    def factory(*_a: object, **_k: object) -> httpx.AsyncClient:
        return real(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def _build_app(*, voice_service_url: str = _VOICE_URL) -> TestClient:
    app = create_app(APIConfig(**_DB_URLS, voice_service_url=voice_service_url))  # type: ignore[arg-type]

    async def _verify(token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id=token, email=None)

    app.state.verify_token = _verify
    app.state.rls_engine = None
    app.state.rate_limiter = RateLimiter(
        InMemoryRateLimitStore(), default_limit=1000, per_endpoint={}
    )
    return TestClient(app)


@pytest.fixture
def client() -> TestClient:
    return _build_app()


def _auth(uid: str = "user_test") -> dict[str, str]:
    return {"Authorization": f"Bearer {uid}"}


def _stub_persona(monkeypatch: pytest.MonkeyPatch, *, yaml_str: str) -> None:
    def _fake_get_persona(*, rls_engine: object, persona_id: str) -> dict[str, object]:  # noqa: ARG001
        return {"id": persona_id, "yaml": yaml_str}

    monkeypatch.setattr(voice_routes.persona_service, "get_persona", _fake_get_persona)


def _stub_persona_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_get_persona(*, rls_engine: object, persona_id: str) -> dict[str, object]:  # noqa: ARG001
        raise PersonaNotFoundError("persona not found", context={"id": persona_id})

    monkeypatch.setattr(voice_routes.persona_service, "get_persona", _fake_get_persona)


# ---------- POST /v1/personas/{persona_id}/tts --------------------------


def test_tts_proxy_requires_bearer(client: TestClient) -> None:
    resp = client.post("/v1/personas/p1/tts", json={"text": "hi"})
    assert resp.status_code == 401


def test_tts_proxy_503_when_voice_service_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gated exactly like the /v1/voices proxy path: unset -> 503, BEFORE
    any persona lookup (the cheapest check runs first)."""
    c = _build_app(voice_service_url="")

    def _boom(*, rls_engine: object, persona_id: str) -> dict[str, object]:  # noqa: ARG001
        raise AssertionError("persona lookup must not run when unconfigured")

    monkeypatch.setattr(voice_routes.persona_service, "get_persona", _boom)
    resp = c.post("/v1/personas/p1/tts", headers=_auth(), json={"text": "hi"})
    assert resp.status_code == 503
    assert resp.json()["error"] == "voice_unavailable"


def test_tts_proxy_404_on_cross_tenant_persona(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_persona_not_found(monkeypatch)
    resp = client.post("/v1/personas/other_tenant/tts", headers=_auth(), json={"text": "hi"})
    assert resp.status_code == 404


def test_tts_proxy_503_when_persona_has_no_configured_voice(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_persona(monkeypatch, yaml_str=_PERSONA_YAML_NO_VOICE)
    resp = client.post("/v1/personas/p1/tts", headers=_auth(), json={"text": "hi"})
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"] == "voice_unavailable"
    assert body["context"]["reason"] == "no_voice_configured"


def test_tts_proxy_resolves_voice_and_forwards_bearer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_persona(monkeypatch, yaml_str=_PERSONA_YAML_WITH_VOICE)
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, content=b"RIFF....WAVEfmt ", headers={"content-type": "audio/wav"}
        )

    _install_mock_httpx(monkeypatch, handler)
    resp = client.post("/v1/personas/p1/tts", headers=_auth("user_x"), json={"text": "hello there"})
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "audio/wav"
    assert resp.content == b"RIFF....WAVEfmt "
    assert captured["url"] == f"{_VOICE_URL}/v1/tts"
    assert captured["auth"] == "Bearer user_x"
    # The RESOLVED voice_id (from the persona's own YAML), never client-supplied.
    assert captured["body"] == {"text": "hello there", "voice_id": "v_astrid"}


def test_tts_proxy_413_passes_through_the_clean_body(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_persona(monkeypatch, yaml_str=_PERSONA_YAML_WITH_VOICE)
    _install_mock_httpx(
        monkeypatch,
        lambda _r: httpx.Response(
            413,
            json={"detail": {"error": "tts_text_too_long", "detail": "text too long"}},
        ),
    )
    resp = client.post("/v1/personas/p1/tts", headers=_auth(), json={"text": "x" * 5000})
    assert resp.status_code == 413
    assert resp.json()["detail"]["error"] == "tts_text_too_long"


def test_tts_proxy_503_on_upstream_5xx_never_leaks_upstream_status(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_persona(monkeypatch, yaml_str=_PERSONA_YAML_WITH_VOICE)
    _install_mock_httpx(
        monkeypatch,
        lambda _r: httpx.Response(502, json={"detail": {"error": "tts_provider_error"}}),
    )
    resp = client.post("/v1/personas/p1/tts", headers=_auth(), json={"text": "hi"})
    assert resp.status_code == 503
    assert resp.json()["error"] == "voice_unavailable"


def test_tts_proxy_503_on_connection_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_persona(monkeypatch, yaml_str=_PERSONA_YAML_WITH_VOICE)

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _install_mock_httpx(monkeypatch, handler)
    resp = client.post("/v1/personas/p1/tts", headers=_auth(), json={"text": "hi"})
    assert resp.status_code == 503
    assert resp.json()["error"] == "voice_unavailable"


def test_tts_proxy_rejects_body_with_extra_fields(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``voice_id`` is resolved server-side — a client cannot smuggle one in."""
    _stub_persona(monkeypatch, yaml_str=_PERSONA_YAML_WITH_VOICE)
    resp = client.post(
        "/v1/personas/p1/tts",
        headers=_auth(),
        json={"text": "hi", "voice_id": "sneaky-spoofed-id"},
    )
    assert resp.status_code == 422


# ---------- POST /v1/stt --------------------------------------------------


def test_stt_proxy_requires_bearer(client: TestClient) -> None:
    resp = client.post("/v1/stt", files={"audio": ("clip.wav", b"bytes", "audio/wav")})
    assert resp.status_code == 401


def test_stt_proxy_503_when_voice_service_unconfigured() -> None:
    c = _build_app(voice_service_url="")
    resp = c.post("/v1/stt", headers=_auth(), files={"audio": ("clip.wav", b"bytes", "audio/wav")})
    assert resp.status_code == 503
    assert resp.json()["error"] == "voice_unavailable"


def test_stt_proxy_forwards_audio_and_bearer_returns_transcript(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = request.content
        return httpx.Response(200, json={"transcript": "hello world"})

    _install_mock_httpx(monkeypatch, handler)
    resp = client.post(
        "/v1/stt",
        headers=_auth("user_y"),
        files={"audio": ("clip.webm", b"fake-audio-bytes", "audio/webm")},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"transcript": "hello world"}
    assert captured["url"] == f"{_VOICE_URL}/v1/stt"
    assert captured["auth"] == "Bearer user_y"
    assert b"fake-audio-bytes" in captured["body"]


def test_stt_proxy_413_passes_through_the_clean_body(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_mock_httpx(
        monkeypatch,
        lambda _r: httpx.Response(
            413, json={"detail": {"error": "stt_audio_too_large", "detail": "too large"}}
        ),
    )
    resp = client.post(
        "/v1/stt", headers=_auth(), files={"audio": ("clip.wav", b"x" * 10, "audio/wav")}
    )
    assert resp.status_code == 413
    assert resp.json()["detail"]["error"] == "stt_audio_too_large"


def test_stt_proxy_503_on_upstream_5xx(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_mock_httpx(
        monkeypatch,
        lambda _r: httpx.Response(502, json={"detail": {"error": "stt_provider_error"}}),
    )
    resp = client.post("/v1/stt", headers=_auth(), files={"audio": ("clip.wav", b"x", "audio/wav")})
    assert resp.status_code == 503
    assert resp.json()["error"] == "voice_unavailable"


def test_stt_proxy_503_on_malformed_upstream_json(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_mock_httpx(monkeypatch, lambda _r: httpx.Response(200, content=b"not json"))
    resp = client.post("/v1/stt", headers=_auth(), files={"audio": ("clip.wav", b"x", "audio/wav")})
    assert resp.status_code == 503
    assert resp.json()["error"] == "voice_unavailable"


def test_stt_proxy_503_on_connection_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _install_mock_httpx(monkeypatch, handler)
    resp = client.post("/v1/stt", headers=_auth(), files={"audio": ("clip.wav", b"x", "audio/wav")})
    assert resp.status_code == 503

"""Integration tests for the R9-025a voice proxy routes against real Postgres.

Drives the REAL route stack — persona creation + RLS ownership + the
voice-config gate — against Docker Postgres, exactly mirroring
``test_api_imagegen.py``'s fixture shape (a real ``TestClient`` + two seeded
users + the superuser engine for direct inspection). The upstream
persona-voice call itself is swapped for an ``httpx.MockTransport`` (the
SAME house pattern the unit suite + ``test_connectors_api.py`` use) — no
live Cartesia/Deepgram spend; the ONE live leg lives in
``packages/voice/tests/external/test_r9_025a_oneshot_live.py``.

Skips cleanly when ``APP_DATABASE_URL`` is unset (no Docker Postgres).
Run: ``PERSONA_TEST_DB=1 uv run pytest packages/api/tests/integration/test_voice_proxy_api.py -q``
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from fastapi.testclient import TestClient
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig
from persona_api.middleware.rls_context import make_rls_engine
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from sqlalchemy import Engine
    from tests.conftest import HashEmbedder384

pytestmark = pytest.mark.integration

_USER_A = "user_voice_a"
_USER_B = "user_voice_b"
_VOICE_URL = "http://voice.test"

_YAML_WITH_VOICE = """\
schema_version: "1.0"
identity:
  name: Astrid
  role: Norwegian tenancy law assistant
  background: |
    Helps tenants understand husleieloven.
  language_default: en
  constraints: []
  voice:
    provider: cartesia
    voice_id: v_astrid_integration
"""

_YAML_NO_VOICE = """\
schema_version: "1.0"
identity:
  name: Bjorn
  role: General assistant
  background: |
    A generalist persona.
  language_default: en
  constraints: []
"""


def _install_mock_httpx(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    """Swap ``httpx.AsyncClient`` for a MockTransport.

    The ``test_connectors_api.py`` house pattern — no real network.
    """
    real = httpx.AsyncClient

    def factory(*_a: object, **_k: object) -> httpx.AsyncClient:
        return real(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(httpx, "AsyncClient", factory)


@pytest.fixture
def client(
    migrated_engine: Engine,  # noqa: ARG001 — ensures schema + persona_app grants
    embedder: HashEmbedder384,
    tmp_path: Path,
) -> Iterator[tuple[TestClient, str, str, Engine]]:
    """Real FastAPI client + two seeded users + the superuser engine.

    Mirrors ``test_api_imagegen.py``'s ``client`` fixture shape so the
    cross-tenant scenario uses the same, already-proven pattern.
    ``voice_service_url`` is set — the "unconfigured" case gets its own
    dedicated fixture below (a distinct app instance).
    """
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")

    cfg = APIConfig(
        app_database_url=app_url,
        audit_root=str(tmp_path / "audit"),
        workspace_root=tmp_path / "workspace",
        voice_service_url=_VOICE_URL,
    )
    app = create_app(cfg)

    async def _fake_verify(token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id=token, email=None)

    with TestClient(app) as c:
        app.state.verify_token = _fake_verify
        app.state.embedder = embedder
        # Drop the lifespan-installed TierRegistry so persona-detail's
        # capabilities surface doesn't lazily instantiate a real chat backend
        # (mirrors test_api_imagegen.py's exact same guard).
        if hasattr(app.state, "tier_registry"):
            app.state.tier_registry = None

        su = make_rls_engine(os.environ["DATABASE_URL"])
        with su.begin() as conn:
            for u in (_USER_A, _USER_B):
                conn.execute(
                    text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
                    {"i": u, "e": f"{u}@x.test"},
                )
        yield c, _USER_A, _USER_B, su
        with su.begin() as conn:
            conn.execute(
                text("DELETE FROM users WHERE id IN (:a, :b)"), {"a": _USER_A, "b": _USER_B}
            )
        su.dispose()


def _auth(uid: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {uid}"}


def _create_persona(c: TestClient, uid: str, yaml_str: str) -> str:
    resp = c.post("/v1/personas", json={"yaml": yaml_str}, headers=_auth(uid))
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


# ---------- POST /v1/personas/{persona_id}/tts ---------------------------


def test_tts_proxy_happy_path_resolves_persona_voice(
    client: tuple[TestClient, str, str, Engine], monkeypatch: pytest.MonkeyPatch
) -> None:
    c, uid_a, _uid_b, _su = client
    pid = _create_persona(c, uid_a, _YAML_WITH_VOICE)
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, content=b"RIFFsmoketestWAVE", headers={"content-type": "audio/wav"}
        )

    _install_mock_httpx(monkeypatch, handler)
    resp = c.post(f"/v1/personas/{pid}/tts", json={"text": "hello there"}, headers=_auth(uid_a))
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "audio/wav"
    assert resp.content == b"RIFFsmoketestWAVE"
    assert captured["url"] == f"{_VOICE_URL}/v1/tts"
    assert captured["auth"] == f"Bearer {uid_a}"
    # The voice_id came from the PERSONA's own stored YAML, not the caller.
    assert captured["body"] == {"text": "hello there", "voice_id": "v_astrid_integration"}


def test_tts_proxy_cross_tenant_persona_is_404_never_reaches_voice_service(
    client: tuple[TestClient, str, str, Engine], monkeypatch: pytest.MonkeyPatch
) -> None:
    """RLS tenant isolation (criterion 11, non-vacuous): B cannot TTS through A's persona."""
    c, uid_a, uid_b, _su = client
    pid = _create_persona(c, uid_a, _YAML_WITH_VOICE)

    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not reach the voice service for a cross-tenant persona")

    _install_mock_httpx(monkeypatch, handler)
    resp = c.post(f"/v1/personas/{pid}/tts", json={"text": "hello"}, headers=_auth(uid_b))
    assert resp.status_code == 404


def test_tts_proxy_persona_without_configured_voice_is_503(
    client: tuple[TestClient, str, str, Engine],
) -> None:
    c, uid_a, _uid_b, _su = client
    pid = _create_persona(c, uid_a, _YAML_NO_VOICE)
    resp = c.post(f"/v1/personas/{pid}/tts", json={"text": "hello"}, headers=_auth(uid_a))
    assert resp.status_code == 503
    assert resp.json()["context"]["reason"] == "no_voice_configured"


def test_tts_proxy_fails_soft_when_voice_service_unconfigured(
    migrated_engine: Engine,  # noqa: ARG001 — ensures schema + persona_app grants
    embedder: HashEmbedder384,
    tmp_path: Path,
) -> None:
    """No PERSONA_VOICE_SERVICE_URL -> honest 503 (mirrors the C6 connector-link precedent)."""
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    cfg = APIConfig(
        app_database_url=app_url,
        audit_root=str(tmp_path / "audit"),
        workspace_root=tmp_path / "workspace",
        # voice_service_url intentionally left unset (default "").
    )
    app = create_app(cfg)

    async def _fake_verify(token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id=token, email=None)

    with TestClient(app) as c:
        app.state.verify_token = _fake_verify
        app.state.embedder = embedder
        if hasattr(app.state, "tier_registry"):
            app.state.tier_registry = None
        su = make_rls_engine(os.environ["DATABASE_URL"])
        with su.begin() as conn:
            conn.execute(
                text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
                {"i": _USER_A, "e": f"{_USER_A}@x.test"},
            )
        try:
            pid = _create_persona(c, _USER_A, _YAML_WITH_VOICE)
            resp = c.post(f"/v1/personas/{pid}/tts", json={"text": "hi"}, headers=_auth(_USER_A))
            assert resp.status_code == 503
            assert resp.json()["error"] == "voice_unavailable"
        finally:
            with su.begin() as conn:
                conn.execute(text("DELETE FROM users WHERE id = :a"), {"a": _USER_A})
            su.dispose()


# ---------- POST /v1/stt --------------------------------------------------


def test_stt_proxy_happy_path(
    client: tuple[TestClient, str, str, Engine], monkeypatch: pytest.MonkeyPatch
) -> None:
    c, uid_a, _uid_b, _su = client

    def handler(request: httpx.Request) -> httpx.Response:
        assert b"fake-dictation-audio" in request.content
        return httpx.Response(200, json={"transcript": "buy milk tomorrow"})

    _install_mock_httpx(monkeypatch, handler)
    resp = c.post(
        "/v1/stt",
        files={"audio": ("clip.webm", b"fake-dictation-audio", "audio/webm")},
        headers=_auth(uid_a),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"transcript": "buy milk tomorrow"}


def test_stt_proxy_works_without_any_persona(
    client: tuple[TestClient, str, str, Engine], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Owner-scoped only — usable before any persona exists (authoring-wizard dictation)."""
    c, uid_a, _uid_b, _su = client
    _install_mock_httpx(
        monkeypatch, lambda _r: httpx.Response(200, json={"transcript": "a fresh persona idea"})
    )
    resp = c.post(
        "/v1/stt",
        files={"audio": ("clip.wav", b"fake-audio", "audio/wav")},
        headers=_auth(uid_a),
    )
    assert resp.status_code == 200
    assert resp.json() == {"transcript": "a fresh persona idea"}


def test_stt_proxy_forwards_language_hint(
    client: tuple[TestClient, str, str, Engine], monkeypatch: pytest.MonkeyPatch
) -> None:
    """R9-025 reopen — context-pinned dictation: the language hint survives
    the real RLS-authed proxy round trip and reaches persona-voice verbatim
    (resolution against the capability matrix is persona-voice's job, unit
    -tested there — this integration leg proves the api layer's plumbing,
    not the resolution)."""
    c, uid_a, _uid_b, _su = client
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(200, json={"transcript": "hallo der"})

    _install_mock_httpx(monkeypatch, handler)
    resp = c.post(
        "/v1/stt",
        files={"audio": ("clip.webm", b"fake-dictation-audio", "audio/webm")},
        data={"language": "no"},
        headers=_auth(uid_a),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"transcript": "hallo der"}
    assert b'name="language"' in captured["body"]
    assert b"\r\n\r\nno\r\n" in captured["body"]


def test_stt_proxy_422_on_shape_invalid_language_hint(
    client: tuple[TestClient, str, str, Engine],
) -> None:
    c, uid_a, _uid_b, _su = client
    resp = c.post(
        "/v1/stt",
        files={"audio": ("clip.webm", b"fake-dictation-audio", "audio/webm")},
        data={"language": "123"},
        headers=_auth(uid_a),
    )
    assert resp.status_code == 422

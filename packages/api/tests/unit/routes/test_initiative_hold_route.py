"""D-ORCH-11-18-u: ``/v1/conversations/{id}/initiative-hold``, the web-only control.

An authenticated GET reads the control and an authenticated PUT changes it, always as the
caller (the owner comes from the token, never from the request). A release needs the version
the user read; a version that moved answers 409 so the page reads again; a conversation that
is not the caller's answers 404. Without a graph nothing is ever tagged, so nothing is held.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig, Edition
from persona_api.errors import ConversationNotFoundError
from persona_api.initiative.conversation_hold import ConversationHold, HoldChangedError
from persona_api.middleware.rate_limit import InMemoryRateLimitStore, RateLimiter
from persona_api.routes import initiative_hold

_AT = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
_URL = "/v1/conversations/conv-d/initiative-hold"


class _Service:
    """Answers as the real service would; records who asked for what."""

    calls: list[tuple[Any, ...]]
    raises: Exception | None = None

    def __init__(self, *_: Any, **__: Any) -> None:  # noqa: ANN401
        pass

    def read(self, owner_id: str, conversation_id: str) -> ConversationHold:
        _Service.calls.append(("read", owner_id, conversation_id))
        if conversation_id != "conv-d" or owner_id != "u_a":
            raise ConversationNotFoundError("conversation not found", context={})
        return ConversationHold(state="held", released_at=None, version=2)

    def change(self, owner_id: str, conversation_id: str, **kwargs: Any) -> ConversationHold:  # noqa: ANN401
        _Service.calls.append(("change", owner_id, conversation_id, kwargs))
        if _Service.raises is not None:
            raise _Service.raises
        if owner_id != "u_a":
            raise ConversationNotFoundError("conversation not found", context={})
        return ConversationHold(state="released", released_at=_AT, version=2)


def _client(monkeypatch: pytest.MonkeyPatch, *, graph: bool = True) -> TestClient:
    _Service.calls = []
    _Service.raises = None
    monkeypatch.setattr(initiative_hold, "ConversationHoldService", _Service)
    app = create_app(
        APIConfig(
            database_url="postgresql+psycopg://u:p@localhost:1/none",  # never connected
            app_database_url="postgresql+psycopg://persona_app@localhost:1/none",
            edition=Edition.cloud,
        )
    )

    async def _verify(token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id=token, email=None)

    app.state.verify_token = _verify
    app.state.rls_engine = object()
    app.state.graph_store = object() if graph else None
    app.state.rate_limiter = RateLimiter(
        InMemoryRateLimitStore(), default_limit=1000, per_endpoint={}
    )
    return TestClient(app)


def _as(user: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {user}"}


def test_the_owner_reads_the_control(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = _client(monkeypatch).get(_URL, headers=_as("u_a"))
    assert resp.status_code == 200
    assert resp.json() == {"state": "held", "released_at": None, "version": 2}
    assert _Service.calls == [("read", "u_a", "conv-d")]


def test_another_owner_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch)
    assert client.get(_URL, headers=_as("u_b")).status_code == 404
    put = client.put(_URL, json={"released": True, "version": 2}, headers=_as("u_b"))
    assert put.status_code == 404


def test_no_token_reads_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch)
    assert client.get(_URL).status_code == 401
    assert client.put(_URL, json={"released": False}).status_code == 401
    assert _Service.calls == []


def test_a_release_is_written_as_the_caller_for_the_version_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resp = _client(monkeypatch).put(_URL, json={"released": True, "version": 2}, headers=_as("u_a"))
    assert resp.status_code == 200
    assert resp.json()["state"] == "released"
    (call,) = _Service.calls
    assert call[:3] == ("change", "u_a", "conv-d")
    assert call[3]["released"] is True
    assert call[3]["version"] == 2
    assert call[3]["at"].tzinfo is not None


@pytest.mark.parametrize(
    "body",
    [
        {"released": True},  # a release without the version read
        {"released": True, "version": -1},
        {"released": "yes"},
        {"released": False, "owner_id": "u_b"},  # no owner from the body, ever
    ],
)
def test_a_malformed_change_is_refused(monkeypatch: pytest.MonkeyPatch, body: Any) -> None:  # noqa: ANN401
    resp = _client(monkeypatch).put(_URL, json=body, headers=_as("u_a"))
    assert resp.status_code == 422
    assert _Service.calls == []


def test_a_version_that_moved_is_a_409(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch)
    _Service.raises = HoldChangedError("changed")
    resp = client.put(_URL, json={"released": True, "version": 1}, headers=_as("u_a"))
    assert resp.status_code == 409
    assert "conversation_changed" in resp.text


def test_a_re_block_needs_no_version(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = _client(monkeypatch).put(_URL, json={"released": False}, headers=_as("u_a"))
    assert resp.status_code == 200
    assert _Service.calls[0][3]["released"] is False


def test_without_a_graph_nothing_is_held_and_nothing_is_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(monkeypatch, graph=False)
    assert client.get(_URL, headers=_as("u_a")).json() == {
        "state": "not_held",
        "released_at": None,
        "version": 0,
    }
    put = client.put(_URL, json={"released": True, "version": 0}, headers=_as("u_a"))
    assert put.json()["state"] == "not_held"
    assert _Service.calls == []

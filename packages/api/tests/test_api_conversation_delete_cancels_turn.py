"""R9-316: deleting a conversation cancels its live turn first, as a persona delete does.

Before this, a turn streaming in a conversation the user deleted kept calling the model,
then failed at its write-back (the row was gone) and ended as an error. The DELETE route
now asks the chat-turn registry to cancel that conversation's live turn before anything
is removed, the same way persona erasure's ``_cancel_live_work`` does. The turn then ends
``cancelled``: it stops spending on provider calls and, like any user cancel, is not
charged. Pinned for the plain delete and the forget-memory delete, and only after the
ownership check (a cross-tenant id cancels nothing).

No DB: the ``test_api_conversation_cascade.py`` harness, with a recording registry.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig
from persona_api.errors import ConversationNotFoundError
from persona_api.middleware.rate_limit import InMemoryRateLimitStore, RateLimiter
from persona_api.services import audit_service, chat_service

if TYPE_CHECKING:
    from pathlib import Path

_OWN = "conv_u1"


class _RecordingRegistry:
    """The registry seam the route uses: ``request_cancel`` only."""

    def __init__(self, events: list[str]) -> None:
        self._events = events

    def request_cancel(self, conversation_id: str) -> bool:
        self._events.append(f"cancel:{conversation_id}")
        return True


@pytest.fixture
def events() -> list[str]:
    return []


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, events: list[str]) -> TestClient:
    app = create_app(
        APIConfig(
            database_url="postgresql+psycopg://super@localhost/persona_shell",
            app_database_url="postgresql+psycopg://persona_app@localhost/persona_shell",
        )
    )

    async def _verify(token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id=token, email=None)

    app.state.verify_token = _verify
    app.state.rls_engine = None
    app.state.workspace_root = tmp_path
    app.state.chat_turn_registry = _RecordingRegistry(events)
    app.state.rate_limiter = RateLimiter(
        InMemoryRateLimitStore(), default_limit=1000, per_endpoint={}
    )

    def _get(*, rls_engine: Any, conversation_id: str) -> dict[str, Any]:  # noqa: ANN401, ARG001
        if conversation_id == _OWN:
            return {"id": _OWN, "owner_id": "u1", "persona_id": "astrid", "messages": []}
        raise ConversationNotFoundError(
            "conversation not found", context={"conversation_id": conversation_id}
        )

    def _delete(**kwargs: Any) -> None:  # noqa: ANN401
        events.append(f"row:forget={kwargs['forget_memory']}")

    monkeypatch.setattr(chat_service, "get_conversation", _get)
    monkeypatch.setattr(chat_service, "delete_conversation", _delete)
    monkeypatch.setattr(audit_service, "record", lambda **_kw: None)
    return TestClient(app)


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer u1"}


def test_a_plain_delete_cancels_the_live_turn_before_the_row(
    client: TestClient, events: list[str]
) -> None:
    resp = client.delete(f"/v1/conversations/{_OWN}", headers=_auth())

    assert resp.status_code == 204
    assert events == [f"cancel:{_OWN}", "row:forget=False"]


def test_a_forget_memory_delete_cancels_the_live_turn_before_the_row(
    client: TestClient, events: list[str]
) -> None:
    resp = client.delete(f"/v1/conversations/{_OWN}?forget_memory=true", headers=_auth())

    assert resp.status_code == 204
    assert events == [f"cancel:{_OWN}", "row:forget=True"]


def test_a_cross_tenant_delete_cancels_nothing(client: TestClient, events: list[str]) -> None:
    resp = client.delete("/v1/conversations/conv_OTHER", headers=_auth())

    assert resp.status_code == 404
    assert events == []


def test_a_delete_without_a_registry_still_deletes(client: TestClient, events: list[str]) -> None:
    """A boot with no chat-turn registry (no sink) deletes as before."""
    client.app.state.chat_turn_registry = None  # type: ignore[attr-defined]

    resp = client.delete(f"/v1/conversations/{_OWN}", headers=_auth())

    assert resp.status_code == 204
    assert events == ["row:forget=False"]

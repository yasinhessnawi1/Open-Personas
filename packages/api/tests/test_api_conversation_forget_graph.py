"""The conversation delete reaches the graph, and its dialog can count what goes (R9-304).

Route level, no database: ``chat_service`` and the graph forget are replaced, so these pin the
wiring. ``DELETE /v1/conversations/{id}?forget_memory=true`` hands the delete a graph step for
this owner and conversation; a plain delete hands it none; a graph held by a consolidation
answers 409 and the conversation stays. ``GET /v1/conversations/{id}/forget-preview`` answers
the counts for the dialog, and 404 for a conversation that is not the caller's. The SQL is
proved on Postgres in tests/integration/test_forget_conversation_graph_postgres.py.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from persona.graph.errors import GraphNodeBusyError
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig
from persona_api.errors import ConversationNotFoundError
from persona_api.middleware.rate_limit import InMemoryRateLimitStore, RateLimiter
from persona_api.services import audit_service, chat_service
from persona_api.services import forget_conversation_graph as fcg

_CONVERSATION = "conv_u1"


class _Calls:
    def __init__(self) -> None:
        self.deletes: list[dict[str, Any]] = []
        self.graph: list[tuple[object, dict[str, Any]]] = []
        self.audits: list[dict[str, Any]] = []
        self.previews: list[dict[str, Any]] = []
        self.busy = False


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> _Calls:
    recorded = _Calls()

    def _get(*, rls_engine: Any, conversation_id: str) -> dict[str, Any]:  # noqa: ANN401, ARG001
        if conversation_id != _CONVERSATION:
            raise ConversationNotFoundError(
                "conversation not found", context={"conversation_id": conversation_id}
            )
        return {"id": _CONVERSATION, "persona_id": "astrid", "messages": []}

    def _delete(**kwargs: Any) -> dict[str, int]:  # noqa: ANN401
        recorded.deletes.append(kwargs)
        if recorded.busy:
            raise GraphNodeBusyError("busy", context={"conversation_id": _CONVERSATION})
        step = kwargs.get("graph_forget")
        return step("the transaction's connection") if step is not None else {}

    def _forget(conn: object, **kwargs: Any) -> dict[str, int]:  # noqa: ANN401
        recorded.graph.append((conn, kwargs))
        return {"nodes_deleted": 2, "nodes_stripped": 1}

    def _preview(engine: object, **kwargs: Any) -> fcg.ConversationGraphPreview:  # noqa: ANN401, ARG001
        recorded.previews.append(kwargs)
        return fcg.ConversationGraphPreview(facts_removed=3, facts_kept=1)

    monkeypatch.setattr(chat_service, "get_conversation", _get)
    monkeypatch.setattr(chat_service, "delete_conversation", _delete)
    monkeypatch.setattr(fcg, "forget_conversation_graph", _forget)
    monkeypatch.setattr(fcg, "preview", _preview)
    monkeypatch.setattr(audit_service, "record", lambda **kw: recorded.audits.append(kw))
    return recorded


@pytest.fixture
def client(calls: _Calls) -> TestClient:  # noqa: ARG001 - the fakes must be in place first
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
    app.state.rate_limiter = RateLimiter(
        InMemoryRateLimitStore(), default_limit=1000, per_endpoint={}
    )
    return TestClient(app)


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer u1"}


def test_forgetting_the_memory_hands_the_delete_this_conversations_graph_step(
    client: TestClient, calls: _Calls
) -> None:
    resp = client.delete(f"/v1/conversations/{_CONVERSATION}?forget_memory=true", headers=_auth())

    assert resp.status_code == 204
    (delete,) = calls.deletes
    assert delete["forget_memory"] is True
    assert calls.graph == [
        ("the transaction's connection", {"owner_id": "u1", "conversation_id": _CONVERSATION})
    ]
    (audit,) = calls.audits
    assert audit["action"] == "conversation.delete"
    assert audit["metadata"] == {"graph_facts_removed": "2", "graph_facts_stripped": "1"}


def test_a_plain_delete_hands_it_no_graph_step(client: TestClient, calls: _Calls) -> None:
    resp = client.delete(f"/v1/conversations/{_CONVERSATION}", headers=_auth())

    assert resp.status_code == 204
    (delete,) = calls.deletes
    assert delete["graph_forget"] is None
    assert calls.graph == []
    assert calls.audits[0].get("metadata") is None


def test_a_busy_graph_answers_409_and_records_no_delete(client: TestClient, calls: _Calls) -> None:
    calls.busy = True

    resp = client.delete(f"/v1/conversations/{_CONVERSATION}?forget_memory=true", headers=_auth())

    assert resp.status_code == 409
    assert resp.json()["error"] == "memory_node_changed"
    assert calls.audits == [], "nothing was deleted, so nothing is audited"


def test_the_preview_counts_the_facts_for_the_dialog(client: TestClient, calls: _Calls) -> None:
    resp = client.get(f"/v1/conversations/{_CONVERSATION}/forget-preview", headers=_auth())

    assert resp.status_code == 200
    assert resp.json() == {"facts_removed": 3, "facts_kept": 1}
    assert calls.previews == [{"owner_id": "u1", "conversation_id": _CONVERSATION}]


def test_the_preview_of_another_users_conversation_is_not_found(
    client: TestClient, calls: _Calls
) -> None:
    resp = client.get("/v1/conversations/someone_elses/forget-preview", headers=_auth())

    assert resp.status_code == 404
    assert calls.previews == []


def test_the_documented_kept_count_does_not_promise_the_words_go(client: TestClient) -> None:
    """R9-311: a kept fact stays because another source supports it, and merging appended this
    conversation's words to its wording, so the API's own description must say they may stay."""
    schema = client.app.openapi()["components"]["schemas"]["ConversationForgetPreview"]  # type: ignore[attr-defined]

    kept = schema["properties"]["facts_kept"]["description"]
    assert "another conversation" in kept
    assert "wording may still include words from this conversation" in kept


def test_the_documented_forget_does_not_promise_a_kept_facts_words_go(client: TestClient) -> None:
    """R9-311: the delete's ``forget_memory`` description says a kept fact keeps its wording."""
    operation = client.app.openapi()["paths"]["/v1/conversations/{conversation_id}"]["delete"]  # type: ignore[attr-defined]
    forget = next(p for p in operation["parameters"] if p["name"] == "forget_memory")

    assert "may still include words from this conversation" in forget["description"]

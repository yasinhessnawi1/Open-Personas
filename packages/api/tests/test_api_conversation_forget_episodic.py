"""The conversation delete hands its episodic forget a plan that cannot strand it (R9-311).

Route level, no database: ``chat_service.delete_conversation`` is replaced, so these pin the
wiring. With ``forget_memory=true`` and a memory backend, the delete gets a plan for this owner
and conversation, durable exactly when a worker in this process runs the forget job; a plain
delete gets none. The behaviour is in tests/unit/services/test_chat_service_forget_episodic.py,
and on Postgres in tests/integration/test_conversation_memory_forget_postgres.py.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig
from persona_api.middleware.rate_limit import InMemoryRateLimitStore, RateLimiter
from persona_api.services import audit_service, chat_service
from persona_api.services import conversation_memory_forget as cmf

_CONVERSATION = "conv_u1"


class _Worker:
    def __init__(self, *types: str) -> None:
        self.job_types = frozenset(types)


@pytest.fixture
def deletes(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    recorded: list[dict[str, Any]] = []

    def _get(*, rls_engine: Any, conversation_id: str) -> dict[str, Any]:  # noqa: ANN401, ARG001
        return {"id": conversation_id, "persona_id": "astrid", "messages": []}

    def _delete(**kwargs: Any) -> dict[str, int]:  # noqa: ANN401
        recorded.append(kwargs)
        return {}

    monkeypatch.setattr(chat_service, "get_conversation", _get)
    monkeypatch.setattr(chat_service, "delete_conversation", _delete)
    monkeypatch.setattr(audit_service, "record", lambda **_kw: None)
    return recorded


def _client(*, worker: object | None) -> TestClient:
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
    app.state.memory_backend = object()
    app.state.audit_logger = object()
    app.state.job_queue = object()
    app.state.in_process_worker = worker
    return TestClient(app)


def _forget(client: TestClient, *, forget: bool = True) -> None:
    query = "?forget_memory=true" if forget else ""
    resp = client.delete(
        f"/v1/conversations/{_CONVERSATION}{query}", headers={"Authorization": "Bearer u1"}
    )
    assert resp.status_code == 204, resp.text


@pytest.mark.parametrize(
    ("worker", "durable"),
    [(_Worker(cmf.JOB_TYPE), True), (_Worker("title_refresh"), False), (None, False)],
)
def test_the_plan_is_durable_exactly_when_a_worker_here_runs_the_job(
    deletes: list[dict[str, Any]], worker: object | None, durable: bool
) -> None:
    _forget(_client(worker=worker))

    (delete,) = deletes
    plan = delete["memory_plan"]
    assert isinstance(plan, cmf.MemoryForgetPlan)
    assert plan.durable is durable
    assert delete["episodic_store"] is not None


def test_the_plan_records_for_this_owner_and_conversation(
    deletes: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[object, list[str], dict[str, Any]]] = []

    def _record(conn: object, ids: list[str], **kwargs: Any) -> list[str]:  # noqa: ANN401
        seen.append((conn, ids, kwargs))
        return []

    monkeypatch.setattr(cmf, "record", _record)
    _forget(_client(worker=_Worker(cmf.JOB_TYPE)))

    (delete,) = deletes
    delete["memory_plan"].record("the transaction", ["s1"])
    assert seen == [
        (
            "the transaction",
            ["s1"],
            {"owner_id": "u1", "conversation_id": _CONVERSATION, "durable": True},
        )
    ]


def test_a_plain_delete_gets_no_plan(deletes: list[dict[str, Any]]) -> None:
    _forget(_client(worker=_Worker(cmf.JOB_TYPE)), forget=False)

    (delete,) = deletes
    assert delete["memory_plan"] is None

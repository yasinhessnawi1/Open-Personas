"""The approvals-inbox route layer — three targeted HTTP smokes (Spec A6, B3).

Exactly the three route-boundary behaviours the service/store/resolver tests don't cover:

1. **503** when the resolution service is inert/unwired — fail-soft, not a 500 crash.
2. **cross-owner → 404** — RLS at the route boundary (owner A cannot see owner B's approval).
3. **422** on a ``modify`` decision with no ``edited_arguments``.

Verbatim replay, dual-resolution, and serialization are covered by the resolver / service / store
tests + the OpenAPI membership check — deliberately NOT duplicated here.
"""

# ruff: noqa: ARG001 — ``migrated_engine`` is a dependency-ordering fixture param.
from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from persona.approvals import ActionProposal
from persona.tools import ActionCategory
from persona_api.app import create_app
from persona_api.approvals import ApprovalStore
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

_CONTRACT = '\'{"goal": "x"}\''


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (non-superuser role) not set; skipping approvals-route test")
    engine = create_engine(app_url.replace("+asyncpg", "+psycopg"))
    yield engine
    engine.dispose()


def _client(app_engine: Engine, *, with_resolver: bool) -> TestClient:
    app = create_app(
        APIConfig(
            database_url="postgresql+psycopg://super@localhost/persona_shell",
            app_database_url="postgresql+psycopg://persona_app@localhost/persona_shell",
        )
    )

    async def _verify(token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id=token, email=None)

    app.state.verify_token = _verify
    app.state.rls_engine = app_engine  # the store self-scopes via rls_connection
    if with_resolver:
        app.state.build_approval_resolver = lambda: None  # never reached on the 422 path
    return TestClient(app)


def _seed_proposal_for(su: Engine, app_engine: Engine, owner: str) -> None:
    persona, task, pid = f"{owner}_p", f"{owner}_t", f"{owner}_pid"
    with su.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:u, :e)"), {"u": owner, "e": f"{owner}@x"}
        )
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :o, 'name: x')"),
            {"p": persona, "o": owner},
        )
        conn.execute(
            text(
                "INSERT INTO tasks (id, owner_id, persona_id, contract_json) "
                f"VALUES (:t, :o, :p, {_CONTRACT}::jsonb)"
            ),
            {"t": task, "o": owner, "p": persona},
        )
    ApprovalStore(app_engine).create_proposal(
        ActionProposal(
            proposal_id=pid,
            owner_id=owner,
            task_id=task,
            persona_id=persona,
            categories=frozenset({ActionCategory.COMMUNICATE_AS_USER}),
            tool_name="send_email",
            arguments={"to": "bob@example.com"},
            description="Send an email to bob@example.com",
            created_at=datetime.now(UTC),
        )
    )


def test_decision_is_503_when_resolution_service_inert(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    client = _client(app_engine, with_resolver=False)
    resp = client.post(
        "/v1/approvals/px/decision",
        json={"decision": "approve"},
        headers={"Authorization": "Bearer user_a"},
    )
    assert resp.status_code == 503  # fail-soft when the approval loop isn't wired, not a crash


def test_decision_is_422_on_modify_without_edits(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    client = _client(app_engine, with_resolver=True)
    resp = client.post(
        "/v1/approvals/px/decision",
        json={"decision": "modify"},  # no edited_arguments
        headers={"Authorization": "Bearer user_a"},
    )
    assert resp.status_code == 422


def test_get_cross_owner_approval_is_404_rls(migrated_engine: Engine, app_engine: Engine) -> None:
    _seed_proposal_for(migrated_engine, app_engine, "owner_b")
    client = _client(app_engine, with_resolver=True)
    # owner_a asks for owner_b's proposal → RLS hides it → a clean 404 (never a cross-tenant read).
    resp = client.get("/v1/approvals/owner_b_pid", headers={"Authorization": "Bearer owner_a"})
    assert resp.status_code == 404

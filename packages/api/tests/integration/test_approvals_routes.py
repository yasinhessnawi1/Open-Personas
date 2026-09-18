"""The approvals-inbox route layer — three targeted HTTP smokes (Spec A6, B3).

Exactly the route-boundary behaviours the service/store/resolver tests don't cover:

1. **503** when the resolution service is inert/unwired — fail-soft, not a 500 crash.
2. **cross-owner → 404** — RLS at the route boundary (owner A cannot see owner B's approval).
3. **422** on a ``modify`` decision with no ``edited_arguments``.
4. **the handled list**: a decided approval is readable again after the tab is gone, and it
   still says whether the action that ran was the user's edited version.

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
from persona.approvals import (
    ActionProposal,
    ApprovalDecision,
    DecisionType,
    ProposalStatus,
)
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


# --- the reopenable half: what a decided approval still says (part1 F10) --------------------


def test_handled_list_reopens_an_edited_approval(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    """A decided, consumed approval reads back with the edit still attached to it.

    The pending list drops a proposal the moment it is decided, so before this endpoint the
    sentence the inbox showed after an edit survived exactly as long as the browser tab. Here
    the page is gone and the record still answers.
    """
    _seed_proposal_for(migrated_engine, app_engine, "owner_c")
    store = ApprovalStore(app_engine)
    now = datetime.now(UTC)
    store.record_decision(
        "owner_c",
        ApprovalDecision(
            decision_id="dec_1",
            proposal_id="owner_c_pid",
            type=DecisionType.MODIFY,
            verbatim_reply="send it to alice instead",
            channel="web",
            edited_arguments={"to": "alice@example.com"},
            decided_at=now,
        ),
    )
    store.transition_proposal(
        "owner_c",
        "owner_c_pid",
        expected=ProposalStatus.PENDING,
        new=ProposalStatus.MODIFIED,
        now=now,
    )
    store.transition_proposal(
        "owner_c",
        "owner_c_pid",
        expected=ProposalStatus.executable(),
        new=ProposalStatus.CONSUMED,
        now=now,
    )
    client = _client(app_engine, with_resolver=True)
    auth = {"Authorization": "Bearer owner_c"}

    # It is gone from the pending list, which is exactly why the handled list has to exist.
    assert client.get("/v1/approvals", headers=auth).json() == []

    handled = client.get("/v1/approvals/handled", headers=auth).json()
    assert [a["proposal_id"] for a in handled] == ["owner_c_pid"]
    assert handled[0]["status"] == "consumed"
    assert handled[0]["edited"] is True  # consumed alone could never have told us this

    one = client.get("/v1/approvals/owner_c_pid", headers=auth).json()
    assert one["status"] == "consumed"
    assert one["edited"] is True


def test_a_pending_approval_reads_as_pending_and_unedited(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    """The plain case stays plain: nothing decided, nothing edited, nothing in the handled list."""
    _seed_proposal_for(migrated_engine, app_engine, "owner_d")
    client = _client(app_engine, with_resolver=True)
    auth = {"Authorization": "Bearer owner_d"}

    pending = client.get("/v1/approvals", headers=auth).json()
    assert [a["proposal_id"] for a in pending] == ["owner_d_pid"]
    assert pending[0]["status"] == "pending"
    assert pending[0]["edited"] is False
    assert client.get("/v1/approvals/handled", headers=auth).json() == []

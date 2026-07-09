"""Integration: ``GET /v1/me/nav-counts`` — the sidebar badge totals (R9-010).

Drives the real app + Docker Postgres with a fake verifier (the
``test_a8_profile_timezone`` harness shape). Asserts the pinned semantics:

- a fresh account reads all-zero;
- ``schedules`` counts ROWS — a recurring schedule with a fire history counts
  ONCE, never its occurrences/fires;
- ``active_tasks`` counts only the non-terminal working set (defined/active/
  waiting), never completed/failed/cancelled history;
- ``conversations`` counts chat-born threads only (call-born excluded);
- ``memory_nodes`` counts canonical graph nodes only (soft-merged excluded)
  and reads 0 when no graph store is wired;
- cross-tenant isolation: another owner's rows never leak into the counts
  (RLS + the explicit owner predicate, like the sibling ``/v1/me`` routes).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from persona.graph._schema import EMBEDDING_DIM
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig
from persona_api.middleware.rls_context import make_rls_engine
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_PREFIX = "r9nc_"


def _auth(uid: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {uid}"}


@pytest.fixture
def client(
    migrated_engine: Engine,  # noqa: ARG001 — ensures the schema is at head
    tmp_path: object,
) -> Iterator[TestClient]:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    cfg = APIConfig(app_database_url=app_url, audit_root=str(tmp_path) + "/audit")
    app = create_app(cfg)

    async def _verify(token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id=token, email=f"{token}@example.test")

    with TestClient(app) as c:
        app.state.verify_token = _verify
        if hasattr(app.state, "tier_registry"):
            app.state.tier_registry = None
        yield c
        su = make_rls_engine(os.environ["DATABASE_URL"])
        with su.begin() as conn:
            # Every seeded table FKs users ON DELETE CASCADE, so this clears all.
            conn.execute(text(f"DELETE FROM users WHERE id LIKE '{_PREFIX}%'"))
        su.dispose()


@pytest.fixture
def su_engine() -> Iterator[Engine]:
    """Superuser engine for direct seeding (bypasses RLS deliberately)."""
    su = make_rls_engine(os.environ["DATABASE_URL"])
    yield su
    su.dispose()


def _seed_owner_rows(su: Engine, uid: str) -> None:
    """Seed one owner's full nav-count surface directly (FK parents first)."""
    with su.begin() as conn:
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES (:pid, :uid, 'name: P One')"),
            {"pid": f"{uid}_p1", "uid": uid},
        )
        # Two chat-born conversations + one call-born (excluded from the count).
        origins = ((f"{uid}_c1", "chat"), (f"{uid}_c2", "chat"), (f"{uid}_c3", "call"))
        for conv_id, origin in origins:
            conn.execute(
                text(
                    "INSERT INTO conversations (id, owner_id, persona_id, title, origin) "
                    "VALUES (:cid, :uid, :pid, 't', :origin)"
                ),
                {"cid": conv_id, "uid": uid, "pid": f"{uid}_p1", "origin": origin},
            )
        conn.execute(
            text(
                "INSERT INTO calls (call_id, conversation_id, persona_id, owner_id, started_at) "
                "VALUES (:id, :cid, :pid, :uid, now())"
            ),
            {"id": f"{uid}_call1", "cid": f"{uid}_c3", "pid": f"{uid}_p1", "uid": uid},
        )
        # One of each task state: exactly 3 are non-terminal (defined/active/waiting).
        for state, wait_kind in (
            ("defined", None),
            ("active", None),
            ("waiting", "on_user"),
            ("completed", None),
            ("failed", None),
            ("cancelled", None),
        ):
            conn.execute(
                text(
                    "INSERT INTO tasks (id, owner_id, persona_id, contract_json, state, wait_kind) "
                    "VALUES (:id, :uid, :pid, '{}', :state, :wk)"
                ),
                {
                    "id": f"{uid}_task_{state}",
                    "uid": uid,
                    "pid": f"{uid}_p1",
                    "state": state,
                    "wk": wait_kind,
                },
            )
        # One RECURRING schedule with a fire history: counts ONCE (rows, not fires).
        conn.execute(
            text(
                "INSERT INTO schedules "
                "(id, owner_id, timezone, recurrence, target_job_type, fire_count, next_fire_at) "
                "VALUES (:id, :uid, 'UTC', 'FREQ=DAILY', 'task_scheduled_fire', 5, now())"
            ),
            {"id": f"{uid}_s1", "uid": uid},
        )


def _seed_graph_nodes(su: Engine, uid: str) -> None:
    """Two canonical graph nodes + one soft-merged (only the canonical count)."""
    zero_vec = "[" + ",".join("0" for _ in range(EMBEDDING_DIM)) + "]"
    with su.begin() as conn:
        for node_id, merged_into in (
            (f"{uid}_n1", None),
            (f"{uid}_n2", None),
            (f"{uid}_n3", f"{uid}_n1"),
        ):
            conn.execute(
                text(
                    "INSERT INTO graph_nodes "
                    "(id, owner_id, node_kind, concept_name, content, metadata, embedding, "
                    " embedding_model, content_hash, provenance, created_at, merged_into) "
                    "VALUES (:id, :uid, 'concept', 'c', 'x', '{}', "
                    f"'{zero_vec}'::vector, 'test', 'h', '[]', now(), :merged)"
                ),
                {"id": node_id, "uid": uid, "merged": merged_into},
            )


def test_fresh_account_reads_all_zero(client: TestClient) -> None:
    r = client.get("/v1/me/nav-counts", headers=_auth(f"{_PREFIX}fresh"))
    assert r.status_code == 200
    assert r.json() == {
        "personas": 0,
        "conversations": 0,
        "calls": 0,
        "memory_nodes": 0,
        "active_tasks": 0,
        "schedules": 0,
    }


def test_counts_pinned_semantics(client: TestClient, su_engine: Engine) -> None:
    uid = f"{_PREFIX}owner"
    # Provision the users row (ensure_user) before seeding FK children.
    assert client.get("/v1/me/nav-counts", headers=_auth(uid)).status_code == 200
    _seed_owner_rows(su_engine, uid)

    body = client.get("/v1/me/nav-counts", headers=_auth(uid)).json()
    assert body["personas"] == 1
    assert body["conversations"] == 2  # chat-born only; the call-born thread excluded
    assert body["calls"] == 1
    assert body["active_tasks"] == 3  # defined + active + waiting; terminal trio excluded
    assert body["schedules"] == 1  # ROWS: fire_count=5 never inflates the count
    assert body["memory_nodes"] == 0  # this owner has no graph nodes


def test_memory_counts_canonical_nodes_when_graph_wired(
    client: TestClient, su_engine: Engine
) -> None:
    uid = f"{_PREFIX}graph"
    assert client.get("/v1/me/nav-counts", headers=_auth(uid)).status_code == 200
    _seed_graph_nodes(su_engine, uid)

    original = getattr(client.app.state, "graph_store", None)  # type: ignore[attr-defined]
    try:
        # Unwired (graph off / community) → the cloud-only table is skipped entirely.
        client.app.state.graph_store = None  # type: ignore[attr-defined]
        assert client.get("/v1/me/nav-counts", headers=_auth(uid)).json()["memory_nodes"] == 0

        # Wired (the route only gates on presence) → the canonical count.
        client.app.state.graph_store = original or object()  # type: ignore[attr-defined]
        body = client.get("/v1/me/nav-counts", headers=_auth(uid)).json()
        assert body["memory_nodes"] == 2  # the soft-merged node is invisible
    finally:
        client.app.state.graph_store = original  # type: ignore[attr-defined]


def test_cross_tenant_isolation(client: TestClient, su_engine: Engine) -> None:
    a, b = f"{_PREFIX}iso_a", f"{_PREFIX}iso_b"
    assert client.get("/v1/me/nav-counts", headers=_auth(a)).status_code == 200
    assert client.get("/v1/me/nav-counts", headers=_auth(b)).status_code == 200
    _seed_owner_rows(su_engine, a)

    # B sees none of A's rows…
    assert client.get("/v1/me/nav-counts", headers=_auth(b)).json() == {
        "personas": 0,
        "conversations": 0,
        "calls": 0,
        "memory_nodes": 0,
        "active_tasks": 0,
        "schedules": 0,
    }
    # …while A still sees exactly its own.
    body = client.get("/v1/me/nav-counts", headers=_auth(a)).json()
    assert (body["personas"], body["conversations"], body["schedules"]) == (1, 2, 1)

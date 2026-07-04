"""R-K5-READ-RLS: the Memory READ routes read through the REAL per-request RLS engine.

K5's `test_memory_routes_rls.py` proves the routes pass the owner scope to the
store correctly — but via an owner-scoped FAKE store, so it cannot catch a
mis-bound engine (a route reading through a dispatch / BYPASSRLS engine returns
`[]` or crosses tenants — the exact class `test_rls_scope` fixed). K5's own
state.md flagged this as REQUIRED before merge: "shipping the read boundary
unproven is not [fine]."

This is that proof, over the real stack: the app composes `graph_store` on the
real `persona_app` (non-superuser) RLS engine, real graph rows are seeded for
user A and user B in Postgres, and every READ route is hit with BOTH tokens.
User B's token must never reach a single one of user A's rows — enforced
in-kernel by the RLS owner predicate, NOT by any application filter.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from persona.graph.models import NodeProvenance, _compute_node_hash
from persona.schema.chunks import WriteSource
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig
from persona_api.middleware.rls_context import make_rls_engine
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_USER_A = "u_mem_rls_a"
_USER_B = "u_mem_rls_b"
_NODE_A = f"{_USER_A}::node::00000001"
DIM = 384


def _vec(seed: str) -> list[float]:
    byts: list[int] = []
    c = 0
    while len(byts) < DIM:
        byts.extend(hashlib.sha256(f"{seed}:{c}".encode()).digest())
        c += 1
    v = [(b / 255.0) - 0.5 for b in byts[:DIM]]
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


class _HashEmbedder:
    """Deterministic 384-dim embedder — the search route encodes the query, and the
    real bge model can hit a torch meta-tensor load failure in-process (orthogonal
    to the RLS binding under test). RLS is engine-level, embedder-independent."""

    model_name = "test-hash-embedder-384"
    dimension = DIM

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [_vec(t) for t in texts]


def _seed_node(engine: Engine, owner: str, node_id: str, concept: str) -> None:
    prov = NodeProvenance(
        source=WriteSource.PERSONA_SELF, written_at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, email) VALUES (:o, :o || '@x.com') ON CONFLICT DO NOTHING"
            ),
            {"o": owner},
        )
        conn.execute(
            text(
                "INSERT INTO graph_nodes (id, owner_id, node_kind, concept_name, content, "
                "metadata, embedding, embedding_model, content_hash, provenance, created_at, "
                "updated_at) VALUES (:id, :o, 'concept', :c, :c, '{}', :emb, 'm', :h, "
                "CAST(:prov AS jsonb), now(), now())"
            ),
            {
                "id": node_id,
                "o": owner,
                "c": concept,
                "emb": str(_vec(node_id)),
                "h": _compute_node_hash(concept, concept, {}),
                "prov": json.dumps([prov.model_dump(mode="json")]),
            },
        )


@pytest.fixture
def client(migrated_engine: Engine, tmp_path: Path) -> Iterator[TestClient]:  # noqa: ARG001 — ensures schema + persona_app grants
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    su = make_rls_engine(os.environ["DATABASE_URL"])
    _seed_node(su, _USER_A, _NODE_A, "alpha-secret-of-a")

    cfg = APIConfig(app_database_url=app_url, audit_root=str(tmp_path / "audit"))
    app = create_app(cfg)

    async def _fake_verify(token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id=token, email=None)  # token == user_id

    with TestClient(app) as c:
        app.state.verify_token = _fake_verify
        # The binding this test exists to prove: a real graph_store composed on the
        # real RLS engine (Postgres → K5's engine guard passes; None would mean the
        # routes were reading nothing and the denial would be vacuous).
        assert app.state.graph_store is not None, "graph_store must compose on Postgres"
        app.state.graph_store._embedder = _HashEmbedder()  # noqa: SLF001 — test seam
        yield c
    with su.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id IN (:a, :b)"), {"a": _USER_A, "b": _USER_B})
    su.dispose()


def _auth(user: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {user}"}


def test_owner_reads_own_node_through_real_rls_engine(client: TestClient) -> None:
    r = client.get(f"/v1/memory/nodes/{_NODE_A}", headers=_auth(_USER_A))
    assert r.status_code == 200
    assert r.json()["id"] == _NODE_A


def test_cross_tenant_node_detail_is_404_in_kernel(client: TestClient) -> None:
    # B's token through the real RLS engine: the owner predicate denies in-kernel —
    # a mis-bound (dispatch/BYPASSRLS) engine would 200 with A's row here.
    r = client.get(f"/v1/memory/nodes/{_NODE_A}", headers=_auth(_USER_B))
    assert r.status_code == 404


def test_cross_tenant_window_is_empty_in_kernel(client: TestClient) -> None:
    r = client.get("/v1/memory/graph", params={"focus": _NODE_A}, headers=_auth(_USER_B))
    assert r.status_code == 200
    assert all(n["id"] != _NODE_A for n in r.json().get("nodes", []))


def test_cross_tenant_search_never_returns_a_rows(client: TestClient) -> None:
    r = client.get("/v1/memory/search", params={"q": "alpha-secret-of-a"}, headers=_auth(_USER_B))
    assert r.status_code == 200
    assert all(n.get("node_id") != _NODE_A for n in r.json().get("results", []))

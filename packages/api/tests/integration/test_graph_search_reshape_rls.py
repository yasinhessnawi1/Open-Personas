"""RLS backstop for the K7 allowlist reshape (Spec K7, T7 / K7-D-9).

The reshaped common path drops the positive surrogate IN-list — so **RLS is now the
per-tenant isolation** on that path (owner predicate in-kernel + the ``persona_app``
policy backstop). This proves it non-vacuously under the non-superuser role: a
``search_dense`` (common path, no IN-list) bound to one owner never returns another
tenant's node, and an unbound owner GUC fails closed.
"""

# ruff: noqa: ARG001 — fixture-ordering params.
from __future__ import annotations

import hashlib
import math
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona.audit import MemoryAuditLogger
from persona.graph.models import NodeProvenance, _compute_node_hash
from persona.graph.store import build_graph_store
from persona.schema.chunks import WriteSource
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

DIM = 384


def _rand_vec(seed: str) -> list[float]:
    byts: list[int] = []
    counter = 0
    while len(byts) < DIM:
        byts.extend(hashlib.sha256(f"{seed}:{counter}".encode()).digest())
        counter += 1
    vec = [(b / 255.0) - 0.5 for b in byts[:DIM]]
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


class _Embedder:
    model_name = "seed"
    dimension = DIM

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [_rand_vec(t) for t in texts]


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Engine:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set; skipping reshape RLS test")
    return make_rls_engine(app_url.replace("+asyncpg", "+psycopg"))


def _seed(engine: Engine, owner: str, n: int) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, email) VALUES (:o, :o || '@x.com') ON CONFLICT DO NOTHING"
            ),
            {"o": owner},
        )
        prov = NodeProvenance(
            source=WriteSource.PERSONA_SELF, written_at=datetime(2026, 1, 1, tzinfo=UTC)
        )
        import json

        for i in range(n):
            conn.execute(
                text(
                    "INSERT INTO graph_nodes (id, owner_id, node_kind, concept_name, content, "
                    "metadata, embedding, embedding_model, content_hash, provenance, created_at, "
                    "updated_at) VALUES (:id, :o, 'concept', 'c', 'c', '{}', :emb, 'm', :h, "
                    "CAST(:prov AS jsonb), now(), now())"
                ),
                {
                    "id": f"{owner}::node::{i:08d}",
                    "o": owner,
                    "emb": str(_rand_vec(f"{owner}:{i}")),
                    "h": _compute_node_hash("c", "c", {}),
                    "prov": json.dumps([prov.model_dump(mode="json")]),
                },
            )


def test_reshaped_common_path_is_rls_isolated(migrated_engine: Engine, app_engine: Engine) -> None:
    _seed(migrated_engine, "rls_u1", 15)  # superuser seed (bypasses RLS)
    _seed(migrated_engine, "rls_u2", 15)
    store = build_graph_store(
        engine=app_engine, embedder=_Embedder(), audit_logger=MemoryAuditLogger()
    )

    # bound to u1 (as the request/worker does): the no-IN-list common path returns
    # only u1's nodes — RLS is the isolation now.
    token = current_user_id.set("rls_u1")
    try:
        hits = store.search_dense("rls_u1", "rls_u1:0", top_k=100)
    finally:
        current_user_id.reset(token)
    assert hits
    assert all(n.id.startswith("rls_u1::") for n in hits)  # non-vacuous: u2 exists, unseen

    # fail-closed: with no owner GUC bound, the common path returns nothing.
    empty = store.search_dense("rls_u1", "rls_u1:0", top_k=100)
    assert empty == []

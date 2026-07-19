"""Cross-tenant RLS sweep for the Memory routes (Spec K5, R-K5-READ-RLS).

The named read-route requirement: the GET window/node/search **and** the PATCH correction
read/write through the authenticated user's scope — user A's token must never reach user B's
graph. The REAL RLS engine denial is covered at the store/SQL level
(``test_rls_isolation_graph.py``); the graph store is built on that RLS engine
(``RuntimeFactory(rls_engine)`` → ``enable_graph_writes`` → ``app.state.graph_store``). This
sweep proves the **route-level binding** — that each route passes the authenticated user's id
as the owner scope and 404s/empties cross-tenant references — via an owner-scoped fake store
that models the RLS engine's behaviour (the ``test_documents_rls`` pattern).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from persona.graph.errors import GraphNodeNotFoundError
from persona.graph.models import ConceptNode, NodeKind, NodeProvenance
from persona.schema.chunks import WriteSource
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig
from persona_api.middleware.rate_limit import InMemoryRateLimitStore, RateLimiter

if TYPE_CHECKING:
    from collections.abc import Sequence

# Cloud-edition RLS routes: the app build requires APP_DATABASE_URL (the non-superuser DSN) —
# a DB-backed integration test. Marked so it runs in the integration job, not the DB-less default.
pytestmark = pytest.mark.integration

NOW = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)


def _node(node_id: str, content: str = "alpha") -> ConceptNode:
    return ConceptNode(
        id=node_id,
        node_kind=NodeKind.FACT,
        concept_name="Alpha",
        content=content,
        provenance=(NodeProvenance(source=WriteSource.PERSONA_SELF, written_at=NOW),),
        created_at=NOW,
    )


class _OwnerScopedStore:
    """A fake graph store that models RLS: every method is strictly owner-scoped.

    If a route ever passed the wrong owner (a binding bug), user B would see user A's
    node here — which is exactly the cross-tenant leak this test must catch.
    """

    def __init__(self) -> None:
        # user 'u_a' owns one node; user 'u_b' owns nothing.
        self._nodes: dict[tuple[str, str], ConceptNode] = {("u_a", "na1"): _node("na1")}

    def _owned(self, owner_id: str) -> list[ConceptNode]:
        return [n for (o, _id), n in self._nodes.items() if o == owner_id]

    def count_nodes(self, owner_id: str) -> int:
        return len(self._owned(owner_id))

    def seed_nodes(self, owner_id: str, *, limit: int) -> list[ConceptNode]:
        return self._owned(owner_id)[:limit]

    def get_node(self, owner_id: str, node_id: str) -> ConceptNode | None:
        return self._nodes.get((owner_id, node_id))

    def edges_among(self, owner_id: str, node_ids: Sequence[str]) -> list[object]:  # noqa: ARG002
        return []

    def entity_edges_among(self, owner_id: str, node_ids: Sequence[str]) -> list[object]:  # noqa: ARG002
        return []

    def neighbors(
        self,
        owner_id: str,  # noqa: ARG002
        node_id: str,  # noqa: ARG002
        *,
        link_types: object = None,  # noqa: ARG002
        limit: int,  # noqa: ARG002
    ) -> list[object]:
        return []

    def search_dense(
        self,
        owner_id: str,
        query: str,  # noqa: ARG002
        top_k: int,
        *,
        allowlist: object = None,  # noqa: ARG002 — K5 search passes None (no K4 subtraction)
    ) -> list[ConceptNode]:
        return self._owned(owner_id)[:top_k]

    def search_fts(self, owner_id: str, query: str, top_k: int) -> list[ConceptNode]:  # noqa: ARG002
        return []  # the dense leg suffices to prove owner-scoping here

    def correct_node(
        self,
        owner_id: str,
        node_id: str,
        new_content: str,
        *,
        interaction_id: str | None = None,  # noqa: ARG002
    ) -> None:
        node = self._nodes.get((owner_id, node_id))
        if node is None:
            raise GraphNodeNotFoundError(
                "memory not found", context={"node_id": node_id, "owner_id": owner_id}
            )
        self._nodes[(owner_id, node_id)] = node.model_copy(update={"content": new_content})

    def delete_node(self, owner_id: str, node_id: str) -> bool:
        return self._nodes.pop((owner_id, node_id), None) is not None


@pytest.fixture
def client() -> TestClient:
    app = create_app(APIConfig())

    async def _verify(token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id=token, email=None)

    app.state.verify_token = _verify
    app.state.rls_engine = None
    app.state.graph_store = _OwnerScopedStore()
    app.state.rate_limiter = RateLimiter(
        InMemoryRateLimitStore(), default_limit=1000, per_endpoint={}
    )
    return TestClient(app)


def _auth(user: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {user}"}


# ---- the owner (u_a) sees + edits their node (positive controls) ----


def test_owner_reads_their_own_node(client: TestClient) -> None:
    resp = client.get("/v1/memory/nodes/na1", headers=_auth("u_a"))
    assert resp.status_code == 200
    assert resp.json()["id"] == "na1"


def test_owner_can_correct_their_own_node(client: TestClient) -> None:
    resp = client.patch("/v1/memory/nodes/na1", headers=_auth("u_a"), json={"content": "revised"})
    assert resp.status_code == 200
    assert resp.json()["content"] == "revised"


# ---- the OTHER tenant (u_b) is denied on every Memory surface ----


def test_cross_tenant_node_detail_is_404(client: TestClient) -> None:
    resp = client.get("/v1/memory/nodes/na1", headers=_auth("u_b"))
    assert resp.status_code == 404
    assert resp.status_code != 403  # existence-disclosure-safe (can't tell "exists elsewhere")


def test_cross_tenant_window_is_empty(client: TestClient) -> None:
    resp = client.get("/v1/memory/graph", params={"focus": "na1"}, headers=_auth("u_b"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["nodes"] == []
    assert body["total_nodes"] == 0  # u_b owns nothing — never u_a's tally


def test_cross_tenant_search_returns_nothing(client: TestClient) -> None:
    resp = client.get("/v1/memory/search", params={"q": "alpha"}, headers=_auth("u_b"))
    assert resp.status_code == 200
    assert resp.json()["results"] == []


def test_cross_tenant_correction_is_404_and_does_not_mutate(client: TestClient) -> None:
    resp = client.patch("/v1/memory/nodes/na1", headers=_auth("u_b"), json={"content": "hijacked"})
    assert resp.status_code == 404
    # u_a's node is untouched.
    owner_view = client.get("/v1/memory/nodes/na1", headers=_auth("u_a"))
    assert owner_view.json()["content"] == "alpha"


def test_cross_tenant_delete_is_404_and_does_not_delete(client: TestClient) -> None:
    resp = client.delete("/v1/memory/nodes/na1", headers=_auth("u_b"))
    assert resp.status_code == 404
    # u_a's node still exists — u_b's delete didn't reach it.
    assert client.get("/v1/memory/nodes/na1", headers=_auth("u_a")).status_code == 200


def test_owner_can_delete_their_own_node(client: TestClient) -> None:
    assert client.delete("/v1/memory/nodes/na1", headers=_auth("u_a")).status_code == 204
    # …and it's gone.
    assert client.get("/v1/memory/nodes/na1", headers=_auth("u_a")).status_code == 404


def test_unauthenticated_is_401_before_any_scope_check(client: TestClient) -> None:
    resp = client.get("/v1/memory/nodes/na1")
    assert resp.status_code == 401


# ---- availability: distinguish "no usable graph store" from "empty graph" (Spec K5) ----


def test_window_reports_available_when_store_present(client: TestClient) -> None:
    """A wired store ⇒ ``available: true`` — the page renders the map / empty-invite."""
    resp = client.get("/v1/memory/graph", headers=_auth("u_a"))
    assert resp.status_code == 200
    assert resp.json()["available"] is True


def test_window_reports_unavailable_when_no_store() -> None:
    """No usable graph store (community-on-SQLite / graph off) ⇒ ``available: false``.

    The route returns 200 + an empty window, but ``available=False`` so the UI shows
    a distinct "Memory isn't available here" state + gates the nav — never the
    "no memories yet" invite, which would misrepresent unavailable as empty (K5).
    """
    app = create_app(APIConfig())

    async def _verify(token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id=token, email=None)

    app.state.verify_token = _verify
    app.state.rls_engine = None
    app.state.graph_store = None
    app.state.rate_limiter = RateLimiter(
        InMemoryRateLimitStore(), default_limit=1000, per_endpoint={}
    )
    resp = TestClient(app).get("/v1/memory/graph", headers=_auth("u_a"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["nodes"] == []
    assert body["total_nodes"] == 0

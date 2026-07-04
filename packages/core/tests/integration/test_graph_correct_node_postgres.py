"""Integration tests for ``correct_node`` — the K5-D-7 user-correction seam (Spec K5).

Against real Postgres + the assembled GraphStore: a correction re-embeds, re-syncs the
index (retrieval reflects the new content — criterion 6), re-evaluates SEMANTIC links
from the fresh embedding **including clearing INBOUND stale edges** (the half-edge a
one-sided delete would leave), records provenance as ``WriteSource.USER`` with the prior
content as ``superseded_content``, and PRESERVES entity/temporal/causal links.
"""

from __future__ import annotations

import math
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona.audit import MemoryAuditLogger
from persona.graph._schema import graph_metadata
from persona.graph.errors import GraphNodeNotFoundError
from persona.graph.models import LinkType, NodeKind, NodeProvenance, TypedLink, make_edge_id
from persona.graph.protocol import KnowledgeCandidate
from persona.graph.store import build_graph_store
from persona.schema.chunks import WriteSource

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

DIM = 384
NOW = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)


def _vec(primary: int, cos: float = 1.0, secondary: int = 383) -> list[float]:
    v = [0.0] * DIM
    v[primary] = cos
    v[secondary] = math.sqrt(max(0.0, 1.0 - cos * cos))
    return v


# cos(alpha, beta)=0.85 → linked (≥0.82) but NOT merged (<0.88); revised is orthogonal to
# both, gamma sits next to revised, delta is far from everything.
_MAP = {
    "alpha original": _vec(0),
    "beta near alpha": _vec(0, 0.85),
    "gamma near revised": _vec(40, 0.85),
    "delta far": _vec(100),
    "alpha revised text": _vec(40),
    "find the revised idea": _vec(40),
    "find the original idea": _vec(0),
}


class _Embedder:
    model_name = "mapping"

    @property
    def dimension(self) -> int:
        return DIM

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [_MAP.get(t, _vec(200)) for t in texts]


def _cand(content: str) -> KnowledgeCandidate:
    return KnowledgeCandidate(
        concept_name=content[:20],
        content=content,
        node_kind=NodeKind.FACT,
        provenance=NodeProvenance(source=WriteSource.PERSONA_SELF, written_at=NOW),
    )


@pytest.fixture(scope="session")
def _engine() -> Iterator[Engine]:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set; skipping Postgres integration test")
    if "+asyncpg" in url:
        url = url.replace("+asyncpg", "+psycopg")
    from sqlalchemy.engine import make_url

    db_name = make_url(url).database or ""
    if os.environ.get("PERSONA_TEST_DB") != "1" and not db_name.endswith("_test"):
        pytest.skip("Use a '*_test' DB or set PERSONA_TEST_DB=1 (destructive fixture).")
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import IntegrityError, OperationalError

    engine: Engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    except IntegrityError:
        pass
    except OperationalError as exc:
        engine.dispose()
        pytest.skip(f"Postgres unreachable: {exc}")
    yield engine
    engine.dispose()


@pytest.fixture
def store(_engine: Engine):  # noqa: ANN201 — PostgresGraphStore (concrete)
    with _engine.begin() as conn:
        graph_metadata.drop_all(conn)
        graph_metadata.create_all(conn)
    s = build_graph_store(engine=_engine, embedder=_Embedder(), audit_logger=MemoryAuditLogger())
    yield s
    with _engine.begin() as conn:
        graph_metadata.drop_all(conn)


def _ids_around(store, owner: str, node_id: str) -> set[str]:  # noqa: ANN001
    return {n.id for _edge, n in store.neighbors(owner, node_id, limit=20)}


def test_correction_clears_the_inbound_half_edge(store) -> None:  # noqa: ANN001
    """B links INTO A; correcting A away from B must remove the inbound B→A edge."""
    a = store.merge("u1", _cand("alpha original")).node_id
    b = store.merge("u1", _cand("beta near alpha")).node_id  # cos .85 → forms B→A
    store.merge("u1", _cand("gamma near revised"))  # exists, near the revised vector
    # Before: A and B see each other (the inbound edge from B's creation).
    assert b in _ids_around(store, "u1", a)
    assert a in _ids_around(store, "u1", b)

    store.correct_node("u1", a, "alpha revised text")

    # After: the stale B→A half-edge is gone from BOTH sides…
    assert b not in _ids_around(store, "u1", a)
    assert a not in _ids_around(store, "u1", b)
    # …and A re-formed a fresh link to the node near its new meaning (gamma).
    around_a = {store.get_node("u1", nid).concept_name for nid in _ids_around(store, "u1", a)}  # type: ignore[union-attr]
    assert any("gamma" in name for name in around_a)


def test_correction_reindexes_so_retrieval_reflects_new_content(store) -> None:  # noqa: ANN001
    """Criterion 6: after correction, dense retrieval finds the node by its NEW meaning."""
    a = store.merge("u1", _cand("alpha original")).node_id
    revised_before = store.search_dense("u1", "find the revised idea", top_k=5)
    a_dist_before = next((n.distance for n in revised_before if n.id == a), 1.0)

    store.correct_node("u1", a, "alpha revised text")

    # A is now the best match for its NEW meaning (the index was re-synced)…
    revised_after = store.search_dense("u1", "find the revised idea", top_k=5)
    assert revised_after[0].id == a
    a_dist_revised = next(n.distance for n in revised_after if n.id == a)
    # …measurably closer to the revised query than before, and closer to "revised"
    # than to "original" — the embedding genuinely moved (search_dense has no
    # similarity floor, so absence-from-top-k is not a sound signal at this scale).
    assert a_dist_revised < a_dist_before  # type: ignore[operator]
    a_dist_original = next(
        n.distance for n in store.search_dense("u1", "find the original idea", top_k=5) if n.id == a
    )
    assert a_dist_revised < a_dist_original  # type: ignore[operator]


def test_correction_records_user_edited_provenance_with_superseded(store) -> None:  # noqa: ANN001
    a = store.merge("u1", _cand("alpha original")).node_id
    store.correct_node("u1", a, "alpha revised text")
    node = store.get_node("u1", a)
    assert node is not None
    assert node.content == "alpha revised text"
    last = node.provenance[-1]
    assert last.source is WriteSource.USER
    assert last.superseded_content == "alpha original"  # the prior content, structured (D-K0-4)
    assert len(node.provenance) >= 2  # noqa: PLR2004 — original + the correction  # noqa: PLR2004


def test_correction_preserves_causal_links(store) -> None:  # noqa: ANN001
    """Entity/temporal/causal are assertion-based — a content edit must not drop them."""
    a = store.merge("u1", _cand("alpha original")).node_id
    d = store.merge("u1", _cand("delta far")).node_id
    causal = TypedLink(
        id=make_edge_id(a, d, LinkType.CAUSAL),
        src_node_id=a,
        dst_node_id=d,
        link_type=LinkType.CAUSAL,
        created_at=NOW,
    )
    store._backend.upsert_edge("u1", causal)  # noqa: SLF001 — seed a stated causal edge

    store.correct_node("u1", a, "alpha revised text")

    causal_targets = {
        n.id for edge, n in store.neighbors("u1", a, limit=20) if edge.link_type is LinkType.CAUSAL
    }
    assert d in causal_targets  # the stated causal link survived the correction


def test_correcting_a_foreign_node_raises_not_found(store) -> None:  # noqa: ANN001
    store.merge("u1", _cand("alpha original"))
    with pytest.raises(GraphNodeNotFoundError):
        store.correct_node("u2", "u1::node::00000000", "alpha revised text")


def test_correction_is_owner_scoped(store) -> None:  # noqa: ANN001
    """A user cannot correct another user's node even with the right id shape."""
    a = store.merge("u1", _cand("alpha original")).node_id
    with pytest.raises(GraphNodeNotFoundError):
        store.correct_node("u2", a, "alpha revised text")  # u2 doesn't own a
    assert store.get_node("u1", a).content == "alpha original"  # type: ignore[union-attr]

"""Integration tests for the Spec K7 T4 ConsolidationPass over real Postgres (K7-D-4).

Proves against real SQL: star-clustering (near-miss NOT merged), deterministic
survivorship, the additive merge act (mirror assertion edges / close member edges /
copy entities / accrete alias / merged_into + index removal), the byte-exact
merge→unmerge round-trip (content + embedding + edge topology + entity associations +
search visibility), idempotent re-run with an honest report, SELF exclusion on both
sides, the allocator interaction, and strict owner-scoping. A mapping embedder gives
deterministic cosine so the [0.82, 0.88) band is exercised exactly.
"""

from __future__ import annotations

import math
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from persona.audit import MemoryAuditLogger
from persona.graph._schema import graph_metadata
from persona.graph.consolidation import _ALIAS_KEY, _MERGE_KEY, ConsolidationPass
from persona.graph.models import (
    ConceptNode,
    LinkType,
    NodeKind,
    NodeProvenance,
    TypedLink,
    make_edge_id,
    make_self_node_id,
)
from persona.graph.postgres import PostgresGraphBackend
from persona.schema.chunks import WriteSource

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

DIM = 384
T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _vec(primary: int, cos: float = 1.0, secondary: int = 383) -> list[float]:
    v = [0.0] * DIM
    v[primary] = cos
    v[secondary] = math.sqrt(max(0.0, 1.0 - cos * cos))
    return v


class _FakeIndex:
    """Records index membership so the pass's same-path add/remove is observable."""

    def __init__(self) -> None:
        self.present: set[int] = set()

    def add(self, *, surrogate: int, vector: Sequence[float]) -> None:  # noqa: ARG002
        self.present.add(surrogate)

    def remove(self, surrogate: int) -> bool:
        existed = surrogate in self.present
        self.present.discard(surrogate)
        return existed


class _Embedder:
    model_name = "mapping"
    dimension = DIM

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [_vec(0) for _ in texts]  # unused by the pass (never re-embeds)


def _prov(n: int) -> tuple[NodeProvenance, ...]:
    return tuple(
        NodeProvenance(source=WriteSource.PERSONA_SELF, written_at=T0 + timedelta(days=i))
        for i in range(n)
    )


def _node(
    node_id: str, name: str, *, kind: NodeKind = NodeKind.CONCEPT, evidence: int = 1
) -> ConceptNode:
    return ConceptNode(
        id=node_id,
        node_kind=kind,
        concept_name=name,
        content=f"content of {name}",
        provenance=_prov(evidence),
        created_at=T0,
    )


def _sem_edge(owner_node_src: str, dst: str, weight: float) -> TypedLink:
    return TypedLink(
        id=make_edge_id(owner_node_src, dst, LinkType.SEMANTIC),
        src_node_id=owner_node_src,
        dst_node_id=dst,
        link_type=LinkType.SEMANTIC,
        weight=weight,
        created_at=T0,
        valid_at=T0,
    )


def _assert_edge(src: str, dst: str, kind: LinkType) -> TypedLink:
    return TypedLink(
        id=make_edge_id(src, dst, kind),
        src_node_id=src,
        dst_node_id=dst,
        link_type=kind,
        provenance=NodeProvenance(source=WriteSource.PERSONA_SELF, written_at=T0),
        created_at=T0,
        valid_at=T0,
    )


@pytest.fixture(scope="session")
def pg_engine() -> Iterator[Engine]:
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
def env(pg_engine: Engine) -> Iterator[tuple[PostgresGraphBackend, _FakeIndex, ConsolidationPass]]:
    with pg_engine.begin() as conn:
        graph_metadata.drop_all(conn)
        graph_metadata.create_all(conn)
    backend = PostgresGraphBackend(engine=pg_engine)
    index = _FakeIndex()
    pass_ = ConsolidationPass(
        backend=backend, index=index, embedder=_Embedder(), audit_logger=MemoryAuditLogger()
    )
    yield backend, index, pass_
    with pg_engine.begin() as conn:
        graph_metadata.drop_all(conn)


# A near-dup cluster: canonical C (evidence 2) + member M (cos-to-seed 0.85, in band).
# A NEAR-MISS N: band EDGE weight 0.83 but recomputed cosine-to-seed 0.80 (< floor) →
# must NOT merge (the star-clustering recompute is the authority). External X (M's
# assertion-edge target). Entity E associated with M only.
def _seed_cluster(backend: PostgresGraphBackend, owner: str = "u1") -> None:
    backend.insert_node(owner, _node(f"{owner}::node::00000000", "Canonical", evidence=2), _vec(0))
    backend.insert_node(
        owner, _node(f"{owner}::node::00000001", "Member", evidence=1), _vec(0, 0.85)
    )
    backend.insert_node(
        owner, _node(f"{owner}::node::00000002", "NearMiss", evidence=1), _vec(0, 0.80)
    )
    backend.insert_node(owner, _node(f"{owner}::node::00000003", "External"), _vec(9))
    # band semantic edges from canonical.
    backend.upsert_edge(
        owner, _sem_edge(f"{owner}::node::00000000", f"{owner}::node::00000001", 0.85)
    )
    backend.upsert_edge(
        owner, _sem_edge(f"{owner}::node::00000000", f"{owner}::node::00000002", 0.83)
    )
    # member's assertion edge to an external node (mirrored on merge).
    backend.upsert_edge(
        owner,
        _assert_edge(f"{owner}::node::00000001", f"{owner}::node::00000003", LinkType.TEMPORAL),
    )
    # entity association on the member only.
    from persona.graph.models import CanonicalEntity

    backend.insert_entity(
        owner,
        CanonicalEntity(id=f"{owner}::entity::00000000", canonical_name="E", created_at=T0),
        _vec(7),
    )
    backend.associate_entities(owner, f"{owner}::node::00000001", [f"{owner}::entity::00000000"])


C = "u1::node::00000000"
M = "u1::node::00000001"
N = "u1::node::00000002"
X = "u1::node::00000003"
E = "u1::entity::00000000"


def _visible_ids(backend: PostgresGraphBackend, owner: str = "u1") -> set[str]:
    # dense_query filters merged_into IS NULL → the search-visible set.
    return {n.id for n in backend.dense_query(owner, _vec(0), top_k=100)}


# ----- merge: additive act, near-miss excluded (K7-D-4) --------------------


def test_pass_merges_band_member_not_near_miss(
    env: tuple[PostgresGraphBackend, _FakeIndex, ConsolidationPass],
) -> None:
    backend, index, pass_ = env
    _seed_cluster(backend)
    report = pass_.run("u1")

    assert report.clusters_formed == 1
    assert report.nodes_merged == 1
    assert report.merges[0].canonical_id == C  # highest-evidence seed survives
    assert report.merges[0].member_ids == (M,)
    # the near-miss (recomputed cosine 0.80 < floor) is skipped, not merged.
    assert N not in {m for g in report.merges for m in g.member_ids}
    assert any(s.node_id == N for s in report.skipped)

    # member left the search-visible set; canonical + near-miss + external remain.
    visible = _visible_ids(backend)
    assert M not in visible
    assert {C, N, X} <= visible

    # the member's assertion edge was mirrored onto the canonical (open) and the
    # member's own edge window-closed.
    mirror_id = make_edge_id(C, X, LinkType.TEMPORAL)
    mirror = [e for e, _ in backend.neighbors("u1", C, link_types={LinkType.TEMPORAL}, limit=10)]
    assert mirror_id in {e.id for e in mirror}
    assert backend.neighbors("u1", M, link_types={LinkType.TEMPORAL}, limit=10) == []  # closed

    # entity association copied to canonical; alias accreted; content byte-untouched.
    assert E in backend.entities_for_node("u1", C)
    canonical = backend.get_node("u1", C)
    assert canonical is not None
    assert "Member" in canonical.metadata.get(_ALIAS_KEY, "")
    assert canonical.content == "content of Canonical"  # §0: content untouched
    merged_member = backend.get_node("u1", M)
    assert merged_member is not None
    assert merged_member.content == "content of Member"  # §0: content untouched
    assert _MERGE_KEY in merged_member.metadata  # the reversal record is stored


# ----- unmerge: byte-exact inverse (K7-D-4.5) ------------------------------


def test_merge_then_unmerge_is_byte_exact(
    env: tuple[PostgresGraphBackend, _FakeIndex, ConsolidationPass],
) -> None:
    backend, index, pass_ = env
    _seed_cluster(backend)
    # snapshot the member's pre-merge state.
    pre_member = backend.get_node("u1", M)
    pre_emb = backend.get_embeddings("u1", [M])[M]
    pre_visible = _visible_ids(backend)
    pre_member_edges = {e.id for e, _ in backend.neighbors("u1", M, limit=10)}
    pre_canonical_entities = set(backend.entities_for_node("u1", C))

    pass_.run("u1")
    assert pass_.unmerge("u1", M) is True

    # content + embedding byte-exact.
    post_member = backend.get_node("u1", M)
    assert post_member is not None
    assert pre_member is not None
    assert post_member.content == pre_member.content
    assert post_member.metadata == pre_member.metadata  # merge record removed
    assert post_member.content_hash == pre_member.content_hash
    assert backend.is_merged("u1", M) is False  # merged_into cleared
    assert backend.get_embeddings("u1", [M])[M] == pre_emb

    # back in the search-visible set (byte-exact visibility).
    assert _visible_ids(backend) == pre_visible

    # edge topology restored: member's edge re-opened, canonical mirror removed.
    assert {e.id for e, _ in backend.neighbors("u1", M, limit=10)} == pre_member_edges
    mirror_id = make_edge_id(C, X, LinkType.TEMPORAL)
    assert mirror_id not in {e.id for e, _ in backend.neighbors("u1", C, limit=10)}

    # entity associations restored (the copy removed from the canonical).
    assert set(backend.entities_for_node("u1", C)) == pre_canonical_entities


# ----- idempotent re-run with an honest report (K7-D-4.6) ------------------


def test_rerun_is_idempotent_and_honest(
    env: tuple[PostgresGraphBackend, _FakeIndex, ConsolidationPass],
) -> None:
    backend, index, pass_ = env
    _seed_cluster(backend)
    first = pass_.run("u1")
    assert first.nodes_merged == 1

    second = pass_.run("u1")
    # nothing new merged; the report is honest (no silent skips).
    assert second.nodes_merged == 0
    assert second.clusters_formed == 0
    # state unchanged: member still merged into the same canonical.
    assert M in {n.id for n in backend.merged_members("u1", C)}


# ----- SELF is never a candidate on either side (K7-D-7) -------------------


def test_self_never_participates(
    env: tuple[PostgresGraphBackend, _FakeIndex, ConsolidationPass],
) -> None:
    backend, index, pass_ = env
    _seed_cluster(backend)
    # add a SELF node band-linked to the canonical — it must never be seed/member.
    self_id = make_self_node_id("u1")
    backend.insert_node(
        "u1", _node(self_id, "Alice", kind=NodeKind.SELF, evidence=5), _vec(0, 0.85)
    )
    backend.upsert_edge("u1", _sem_edge(C, self_id, 0.85))

    report = pass_.run("u1")
    all_touched = {report.merges[0].canonical_id, *report.merges[0].member_ids}
    assert self_id not in all_touched
    assert backend.is_merged("u1", self_id) is False  # never merged


# ----- allocator interaction (K7-D-9) --------------------------------------


def test_allocator_skips_merged_ids(
    env: tuple[PostgresGraphBackend, _FakeIndex, ConsolidationPass],
) -> None:
    backend, index, pass_ = env
    _seed_cluster(backend)  # nodes 0..3
    pass_.run("u1")  # merges node 1 into node 0
    # next index must be 4 (MAX live index +1) — a merged node's id (1) is NOT reused.
    assert backend.next_node_index("u1") == 4
    assert pass_.unmerge("u1", M) is True
    assert backend.next_node_index("u1") == 4  # still collision-free after unmerge


# ----- strict owner-scoping (K7-D-4) ---------------------------------------


def test_pass_is_owner_scoped(
    env: tuple[PostgresGraphBackend, _FakeIndex, ConsolidationPass],
) -> None:
    backend, index, pass_ = env
    _seed_cluster(backend, "u1")
    _seed_cluster(backend, "u2")  # a genuine second-tenant cluster
    report = pass_.run("u1")
    # only u1's nodes touched.
    touched = {report.merges[0].canonical_id, *report.merges[0].member_ids}
    assert all(nid.startswith("u1::") for nid in touched)
    # u2's would-be member is untouched (non-vacuous: it WOULD merge if scanned).
    assert backend.is_merged("u2", "u2::node::00000001") is False

"""Integration tests for the Spec K7 T2 versioning path over real Postgres (K7-D-1/-2/-7/-9).

Drives the full ``PostgresGraphStore`` (transport + merge + index + audit) so the
node-version write on ``_evolve``, the supersede gate, point-in-time ``get_node(as_of)``,
the byte-exact restore round-trip, the allocator collision fix, and the SELF guards
are all proven against real SQL (partial-unique windows, the version table, RLS-shaped
predicates). A mapping embedder injects controlled vectors for deterministic cosine.
"""

from __future__ import annotations

import math
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from persona.audit import MemoryAuditLogger
from persona.graph._schema import graph_metadata
from persona.graph.errors import GraphProtectedNodeError, NodeMergeError
from persona.graph.models import NodeKind, NodeProvenance, make_self_node_id
from persona.graph.protocol import KnowledgeCandidate, MergeAction, UpdateIntent
from persona.graph.store import build_graph_store
from persona.schema.chunks import WriteSource

if TYPE_CHECKING:
    from collections.abc import Iterator

    from persona.graph.store import PostgresGraphStore
    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

DIM = 384
T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
T1 = T0 + timedelta(days=30)
T2 = T0 + timedelta(days=60)


def _vec(primary: int, cos: float = 1.0, secondary: int = 383) -> list[float]:
    v = [0.0] * DIM
    v[primary] = cos
    v[secondary] = math.sqrt(max(0.0, 1.0 - cos * cos))
    return v


_MAP = {
    "lives in Bergen": _vec(0),
    "lives in Oslo": _vec(5),
    "lives in Trondheim": _vec(9),
    "enjoys hiking": _vec(2),
    "quit the gym": _vec(3),
    "n-zero": _vec(10),
    "n-one": _vec(12),
    "n-two": _vec(14),
}


class _Embedder:
    model_name = "mapping"

    @property
    def dimension(self) -> int:
        return DIM

    def encode(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            if t in _MAP:
                out.append(_MAP[t])
                continue
            keys = [k for k in _MAP if k in t]
            out.append(_MAP[max(keys, key=len)] if keys else _vec(100))
        return out


def _cand(content: str, *, written_at: datetime = T0, **kw: object) -> KnowledgeCandidate:
    base: dict[str, object] = {
        "concept_name": content[:20],
        "content": content,
        "node_kind": NodeKind.FACT,
        "provenance": NodeProvenance(source=WriteSource.PERSONA_SELF, written_at=written_at),
    }
    base.update(kw)
    return KnowledgeCandidate(**base)  # type: ignore[arg-type]


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
def store(_engine: Engine) -> Iterator[PostgresGraphStore]:
    with _engine.begin() as conn:
        graph_metadata.drop_all(conn)
        graph_metadata.create_all(conn)
    yield build_graph_store(engine=_engine, embedder=_Embedder(), audit_logger=MemoryAuditLogger())
    with _engine.begin() as conn:
        graph_metadata.drop_all(conn)


def _node_count(store: PostgresGraphStore, owner: str) -> int:
    return len(store.node_ids_for_owner(owner))


# ----- supersede writes a version; nothing destroyed (K7-D-1) --------------


def test_supersede_writes_version_preserving_content_and_embedding(
    store: PostgresGraphStore,
) -> None:
    created = store.merge("u1", _cand("lives in Bergen", written_at=T0))
    before = _node_count(store, "u1")
    out = store.merge(
        "u1",
        _cand(
            "lives in Oslo",
            written_at=T1,  # later ⇒ supersedes
            update_intent=UpdateIntent.CONTRADICT,
            target_node_id=created.node_id,
        ),
    )
    assert out.action is MergeAction.EVOLVED
    assert out.superseded_version_id is not None
    # nothing destroyed: the live node count is unchanged (monotone), version added.
    assert _node_count(store, "u1") == before
    versions = store.get_node_versions("u1", created.node_id)
    assert len(versions) == 1
    v = versions[0]
    assert v.content == "lives in Bergen"  # prior account preserved
    assert v.valid_at == T0
    assert v.invalid_at == T1
    assert v.version_id == out.superseded_version_id  # outcome points at the version
    # current row now holds the new account (prior embedding byte-exactness is proven
    # by the restore round-trip test).
    cur = store.get_node("u1", created.node_id)
    assert cur is not None
    assert cur.content == "lives in Oslo"


# ----- point-in-time get_node(as_of) (K7-D-1) ------------------------------


def test_get_node_as_of_returns_the_account_valid_then(store: PostgresGraphStore) -> None:
    created = store.merge("u1", _cand("lives in Bergen", written_at=T0))
    store.merge(
        "u1",
        _cand(
            "lives in Oslo",
            written_at=T1,
            update_intent=UpdateIntent.CONTRADICT,
            target_node_id=created.node_id,
        ),
    )
    store.merge(
        "u1",
        _cand(
            "lives in Trondheim",
            written_at=T2,
            update_intent=UpdateIntent.CONTRADICT,
            target_node_id=created.node_id,
        ),
    )
    # before creation → did not exist.
    assert store.get_node("u1", created.node_id, as_of=T0 - timedelta(days=1)) is None
    # within each window → that account.
    at_t0 = store.get_node("u1", created.node_id, as_of=T0 + timedelta(days=1))
    assert at_t0 is not None
    assert at_t0.content == "lives in Bergen"
    at_t1 = store.get_node("u1", created.node_id, as_of=T1 + timedelta(days=1))
    assert at_t1 is not None
    assert at_t1.content == "lives in Oslo"
    # at/after the last supersede → current account.
    now = store.get_node("u1", created.node_id, as_of=T2 + timedelta(days=1))
    assert now is not None
    assert now.content == "lives in Trondheim"
    # default (no as_of) → current.
    current = store.get_node("u1", created.node_id)
    assert current is not None
    assert current.content == "lives in Trondheim"


# ----- restore round-trip is byte-exact (K7-D-1 reversibility) -------------


def test_restore_version_round_trips_byte_exact(store: PostgresGraphStore) -> None:
    created = store.merge("u1", _cand("lives in Bergen", written_at=T0))
    original_emb = store.get_embeddings("u1", [created.node_id])[created.node_id]
    out = store.merge(
        "u1",
        _cand(
            "lives in Oslo",
            written_at=T1,
            update_intent=UpdateIntent.CONTRADICT,
            target_node_id=created.node_id,
        ),
    )
    assert out.superseded_version_id is not None
    restored = store.restore_node_version("u1", created.node_id, out.superseded_version_id)
    assert restored.content == "lives in Bergen"
    # byte-exact embedding + content restored via the durable version row.
    now = store.get_node("u1", created.node_id)
    assert now is not None
    assert now.content == "lives in Bergen"
    assert store.get_embeddings("u1", [created.node_id])[created.node_id] == original_emb
    # reversibility is additive: the restore appended provenance, destroyed nothing.
    reason = now.provenance[-1].reason
    assert reason is not None
    assert "restored" in reason


# ----- allocator collision fix over real SQL (K7-D-9) ----------------------


def test_allocator_no_collision_after_delete(store: PostgresGraphStore) -> None:
    a = store.merge("u1", _cand("n-zero"))
    b = store.merge("u1", _cand("n-one"))
    assert a.node_id.endswith("00000000")
    assert b.node_id.endswith("00000001")
    assert store.delete_node("u1", a.node_id) is True
    c = store.merge("u1", _cand("n-two"))
    # MAX(live index)+1 = 2, NOT count (=1) → no collision with the live node b.
    assert c.node_id.endswith("00000002")
    assert c.node_id != b.node_id


def test_allocator_counts_self_kind_node_with_numeric_id(
    store: PostgresGraphStore,
    _engine: Engine,  # noqa: PT019 — used directly for the raw seed insert
) -> None:
    """R4-C1-12: a SELF-*kind* node carrying a numeric ``::node::`` id (synthesis /
    entity resolution mis-classifying a person as self) MUST be counted by the
    allocator. Excluding by node_kind dropped it from the MAX, so the allocator
    re-handed-out its index → an INSERT PK collision that dead-lettered every
    synthesis job. Only the ``{owner}::self`` anchor (non-numeric suffix) is skipped.
    """
    import json

    from sqlalchemy import text

    store.merge("u1", _cand("n-zero"))  # ::node::00000000
    store.merge("u1", _cand("n-one"))  # ::node::00000001
    # The pathological row: SELF kind, but a numeric ``::node::`` id (NOT the anchor).
    prov = NodeProvenance(source=WriteSource.SYSTEM, written_at=datetime(2026, 1, 1, tzinfo=UTC))
    with _engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO graph_nodes (id, owner_id, node_kind, concept_name, content, "
                "metadata, embedding, embedding_model, content_hash, provenance, created_at, "
                "updated_at) VALUES ('u1::node::00000002', 'u1', 'self', 'p', 'p', '{}', :emb, "
                "'m', 'h', CAST(:prov AS jsonb), now(), now())"
            ),
            {"emb": str([0.0] * 384), "prov": json.dumps([prov.model_dump(mode="json")])},
        )
    # The allocator must return 3 (MAX over ALL numeric ids incl. the self-kind one),
    # never 2 — which would collide with the row just inserted and dead-letter the job.
    assert store._backend.next_node_index("u1") == 3  # noqa: SLF001 — the method under test


# ----- SELF guards over the store surface (K7-D-7) -------------------------


def test_delete_self_node_raises(store: PostgresGraphStore) -> None:
    store.get_or_create_self_node("u1", display_name="Alice")
    with pytest.raises(GraphProtectedNodeError):
        store.delete_node("u1", make_self_node_id("u1"))
    # still present.
    assert store.get_self_node("u1") is not None


def test_evolve_targeting_self_raises(store: PostgresGraphStore) -> None:
    store.get_or_create_self_node("u1", display_name="Alice")
    with pytest.raises(GraphProtectedNodeError):
        store.merge(
            "u1",
            _cand(
                "lives in Oslo",
                update_intent=UpdateIntent.UPDATE,
                target_node_id=make_self_node_id("u1"),
            ),
        )


def test_evolve_missing_target_raises(store: PostgresGraphStore) -> None:
    with pytest.raises(NodeMergeError):
        store.merge(
            "u1",
            _cand(
                "lives in Oslo",
                update_intent=UpdateIntent.UPDATE,
                target_node_id="u1::node::00099999",
            ),
        )

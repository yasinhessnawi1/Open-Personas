"""Integration tests for the K6 self node — ``get_or_create_self_node`` end to end.

Drives the assembled ``build_graph_store`` against real Postgres (both index
backends): create + read, idempotency, the reserved-id race (concurrent
double-create → exactly one node, no crash — K6-D-5), the rename→``concept_name``
sync with a provenance append (K6-D-9), the "``None`` never clobbers a set name"
rule, and the ``count_nodes`` exclusion (the self node doesn't consume a fact index).
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import pytest
from persona.audit import MemoryAuditLogger
from persona.graph._schema import graph_metadata
from persona.graph.config import GraphSettings
from persona.graph.models import NodeKind, NodeProvenance, make_self_node_id
from persona.graph.protocol import GraphStore, KnowledgeCandidate
from persona.graph.store import build_graph_store
from persona.schema.chunks import WriteSource
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

NOW = datetime(2026, 7, 2, 12, 0, tzinfo=UTC)
DIM = 384


def _vec(primary: int) -> list[float]:
    v = [0.0] * DIM
    v[primary] = 1.0
    return v


class _Embedder:
    model_name = "static"

    @property
    def dimension(self) -> int:
        return DIM

    def encode(self, texts: list[str]) -> list[list[float]]:
        # Deterministic non-zero vector per distinct text (content-independent is fine
        # here — these tests assert on identity/name/provenance, not recall).
        return [_vec(hash(t) % DIM) for t in texts]


def _fact(content: str) -> KnowledgeCandidate:
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

    from sqlalchemy import create_engine
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


def _settings(backend: str) -> GraphSettings:
    if backend == "turbovec":
        pytest.importorskip("turbovec")
        return GraphSettings(index_backend="turbovec", index_bit_width=4)
    return GraphSettings()


@pytest.fixture(params=["pgvector", "turbovec"])
def store(request: pytest.FixtureRequest, _engine: Engine) -> Iterator[tuple[GraphStore, Engine]]:
    settings = _settings(request.param)
    with _engine.begin() as conn:
        graph_metadata.drop_all(conn)
        graph_metadata.create_all(conn)
    built = build_graph_store(
        engine=_engine, embedder=_Embedder(), audit_logger=MemoryAuditLogger(), settings=settings
    )
    yield cast("GraphStore", built), _engine
    with _engine.begin() as conn:
        graph_metadata.drop_all(conn)


def _self_count(engine: Engine, owner_id: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                text("SELECT count(*) FROM graph_nodes WHERE owner_id = :o AND node_kind = 'self'"),
                {"o": owner_id},
            ).scalar_one()
        )


def test_get_self_node_is_none_before_creation(store: tuple[GraphStore, Engine]) -> None:
    s, _e = store
    assert s.get_self_node("u1") is None


def test_create_then_read_back_self_node(store: tuple[GraphStore, Engine]) -> None:
    s, _e = store
    node = s.get_or_create_self_node("u1", display_name="Ada Lovelace")
    assert node.node_kind is NodeKind.SELF
    assert node.id == make_self_node_id("u1")
    assert node.concept_name == "Ada Lovelace"

    read = s.get_self_node("u1")
    assert read is not None
    assert read.id == node.id
    assert read.concept_name == "Ada Lovelace"


def test_unnamed_create_uses_generic_label(store: tuple[GraphStore, Engine]) -> None:
    s, _e = store
    node = s.get_or_create_self_node("u1")  # no display_name yet
    assert node.concept_name == "the user"


def test_idempotent_second_call_returns_same_single_node(
    store: tuple[GraphStore, Engine],
) -> None:
    s, e = store
    first = s.get_or_create_self_node("u1", display_name="Ada")
    second = s.get_or_create_self_node("u1", display_name="Ada")
    assert first.id == second.id
    assert _self_count(e, "u1") == 1  # exactly one, not two


def test_none_display_name_never_clobbers_a_set_name(
    store: tuple[GraphStore, Engine],
) -> None:
    s, _e = store
    s.get_or_create_self_node("u1", display_name="Ada")
    # A graph write with no name in hand must not wipe the set name back to generic.
    node = s.get_or_create_self_node("u1", display_name=None)
    assert node.concept_name == "Ada"


def test_rename_syncs_concept_name_and_appends_provenance(
    store: tuple[GraphStore, Engine],
) -> None:
    s, _e = store
    created = s.get_or_create_self_node("u1", display_name="Ada")
    assert len(created.provenance) == 1

    renamed = s.get_or_create_self_node("u1", display_name="Grace")
    assert renamed.concept_name == "Grace"
    # Provenance APPENDED (no silent overwrite, K0-D-4): the prior name is preserved.
    assert len(renamed.provenance) == 2
    last = renamed.provenance[-1]
    assert last.source is WriteSource.USER
    assert last.reason == "name updated"
    assert last.superseded_content == "Ada"

    # Persisted, not just in-memory.
    read = s.get_self_node("u1")
    assert read is not None
    assert read.concept_name == "Grace"
    assert read.provenance[-1].superseded_content == "Ada"


def test_rename_to_same_name_is_a_noop(store: tuple[GraphStore, Engine]) -> None:
    s, _e = store
    s.get_or_create_self_node("u1", display_name="Ada")
    again = s.get_or_create_self_node("u1", display_name="Ada")
    assert len(again.provenance) == 1  # no spurious provenance append


def test_self_node_excluded_from_fact_index(store: tuple[GraphStore, Engine]) -> None:
    s, _e = store
    # Self exists first; the first fact must still get index 0 (self isn't counted).
    s.get_or_create_self_node("u1", display_name="Ada")
    out = s.merge("u1", _fact("likes coffee"))
    assert out.node_id == "u1::node::00000000"


def test_concurrent_double_create_yields_exactly_one(
    store: tuple[GraphStore, Engine],
) -> None:
    s, e = store

    def _call(_: int) -> str:
        return s.get_or_create_self_node("u_race", display_name="Racer").id

    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(_call, range(4)))

    # No crash; every caller sees the same reserved id; the DB holds exactly one row.
    assert set(ids) == {make_self_node_id("u_race")}
    assert _self_count(e, "u_race") == 1


def test_self_node_is_owner_scoped(store: tuple[GraphStore, Engine]) -> None:
    s, _e = store
    s.get_or_create_self_node("u1", display_name="Ada")
    assert s.get_self_node("u2") is None  # another user has no self node

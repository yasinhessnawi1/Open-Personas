"""Integration tests for the entity registry against Postgres (Spec K0, T5).

Validates the real SQL the registry relies on — especially
``find_entity_by_text``'s ``jsonb_array_elements`` alias lookup, which fakes
can't exercise — plus the create→resolve round-trip. Self-contained schema via
``graph_metadata.create_all`` (RLS is T4's), DATABASE_URL safety gate as elsewhere.
"""

from __future__ import annotations

import hashlib
import os
from typing import TYPE_CHECKING

import pytest
from persona.graph._schema import graph_metadata
from persona.graph.entities import PostgresEntityRegistry
from persona.graph.models import EntityAlias
from persona.graph.postgres import PostgresGraphBackend
from persona.graph.protocol import ResolutionDecision

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from sqlalchemy import Engine

pytestmark = pytest.mark.integration


class _Embedder:
    """Deterministic, NaN-safe 384-d embedder (byte-value derived; no model load)."""

    model_name = "test-bytes"

    @property
    def dimension(self) -> int:
        return 384

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        # Centered to [-1, 1] so distinct texts are ~orthogonal (positive-orthant
        # byte vectors would make every cosine ~0.8 and blur SEPARATE vs AMBIGUOUS).
        out: list[list[float]] = []
        for t in texts:
            digest = hashlib.sha256(t.encode("utf-8")).digest()  # 32 bytes
            vals = [(b - 127.5) / 127.5 for b in digest]
            out.append((vals * 12)[:384])
        return out


@pytest.fixture(scope="session")
def _graph_engine() -> Iterator[Engine]:
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
        pass  # concurrent CREATE EXTENSION IF NOT EXISTS catalog race; the extension exists
    except OperationalError as exc:
        engine.dispose()
        pytest.skip(f"Postgres unreachable: {exc}")
    yield engine
    engine.dispose()


@pytest.fixture
def registry(_graph_engine: Engine) -> Iterator[PostgresEntityRegistry]:
    with _graph_engine.begin() as conn:
        graph_metadata.drop_all(conn)
        graph_metadata.create_all(conn)
    backend = PostgresGraphBackend(engine=_graph_engine, persona_guard=False, node_releaser=None)
    yield PostgresEntityRegistry(backend=backend, embedder=_Embedder())
    with _graph_engine.begin() as conn:
        graph_metadata.drop_all(conn)


def test_create_entity_assigns_sequential_ids(registry: PostgresEntityRegistry) -> None:
    e0 = registry.create_entity("u1", canonical_name="Dr. Hansen")
    e1 = registry.create_entity("u1", canonical_name="Sara")
    assert e0.id == "u1::entity::00000000"
    assert e1.id == "u1::entity::00000001"


def test_resolve_exact_canonical_merges(registry: PostgresEntityRegistry) -> None:
    ent = registry.create_entity("u1", canonical_name="Dr. Hansen")
    verdict = registry.resolve("u1", "  dr. HANSEN  ")
    assert verdict.decision is ResolutionDecision.MERGE
    assert verdict.canonical_id == ent.id


def test_resolve_exact_alias_merges_via_jsonb_lookup(registry: PostgresEntityRegistry) -> None:
    # The real jsonb_array_elements alias query — the path fakes cannot exercise.
    ent = registry.create_entity(
        "u1",
        canonical_name="Dr. Hansen",
        aliases=(EntityAlias(surface="my doctor"), EntityAlias(surface="the GP")),
    )
    for mention in ("my doctor", "THE gp"):
        verdict = registry.resolve("u1", mention)
        assert verdict.decision is ResolutionDecision.MERGE, mention
        assert verdict.canonical_id == ent.id


def test_resolve_unknown_mention_separates(registry: PostgresEntityRegistry) -> None:
    registry.create_entity("u1", canonical_name="Dr. Hansen")
    verdict = registry.resolve("u1", "weekend hiking trip to Bergen")
    assert verdict.decision is ResolutionDecision.SEPARATE


def test_resolve_is_owner_scoped(registry: PostgresEntityRegistry) -> None:
    registry.create_entity("u1", canonical_name="Dr. Hansen")
    # Another user resolving the same surface sees no match → SEPARATE.
    assert registry.resolve("u2", "Dr. Hansen").decision is ResolutionDecision.SEPARATE


def test_add_alias_then_resolve_merges(registry: PostgresEntityRegistry) -> None:
    ent = registry.create_entity("u1", canonical_name="Dr. Hansen")
    assert registry.resolve("u1", "the GP").decision is ResolutionDecision.SEPARATE
    registry.add_alias("u1", ent.id, EntityAlias(surface="the GP", confidence=0.9))
    verdict = registry.resolve("u1", "the GP")
    assert verdict.decision is ResolutionDecision.MERGE
    assert verdict.canonical_id == ent.id


def test_alias_heavy_fixture_one_entity_round_trip(registry: PostgresEntityRegistry) -> None:
    # criterion 3 against the real DB: three surfaces → one canonical id.
    ent = registry.create_entity(
        "u1",
        canonical_name="Dr. Hansen",
        aliases=(EntityAlias(surface="my doctor"), EntityAlias(surface="the GP")),
    )
    ids = {registry.resolve("u1", m).canonical_id for m in ("Dr. Hansen", "my doctor", "the GP")}
    assert ids == {ent.id}

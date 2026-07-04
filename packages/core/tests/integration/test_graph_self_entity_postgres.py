"""Integration test for the self-node → canonical-entity gap (graph-links defect).

Live-operator finding: a user's graph held ``yasin hessnawi`` (the SELF node) and a
separate ``Yasin`` node — obviously the same person — yet nothing linked them. With
the REAL bge-small embedder the two bare-name *contents* sit at cosine ~0.813, below
every content bar (semantic-link 0.82, merge 0.88, consolidation band floor 0.82), so
neither auto-linking nor consolidation is the right mechanism for name identity —
**entity resolution** is. But the user's own identity was never registered as a
canonical entity, so a name variant of the user could never resolve to them (the
registry returned ``SEPARATE``), leaving the person fragmented.

These tests pin the fix: creating (or naming) the SELF node registers the user as a
canonical entity and associates the SELF node with it, so the registry now resolves a
name variant ("Yasin") ONTO the user (``MERGE``/``AMBIGUOUS``, never ``SEPARATE``).
The real bge embedder is used so the AMBIGUOUS review band is exercised genuinely.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, cast

import pytest
from persona.audit import MemoryAuditLogger
from persona.graph._schema import graph_metadata
from persona.graph.config import GraphSettings
from persona.graph.entities import PostgresEntityRegistry, normalize_surface
from persona.graph.postgres import PostgresGraphBackend
from persona.graph.protocol import GraphStore, ResolutionDecision
from persona.graph.store import build_graph_store
from persona.stores.embedder import SentenceTransformerEmbedder
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

SELF_NAME = "yasin hessnawi"
VARIANT = "Yasin"


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


@pytest.fixture(scope="session")
def bge_embedder() -> SentenceTransformerEmbedder:
    # The REAL bge-small embedder — the name embeddings + the AMBIGUOUS band must be
    # exercised with production semantics, not a mapping stub.
    return SentenceTransformerEmbedder(model_name="BAAI/bge-small-en-v1.5", device="cpu")


@pytest.fixture
def env(
    _engine: Engine, bge_embedder: SentenceTransformerEmbedder
) -> Iterator[tuple[GraphStore, PostgresGraphBackend]]:
    with _engine.begin() as conn:
        graph_metadata.drop_all(conn)
        graph_metadata.create_all(conn)
    store = build_graph_store(
        engine=_engine,
        embedder=bge_embedder,
        audit_logger=MemoryAuditLogger(),
        settings=GraphSettings(),
    )
    backend = PostgresGraphBackend(engine=_engine)
    yield cast("GraphStore", store), backend
    with _engine.begin() as conn:
        graph_metadata.drop_all(conn)


def test_self_node_creation_registers_a_canonical_entity_for_the_user(
    env: tuple[GraphStore, PostgresGraphBackend],
) -> None:
    """Naming the SELF node registers the user as a canonical entity + associates it."""
    store, backend = env
    self_node = store.get_or_create_self_node("u1", display_name=SELF_NAME)

    entity = backend.find_entity_by_text("u1", normalize_surface(SELF_NAME))
    assert entity is not None, "the user must be registered as a canonical entity"
    assert normalize_surface(entity.canonical_name) == normalize_surface(SELF_NAME)

    associated = backend.entities_for_node("u1", self_node.id)
    assert entity.id in associated, "the SELF node must be associated with the user entity"


def test_name_variant_resolves_onto_the_user_entity_not_separate(
    env: tuple[GraphStore, PostgresGraphBackend], bge_embedder: SentenceTransformerEmbedder
) -> None:
    """A variant of the user's name ("Yasin") now resolves ONTO the user, never SEPARATE.

    Before the fix the user was not an entity, so ``resolve`` returned ``SEPARATE`` and a
    fresh, disconnected entity was minted — the person fragments. After it, the registry
    finds the user (exact/MERGE or the AMBIGUOUS review band handed to K2's judge).
    """
    store, backend = env
    store.get_or_create_self_node("u1", display_name=SELF_NAME)

    registry = PostgresEntityRegistry(backend=backend, embedder=bge_embedder)
    verdict = registry.resolve("u1", VARIANT)

    assert verdict.decision is not ResolutionDecision.SEPARATE, (
        "a variant of the user's name must resolve onto the user, not mint a new entity"
    )
    self_entity = backend.find_entity_by_text("u1", normalize_surface(SELF_NAME))
    assert self_entity is not None
    if verdict.decision is ResolutionDecision.MERGE:
        assert verdict.canonical_id == self_entity.id
    else:  # AMBIGUOUS → the user is the candidate the K2 judge weighs
        assert self_entity.id in {c.entity_id for c in verdict.candidates}

"""Integration tests: the four typed stores backed by PostgresBackend (T07).

Mirrors ``packages/core/tests/integration/test_stores_chroma.py`` against
Postgres + pgvector. These prove the transport reuse works: policy, versioning,
audit, history/rollback, and episodic decay all come from the unchanged
``TypedStore``/``EpisodicStore`` above ``PostgresBackend``.

Spec §8 acceptance: #1 (CRUD), #2 (identity rejects / episodic accepts),
#3 (decay ordering).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from persona.audit import AuditAction, MemoryAuditLogger
from persona.errors import RuntimeWriteForbiddenError
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource
from persona.stores.episodic import EpisodicStore
from persona.stores.identity import IdentityStore
from persona.stores.postgres import PostgresBackend
from persona.stores.self_facts import SelfFactsStore

if TYPE_CHECKING:
    from sqlalchemy import Engine
    from tests.conftest import HashEmbedder384

pytestmark = pytest.mark.integration

UTC_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def backend(pg_engine: Engine, embedder: HashEmbedder384) -> PostgresBackend:
    # memory_chunks.persona_id FK-references personas(id); seed the owner + a
    # persona "p1" so writes satisfy the constraint.
    from sqlalchemy import text

    with pg_engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES ('u1', 'u1@example.com')"))
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES ('p1', 'u1', 'name: p1')")
        )
    return PostgresBackend(engine=pg_engine, embedder=embedder)


@pytest.fixture
def audit() -> MemoryAuditLogger:
    return MemoryAuditLogger()


def _chunk(
    *,
    chunk_id: str = "p1::self_facts::0001",
    text: str = "I specialise in Norwegian tenancy.",
    created_at: datetime = UTC_NOW,
    metadata: dict[str, str] | None = None,
) -> PersonaChunk:
    return PersonaChunk(id=chunk_id, text=text, metadata=metadata or {}, created_at=created_at)


# --- CRUD round-trip (acceptance #1) ----------------------------------------


def test_write_query_get_all_delete_remove(
    backend: PostgresBackend, audit: MemoryAuditLogger
) -> None:
    store = SelfFactsStore(backend=backend, audit_logger=audit)
    store.write("p1", [_chunk(text="I like strong coffee.")], source=WriteSource.USER)

    got = store.get_all("p1")
    assert len(got) == 1
    assert got[0].text == "I like strong coffee."

    hits = store.query("p1", "coffee preference", top_k=3)
    assert len(hits) == 1
    assert hits[0].distance is not None  # populated from cosine <=>

    store.remove_documents("p1", [got[0].id])
    assert store.get_all("p1") == []

    store.write("p1", [_chunk()], source=WriteSource.USER)
    store.delete("p1")
    assert store.get_all("p1") == []


# --- policy (acceptance #2) -------------------------------------------------


def test_identity_store_rejects_runtime_write(
    backend: PostgresBackend, audit: MemoryAuditLogger
) -> None:
    store = IdentityStore(backend=backend, audit_logger=audit)
    with pytest.raises(RuntimeWriteForbiddenError):
        store.write("p1", [_chunk(chunk_id="p1::identity::0001")], source=WriteSource.SYSTEM)


def test_episodic_store_accepts_runtime_write(
    backend: PostgresBackend, audit: MemoryAuditLogger
) -> None:
    store = EpisodicStore(backend=backend, audit_logger=audit)
    store.write("p1", [_chunk(chunk_id="p1::episodic::0001")], source=WriteSource.SYSTEM)
    assert len(store.get_all("p1")) == 1
    assert any(e.action == AuditAction.WRITE for e in audit.events)


# --- versioning + history (acceptance #1 / spec-01 contract) ----------------


def test_two_writes_same_logical_id_version_and_supersede(
    backend: PostgresBackend, audit: MemoryAuditLogger
) -> None:
    store = SelfFactsStore(backend=backend, audit_logger=audit)
    v1 = PersonaChunk(
        id="lg::v1",
        text="v1",
        created_at=UTC_NOW,
        provenance=ChunkProvenance(
            source=WriteSource.USER, logical_id="lg", version=1, written_at=UTC_NOW
        ),
    )
    store.write("p1", [v1], source=WriteSource.USER)
    v2 = PersonaChunk(
        id="lg::v2",
        text="v2",
        created_at=UTC_NOW,
        provenance=ChunkProvenance(
            source=WriteSource.USER, logical_id="lg", version=1, written_at=UTC_NOW
        ),
    )
    store.write("p1", [v2], source=WriteSource.USER)

    chain = store.history("p1", "lg")
    assert [c.provenance.version for c in chain if c.provenance] == [1, 2]
    # get_all default returns only the current head.
    heads = store.get_all("p1")
    assert len(heads) == 1
    assert heads[0].text == "v2"


def test_rollback_appends_new_head(backend: PostgresBackend, audit: MemoryAuditLogger) -> None:
    store = SelfFactsStore(backend=backend, audit_logger=audit)
    for v, txt in ((1, "first"), (1, "second")):
        store.write(
            "p1",
            [
                PersonaChunk(
                    id=f"lg::{txt}",
                    text=txt,
                    created_at=UTC_NOW,
                    provenance=ChunkProvenance(
                        source=WriteSource.USER, logical_id="lg", version=v, written_at=UTC_NOW
                    ),
                )
            ],
            source=WriteSource.USER,
        )
    store.rollback("p1", "lg", to_version=1, source=WriteSource.USER, reason="revert")
    head = store.get_all("p1")
    assert len(head) == 1
    assert head[0].text == "first"  # rolled back to v1's content


# --- episodic decay ordering (acceptance #3) --------------------------------


def test_episodic_decay_ranks_recent_above_stale(
    backend: PostgresBackend, audit: MemoryAuditLogger
) -> None:
    # Two equally-relevant chunks (identical text → identical embedding), one
    # 1h old and one 48h old. Under K8-D-4 retention (tau0 168h, strength 1)
    # the recent one still ranks first — the ordering property survives the
    # Spec K8 re-baseline off the flat-24h decay.
    store = EpisodicStore(backend=backend, audit_logger=audit)
    now = datetime.now(UTC)
    recent = _chunk(
        chunk_id="p1::episodic::recent",
        text="mould complaint about the landlord",
        created_at=now - timedelta(hours=1),
    )
    stale = _chunk(
        chunk_id="p1::episodic::stale",
        text="mould complaint about the landlord",
        created_at=now - timedelta(hours=48),
    )
    store.write("p1", [stale], source=WriteSource.SYSTEM)
    store.write("p1", [recent], source=WriteSource.SYSTEM)

    results = store.query("p1", "mould complaint about the landlord", top_k=1)
    assert len(results) == 1
    assert results[0].id == "p1::episodic::recent"

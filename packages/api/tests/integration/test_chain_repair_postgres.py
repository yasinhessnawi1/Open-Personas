"""The repair works on the production transport, and it does not embed (Spec K13, T3).

The Chroma half of this is covered through the CLI in
``packages/core/tests/integration/test_cli_repair.py``. This is the same behaviour against
Postgres, which is the transport that holds the only version chains production actually has
(``core_memory``, one per persona, rewritten by the background engine).

Two claims that only a real database can settle. That ``relink`` moves the version pointer and
leaves everything else exactly as it was, checked column by column rather than by reading the
chunk back through the mapper that would hide a difference. And that a repair never embeds:
the backend here is built with an embedder that raises if anything asks it to work, so a
repair that reached for a model would fail loudly instead of passing slowly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from persona.audit import AuditAction, MemoryAuditLogger
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource
from persona.stores.postgres import PostgresBackend
from persona.stores.self_facts import SelfFactsStore
from persona.stores.versioning import ChainDefect
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy import Engine
    from tests.conftest import HashEmbedder384

pytestmark = pytest.mark.integration

_PERSONA = "p1"
_LOGICAL = "p1::self_facts::0000"
_NOW = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)


class _RefusingEmbedder:
    """Mirrors what ``persona repair`` injects: an embedder that must never be called."""

    model_name: str = "must-not-be-called"

    @property
    def dimension(self) -> int:
        msg = "the repair asked for an embedding dimension"
        raise AssertionError(msg)

    def encode(self, texts: Sequence[str]) -> list[list[float]]:  # noqa: ARG002
        msg = "the repair tried to embed something"
        raise AssertionError(msg)


@pytest.fixture
def seeded(pg_engine: Engine) -> Engine:
    with pg_engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES ('u1', 'u1@example.com')"))
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES ('p1', 'u1', 'name: p1')")
        )
    return pg_engine


def _chunk(version: int, *, superseded_by: str | None, chunk_id: str) -> PersonaChunk:
    return PersonaChunk(
        id=chunk_id,
        text=f"version {version}",
        metadata={"importance": "0.5"},
        created_at=_NOW,
        provenance=ChunkProvenance(
            source=WriteSource.USER,
            logical_id=_LOGICAL,
            version=version,
            superseded_by=superseded_by,
            written_at=_NOW,
        ),
    )


def _row(engine: Engine, chunk_id: str) -> dict[str, Any]:
    with engine.connect() as conn:
        got = conn.execute(
            text(
                "SELECT id, text, metadata, content_hash, superseded_by, version, "
                "embedding::text AS embedding FROM memory_chunks WHERE id = :id"
            ),
            {"id": chunk_id},
        ).mappings()
        return dict(next(iter(got)))


def test_relink_moves_the_pointer_and_nothing_else(
    seeded: Engine, embedder: HashEmbedder384
) -> None:
    """Column by column: the vector, the text, the metadata and the hash are untouched."""
    writer = PostgresBackend(engine=seeded, embedder=embedder)
    writer.upsert(
        persona_id=_PERSONA,
        store_kind="self_facts",
        chunks=[_chunk(1, superseded_by="never-written", chunk_id=_LOGICAL)],
    )
    before = _row(seeded, _LOGICAL)

    # A backend that cannot embed, which is what the repair command builds.
    PostgresBackend(engine=seeded, embedder=_RefusingEmbedder()).relink(
        persona_id=_PERSONA, store_kind="self_facts", links={_LOGICAL: None}
    )

    after = _row(seeded, _LOGICAL)
    assert before["superseded_by"] == "never-written"
    assert after["superseded_by"] is None
    for column in ("text", "metadata", "content_hash", "version", "embedding"):
        assert after[column] == before[column], f"relink changed {column}"


def test_repair_mends_a_broken_chain_on_postgres(seeded: Engine, embedder: HashEmbedder384) -> None:
    writer = PostgresBackend(engine=seeded, embedder=embedder)
    writer.upsert(
        persona_id=_PERSONA,
        store_kind="self_facts",
        chunks=[
            _chunk(1, superseded_by="never-written", chunk_id=_LOGICAL),
            _chunk(2, superseded_by=None, chunk_id="retry"),
        ],
    )

    audit = MemoryAuditLogger()
    store = SelfFactsStore(
        backend=PostgresBackend(engine=seeded, embedder=_RefusingEmbedder()),
        audit_logger=audit,
    )

    found = store.diagnose(_PERSONA)
    assert [f.defect for f in found.findings] == [ChainDefect.DANGLING_LINK]

    assert store.repair(_PERSONA) == 1

    assert store.diagnose(_PERSONA).healthy
    assert [c.id for c in store.history(_PERSONA, _LOGICAL)] == [_LOGICAL, "retry"]
    assert [e.action for e in audit.events] == [AuditAction.REPAIR]


def test_a_chain_with_no_current_version_is_readable_before_any_repair(
    seeded: Engine, embedder: HashEmbedder384
) -> None:
    """The read tolerance on the production transport: the node is not lost while it waits."""
    writer = PostgresBackend(engine=seeded, embedder=embedder)
    writer.upsert(
        persona_id=_PERSONA,
        store_kind="self_facts",
        chunks=[_chunk(1, superseded_by="never-written", chunk_id=_LOGICAL)],
    )
    store = SelfFactsStore(
        backend=PostgresBackend(engine=seeded, embedder=_RefusingEmbedder()),
        audit_logger=MemoryAuditLogger(),
    )

    assert [c.id for c in store.get_all(_PERSONA)] == [_LOGICAL]

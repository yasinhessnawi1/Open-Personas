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

import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from persona.audit import AuditAction, MemoryAuditLogger
from persona.cli.main import app
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource
from persona.stores.postgres import PostgresBackend
from persona.stores.self_facts import SelfFactsStore
from persona.stores.versioning import ChainDefect
from sqlalchemy import text
from typer.testing import CliRunner

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


# --- settling a raced chain, and sweeping every persona (R9-205, R9-211) --------
#
# Restored data is what these model: rows that were written before the head index existed. The
# index would refuse to seed them, so it is dropped first, which is the state an operator is in
# when the migration has refused.


def _seed_raced(engine: Engine, embedder: HashEmbedder384, persona_id: str) -> None:
    with engine.begin() as conn:
        conn.execute(text("DROP INDEX IF EXISTS uq_memory_chain_head"))
    prefix = f"{persona_id}::"
    PostgresBackend(engine=engine, embedder=embedder).upsert(
        persona_id=persona_id,
        store_kind="self_facts",
        chunks=[
            _chunk(1, superseded_by=f"{prefix}b", chunk_id=f"{prefix}{_LOGICAL}"),
            _chunk(2, superseded_by=None, chunk_id=f"{prefix}a"),
            _chunk(2, superseded_by=None, chunk_id=f"{prefix}b"),
        ],
    )


def _second_persona(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES ('p2', 'u1', 'name: p2')")
        )


def _version_of(engine: Engine, chunk_id: str) -> int:
    return int(_row(engine, chunk_id)["version"])


def test_a_renumbering_relink_moves_the_version_and_nothing_else(
    seeded: Engine, embedder: HashEmbedder384
) -> None:
    _seed_raced(seeded, embedder, _PERSONA)
    before = _row(seeded, f"{_PERSONA}::b")

    PostgresBackend(engine=seeded, embedder=_RefusingEmbedder()).relink(
        persona_id=_PERSONA,
        store_kind="self_facts",
        links={f"{_PERSONA}::b": None},
        versions={f"{_PERSONA}::b": 3},
    )

    after = _row(seeded, f"{_PERSONA}::b")
    assert after["version"] == 3
    for column in ("text", "metadata", "content_hash", "superseded_by", "embedding"):
        assert after[column] == before[column], f"the renumbering changed {column}"


def _repair_cli(*args: str) -> Any:  # noqa: ANN401 - typer's Result
    return CliRunner().invoke(
        app, ["repair", "--all-personas", "--database-url", os.environ["DATABASE_URL"], *args]
    )


def test_the_sweep_finds_a_raced_chain_in_every_persona_and_changes_nothing_by_default(
    seeded: Engine, embedder: HashEmbedder384
) -> None:
    _second_persona(seeded)
    _seed_raced(seeded, embedder, "p1")
    _seed_raced(seeded, embedder, "p2")

    result = _repair_cli()

    assert result.exit_code == 1, result.output
    assert "Checking 2 personas" in result.output
    assert "--resolve-raced" in result.output
    assert _version_of(seeded, "p1::b") == 2
    assert _version_of(seeded, "p2::b") == 2


def test_the_sweep_settles_every_persona_and_records_it_in_the_database(
    seeded: Engine, embedder: HashEmbedder384
) -> None:
    _second_persona(seeded)
    _seed_raced(seeded, embedder, "p1")
    _seed_raced(seeded, embedder, "p2")

    result = _repair_cli("--apply", "--resolve-raced")

    assert result.exit_code == 0, result.output
    assert "now intact" in result.output
    assert _version_of(seeded, "p1::b") == 3
    assert _version_of(seeded, "p2::b") == 3
    with seeded.connect() as conn:
        audited = conn.execute(
            text(
                "SELECT persona_id FROM store_audit_events WHERE action = 'repair' "
                "ORDER BY persona_id"
            )
        ).scalars()
        assert list(audited) == ["p1", "p2"], "the record must outlive a one-shot container"
    assert _repair_cli().exit_code == 0, "a second sweep must be clean"


def test_the_sweep_refuses_a_role_that_cannot_see_every_tenant(seeded: Engine) -> None:
    """``persona_app`` reads zero rows, so a sweep would report a healthy database."""
    _ = seeded
    result = CliRunner().invoke(
        app,
        ["repair", "--all-personas", "--database-url", os.environ["APP_DATABASE_URL"]],
    )
    assert result.exit_code == 2, result.output
    assert "row level security" in result.output


def test_a_versions_only_relink_is_applied_as_on_chroma(
    seeded: Engine, embedder: HashEmbedder384
) -> None:
    """Round 2 review, LOW-3: Postgres returned early when ``links`` was empty."""
    PostgresBackend(engine=seeded, embedder=embedder).upsert(
        persona_id=_PERSONA,
        store_kind="self_facts",
        chunks=[_chunk(5, superseded_by=None, chunk_id=_LOGICAL)],
    )

    PostgresBackend(engine=seeded, embedder=_RefusingEmbedder()).relink(
        persona_id=_PERSONA, store_kind="self_facts", links={}, versions={_LOGICAL: 1}
    )

    assert _row(seeded, _LOGICAL)["version"] == 1

"""Spec K8 T6 — the sleep-time engine against the REAL K7 merge (bars 1/3/6).

The integration halves of the T6 gate, on the isolated worktree DB:

- **Idempotency against the real merge path** (bar 3): two full engine runs —
  the second writes zero gists, emits zero candidates, and the graph's node
  count is stable; a FORCED re-merge of the same candidate is K7's content
  short-circuit (no new node, no duplicated content).
- **Layer separation on real rows** (bar 6): raw episodic rows byte-untouched
  (text + content_hash) after the run; gists in the gist kind; concepts in
  ``graph_nodes`` — three distinct layers, no double-write.
- **The trigger chain's durable half** (bar 1): ``enqueue_episodic_consolidation``
  writes a real ``jobs`` row with the per-persona bucket key; a same-bucket
  re-enqueue is A0's ``ON CONFLICT`` no-op (one row). The full worker-claim
  live-fire leg runs at T8's integrated wiring pass (with the A4
  no-hand-invoked-step rule applied there).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from persona.audit import MemoryAuditLogger
from persona.graph import build_graph_store
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource, mint_chunk_id
from persona.schema.conversation import ORIGINATED_METADATA_KEY
from persona.stores.engine import EpisodicConsolidationEngine
from persona.stores.lifecycle import EpisodicSettings
from persona.stores.postgres import PostgresBackend
from persona.stores.pyramid import EpisodicPyramid
from persona.stores.summarizer import StubSummarizer
from persona_api.jobs.handlers.episodic_consolidation import enqueue_episodic_consolidation
from persona_api.jobs.queue import JobQueue

if TYPE_CHECKING:
    from sqlalchemy import Engine
    from tests.conftest import HashEmbedder384

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
_SETTINGS = EpisodicSettings(min_chunks_per_run=2, cluster_gap_minutes=45.0)


@pytest.fixture
def env(pg_engine: Engine, embedder: HashEmbedder384) -> dict[str, object]:
    from sqlalchemy import text

    with pg_engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES ('u1', 'u1@example.com')"))
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES ('p1', 'u1', 'name: p1')")
        )
    backend = PostgresBackend(engine=pg_engine, embedder=embedder)
    graph = build_graph_store(engine=pg_engine, embedder=embedder, audit_logger=MemoryAuditLogger())
    engine = EpisodicConsolidationEngine(
        backend=backend,
        pyramid=EpisodicPyramid(backend=backend, audit_logger=MemoryAuditLogger()),
        summarizer=StubSummarizer(),
        graph=graph,
        settings=_SETTINGS,
        embedder=None,
    )
    return {"backend": backend, "graph": graph, "engine": engine, "pg": pg_engine}


def _chunk(*, hours_ago: float, text: str, metadata: dict[str, str] | None = None) -> PersonaChunk:
    cid = mint_chunk_id("p1", "episodic")
    created = _NOW - timedelta(hours=hours_ago)
    return PersonaChunk(
        id=cid,
        text=text,
        metadata=metadata or {},
        created_at=created,
        provenance=ChunkProvenance(
            source=WriteSource.SYSTEM,
            logical_id=cid,
            version=1,
            written_at=created,
            written_by="test",
        ),
    )


def _seed_sessions(backend: PostgresBackend) -> list[PersonaChunk]:
    chunks = [
        _chunk(hours_ago=3.0, text="USER: we moved to Oslo\nASSISTANT: congratulations"),
        _chunk(hours_ago=2.9, text="USER: the flat is in Grünerløkka\nASSISTANT: lovely area"),
        _chunk(hours_ago=1.5, text="USER: new job starts Monday\nASSISTANT: exciting"),
        _chunk(hours_ago=1.4, text="USER: at the tax office\nASSISTANT: bring your contract"),
    ]
    backend.upsert(persona_id="p1", store_kind="episodic", chunks=chunks)
    return chunks


def test_engine_is_idempotent_against_the_real_merge(env: dict[str, object]) -> None:
    backend: PostgresBackend = env["backend"]  # type: ignore[assignment]
    engine: EpisodicConsolidationEngine = env["engine"]  # type: ignore[assignment]
    graph = env["graph"]
    raw = _seed_sessions(backend)

    pg: Engine = env["pg"]  # type: ignore[assignment]

    def _node_count() -> int:
        from sqlalchemy import text

        with pg.connect() as conn:
            return int(
                conn.execute(
                    text("SELECT count(*) FROM graph_nodes WHERE owner_id = 'u1'")
                ).scalar_one()
            )

    first = asyncio.run(engine.run("u1", "p1", now=_NOW))
    assert first.gists_written == 2  # noqa: PLR2004 — two closed sessions
    assert first.candidates_emitted == 2  # noqa: PLR2004
    nodes_after_first = _node_count()
    assert nodes_after_first >= 2  # noqa: PLR2004 — the two episode concepts landed

    second = asyncio.run(engine.run("u1", "p1", now=_NOW))
    assert second.gists_written == 0
    assert second.candidates_emitted == 0
    assert _node_count() == nodes_after_first

    # Forced re-merge of an identical candidate: K7's content short-circuit —
    # accumulate is a no-op when the content is already present (contract §3).
    gists = backend.get_all(persona_id="p1", store_kind="episodic_gist")
    candidate = engine._candidate_for(  # noqa: SLF001 — driving the exact emitted shape
        "p1", gists[0].text, max(c.created_at for c in raw)
    )
    graph.merge("u1", candidate)  # type: ignore[attr-defined]
    graph.merge("u1", candidate)  # type: ignore[attr-defined]
    assert _node_count() == nodes_after_first


def test_layer_separation_on_real_rows(env: dict[str, object]) -> None:
    backend: PostgresBackend = env["backend"]  # type: ignore[assignment]
    engine: EpisodicConsolidationEngine = env["engine"]  # type: ignore[assignment]
    raw = _seed_sessions(backend)
    before = {c.id: c.content_hash for c in raw}

    asyncio.run(engine.run("u1", "p1", now=_NOW))

    after = {c.id: c.content_hash for c in backend.get_all(persona_id="p1", store_kind="episodic")}
    assert after == before  # raw layer byte-untouched (the §0 floor)
    gists = backend.get_all(persona_id="p1", store_kind="episodic_gist")
    assert len(gists) == 2  # noqa: PLR2004
    assert all(g.member_ids for g in gists)  # drill pointers present


def test_enqueue_writes_one_coalesced_job_row(env: dict[str, object]) -> None:
    pg: Engine = env["pg"]  # type: ignore[assignment]
    from sqlalchemy import text

    queue = JobQueue(pg)
    for _ in range(3):  # a burst within one bucket coalesces to ONE job
        enqueue_episodic_consolidation(
            queue,
            owner_id="u1",
            persona_id="p1",
            delay_seconds=900.0,
            bucket_seconds=3600.0,
            now=_NOW,
        )
    with pg.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT idempotency_key, scheduled_at FROM jobs "
                "WHERE type = 'episodic_consolidation'"
            )
        ).all()
    assert len(rows) == 1
    bucket = str(int(_NOW.timestamp() // 3600.0))
    assert rows[0].idempotency_key == f"episodic_consolidation:p1:{bucket}"


def test_engine_demotes_old_covered_chunks_on_the_real_db(env: dict[str, object]) -> None:
    """K8 T7 end-to-end: gist THEN demote in one pass; display resolves to the gist."""
    from persona.stores.episodic import EpisodicStore
    from persona.stores.pyramid import OLDER_MEMORY_MARKER

    backend: PostgresBackend = env["backend"]  # type: ignore[assignment]
    # A tail-2 engine: the default 200-chunk verbatim tail would keep every
    # seeded chunk FULL — the bound must be visible at test scale.
    tiering_settings = EpisodicSettings(
        min_chunks_per_run=2, cluster_gap_minutes=45.0, verbatim_tail_count=2
    )
    engine = EpisodicConsolidationEngine(
        backend=backend,
        pyramid=EpisodicPyramid(backend=backend, audit_logger=MemoryAuditLogger()),
        summarizer=StubSummarizer(),
        graph=env["graph"],  # type: ignore[arg-type]
        settings=tiering_settings,
    )
    # A session far past the demote horizon (90 days) at strength 1.
    old = [
        _chunk(hours_ago=90 * 24.0, text="USER: old thing one\nASSISTANT: ok"),
        _chunk(hours_ago=90 * 24.0 - 0.1, text="USER: old thing two\nASSISTANT: ok"),
    ]
    fresh = [
        _chunk(hours_ago=0.01, text="USER: hi\nASSISTANT: hello"),
        _chunk(hours_ago=0.02, text="USER: hey\nASSISTANT: hi"),
    ]
    backend.upsert(persona_id="p1", store_kind="episodic", chunks=[*old, *fresh])

    report = asyncio.run(engine.run("u1", "p1", now=_NOW))
    assert report.gists_written >= 1
    assert report.bands_demoted == 2  # noqa: PLR2004 — the covered old session

    rows = {c.id: c for c in backend.get_all(persona_id="p1", store_kind="episodic")}
    assert all(rows[c.id].band == 1 for c in old)
    assert all(rows[c.id].band == 0 for c in fresh)
    assert report.full_band_size == 2  # noqa: PLR2004

    # The K8-D-11 display: a demoted hit renders its gist with the marker.
    store = EpisodicStore(backend=backend, audit_logger=MemoryAuditLogger(), settings=_SETTINGS)
    displayed = store.resolve_display("p1", [rows[old[0].id]])
    assert displayed[0].text.startswith(OLDER_MEMORY_MARKER)


# --- D-K12-F: transient assistant-action/system-event logs never graduate -------


def _live_graph_node_contents(pg: Engine, owner_id: str = "u1") -> list[str]:
    """Every non-merged-away graph node's ``content`` for ``owner_id`` (the real table)."""
    from sqlalchemy import text

    with pg.connect() as conn:
        rows = conn.execute(
            text("SELECT content FROM graph_nodes WHERE owner_id = :owner AND merged_into IS NULL"),
            {"owner": owner_id},
        ).all()
    return [r.content for r in rows]


def test_transient_action_event_logs_do_not_graduate_to_the_graph(
    env: dict[str, object],
) -> None:
    """The D-K12-F gate, against the REAL K7 merge (not a hand-forced end state).

    A window of purely transient assistant-action/system-event chunks (a
    scheduled-fire origination report + a task-milestone completion notice —
    the exact shapes the owner's live graph showed polluting it) produces NO
    durable graph node, while a separate window stating a real user goal still
    graduates — proven by driving one real ``engine.run`` consolidation pass
    end to end, then reading the actual ``graph_nodes`` table.
    """
    backend: PostgresBackend = env["backend"]  # type: ignore[assignment]
    engine: EpisodicConsolidationEngine = env["engine"]  # type: ignore[assignment]
    pg: Engine = env["pg"]  # type: ignore[assignment]

    transient = [
        _chunk(
            hours_ago=3.0,
            text=(
                "ASSISTANT (originated): Scheduled reminder fired on 2026-07-07 "
                "at 09:00 — sched-abc123"
            ),
            metadata={ORIGINATED_METADATA_KEY: "true", "conversation_id": "conv-1"},
        ),
        _chunk(
            hours_ago=2.9,
            text="The assistant completed the task: daily inbox check",
            metadata={"source": "task_milestone", "milestone": "completed", "task_id": "t1"},
        ),
    ]
    durable = [
        _chunk(
            hours_ago=1.5,
            text=(
                "USER: I want you to set up a recurring email check every "
                "morning\nASSISTANT: done, I'll check every morning"
            ),
        ),
        _chunk(hours_ago=1.4, text="USER: also my dog is named Balto\nASSISTANT: noted"),
    ]
    backend.upsert(persona_id="p1", store_kind="episodic", chunks=[*transient, *durable])

    report = asyncio.run(engine.run("u1", "p1", now=_NOW))
    assert report.gists_written == 2  # noqa: PLR2004 — both sessions still gist (recall untouched)
    assert report.candidates_emitted == 1  # only the durable session graduates
    assert report.candidates_excluded_transient == 1

    contents = _live_graph_node_contents(pg)
    assert not any("fired on" in c or "completed the task" in c for c in contents)
    assert any("wants" in c or "recurring email check" in c for c in contents)

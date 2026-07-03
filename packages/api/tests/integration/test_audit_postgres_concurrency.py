"""R5-D-1 load-bearing proof: multi-worker audit integrity under REAL concurrency.

The whole point of moving the store-mutation + tool audit logs off single-writer
JSONL (``threading.Lock``, per-process) onto Postgres is that N workers / N Fly
Machines can write AT THE SAME TIME with no lost / duplicated / interleaved-
corrupted entries. A single-threaded test would false-green exactly the property
production needs, so this drives M **concurrent writers with independent
connections** against a real Postgres (the sanctioned pattern from
``test_credits_double_spend`` — each writer builds its OWN engine, i.e. its own
pool = its own backend, and a ``Barrier`` releases all M into the emit loop at
once for maximum contention).

The integrity assertions (per R5 kickoff §"things to get exactly right" #1):
  * ``rows_out == M×K`` — nothing lost, nothing duplicated.
  * ``count(distinct id) == M×K`` — no duplicate primary keys.
  * every persisted row reconstructs to a valid ``AuditEvent`` / ``ToolAuditEvent``.
  * the full ``(worker, seq)`` marker set round-trips exactly once each.
  * per-persona reads come back timestamp-ordered (interleaving preserved).

Writers connect as ``persona_app`` (``APP_DATABASE_URL``) when available — the
production role — falling back to the superuser ``DATABASE_URL``. ``migrated_engine``
guarantees the schema is at head + ``persona_app`` is granted before writers run.
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona.audit import AuditAction, AuditEvent
from persona.schema.chunks import WriteSource
from persona.tools.audit import ToolAuditEvent
from persona_api.db.audit_loggers import PostgresAuditLogger, PostgresToolAuditLogger
from persona_api.db.models import store_audit_events, tool_audit_events
from sqlalchemy import create_engine, func, select

if TYPE_CHECKING:
    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_M_WRITERS = 8
_K_EVENTS = 50  # 8 × 50 = 400 concurrent writes per subsystem
_PERSONAS = ("r5-audit-alpha", "r5-audit-beta", "r5-audit-gamma")


def _writer_url() -> str:
    """The production (persona_app) DSN when set, else the superuser test DSN."""
    return os.environ.get("APP_DATABASE_URL") or os.environ["DATABASE_URL"]


def _persona_for(worker: int, seq: int) -> str:
    """Interleave personas across (worker, seq) so multiple workers hammer the
    SAME persona concurrently — the case JSONL's per-persona file would corrupt."""
    return _PERSONAS[(worker + seq) % len(_PERSONAS)]


# --------------------------------------------------------------------------- #
# Store-mutation audit (PostgresAuditLogger)                                   #
# --------------------------------------------------------------------------- #
def test_store_audit_concurrent_writers_no_loss_no_dup(migrated_engine: Engine) -> None:
    _ = migrated_engine  # ensures schema-at-head + persona_app grant before writers
    url = _writer_url()
    start = threading.Barrier(_M_WRITERS)

    def _writer(worker: int) -> None:
        engine = create_engine(url)
        try:
            logger = PostgresAuditLogger(engine)
            start.wait()  # all M writers enter the emit loop together
            for seq in range(_K_EVENTS):
                logger.emit(
                    AuditEvent(
                        timestamp=datetime.now(UTC),
                        persona_id=_persona_for(worker, seq),
                        action=AuditAction.WRITE,
                        store="self_facts",
                        source=WriteSource.SYSTEM,
                        written_by=f"worker-{worker}",
                        reason="r5-concurrency-probe",
                        chunk_ids=[f"c-{worker}-{seq}"],
                        logical_ids=[f"l-{worker}-{seq}"],
                        metadata={"worker": str(worker), "seq": str(seq)},
                    )
                )
        finally:
            engine.dispose()

    with ThreadPoolExecutor(max_workers=_M_WRITERS) as pool:
        list(pool.map(_writer, range(_M_WRITERS)))

    expected = _M_WRITERS * _K_EVENTS
    reader = create_engine(url)
    try:
        # (2) no duplicate primary keys across all our personas' rows.
        with reader.begin() as conn:
            total, distinct_ids = conn.execute(
                select(func.count(), func.count(store_audit_events.c.id.distinct())).where(
                    store_audit_events.c.persona_id.in_(_PERSONAS)
                )
            ).one()
        assert total == expected, f"lost/extra rows: got {total}, want {expected}"
        assert distinct_ids == expected, f"duplicate ids: {distinct_ids} distinct of {total}"

        # (1,3,4,5) read every persona back through the real read path — this both
        # reconstructs+validates each AuditEvent and proves per-persona ordering.
        logger = PostgresAuditLogger(reader)
        seen: set[tuple[int, int]] = set()
        read_total = 0
        for persona in _PERSONAS:
            events = logger.read(persona)
            read_total += len(events)
            timestamps = [e.timestamp for e in events]
            assert timestamps == sorted(timestamps), f"{persona}: not timestamp-ordered"
            for e in events:
                assert e.persona_id == persona
                seen.add((int(e.metadata["worker"]), int(e.metadata["seq"])))
    finally:
        reader.dispose()

    assert read_total == expected, f"read path lost rows: {read_total} of {expected}"
    # The full grid appears exactly once — nothing lost, nothing duplicated.
    assert seen == {(w, s) for w in range(_M_WRITERS) for s in range(_K_EVENTS)}


# --------------------------------------------------------------------------- #
# Tool audit (PostgresToolAuditLogger) — same M-writer proof                   #
# --------------------------------------------------------------------------- #
def test_tool_audit_concurrent_writers_no_loss_no_dup(migrated_engine: Engine) -> None:
    _ = migrated_engine
    url = _writer_url()
    start = threading.Barrier(_M_WRITERS)

    def _writer(worker: int) -> None:
        engine = create_engine(url)
        try:
            logger = PostgresToolAuditLogger(engine)
            start.wait()
            for seq in range(_K_EVENTS):
                logger.emit(
                    ToolAuditEvent(
                        timestamp=datetime.now(UTC),
                        persona_id=_persona_for(worker, seq),
                        tool_name="file_write",
                        action="write",
                        resource=f"sandbox/w{worker}/s{seq}.txt",
                        is_error=False,
                        metadata={"worker": str(worker), "seq": str(seq)},
                    )
                )
        finally:
            engine.dispose()

    with ThreadPoolExecutor(max_workers=_M_WRITERS) as pool:
        list(pool.map(_writer, range(_M_WRITERS)))

    expected = _M_WRITERS * _K_EVENTS
    reader = create_engine(url)
    try:
        with reader.begin() as conn:
            total, distinct_ids = conn.execute(
                select(func.count(), func.count(tool_audit_events.c.id.distinct())).where(
                    tool_audit_events.c.persona_id.in_(_PERSONAS)
                )
            ).one()
            rows = (
                conn.execute(
                    select(
                        tool_audit_events.c.persona_id,
                        tool_audit_events.c.tool_name,
                        tool_audit_events.c.action,
                        tool_audit_events.c.resource,
                        tool_audit_events.c.is_error,
                        tool_audit_events.c.metadata,
                        tool_audit_events.c.timestamp,
                    ).where(tool_audit_events.c.persona_id.in_(_PERSONAS))
                )
                .mappings()
                .all()
            )
    finally:
        reader.dispose()

    assert total == expected, f"lost/extra rows: got {total}, want {expected}"
    assert distinct_ids == expected, f"duplicate ids: {distinct_ids} distinct of {total}"
    seen: set[tuple[int, int]] = set()
    for row in rows:
        # every persisted row reconstructs to a valid ToolAuditEvent (Pydantic validates).
        event = ToolAuditEvent(
            timestamp=row["timestamp"],
            persona_id=row["persona_id"],
            tool_name=row["tool_name"],
            action=row["action"],
            resource=row["resource"],
            is_error=row["is_error"],
            metadata=dict(row["metadata"] or {}),
        )
        seen.add((int(event.metadata["worker"]), int(event.metadata["seq"])))
    assert seen == {(w, s) for w in range(_M_WRITERS) for s in range(_K_EVENTS)}


# --------------------------------------------------------------------------- #
# Read-path signature translation (PostgresAuditLogger.read)                   #
# --------------------------------------------------------------------------- #
def test_store_audit_read_translates_the_filter_signature(migrated_engine: Engine) -> None:
    """``.read(persona_id, *, since, action, source, store)`` maps each optional
    filter to a WHERE clause, ANDs them, and returns oldest-first — parity with
    ``JSONLAuditLogger.read`` / ``_filter_events``."""
    _ = migrated_engine
    persona = "r5-read-filters"
    engine = create_engine(_writer_url())
    try:
        logger = PostgresAuditLogger(engine)
        base = datetime(2026, 1, 1, tzinfo=UTC)
        # Four events spanning the filter dimensions, emitted out of time order.
        events = [
            AuditEvent(
                timestamp=base.replace(hour=h),
                persona_id=persona,
                action=action,
                store=store,
                source=source,
                chunk_ids=[f"c{h}"],
            )
            for h, action, store, source in [
                (3, AuditAction.WRITE, "self_facts", WriteSource.SYSTEM),
                (1, AuditAction.DELETE, "worldview", WriteSource.USER),
                (2, AuditAction.WRITE, "worldview", WriteSource.PERSONA_SELF),
                (0, AuditAction.ROLLBACK, "self_facts", WriteSource.USER),
            ]
        ]
        for event in events:
            logger.emit(event)

        # No filter → all four, oldest-first (order by timestamp).
        allev = logger.read(persona)
        assert [e.timestamp.hour for e in allev] == [0, 1, 2, 3]

        # Single-dimension filters.
        assert {e.action for e in logger.read(persona, action=AuditAction.WRITE)} == {
            AuditAction.WRITE
        }
        assert {e.source for e in logger.read(persona, source=WriteSource.USER)} == {
            WriteSource.USER
        }
        assert {e.store for e in logger.read(persona, store="worldview")} == {"worldview"}
        assert [e.timestamp.hour for e in logger.read(persona, since=base.replace(hour=2))] == [
            2,
            3,
        ]

        # Filters AND together (WRITE ∧ worldview → only the hour-2 event).
        anded = logger.read(persona, action=AuditAction.WRITE, store="worldview")
        assert [e.timestamp.hour for e in anded] == [2]

        # Missing persona → [], not an error; naive `since` → ValueError (core parity).
        assert logger.read("does-not-exist") == []
        with pytest.raises(ValueError, match="tz-aware"):
            logger.read(persona, since=datetime(2026, 1, 1))  # noqa: DTZ001 — intentionally naive
    finally:
        engine.dispose()

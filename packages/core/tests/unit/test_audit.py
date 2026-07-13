"""Tests for ``persona.audit``."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path  # noqa: TC003 — used at runtime in fixtures

import pytest
from persona.audit import (
    AuditAction,
    AuditEvent,
    AuditLogger,
    JSONLAuditLogger,
    MemoryAuditLogger,
)
from persona.errors import AuditWriteError
from persona.schema.chunks import WriteSource
from pydantic import ValidationError

UTC_NOW = datetime(2026, 5, 27, 12, 0, 0, tzinfo=UTC)


def _event(
    *,
    persona_id: str = "astrid",
    action: AuditAction = AuditAction.WRITE,
    store: str = "episodic",
    source: WriteSource = WriteSource.SYSTEM,
    when: datetime = UTC_NOW,
    chunk_ids: list[str] | None = None,
) -> AuditEvent:
    return AuditEvent(
        timestamp=when,
        persona_id=persona_id,
        action=action,
        store=store,  # type: ignore[arg-type]
        source=source,
        chunk_ids=chunk_ids or [],
    )


class TestAuditEvent:
    def test_minimal_event(self) -> None:
        e = _event()
        assert e.persona_id == "astrid"
        assert e.action == AuditAction.WRITE
        assert e.chunk_ids == []
        assert e.metadata == {}

    def test_naive_timestamp_rejected(self) -> None:
        with pytest.raises(ValidationError, match="naive"):
            AuditEvent(
                timestamp=datetime(2026, 5, 27, 12, 0, 0),  # noqa: DTZ001
                persona_id="x",
                action=AuditAction.WRITE,
                store="episodic",
                source=WriteSource.SYSTEM,
            )

    def test_frozen(self) -> None:
        e = _event()
        with pytest.raises(ValidationError):
            e.persona_id = "y"  # type: ignore[misc]

    def test_round_trip_via_json(self) -> None:
        e = _event(
            action=AuditAction.ROLLBACK,
            source=WriteSource.USER,
            chunk_ids=["a", "b"],
        )
        restored = AuditEvent.model_validate_json(e.model_dump_json())
        assert restored == e

    def test_store_literal_enforced(self) -> None:
        with pytest.raises(ValidationError):
            AuditEvent(
                timestamp=UTC_NOW,
                persona_id="x",
                action=AuditAction.WRITE,
                store="bogus",  # type: ignore[arg-type]
                source=WriteSource.SYSTEM,
            )

    def test_store_accepts_core_memory(self) -> None:
        """R9-031: ``core_memory`` (Spec K9's CoreMemoryStore, a fifth
        TypedStore) used to pydantic-literal_error here — EVERY core-block
        refresh crashed at ``TypedStore._emit_audit``. Round-trips like any
        other store kind."""
        e = _event(store="core_memory")
        assert e.store == "core_memory"
        restored = AuditEvent.model_validate_json(e.model_dump_json())
        assert restored == e


class TestMemoryAuditLogger:
    def test_emit_and_read(self) -> None:
        log = MemoryAuditLogger()
        e = _event()
        log.emit(e)
        events = log.read("astrid")
        assert events == [e]

    def test_read_returns_only_matching_persona(self) -> None:
        log = MemoryAuditLogger()
        log.emit(_event(persona_id="astrid"))
        log.emit(_event(persona_id="other"))
        assert len(log.read("astrid")) == 1
        assert len(log.read("other")) == 1

    def test_implements_audit_logger_protocol(self) -> None:
        assert isinstance(MemoryAuditLogger(), AuditLogger)


class TestJSONLAuditLogger:
    def test_emit_creates_file_and_directory(self, tmp_path: Path) -> None:
        log = JSONLAuditLogger(tmp_path / "audit")
        log.emit(_event())
        target = tmp_path / "audit" / "astrid.jsonl"
        assert target.exists()
        content = target.read_text(encoding="utf-8")
        assert content.endswith("\n")
        assert content.count("\n") == 1

    def test_emit_appends(self, tmp_path: Path) -> None:
        log = JSONLAuditLogger(tmp_path)
        log.emit(_event())
        log.emit(_event(action=AuditAction.ROLLBACK))
        content = (tmp_path / "astrid.jsonl").read_text(encoding="utf-8")
        assert content.count("\n") == 2

    def test_read_missing_file_returns_empty(self, tmp_path: Path) -> None:
        log = JSONLAuditLogger(tmp_path)
        assert log.read("never_written") == []

    def test_read_skips_corrupt_lines(self, tmp_path: Path) -> None:
        log = JSONLAuditLogger(tmp_path)
        log.emit(_event())
        # Corrupt the file by appending a malformed line.
        with (tmp_path / "astrid.jsonl").open("a", encoding="utf-8") as f:
            f.write("not-valid-json{\n")
        log.emit(_event(action=AuditAction.DELETE))
        events = log.read("astrid")
        # The two good lines survive; the corrupt one is silently skipped.
        assert len(events) == 2
        assert events[0].action == AuditAction.WRITE
        assert events[1].action == AuditAction.DELETE

    def test_filter_by_action(self, tmp_path: Path) -> None:
        log = JSONLAuditLogger(tmp_path)
        log.emit(_event(action=AuditAction.WRITE))
        log.emit(_event(action=AuditAction.DELETE))
        log.emit(_event(action=AuditAction.ROLLBACK))
        assert len(log.read("astrid", action=AuditAction.WRITE)) == 1
        assert len(log.read("astrid", action=AuditAction.DELETE)) == 1

    def test_filter_by_source(self, tmp_path: Path) -> None:
        log = JSONLAuditLogger(tmp_path)
        log.emit(_event(source=WriteSource.SYSTEM))
        log.emit(_event(source=WriteSource.USER))
        log.emit(_event(source=WriteSource.PERSONA_SELF))
        assert len(log.read("astrid", source=WriteSource.USER)) == 1

    def test_filter_by_since(self, tmp_path: Path) -> None:
        log = JSONLAuditLogger(tmp_path)
        early = UTC_NOW - timedelta(days=2)
        late = UTC_NOW + timedelta(days=2)
        log.emit(_event(when=early))
        log.emit(_event(when=UTC_NOW))
        log.emit(_event(when=late))
        assert len(log.read("astrid", since=UTC_NOW)) == 2

    def test_filter_since_must_be_tz_aware(self, tmp_path: Path) -> None:
        log = JSONLAuditLogger(tmp_path)
        log.emit(_event())
        with pytest.raises(ValueError, match="tz-aware"):
            log.read("astrid", since=datetime(2026, 5, 27, 12, 0, 0))  # noqa: DTZ001

    def test_combined_filters_and_together(self, tmp_path: Path) -> None:
        log = JSONLAuditLogger(tmp_path)
        log.emit(_event(action=AuditAction.WRITE, source=WriteSource.SYSTEM))
        log.emit(_event(action=AuditAction.WRITE, source=WriteSource.USER))
        log.emit(_event(action=AuditAction.DELETE, source=WriteSource.USER))
        result = log.read(
            "astrid",
            action=AuditAction.WRITE,
            source=WriteSource.USER,
        )
        assert len(result) == 1

    def test_implements_audit_logger_protocol(self, tmp_path: Path) -> None:
        assert isinstance(JSONLAuditLogger(tmp_path), AuditLogger)

    def test_thousand_sequential_emits_land_as_thousand_lines(self, tmp_path: Path) -> None:
        log = JSONLAuditLogger(tmp_path)
        for i in range(1000):
            log.emit(_event(chunk_ids=[f"c{i}"]))
        content = (tmp_path / "astrid.jsonl").read_text(encoding="utf-8")
        assert content.count("\n") == 1000

    def test_concurrent_emits_do_not_tear_writes(self, tmp_path: Path) -> None:
        log = JSONLAuditLogger(tmp_path)
        n_threads = 20
        per_thread = 50

        def worker(thread_id: int) -> None:
            for i in range(per_thread):
                log.emit(_event(chunk_ids=[f"t{thread_id}_i{i}"]))

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        events = log.read("astrid")
        assert len(events) == n_threads * per_thread
        # Every line parsed cleanly — proving no torn writes.

    def test_emit_failure_raises_audit_write_error(self, tmp_path: Path) -> None:
        # Use a path where the parent is a file, not a directory, so
        # mkdir(exist_ok=True) on the audit root fails.
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        log = JSONLAuditLogger(blocker / "audit")
        with pytest.raises(AuditWriteError, match="append"):
            log.emit(_event())


def test_memory_logger_fixture_is_fresh_per_test(memory_audit_logger: MemoryAuditLogger) -> None:
    """The fixture is recreated per test, so leftover events don't leak."""
    assert memory_audit_logger.events == []
    memory_audit_logger.emit(_event())
    assert len(memory_audit_logger.events) == 1

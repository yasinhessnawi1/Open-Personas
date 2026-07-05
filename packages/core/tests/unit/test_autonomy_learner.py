"""Tests for ``persona.autonomy.record_autonomy_update`` — spec 21 T03.

The learner is exercised against a real :class:`SelfFactsStore` over a stateful
in-memory backend, so the genuine persona_self policy (force + confidence +
reason), version-chain append, and single-AuditEvent emission all run. Cooldown
windows (D-21-4) are tested by pre-seeding a chain head with a controlled
``written_at`` (the store stamps its own ``written_at`` from the wall clock, so
the head cannot be back-dated through ``record_autonomy_update`` itself).
"""
# ruff: noqa: ANN401, ARG002

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from persona.audit import AuditAction, MemoryAuditLogger
from persona.autonomy import (
    AUTONOMY_LOGICAL_ID,
    AUTONOMY_METADATA_KEY,
    AUTONOMY_SESSION_METADATA_KEY,
    record_autonomy_update,
    resolve_autonomy,
)
from persona.errors import (
    AutonomyCooldownError,
    InvalidAutonomyLevelError,
    PersonaSelfWriteForbiddenError,
)
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource
from persona.schema.persona import Persona
from persona.stores.self_facts import SelfFactsStore

if TYPE_CHECKING:
    from collections.abc import Iterable

PERSONA_ID = "p_learn"


class _StatefulBackend:
    """An in-memory ``Backend`` that actually stores chunks (upsert + get_all)."""

    def __init__(self) -> None:
        self._chunks: dict[tuple[str, str], dict[str, PersonaChunk]] = {}

    def upsert(self, *, persona_id: str, store_kind: str, chunks: list[PersonaChunk]) -> None:
        bucket = self._chunks.setdefault((persona_id, store_kind), {})
        for chunk in chunks:
            bucket[chunk.id] = chunk

    def query(
        self,
        *,
        persona_id: str,
        store_kind: str,
        text: str,
        top_k: int,
        where: dict[str, Any] | None = None,
    ) -> list[PersonaChunk]:
        return []

    def get_all(self, *, persona_id: str, store_kind: str) -> list[PersonaChunk]:
        return list(self._chunks.get((persona_id, store_kind), {}).values())

    def delete_persona(self, persona_id: str, store_kind: str) -> None:
        self._chunks.pop((persona_id, store_kind), None)

    def reinforce(
        self,
        *,
        persona_id: str,
        store_kind: str,
        ids: list[str],
        recalled_at: object,
    ) -> None:
        from datetime import datetime as _dt

        assert isinstance(recalled_at, _dt)
        bucket = self._chunks.get((persona_id, store_kind), {})
        for cid in ids:
            if cid in bucket:
                c = bucket[cid]
                bucket[cid] = c.model_copy(
                    update={"strength": c.strength + 1, "last_recalled_at": recalled_at}
                )

    def set_bands(self, *, persona_id: str, store_kind: str, bands: dict[str, int]) -> None:
        bucket = self._chunks.get((persona_id, store_kind), {})
        for cid, band in bands.items():
            if cid in bucket:
                bucket[cid] = bucket[cid].model_copy(update={"band": band})

    def band_histogram(self, *, persona_id: str, store_kind: str) -> dict[int, int]:
        histogram: dict[int, int] = {}
        for c in self._chunks.get((persona_id, store_kind), {}).values():
            if c.provenance is None or c.provenance.superseded_by is None:
                histogram[c.band] = histogram.get(c.band, 0) + 1
        return histogram

    def count(
        self,
        *,
        persona_id: str,
        store_kind: str,
        include_superseded: bool = False,
    ) -> int:
        chunks = self.get_all(persona_id=persona_id, store_kind=store_kind)
        if include_superseded:
            return len(chunks)
        return len(
            [c for c in chunks if c.provenance is None or c.provenance.superseded_by is None]
        )

    def recent(self, *, persona_id: str, store_kind: str, limit: int) -> list[PersonaChunk]:
        current = [
            c
            for c in self.get_all(persona_id=persona_id, store_kind=store_kind)
            if c.provenance is None or c.provenance.superseded_by is None
        ]
        current.sort(key=lambda c: (c.created_at, c.id), reverse=True)
        return current[:limit]

    def get_by_logical_ids(
        self,
        *,
        persona_id: str,
        store_kind: str,
        logical_ids: list[str],
    ) -> list[PersonaChunk]:
        wanted = set(logical_ids)
        return [
            c
            for c in self.get_all(persona_id=persona_id, store_kind=store_kind)
            if c.provenance is not None and c.provenance.logical_id in wanted
        ]

    def delete_documents(self, *, persona_id: str, store_kind: str, ids: list[str]) -> None:
        bucket = self._chunks.get((persona_id, store_kind), {})
        for chunk_id in ids:
            bucket.pop(chunk_id, None)


@pytest.fixture
def store(memory_audit_logger: MemoryAuditLogger) -> SelfFactsStore:
    return SelfFactsStore(backend=_StatefulBackend(), audit_logger=memory_audit_logger)


def _persona(level: str = "cautious") -> Persona:
    return Persona.model_validate(
        {
            "schema_version": "1.0",
            "persona_id": PERSONA_ID,
            "identity": {"name": "n", "role": "r", "background": "b"},
            "autonomy": level,
        },
    )


def _seed_head(
    store: SelfFactsStore,
    *,
    level: str,
    written_at: datetime,
    session_id: str | None,
) -> None:
    """Pre-seed a single (version 1) autonomy chain head with a controlled timestamp."""
    metadata = {AUTONOMY_METADATA_KEY: level, "confidence": "0.95"}
    if session_id is not None:
        metadata[AUTONOMY_SESSION_METADATA_KEY] = session_id
    chunk = PersonaChunk(
        id=f"{PERSONA_ID}::self_facts::autonomy::0001",
        text=f"Autonomy preference set to {level}.",
        metadata=metadata,
        created_at=written_at,
        provenance=ChunkProvenance(
            source=WriteSource.PERSONA_SELF,
            logical_id=AUTONOMY_LOGICAL_ID,
            version=1,
            superseded_by=None,
            written_at=written_at,
            written_by="frontier:test",
            reason="seed",
        ),
    )
    store._backend.upsert(  # noqa: SLF001 — test pre-seeds the transport directly
        persona_id=PERSONA_ID, store_kind="self_facts", chunks=[chunk]
    )


def _chunks(store: SelfFactsStore) -> Iterable[PersonaChunk]:
    return store.get_all(PERSONA_ID, include_superseded=True)


class TestFirstUpdate:
    def test_first_update_persists_and_resolves(
        self, store: SelfFactsStore, memory_audit_logger: MemoryAuditLogger
    ) -> None:
        record_autonomy_update(
            store,
            PERSONA_ID,
            "decisive",
            now=datetime.now(UTC),
            written_by="frontier:test",
            reason="user kept overriding my questions",
            confidence=0.95,
        )
        assert resolve_autonomy(_persona("cautious"), _chunks(store)) == "decisive"

    def test_first_update_emits_exactly_one_audit_event(
        self, store: SelfFactsStore, memory_audit_logger: MemoryAuditLogger
    ) -> None:
        record_autonomy_update(
            store,
            PERSONA_ID,
            "balanced",
            now=datetime.now(UTC),
            written_by="frontier:test",
            reason="settling into a rhythm",
            confidence=0.9,
        )
        events = memory_audit_logger.events
        assert len(events) == 1
        assert events[0].action is AuditAction.WRITE
        assert events[0].source is WriteSource.PERSONA_SELF
        assert AUTONOMY_LOGICAL_ID in events[0].logical_ids

    def test_session_id_recorded_on_chunk(self, store: SelfFactsStore) -> None:
        record_autonomy_update(
            store,
            PERSONA_ID,
            "balanced",
            now=datetime.now(UTC),
            written_by="frontier:test",
            reason="r",
            confidence=0.9,
            session_id="conv_42",
        )
        head = next(
            c
            for c in _chunks(store)
            if c.provenance and c.provenance.logical_id == AUTONOMY_LOGICAL_ID
        )
        assert head.metadata[AUTONOMY_SESSION_METADATA_KEY] == "conv_42"


class TestPolicyEnforcement:
    def test_confidence_below_threshold_rejected(
        self, store: SelfFactsStore, memory_audit_logger: MemoryAuditLogger
    ) -> None:
        with pytest.raises(PersonaSelfWriteForbiddenError):
            record_autonomy_update(
                store,
                PERSONA_ID,
                "decisive",
                now=datetime.now(UTC),
                written_by="frontier:test",
                reason="r",
                confidence=0.5,
            )
        assert memory_audit_logger.events == []  # rejected writes never audit

    def test_empty_reason_rejected(self, store: SelfFactsStore) -> None:
        with pytest.raises(PersonaSelfWriteForbiddenError):
            record_autonomy_update(
                store,
                PERSONA_ID,
                "decisive",
                now=datetime.now(UTC),
                written_by="frontier:test",
                reason="   ",
                confidence=0.95,
            )

    def test_invalid_level_rejected_before_write(
        self, store: SelfFactsStore, memory_audit_logger: MemoryAuditLogger
    ) -> None:
        with pytest.raises(InvalidAutonomyLevelError):
            record_autonomy_update(
                store,
                PERSONA_ID,
                "reckless",  # type: ignore[arg-type]
                now=datetime.now(UTC),
                written_by="frontier:test",
                reason="r",
                confidence=0.95,
            )
        assert memory_audit_logger.events == []


class TestCooldown:
    def test_same_day_update_rejected(
        self, store: SelfFactsStore, memory_audit_logger: MemoryAuditLogger
    ) -> None:
        now = datetime.now(UTC)
        _seed_head(store, level="balanced", written_at=now, session_id="S1")
        with pytest.raises(AutonomyCooldownError, match="today") as exc:
            record_autonomy_update(
                store,
                PERSONA_ID,
                "decisive",
                now=now,
                written_by="frontier:test",
                reason="r",
                confidence=0.95,
                session_id="S2",
            )
        assert exc.value.context["window"] == "day"
        assert memory_audit_logger.events == []

    def test_cross_midnight_same_session_rejected(self, store: SelfFactsStore) -> None:
        yesterday = datetime.now(UTC) - timedelta(days=1)
        _seed_head(store, level="balanced", written_at=yesterday, session_id="S1")
        with pytest.raises(AutonomyCooldownError, match="this session") as exc:
            record_autonomy_update(
                store,
                PERSONA_ID,
                "decisive",
                now=datetime.now(UTC),
                written_by="frontier:test",
                reason="r",
                confidence=0.95,
                session_id="S1",
            )
        assert exc.value.context["window"] == "session"

    def test_new_day_new_session_appends_version_two(self, store: SelfFactsStore) -> None:
        yesterday = datetime.now(UTC) - timedelta(days=1)
        _seed_head(store, level="balanced", written_at=yesterday, session_id="S1")
        record_autonomy_update(
            store,
            PERSONA_ID,
            "decisive",
            now=datetime.now(UTC),
            written_by="frontier:test",
            reason="moved on",
            confidence=0.95,
            session_id="S2",
        )
        # Chain now has two versions; resolution follows the live head.
        chain = store.history(PERSONA_ID, AUTONOMY_LOGICAL_ID)
        assert len(chain) == 2
        assert resolve_autonomy(_persona("cautious"), _chunks(store)) == "decisive"

    def test_no_session_id_only_day_window_applies(self, store: SelfFactsStore) -> None:
        yesterday = datetime.now(UTC) - timedelta(days=1)
        _seed_head(store, level="balanced", written_at=yesterday, session_id=None)
        # Different day, no session_id supplied → update proceeds.
        record_autonomy_update(
            store,
            PERSONA_ID,
            "decisive",
            now=datetime.now(UTC),
            written_by="frontier:test",
            reason="r",
            confidence=0.95,
        )
        assert resolve_autonomy(_persona("cautious"), _chunks(store)) == "decisive"

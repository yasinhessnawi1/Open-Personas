"""A rollback is a write, and the write policy decides it (R9-207).

``TypedStore.rollback`` called ``backend.upsert`` directly and never ran
``evaluate_write_policy``, so it skipped the force flag, the required reason and the confidence
floor every ordinary write goes through. A ``persona_self`` rollback was a write the policy never
saw. So was a USER rollback on ``core_memory``, whose table has no USER rule at all.

Rollback had no production caller, which is exactly why this was easy to miss. ME0's retraction
is the first one, and it is the path by which a fact the persona committed gets changed.

Two details are the ones a careless fix gets wrong, so each has its own test. The policy reads the
caller's OWN reason, because the default ``rollback to version N`` string would otherwise satisfy
``requires_reason`` for everyone. And it judges the chunk that would actually land, which carries
the TARGET version's metadata, not the current head's.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona.audit import AuditAction, MemoryAuditLogger
from persona.errors import (
    BrokenVersionChainError,
    PersonaSelfWriteForbiddenError,
    RuntimeWriteForbiddenError,
)
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource
from persona.stores.core_memory import CoreMemoryStore
from persona.stores.episodic import EpisodicStore
from persona.stores.self_facts import SelfFactsStore
from persona.stores.worldview import WorldviewStore

if TYPE_CHECKING:
    from persona.stores.base import TypedStore

_NOW = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)
_PERSONA = "p1"
_LOGICAL = "fact"


class _Backend:
    """Holds rows by id and records every upsert call, in order, as the chunks it was handed."""

    def __init__(self) -> None:
        self.rows: dict[str, PersonaChunk] = {}
        self.upserts: list[list[PersonaChunk]] = []

    def upsert(self, *, persona_id: str, store_kind: str, chunks: list[PersonaChunk]) -> None:
        _ = (persona_id, store_kind)
        self.upserts.append(list(chunks))
        self.rows.update({chunk.id: chunk for chunk in chunks})

    def get_by_logical_ids(
        self, *, persona_id: str, store_kind: str, logical_ids: list[str]
    ) -> list[PersonaChunk]:
        _ = (persona_id, store_kind)
        return [
            chunk
            for chunk in self.rows.values()
            if chunk.provenance is not None and chunk.provenance.logical_id in logical_ids
        ]


def _version(chunk_id: str, text: str, metadata: dict[str, str]) -> PersonaChunk:
    return PersonaChunk(
        id=chunk_id,
        text=text,
        metadata=metadata,
        created_at=_NOW,
        provenance=ChunkProvenance(source=WriteSource.USER, logical_id=_LOGICAL, written_at=_NOW),
    )


def _seeded(
    store_class: type[TypedStore],
    *,
    v1: dict[str, str] | None = None,
    v2: dict[str, str] | None = None,
    seed_source: WriteSource = WriteSource.USER,
) -> tuple[TypedStore, _Backend, MemoryAuditLogger]:
    """A two version chain, written through the store so it is shaped like a real one."""
    backend = _Backend()
    audit = MemoryAuditLogger()
    store = store_class(backend=backend, audit_logger=audit)  # type: ignore[arg-type]
    for chunk_id, text, metadata in (
        ("v1", "the first text", v1 or {}),
        ("v2", "the second text", v2 or {}),
    ):
        store.write(_PERSONA, [_version(chunk_id, text, metadata)], source=seed_source)
    backend.upserts.clear()
    return store, backend, audit


#: ``_seeded`` writes two versions, and ``MemoryAuditLogger.events`` is a snapshot of them.
_SEED_EVENTS = 2
_CONFIDENT = {"confidence": "0.9"}
_DOUBTFUL = {"confidence": "0.5"}


def _refused_and_nothing_landed(backend: _Backend, audit: MemoryAuditLogger) -> None:
    assert backend.upserts == [], "a refused rollback must not reach the transport"
    assert len(audit.events) == _SEED_EVENTS, "a refused rollback must not be audited"
    assert len(backend.rows) == 2, "a refused rollback must leave the chain as it was"


def test_persona_self_needs_force_on_self_facts() -> None:
    store, backend, audit = _seeded(SelfFactsStore, v1=_CONFIDENT, v2=_CONFIDENT)
    with pytest.raises(PersonaSelfWriteForbiddenError):
        store.rollback(_PERSONA, _LOGICAL, 1, source=WriteSource.PERSONA_SELF, reason="because")
    _refused_and_nothing_landed(backend, audit)


def test_the_default_reason_does_not_satisfy_requires_reason() -> None:
    """``rollback to version 1`` is a label for the record, not a reason anyone gave."""
    store, backend, audit = _seeded(SelfFactsStore, v1=_CONFIDENT, v2=_CONFIDENT)
    with pytest.raises(PersonaSelfWriteForbiddenError, match="reason"):
        store.rollback(_PERSONA, _LOGICAL, 1, source=WriteSource.PERSONA_SELF, force=True)
    _refused_and_nothing_landed(backend, audit)


def test_a_blank_reason_is_refused_too() -> None:
    store, backend, audit = _seeded(SelfFactsStore, v1=_CONFIDENT, v2=_CONFIDENT)
    with pytest.raises(PersonaSelfWriteForbiddenError, match="reason"):
        store.rollback(
            _PERSONA, _LOGICAL, 1, source=WriteSource.PERSONA_SELF, force=True, reason="   "
        )
    _refused_and_nothing_landed(backend, audit)


def test_the_floor_reads_the_target_not_the_current_head() -> None:
    """Rolling back TO a doubtful version is a doubtful write, however sure the head is."""
    store, backend, audit = _seeded(SelfFactsStore, v1=_DOUBTFUL, v2=_CONFIDENT)
    with pytest.raises(PersonaSelfWriteForbiddenError, match="threshold"):
        store.rollback(
            _PERSONA, _LOGICAL, 1, source=WriteSource.PERSONA_SELF, force=True, reason="because"
        )
    _refused_and_nothing_landed(backend, audit)


def test_a_doubtful_head_does_not_block_rolling_back_to_a_confident_version() -> None:
    store, backend, _ = _seeded(SelfFactsStore, v1=_CONFIDENT, v2=_DOUBTFUL)
    store.rollback(
        _PERSONA, _LOGICAL, 1, source=WriteSource.PERSONA_SELF, force=True, reason="because"
    )
    assert store.history(_PERSONA, _LOGICAL)[-1].text == "the first text"
    assert len(backend.rows) == 3


def test_a_persona_self_rollback_that_meets_the_rules_lands() -> None:
    store, _, audit = _seeded(SelfFactsStore, v1=_CONFIDENT, v2=_CONFIDENT)
    store.rollback(
        _PERSONA, _LOGICAL, 1, source=WriteSource.PERSONA_SELF, force=True, reason="corrected"
    )
    head = store.history(_PERSONA, _LOGICAL)[-1]
    assert head.text == "the first text"
    assert head.provenance is not None
    assert head.provenance.source is WriteSource.PERSONA_SELF
    assert head.provenance.reason == "corrected"
    assert audit.events[-1].action is AuditAction.ROLLBACK
    assert len(audit.events) == _SEED_EVENTS + 1


def test_system_needs_force_on_self_facts() -> None:
    store, backend, audit = _seeded(SelfFactsStore)
    with pytest.raises(RuntimeWriteForbiddenError):
        store.rollback(_PERSONA, _LOGICAL, 1, source=WriteSource.SYSTEM)
    _refused_and_nothing_landed(backend, audit)


def test_force_lets_system_through_on_self_facts() -> None:
    store, backend, _ = _seeded(SelfFactsStore)
    store.rollback(_PERSONA, _LOGICAL, 1, source=WriteSource.SYSTEM, force=True)
    assert len(backend.rows) == 3


def test_a_person_can_roll_back_their_own_facts_with_no_force() -> None:
    """ME0's retraction: the user confirmed it, and USER is ACCEPT on self_facts and worldview."""
    for store_class in (SelfFactsStore, WorldviewStore):
        store, backend, _ = _seeded(store_class)
        store.rollback(_PERSONA, _LOGICAL, 1, source=WriteSource.USER)
        assert len(backend.rows) == 3


def test_worldview_persona_self_needs_the_epistemic_tag_on_the_target() -> None:
    store, backend, audit = _seeded(WorldviewStore, v1={}, v2={"epistemic": "belief"})
    with pytest.raises(PersonaSelfWriteForbiddenError, match="epistemic"):
        store.rollback(
            _PERSONA, _LOGICAL, 1, source=WriteSource.PERSONA_SELF, force=True, reason="because"
        )
    _refused_and_nothing_landed(backend, audit)


def test_a_person_cannot_roll_back_a_core_block() -> None:
    """Core memory's table has one rule, SYSTEM: no user write path, so no user rollback."""
    store, backend, audit = _seeded(CoreMemoryStore, seed_source=WriteSource.SYSTEM)
    with pytest.raises(RuntimeWriteForbiddenError):
        store.rollback(_PERSONA, _LOGICAL, 1, source=WriteSource.USER)
    _refused_and_nothing_landed(backend, audit)


def test_system_can_roll_back_a_core_block() -> None:
    store, backend, _ = _seeded(CoreMemoryStore, seed_source=WriteSource.SYSTEM)
    store.rollback(_PERSONA, _LOGICAL, 1, source=WriteSource.SYSTEM)
    assert len(backend.rows) == 3


def test_episodic_accepts_any_source_as_its_table_says() -> None:
    for source in (WriteSource.SYSTEM, WriteSource.USER, WriteSource.PERSONA_SELF):
        store, backend, _ = _seeded(EpisodicStore)
        store.rollback(_PERSONA, _LOGICAL, 1, source=source)
        assert len(backend.rows) == 3


def test_a_missing_version_is_still_a_chain_error_not_a_policy_one() -> None:
    """SYSTEM with no force would be refused by the policy, so this proves the order."""
    store, _, _ = _seeded(SelfFactsStore)
    with pytest.raises(BrokenVersionChainError, match="does not exist"):
        store.rollback(_PERSONA, _LOGICAL, 99, source=WriteSource.SYSTEM)


def test_rollback_writes_through_the_same_one_call_as_a_write() -> None:
    """One upsert, link before head: K13's atomicity and the order the head index relies on."""
    store, backend, _ = _seeded(SelfFactsStore)
    store.rollback(_PERSONA, _LOGICAL, 1, source=WriteSource.USER)

    assert len(backend.upserts) == 1, "a second call reopens the kill window K13 closed"
    prior, head = backend.upserts[0]
    assert prior.id == "v2"
    assert prior.provenance is not None
    assert prior.provenance.superseded_by == head.id
    assert head.provenance is not None
    assert head.provenance.superseded_by is None
    assert head.provenance.version == 3


def test_the_record_still_says_it_was_a_rollback() -> None:
    """Sharing the path with ``write`` must not turn the audit into a plain write."""
    store, _, audit = _seeded(SelfFactsStore)
    store.rollback(_PERSONA, _LOGICAL, 1, source=WriteSource.USER, written_by="owner")

    event = audit.events[-1]
    assert event.action is AuditAction.ROLLBACK
    assert event.metadata == {"to_version": "1"}
    assert event.written_by == "owner"
    assert event.reason == "rollback to version 1"
    assert event.logical_ids == [_LOGICAL]
    head = store.history(_PERSONA, _LOGICAL)[-1]
    assert event.chunk_ids == [head.id]
    assert head.provenance is not None
    assert head.provenance.reason == "rollback to version 1"

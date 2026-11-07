"""The loser of a chain race re-reads and appends (R9-205, the store half).

``TypedStore.write`` reads a chain, computes version N+1 and writes it, with nothing around the
three steps. Two writers that read the same chain both computed N+1. The partial unique index on
chain heads makes the second one fail instead of landing, and this is what happens next: the
transport says ``ChainConflictError``, and the store reads the chain again and appends on top of
what won, a bounded number of times.

The fake models that index, because raising a canned exception would prove nothing about what the
loser does next. A batch is checked as a whole and either lands or changes nothing, which is what
a transaction gives Postgres. Real threads on a real index are in the api integration suite.

Two things a retry must NOT do, which each have tests. It must not append a version identical to
what just won, or two callers asking for the same thing leave a duplicate in the history. And it
must not walk past a decision the caller made from the chain it read: the autonomy cooldown is
such a decision, so autonomy takes the conflict itself and goes back through its gate.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from persona.audit import AuditAction, MemoryAuditLogger
from persona.autonomy import AUTONOMY_LOGICAL_ID, record_autonomy_update
from persona.errors import AutonomyCooldownError, ChainConflictError, PersonaError
from persona.registry import PersonaRegistry
from persona.schema.chunks import (
    ChunkProvenance,
    PersonaChunk,
    WriteSource,
    make_chunk_id,
)
from persona.schema.persona import Persona
from persona.stores.core_memory import CoreMemoryStore
from persona.stores.episodic import EpisodicStore
from persona.stores.identity import IdentityStore
from persona.stores.postgres import CHAIN_HEAD_INDEX, EMBEDDING_DIM, PostgresBackend
from persona.stores.self_facts import SelfFactsStore
from persona.stores.versioning import ChainDefect, diagnose_chain
from persona.stores.worldview import WorldviewStore
from sqlalchemy.exc import IntegrityError

if TYPE_CHECKING:
    from persona.stores.base import TypedStore

_NOW = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)
_PERSONA = "p1"
_LOGICAL = "L"
_YAML = (
    Path(__file__).parents[2] / "fixtures" / "personas" / "valid" / "03_legal_assistant_full.yaml"
)


class _IndexedBackend:
    """In-memory ``memory_chunks`` with the partial unique index on chain heads.

    ``before_next_upsert`` runs once, inside the next upsert, after that writer has read its
    chain: writer A has read, writer B runs to the end, then A tries to land.
    """

    def __init__(self) -> None:
        self.rows: dict[str, tuple[str, str, PersonaChunk]] = {}
        self.before_next_upsert: Any = None
        self.reads = 0
        self.upserts = 0
        self.always_conflict = False

    def _of(self, persona_id: str, store_kind: str) -> list[PersonaChunk]:
        return [c for p, k, c in self.rows.values() if p == persona_id and k == store_kind]

    def upsert(self, *, persona_id: str, store_kind: str, chunks: list[PersonaChunk]) -> None:
        self.upserts += 1
        hook, self.before_next_upsert = self.before_next_upsert, None
        if callable(hook):
            hook()
        if self.always_conflict:
            raise ChainConflictError("another writer got there first")
        after = {**self.rows, **{c.id: (persona_id, store_kind, c) for c in chunks}}
        heads: set[tuple[str, str, str]] = set()
        for owner, kind, chunk in after.values():
            prov = chunk.provenance
            if prov is None or prov.superseded_by is not None:
                continue
            key = (owner, kind, prov.logical_id)
            if key in heads:
                raise ChainConflictError("another writer got there first")
            heads.add(key)
        self.rows = after

    def get_all(self, *, persona_id: str, store_kind: str) -> list[PersonaChunk]:
        return self._of(persona_id, store_kind)

    def get_by_logical_ids(
        self, *, persona_id: str, store_kind: str, logical_ids: list[str]
    ) -> list[PersonaChunk]:
        self.reads += 1
        return [
            c
            for c in self._of(persona_id, store_kind)
            if c.provenance is not None and c.provenance.logical_id in logical_ids
        ]

    def count(self, *, persona_id: str, store_kind: str, include_superseded: bool = False) -> int:
        _ = include_superseded
        return len(self._of(persona_id, store_kind))


def _chunk(chunk_id: str, text: str, logical_id: str = _LOGICAL) -> PersonaChunk:
    return PersonaChunk(
        id=chunk_id,
        text=text,
        metadata={"confidence": "0.9"},
        created_at=_NOW,
        provenance=ChunkProvenance(source=WriteSource.USER, logical_id=logical_id, written_at=_NOW),
    )


def _store(
    backend: _IndexedBackend, store_class: type[TypedStore] = SelfFactsStore
) -> tuple[Any, MemoryAuditLogger]:
    audit = MemoryAuditLogger()
    return store_class(backend=backend, audit_logger=audit), audit  # type: ignore[arg-type]


def _chain(backend: _IndexedBackend, logical_id: str = _LOGICAL) -> list[PersonaChunk]:
    rows = [
        c
        for _, _, c in backend.rows.values()
        if c.provenance is not None and c.provenance.logical_id == logical_id
    ]
    return sorted(rows, key=lambda c: c.provenance.version if c.provenance else 0)


def test_the_loser_appends_on_top_of_what_won() -> None:
    backend = _IndexedBackend()
    store, audit = _store(backend)
    store.write(_PERSONA, [_chunk("base", "v1")], source=WriteSource.USER)
    backend.before_next_upsert = lambda: store.write(
        _PERSONA, [_chunk("winner", "from the winner")], source=WriteSource.USER
    )

    store.write(_PERSONA, [_chunk("loser", "from the loser")], source=WriteSource.USER)

    chain = _chain(backend)
    assert [c.text for c in chain] == ["v1", "from the winner", "from the loser"]
    assert diagnose_chain(chain).defect is ChainDefect.NONE
    assert len(audit.events) == 3, "one event per write that landed, and no more"


def test_the_retry_reads_the_chain_again() -> None:
    backend = _IndexedBackend()
    store, _ = _store(backend)
    store.write(_PERSONA, [_chunk("base", "v1")], source=WriteSource.USER)
    backend.before_next_upsert = lambda: store.write(
        _PERSONA, [_chunk("winner", "w")], source=WriteSource.USER
    )
    backend.reads = 0

    store.write(_PERSONA, [_chunk("loser", "l")], source=WriteSource.USER)

    assert backend.reads == 3, "the loser's first read, the winner's read, the loser's re-read"


def test_a_loser_writing_what_already_won_appends_nothing() -> None:
    """Two refreshes of one block, or two grants of one tool. Distinct ids, identical content."""
    backend = _IndexedBackend()
    store, audit = _store(backend)
    store.write(_PERSONA, [_chunk("base", "v1")], source=WriteSource.USER)
    backend.before_next_upsert = lambda: store.write(
        _PERSONA, [_chunk("winner", "the same answer")], source=WriteSource.USER
    )

    store.write(_PERSONA, [_chunk("loser", "the same answer")], source=WriteSource.USER)

    assert [c.text for c in _chain(backend)] == ["v1", "the same answer"]
    assert len(audit.events) == 2, "the loser changed nothing, so it records nothing"


def test_a_batch_keeps_only_what_did_not_already_land() -> None:
    backend = _IndexedBackend()
    store, _ = _store(backend)
    store.write(
        _PERSONA,
        [_chunk("a0", "a v1", "A"), _chunk("b0", "b v1", "B")],
        source=WriteSource.USER,
    )
    backend.before_next_upsert = lambda: store.write(
        _PERSONA,
        [_chunk("w-a", "same", "A"), _chunk("w-b", "winner's b", "B")],
        source=WriteSource.USER,
    )

    store.write(
        _PERSONA,
        [_chunk("l-a", "same", "A"), _chunk("l-b", "loser's b", "B")],
        source=WriteSource.USER,
    )

    assert [c.text for c in _chain(backend, "A")] == ["a v1", "same"]
    assert [c.text for c in _chain(backend, "B")] == ["b v1", "winner's b", "loser's b"]


def test_a_first_write_race_retries_too() -> None:
    backend = _IndexedBackend()
    store, _ = _store(backend)
    backend.before_next_upsert = lambda: store.write(
        _PERSONA, [_chunk("winner", "from the winner")], source=WriteSource.USER
    )

    store.write(_PERSONA, [_chunk("loser", "from the loser")], source=WriteSource.USER)

    assert [c.text for c in _chain(backend)] == ["from the winner", "from the loser"]


def test_the_retry_is_bounded_and_then_says_so() -> None:
    backend = _IndexedBackend()
    backend.always_conflict = True
    store, audit = _store(backend)

    with pytest.raises(ChainConflictError):
        store.write(_PERSONA, [_chunk("x", "text")], source=WriteSource.USER)

    assert backend.upserts == 3
    assert audit.events == [], "nothing landed, so nothing is recorded"


def test_only_a_chain_conflict_is_retried() -> None:
    class _Broken(_IndexedBackend):
        def upsert(self, **_: Any) -> None:  # noqa: ANN401 - the transport's keywords
            self.upserts += 1
            msg = "disk full"
            raise OSError(msg)

    backend = _Broken()
    store, _ = _store(backend)
    with pytest.raises(OSError, match="disk full"):
        store.write(_PERSONA, [_chunk("x", "text")], source=WriteSource.USER)
    assert backend.upserts == 1


def test_a_caller_can_take_the_conflict_itself() -> None:
    backend = _IndexedBackend()
    backend.always_conflict = True
    store, _ = _store(backend)
    with pytest.raises(ChainConflictError):
        store.write(
            _PERSONA, [_chunk("x", "text")], source=WriteSource.USER, retry_on_conflict=False
        )
    assert backend.upserts == 1


def test_a_racing_rollback_lands_on_top_and_stays_a_rollback() -> None:
    backend = _IndexedBackend()
    store, audit = _store(backend)
    store.write(_PERSONA, [_chunk("v1", "first")], source=WriteSource.USER)
    store.write(_PERSONA, [_chunk("v2", "second")], source=WriteSource.USER)
    backend.before_next_upsert = lambda: store.write(
        _PERSONA, [_chunk("v3", "third")], source=WriteSource.USER
    )

    store.rollback(_PERSONA, _LOGICAL, 1, source=WriteSource.USER)

    chain = _chain(backend)
    assert [c.text for c in chain] == ["first", "second", "third", "first"]
    assert chain[-1].provenance is not None
    assert chain[-1].provenance.version == 4
    assert audit.events[-1].action is AuditAction.ROLLBACK
    assert audit.events[-1].chunk_ids == [chain[-1].id]


def test_a_rollback_that_lost_to_the_same_text_records_nothing() -> None:
    backend = _IndexedBackend()
    store, audit = _store(backend)
    store.write(_PERSONA, [_chunk("v1", "first")], source=WriteSource.USER)
    store.write(_PERSONA, [_chunk("v2", "second")], source=WriteSource.USER)
    backend.before_next_upsert = lambda: store.write(
        _PERSONA, [_chunk("v3", "first")], source=WriteSource.USER
    )

    store.rollback(_PERSONA, _LOGICAL, 1, source=WriteSource.USER)

    assert len(_chain(backend)) == 3
    assert AuditAction.ROLLBACK not in {e.action for e in audit.events}


def test_two_core_refreshes_both_land_in_order() -> None:
    """The kind that really carries chains. System writes, uuid ids, different text."""
    backend = _IndexedBackend()
    store, _ = _store(backend, CoreMemoryStore)
    store.write(_PERSONA, [_chunk("c0", "block")], source=WriteSource.SYSTEM)
    backend.before_next_upsert = lambda: store.write(
        _PERSONA, [_chunk("c1", "refresh one")], source=WriteSource.SYSTEM
    )

    store.write(_PERSONA, [_chunk("c2", "refresh two")], source=WriteSource.SYSTEM)

    assert [c.text for c in _chain(backend)] == ["block", "refresh one", "refresh two"]
    assert store.current(_PERSONA).text == "refresh two"


def _autonomy(store: Any, level: str, *, session: str) -> None:  # noqa: ANN401 - a typed store
    record_autonomy_update(
        store,
        _PERSONA,
        level,  # type: ignore[arg-type]
        now=_NOW,
        written_by="frontier:test",
        reason=f"moved to {level}",
        session_id=session,
    )


def _seed_autonomy(backend: _IndexedBackend) -> None:
    """Dated back, because the store stamps the wall clock and a real write would be in cooldown."""
    earlier = _NOW - timedelta(days=2)
    seeded = PersonaChunk(
        id="seed",
        text="Autonomy preference set to cautious.",
        metadata={"autonomy_level": "cautious", "confidence": "1.0"},
        created_at=earlier,
        provenance=ChunkProvenance(
            source=WriteSource.PERSONA_SELF, logical_id=AUTONOMY_LOGICAL_ID, written_at=earlier
        ),
    )
    backend.rows[seeded.id] = (_PERSONA, "self_facts", seeded)


class _FrozenClock(datetime):
    """The store stamps ``written_at`` from the wall clock, and the cooldown compares dates."""

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> datetime:
        _ = tz
        return _NOW


def test_a_retry_does_not_walk_past_the_autonomy_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One update a day (D-21-4). A store level retry would have landed the second one."""
    monkeypatch.setattr("persona.stores.base.datetime", _FrozenClock)
    backend = _IndexedBackend()
    store, _ = _store(backend)
    _seed_autonomy(backend)
    backend.before_next_upsert = lambda: _autonomy(store, "decisive", session="racer")

    with pytest.raises(AutonomyCooldownError):
        _autonomy(store, "balanced", session="s1")

    assert [c.text for c in _chain(backend, AUTONOMY_LOGICAL_ID)] == [
        "Autonomy preference set to cautious.",
        "Autonomy preference set to decisive.",
    ]


def test_a_second_autonomy_conflict_reaches_the_caller() -> None:
    backend = _IndexedBackend()
    backend.always_conflict = True
    store, _ = _store(backend)
    _seed_autonomy(backend)
    with pytest.raises(ChainConflictError):
        _autonomy(store, "balanced", session="s1")
    assert backend.upserts == 2


def _registry(backend: _IndexedBackend) -> tuple[PersonaRegistry, SelfFactsStore]:
    audit = MemoryAuditLogger()
    stores = {
        "identity": IdentityStore(backend=backend, audit_logger=audit),  # type: ignore[arg-type]
        "self_facts": SelfFactsStore(backend=backend, audit_logger=audit),  # type: ignore[arg-type]
        "worldview": WorldviewStore(backend=backend, audit_logger=audit),  # type: ignore[arg-type]
        "episodic": EpisodicStore(backend=backend, audit_logger=audit),  # type: ignore[arg-type]
    }
    return PersonaRegistry(stores=stores, audit_logger=audit), stores["self_facts"]  # type: ignore[arg-type,return-value]


def _save(persona: Persona, fact: str) -> Persona:
    edited = persona.self_facts[0].model_copy(update={"fact": fact})
    return persona.model_copy(update={"self_facts": [edited, *persona.self_facts[1:]]})


def test_two_different_persona_saves_both_land_and_the_persona_stays_saveable() -> None:
    backend = _IndexedBackend()
    registry, store = _registry(backend)
    base = Persona.from_yaml(_YAML).model_copy(update={"persona_id": _PERSONA})
    registry.load_persona(base)
    logical = make_chunk_id(_PERSONA, "self_facts", 0)

    backend.before_next_upsert = lambda: registry.load_persona(_save(base, "One save"))
    registry.load_persona(_save(base, "Another save"))

    chain = store.history(_PERSONA, logical)
    assert [c.text for c in chain[-2:]] == ["self_fact: One save", "self_fact: Another save"]
    registry.load_persona(_save(base, "A later save"))  # must not raise


class _Conn:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def __enter__(self) -> _Conn:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, *_: object) -> None:
        raise self._error


class _Engine:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def begin(self) -> _Conn:
        return _Conn(self._error)


class _Embedder:
    model_name = "stub"
    dimension = EMBEDDING_DIM

    def encode(self, texts: Any) -> list[list[float]]:  # noqa: ANN401 - Embedder protocol
        return [[0.0] * EMBEDDING_DIM for _ in texts]


class _Diag:
    def __init__(self, constraint_name: str | None) -> None:
        self.constraint_name = constraint_name


class _DriverError(Exception):
    def __init__(self, constraint_name: str | None) -> None:
        super().__init__("Key (persona_id, kind, logical_id)=(a private value) already exists")
        self.diag = _Diag(constraint_name)


def _violation(constraint_name: str | None) -> IntegrityError:
    return IntegrityError("INSERT", {"text": "the memory text"}, _DriverError(constraint_name))


def _upsert_against(error: Exception) -> None:
    backend = PostgresBackend(engine=_Engine(error), embedder=_Embedder())  # type: ignore[arg-type]
    backend.upsert(persona_id=_PERSONA, store_kind="self_facts", chunks=[_chunk("x", "text")])


def test_postgres_turns_the_head_index_into_the_domain_error() -> None:
    with pytest.raises(ChainConflictError) as caught:
        _upsert_against(_violation(CHAIN_HEAD_INDEX))
    assert isinstance(caught.value, PersonaError)
    assert "private" not in str(caught.value), "the driver's message carries row values"
    assert caught.value.context["constraint"] == CHAIN_HEAD_INDEX


def test_a_logged_conflict_carries_no_memory_text() -> None:
    """Round 2 review, LOW-4. The driver's error quotes the statement's parameters, which are
    the rows being written. A caller that logs the conflict with its traceback, as the core
    refresh does, would print a person's memory into the log. So the cause is not chained."""
    import traceback

    from loguru import logger

    with pytest.raises(ChainConflictError) as caught:
        _upsert_against(_violation(CHAIN_HEAD_INDEX))
    rendered = "".join(traceback.format_exception(caught.value))
    assert "private" not in rendered
    assert "the memory text" not in rendered

    lines: list[str] = []
    sink = logger.add(lines.append, format="{message}", backtrace=True, diagnose=False)
    try:
        logger.opt(exception=caught.value).warning("core-block refresh failed")
    finally:
        logger.remove(sink)
    assert lines
    assert all("private" not in line and "the memory text" not in line for line in lines)


@pytest.mark.parametrize("constraint", ["memory_chunks_kind_check", "memory_chunks_pkey", None])
def test_postgres_leaves_every_other_violation_alone(constraint: str | None) -> None:
    with pytest.raises(IntegrityError):
        _upsert_against(_violation(constraint))

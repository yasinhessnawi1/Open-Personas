"""A forget racing a write, on real Postgres transactions (R9-271).

The unit suite drives the same interleavings over a model of the transport. These prove the model
is honest about the locks. Nothing here waits for time to pass: a writer is held inside its own
transaction by a gate, a forget is held inside an explicit transaction on a raw connection, and
"the other side is now waiting on a lock" is read from ``pg_stat_activity`` rather than assumed,
for the waiting side's own backends only, and with the statement it is waiting in.
Every connection the code under test uses carries a ``lock_timeout`` and a
``statement_timeout``, and every wait on a thread is bounded, so a regression fails instead of
hanging the job.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from persona.audit import MemoryAuditLogger
from persona.errors import ChainConflictError, ForgottenEvidenceError
from persona.recall.core_memory import CoreBlock, core_block_to_chunk
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource
from persona.stores import postgres as pg
from persona.stores.episodic import EpisodicStore
from persona.stores.postgres import PostgresBackend
from persona.stores.self_facts import SelfFactsStore
from sqlalchemy import create_engine, event, text

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from sqlalchemy.engine import Engine
    from tests.conftest import HashEmbedder384

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)
_PERSONA = "p1"
_FACT = "fact-chain"
_WAIT_S = 20.0
#: Short enough that a lock nobody releases fails the test well inside the job's own timeout.
_GUARDED = "-c lock_timeout=10s -c statement_timeout=20s"


@pytest.fixture
def engine(pg_engine: Engine, database_url: str) -> Iterator[Engine]:
    """The schema ``pg_engine`` built, through connections that cannot wait forever."""
    with pg_engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES ('u1', 'u1@example.com')"))
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES ('p1', 'u1', 'name: p1')")
        )
    guarded = create_engine(database_url, connect_args={"options": _GUARDED})
    yield guarded
    guarded.dispose()


def _chunk(chunk_id: str, body: str, logical_id: str | None = None) -> PersonaChunk:
    return PersonaChunk(
        id=chunk_id,
        text=body,
        created_at=_NOW,
        provenance=ChunkProvenance(
            source=WriteSource.SYSTEM, logical_id=logical_id or chunk_id, written_at=_NOW
        ),
    )


def _texts(engine: Engine, kind: str) -> list[str]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                text("SELECT text FROM memory_chunks WHERE persona_id = :p AND kind = :k"),
                {"p": _PERSONA, "k": kind},
            ).scalars()
        )


class _Waiter:
    """An engine for the side expected to wait, recording the backend pid of every connection."""

    def __init__(self, database_url: str) -> None:
        self.engine = create_engine(database_url, connect_args={"options": _GUARDED})
        self._pids: set[int] = set()
        # Checkouts happen on the waiting side's own threads while the main thread reads.
        self._pids_lock = threading.Lock()
        event.listen(self.engine, "checkout", self._record)

    def _record(self, dbapi_connection: Any, *_: object) -> None:  # noqa: ANN401 - the driver's
        with self._pids_lock:
            self._pids.add(int(dbapi_connection.info.backend_pid))

    def pids(self) -> list[int]:
        """A snapshot of every backend pid this side has used so far."""
        with self._pids_lock:
            return sorted(self._pids)

    def backend(self, embedder: HashEmbedder384) -> PostgresBackend:
        return PostgresBackend(engine=self.engine, embedder=embedder)  # type: ignore[arg-type]


@pytest.fixture
def waiter(engine: Engine, database_url: str) -> Iterator[_Waiter]:
    _ = engine  # the schema and the seed rows come first
    made = _Waiter(database_url)
    yield made
    made.engine.dispose()


def _waiting_on_a_lock(observer: Engine, waiter: _Waiter) -> str:
    """Return a statement once one of ``waiter``'s own sessions is waiting on a lock.

    More than one of them may be waiting; any one will do, the lowest pid first.
    """
    deadline = time.monotonic() + _WAIT_S
    while time.monotonic() < deadline:
        with observer.connect() as conn:
            waiting = (
                conn.execute(
                    text(
                        "SELECT query FROM pg_stat_activity "
                        "WHERE pid = ANY(:pids) AND wait_event_type = 'Lock' ORDER BY pid"
                    ),
                    {"pids": waiter.pids()},
                )
                .scalars()
                .first()
            )
        if waiting is not None:
            return str(waiting)
        time.sleep(0.02)
    pytest.fail("the other session never started waiting on the lock")


class _Thread:
    def __init__(self, target: Callable[[], object]) -> None:
        self.errors: list[BaseException] = []
        self.done = threading.Event()

        def run() -> None:
            try:
                target()
            except BaseException as exc:  # noqa: BLE001 - reported to the main thread
                self.errors.append(exc)
            finally:
                self.done.set()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def finish(self) -> list[BaseException]:
        assert self.done.wait(_WAIT_S), "a thread never finished"
        self._thread.join(_WAIT_S)
        return self.errors


class _HeldAfterTheEvidenceLock:
    """Holds a guarded write inside its transaction, with its evidence locked, until released."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.inside = threading.Event()
        self.release = threading.Event()
        real = pg._lock_evidence

        def held(*args: Any, **kwargs: Any) -> None:  # noqa: ANN401 - forwards the helper
            real(*args, **kwargs)
            self.inside.set()
            assert self.release.wait(_WAIT_S), "the writer was never released"

        monkeypatch.setattr(pg, "_lock_evidence", held)


def test_a_forget_between_the_read_and_the_write_does_not_bring_the_old_version_back(
    engine: Engine, embedder: HashEmbedder384, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = PostgresBackend(engine=engine, embedder=embedder)  # type: ignore[arg-type]
    store = SelfFactsStore(backend=backend, audit_logger=MemoryAuditLogger())
    store.write(_PERSONA, [_chunk("f1", "the thing to forget", _FACT)], force=True)
    read = backend.get_by_logical_ids
    armed = {"on": True}

    def read_then_forget(**kwargs: Any) -> list[PersonaChunk]:  # noqa: ANN401 - forwards
        chain = read(**kwargs)
        if armed["on"]:
            armed["on"] = False
            store.remove_documents(_PERSONA, ["f1"])
        return chain

    monkeypatch.setattr(backend, "get_by_logical_ids", read_then_forget)

    store.write(_PERSONA, [_chunk("f1", "a later, unrelated update", _FACT)], force=True)

    assert _texts(engine, "self_facts") == ["a later, unrelated update"]


def test_a_write_over_a_version_a_forget_renumbered_is_refused(
    engine: Engine, embedder: HashEmbedder384
) -> None:
    backend = PostgresBackend(engine=engine, embedder=embedder)  # type: ignore[arg-type]
    store = SelfFactsStore(backend=backend, audit_logger=MemoryAuditLogger())
    store.write(_PERSONA, [_chunk("f1", "first", _FACT)], force=True)
    with engine.begin() as conn:
        conn.execute(text("UPDATE memory_chunks SET version = 7 WHERE id = 'f1'"))

    with pytest.raises(ChainConflictError):
        backend.upsert(
            persona_id=_PERSONA,
            store_kind="self_facts",
            chunks=[_chunk("f2", "second", _FACT)],
            supersedes={"f1": (1, "f2")},
        )
    assert _texts(engine, "self_facts") == ["first"]


def test_a_write_citing_evidence_a_forget_is_deleting_waits_and_is_refused(
    engine: Engine, waiter: _Waiter, embedder: HashEmbedder384
) -> None:
    backend = waiter.backend(embedder)
    backend.upsert(
        persona_id=_PERSONA, store_kind="episodic", chunks=[_chunk("a", "a"), _chunk("x", "x")]
    )

    with engine.connect() as forget:
        transaction = forget.begin()
        forget.execute(text("DELETE FROM memory_chunks WHERE id = 'x'"))
        writer = _Thread(
            lambda: backend.upsert(
                persona_id=_PERSONA,
                store_kind="core_memory",
                chunks=[_chunk("block", "a summary of a and x")],
                requires=["a", "x"],
                requires_kinds=["episodic"],
            )
        )
        _waiting_on_a_lock(engine, waiter)
        transaction.commit()
        errors = writer.finish()

    assert [type(e) for e in errors] == [ForgottenEvidenceError]
    assert _texts(engine, "core_memory") == []


def test_a_forget_waits_for_a_write_holding_its_evidence_and_then_removes_what_it_wrote(
    engine: Engine, waiter: _Waiter, embedder: HashEmbedder384, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = PostgresBackend(engine=engine, embedder=embedder)  # type: ignore[arg-type]
    backend.upsert(
        persona_id=_PERSONA, store_kind="episodic", chunks=[_chunk("a", "a"), _chunk("x", "x")]
    )
    gate = _HeldAfterTheEvidenceLock(monkeypatch)
    block = core_block_to_chunk(
        CoreBlock(text="a summary of a and x", source_ids=("a", "x")), _PERSONA, now=_NOW
    )
    writer = _Thread(
        lambda: backend.upsert(
            persona_id=_PERSONA,
            store_kind="core_memory",
            chunks=[block],
            requires=["a", "x"],
            requires_kinds=["episodic"],
        )
    )
    assert gate.inside.wait(_WAIT_S), "the writer never locked its evidence"

    forget = _Thread(
        lambda: EpisodicStore(
            backend=waiter.backend(embedder), audit_logger=MemoryAuditLogger()
        ).remove_documents(_PERSONA, ["x"])
    )
    _waiting_on_a_lock(engine, waiter)
    gate.release.set()

    assert writer.finish() == []
    assert forget.finish() == []
    assert _texts(engine, "episodic") == ["a"]
    assert _texts(engine, "core_memory") == [], "the block citing forgotten x outlived the forget"


def test_reinforcing_evidence_is_not_held_up_by_a_write_holding_it(
    engine: Engine, embedder: HashEmbedder384, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``FOR KEY SHARE`` keeps a DELETE out and lets an ordinary UPDATE through."""
    backend = PostgresBackend(engine=engine, embedder=embedder)  # type: ignore[arg-type]
    backend.upsert(persona_id=_PERSONA, store_kind="episodic", chunks=[_chunk("a", "a")])
    gate = _HeldAfterTheEvidenceLock(monkeypatch)
    writer = _Thread(
        lambda: backend.upsert(
            persona_id=_PERSONA,
            store_kind="core_memory",
            chunks=[_chunk("block", "a summary of a")],
            requires=["a"],
            requires_kinds=["episodic"],
        )
    )
    assert gate.inside.wait(_WAIT_S)
    try:
        # Would wait on the writer's lock, and then fail on lock_timeout, if the guard took more.
        backend.reinforce(persona_id=_PERSONA, store_kind="episodic", ids=["a"], recalled_at=_NOW)
    finally:
        gate.release.set()
    assert writer.finish() == []


def test_a_deadlock_comes_back_as_a_conflict_the_store_can_retry(
    engine: Engine, waiter: _Waiter, embedder: HashEmbedder384
) -> None:
    """Postgres breaks a lock cycle by failing one side. That side gets the retryable error.

    The cycle is built by hand with a session that locks out of order, which no code path here
    does. The other session's deadlock check is set late, so the guarded write is the one Postgres
    picks, every time.
    """
    backend = PostgresBackend(engine=engine, embedder=embedder)  # type: ignore[arg-type]
    backend.upsert(
        persona_id=_PERSONA, store_kind="episodic", chunks=[_chunk("a", "a"), _chunk("b", "b")]
    )

    with engine.connect() as other:
        transaction = other.begin()
        other.execute(text("SET LOCAL deadlock_timeout = '15s'"))
        other.execute(text("SELECT id FROM memory_chunks WHERE id = 'b' FOR UPDATE"))
        writer = _Thread(
            lambda: waiter.backend(embedder).upsert(
                persona_id=_PERSONA,
                store_kind="core_memory",
                chunks=[_chunk("block", "a summary of a and b")],
                requires=["a", "b"],
                requires_kinds=["episodic"],
            )
        )
        _waiting_on_a_lock(engine, waiter)  # the writer holds a and waits for b
        other.execute(text("DELETE FROM memory_chunks WHERE id = 'a'"))  # waits on the writer
        errors = writer.finish()
        transaction.rollback()

    assert [type(e) for e in errors] == [ChainConflictError]
    assert _texts(engine, "core_memory") == []


def test_a_write_waits_on_the_lock_of_a_version_being_renumbered_and_is_refused(
    engine: Engine, waiter: _Waiter, embedder: HashEmbedder384
) -> None:
    """The writer must wait in its own lock on the superseded version, not later in the UPDATE.

    A forget closing up a chain holds the head and renumbers it. The writer read the old number.
    Waiting at the lock means it compares what the forget committed and is refused there; the
    version guard on the UPDATE is the second line behind that, not the first (PR #39 review).
    """
    store = SelfFactsStore(backend=waiter.backend(embedder), audit_logger=MemoryAuditLogger())
    store.write(_PERSONA, [_chunk("f1", "first", _FACT)], force=True)

    with engine.connect() as forget:
        transaction = forget.begin()
        forget.execute(text("SELECT id FROM memory_chunks WHERE id = 'f1' FOR UPDATE"))
        forget.execute(text("UPDATE memory_chunks SET version = 2 WHERE id = 'f1'"))
        writer = _Thread(
            lambda: waiter.backend(embedder).upsert(
                persona_id=_PERSONA,
                store_kind="self_facts",
                chunks=[_chunk("f2", "second", _FACT)],
                supersedes={"f1": (1, "f2")},
            )
        )
        statement = _waiting_on_a_lock(engine, waiter)
        transaction.commit()
        errors = writer.finish()

    assert statement.lstrip().upper().startswith("SELECT"), statement[:60]
    assert "FOR UPDATE" in statement.upper()
    assert [type(e) for e in errors] == [ChainConflictError]
    assert _texts(engine, "self_facts") == ["first"]

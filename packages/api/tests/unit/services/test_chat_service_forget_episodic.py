"""A failed episodic forget can be retried or finishes later; it never strands the memory (R9-311).

Deleting a conversation with ``forget_memory`` used to delete the row first and forget its
episodic memory after the commit. When that forget failed, the conversation was already gone,
so the user could not ask again, and the chunks stayed for good.

Now there are two shapes, and neither leaves the memory behind:

- **With a durable job** (a worker in this process runs ``conversation_memory_forget``): the
  forget is recorded inside the row delete's transaction (its chunk ids, the forgotten
  marker, the job), so the two commit together. A failure of the forget right after the commit
  is left to the job, which retries until it is done.
- **Without one**: the forget runs before the row delete. A failure leaves the conversation in
  place, and asking again finishes it.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest
from persona_api.services import chat_service
from persona_api.services.conversation_memory_forget import MemoryForgetPlan

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

_CONV = "c1"
_PERSONA = "p1"


class _Result:
    def __init__(self, row: object) -> None:
        self._row = row

    def first(self) -> object:
        return self._row


class _Conn:
    def __init__(self, engine: _Engine) -> None:
        self._engine = engine

    def execute(self, stmt: Any, params: object = None) -> _Result:  # noqa: ANN401, ARG002
        self._engine.log.append(("execute", str(stmt).split()[0], id(self)))
        return _Result((_CONV,))


class _Engine:
    def __init__(self) -> None:
        self.log: list[tuple[str, Any, int]] = []
        self.committed = 0
        self.rolled_back = 0

    @contextmanager
    def begin(self) -> Iterator[_Conn]:
        conn = _Conn(self)
        try:
            yield conn
        except BaseException:
            self.rolled_back += 1
            raise
        self.committed += 1

    def statements(self) -> list[str]:
        return [entry[1] for entry in self.log if entry[0] == "execute"]


@dataclass
class _Chunk:
    id: str
    metadata: dict[str, str]


@dataclass
class _Store:
    """The two calls the forget makes on an :class:`EpisodicStore`."""

    chunks: list[_Chunk]
    fail: bool = False
    removed: list[list[str]] = field(default_factory=list)

    def get_all(self, persona_id: str) -> list[_Chunk]:
        assert persona_id == _PERSONA
        return list(self.chunks)

    def remove_documents(self, persona_id: str, doc_ids: list[str]) -> None:
        assert persona_id == _PERSONA
        if self.fail:
            msg = "the memory store is unavailable"
            raise RuntimeError(msg)
        self.removed.append(sorted(doc_ids))
        self.chunks = [c for c in self.chunks if c.id not in doc_ids]


def _stamped(chunk_id: str, conversation_id: str = _CONV) -> _Chunk:
    return _Chunk(id=chunk_id, metadata={"conversation_id": conversation_id})


@pytest.fixture(autouse=True)
def _no_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat_service, "_conversation_message_texts", lambda *_a: [])


def _delete(engine: _Engine, store: _Store, plan: MemoryForgetPlan | None = None) -> None:
    extra: dict[str, Any] = {} if plan is None else {"memory_plan": plan}
    chat_service.delete_conversation(
        rls_engine=engine,  # type: ignore[arg-type]
        conversation_id=_CONV,
        forget_memory=True,
        persona_id=_PERSONA,
        episodic_store=store,  # type: ignore[arg-type]
        **extra,
    )


def _recording_plan(
    engine: _Engine, *, durable: bool, late: Sequence[str] = ()
) -> MemoryForgetPlan:
    def _record(conn: _Conn, ids: Sequence[str]) -> list[str]:
        engine.log.append(("record", sorted(ids), id(conn)))
        return list(late)

    return MemoryForgetPlan(durable=durable, record=_record)  # type: ignore[arg-type]


def test_a_failed_episodic_forget_leaves_the_conversation_for_a_retry() -> None:
    engine = _Engine()
    store = _Store(chunks=[_stamped("s1"), _stamped("other", "c2")], fail=True)

    with pytest.raises(RuntimeError):
        _delete(engine, store)

    assert "DELETE" not in engine.statements(), "the conversation must stay so the user can retry"
    assert {c.id for c in store.chunks} == {"s1", "other"}

    store.fail = False
    _delete(engine, store)

    assert "DELETE" in engine.statements()
    assert {c.id for c in store.chunks} == {"other"}, "the retry forgets this conversation only"


def test_a_durable_forget_is_recorded_in_the_row_delete_transaction_before_the_delete() -> None:
    engine = _Engine()
    store = _Store(chunks=[_stamped("s1"), _stamped("s2")])

    _delete(engine, store, _recording_plan(engine, durable=True))

    kinds = [entry[:2] for entry in engine.log]
    assert ("record", ["s1", "s2"]) in kinds
    record_at = kinds.index(("record", ["s1", "s2"]))
    assert kinds[record_at + 1 :] == [("execute", "DELETE")], "recorded, then the row goes"
    assert len({entry[2] for entry in engine.log}) == 1, "one connection: one transaction"
    assert engine.committed == 1
    assert store.chunks == [], "the forget still runs at once when it can"


def test_a_durable_forget_that_fails_after_the_commit_is_left_to_the_job() -> None:
    engine = _Engine()
    store = _Store(chunks=[_stamped("s1")], fail=True)

    _delete(engine, store, _recording_plan(engine, durable=True))

    assert engine.committed == 1, "the delete answers success: the job holds the forget"
    assert ("record", ["s1"]) in [entry[:2] for entry in engine.log]
    assert [c.id for c in store.chunks] == ["s1"], "still there, for the job to remove"


def test_a_durable_forget_removes_what_was_written_before_the_row_lock() -> None:
    engine = _Engine()
    store = _Store(chunks=[_stamped("s1")])

    _delete(engine, store, _recording_plan(engine, durable=True, late=["late1"]))

    assert store.removed == [["late1", "s1"]]


def test_without_a_job_the_forget_runs_before_the_row_and_then_takes_late_writes() -> None:
    engine = _Engine()
    store = _Store(chunks=[_stamped("s1")])

    _delete(engine, store, _recording_plan(engine, durable=False, late=["s1", "late1"]))

    assert store.removed == [["s1"], ["late1"]], "before the row, then what landed in between"
    assert engine.committed == 1


def test_a_plain_delete_never_records_or_forgets() -> None:
    engine = _Engine()
    store = _Store(chunks=[_stamped("s1")])
    plan = _recording_plan(engine, durable=True)

    chat_service.delete_conversation(
        rls_engine=engine,  # type: ignore[arg-type]
        conversation_id=_CONV,
        forget_memory=False,
        persona_id=_PERSONA,
        episodic_store=store,  # type: ignore[arg-type]
        memory_plan=plan,
    )

    assert engine.statements() == ["DELETE"]
    assert [entry[0] for entry in engine.log] == ["execute"]
    assert [c.id for c in store.chunks] == ["s1"]

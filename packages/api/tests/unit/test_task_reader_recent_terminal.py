"""Unit test — ``APITaskStateReader.list_recent_terminal`` (Spec A5, A5-D-X-reads).

The additive task-history lens: terminal-only, newest-first, bounded — and the
untouched-consumer half: ``list_active`` behaviour is byte-identical (the same
``list_for_owner`` read, complementary filter).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from persona.tasks import Contract, CostLedger, Task, TaskState
from persona_api.tasks.reader import APITaskStateReader

_NOW = datetime(2026, 7, 4, 9, 0, tzinfo=UTC)


def _task(task_id: str, state: TaskState, *, age_days: int) -> Task:
    stamp = _NOW - timedelta(days=age_days)
    return Task(
        id=task_id,
        owner_id="user-a",
        persona_id="astrid",
        contract=Contract(goal=f"goal {task_id}"),
        state=state,
        ledger=CostLedger(),
        created_at=stamp,
        updated_at=stamp,
    )


class _FakeTaskStore:
    def __init__(self, tasks: list[Task]) -> None:
        self._tasks = tasks
        self.calls: list[str] = []

    def list_for_owner(self, owner_id: str) -> list[Task]:
        self.calls.append(owner_id)
        return list(self._tasks)


_MIXED = [
    _task("t-active", TaskState.ACTIVE, age_days=0),
    _task("t-done-old", TaskState.COMPLETED, age_days=9),
    _task("t-done-new", TaskState.COMPLETED, age_days=1),
    _task("t-failed", TaskState.FAILED, age_days=3),
    _task("t-cancelled", TaskState.CANCELLED, age_days=5),
]


def _reader(store: _FakeTaskStore) -> APITaskStateReader:
    return APITaskStateReader(store, None, "user-a")  # type: ignore[arg-type] — checkpoints unused


def test_terminal_only_newest_first_bounded() -> None:
    store = _FakeTaskStore(_MIXED)
    out = _reader(store).list_recent_terminal(limit=3)
    assert [t.id for t in out] == ["t-done-new", "t-failed", "t-cancelled"]
    assert store.calls == ["user-a"]  # owner-bound, one read


def test_limit_zero_is_empty() -> None:
    assert _reader(_FakeTaskStore(_MIXED)).list_recent_terminal(limit=0) == []


def test_list_active_untouched_by_the_addition() -> None:
    """The untouched-consumer proof at the unit level: active filtering unchanged."""
    out = _reader(_FakeTaskStore(_MIXED)).list_active()
    assert [t.id for t in out] == ["t-active"]

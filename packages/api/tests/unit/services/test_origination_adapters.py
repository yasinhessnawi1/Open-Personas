"""Unit tests for the A4 origination store adapters (Spec A4, composition-root wiring).

The adapters give :class:`OriginationService` its idempotent ``create_if_absent`` + ``get_optional``
over the real store shapes. These pin the two behaviours the service's invariants rest on: a miss
reads as ``None`` (not an error), and a unique-violation on create is swallowed (a racing replay is
a no-op, so origination converges on one row). The :class:`OriginatorFailureNotifier`'s durable
persistence is proven end-to-end on the real stack in the live composition test.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from persona.errors import ScheduleNotFoundError, TaskNotFoundError
from persona.tasks import Contract, Task
from persona_api.approvals.failure import account_for_origination_failure
from persona_api.services.origination_adapters import (
    ScheduleCreatorAdapter,
    TaskCreatorAdapter,
    render_failure_account,
)
from sqlalchemy.exc import IntegrityError

_NOW = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)


def _task(task_id: str = "t1", owner_id: str = "user-a") -> Task:
    return Task(
        id=task_id,
        owner_id=owner_id,
        persona_id="astrid",
        contract=Contract(goal="g"),
        created_at=_NOW,
        updated_at=_NOW,
    )


def _integrity_error() -> IntegrityError:
    return IntegrityError("INSERT ...", {}, Exception("duplicate key"))


class _FakeTaskStore:
    def __init__(self, *, existing: Task | None = None, raise_on_create: bool = False) -> None:
        self._existing = existing
        self._raise = raise_on_create
        self.created: list[Task] = []

    def get(self, owner_id: str, task_id: str) -> Task:  # noqa: ARG002
        if self._existing is None:
            raise TaskNotFoundError("nope", context={"task_id": task_id})
        return self._existing

    def create(self, task: Task) -> Task:
        if self._raise:
            raise _integrity_error()
        self.created.append(task)
        return task


class _FakeScheduleStore:
    def __init__(self, *, raise_on_create: bool = False, raise_on_delete: bool = False) -> None:
        self._raise_create = raise_on_create
        self._raise_delete = raise_on_delete
        self.deleted: list[str] = []

    def create(self, schedule: object, *, now: datetime) -> object:  # noqa: ARG002
        if self._raise_create:
            raise _integrity_error()
        return schedule

    def delete(self, owner_id: str, schedule_id: str) -> None:  # noqa: ARG002
        if self._raise_delete:
            raise ScheduleNotFoundError("gone", context={"schedule_id": schedule_id})
        self.deleted.append(schedule_id)


def test_get_optional_returns_none_on_miss() -> None:
    adapter = TaskCreatorAdapter(_FakeTaskStore())  # type: ignore[arg-type]
    assert adapter.get_optional("user-a", "t1") is None


def test_get_optional_returns_the_task_when_present() -> None:
    task = _task()
    adapter = TaskCreatorAdapter(_FakeTaskStore(existing=task))  # type: ignore[arg-type]
    assert adapter.get_optional("user-a", "t1") is task


def test_create_if_absent_persists_when_new() -> None:
    store = _FakeTaskStore()
    TaskCreatorAdapter(store).create_if_absent(_task())  # type: ignore[arg-type]
    assert len(store.created) == 1


def test_create_if_absent_swallows_unique_violation() -> None:
    # A racing replay hits the PK — the adapter must treat it as an idempotent no-op, not raise.
    store = _FakeTaskStore(raise_on_create=True)
    TaskCreatorAdapter(store).create_if_absent(_task())  # type: ignore[arg-type]  # must not raise


class _StubSchedule:
    id = "sched-1"


def test_schedule_create_if_absent_swallows_unique_violation() -> None:
    store = _FakeScheduleStore(raise_on_create=True)
    ScheduleCreatorAdapter(store).create_if_absent(_StubSchedule(), now=_NOW)  # type: ignore[arg-type]


def test_schedule_delete_swallows_missing() -> None:
    store = _FakeScheduleStore(raise_on_delete=True)
    ScheduleCreatorAdapter(store).delete("user-a", "sched-x")  # type: ignore[arg-type]  # no raise


def test_schedule_delete_removes_present() -> None:
    store = _FakeScheduleStore()
    ScheduleCreatorAdapter(store).delete("user-a", "sched-x")  # type: ignore[arg-type]
    assert store.deleted == ["sched-x"]


def test_render_failure_account_carries_cause_and_options() -> None:
    account = account_for_origination_failure("task-1", cause="DB was down")
    text = render_failure_account(account)
    assert "DB was down" in text  # the honest cause, never disguised
    for option in account.options:
        assert option in text  # every concrete next step surfaced (no dead end)


@pytest.mark.parametrize("cause", ["", "   "])
def test_render_handles_the_builder_fallback_cause(cause: str) -> None:
    # The builder substitutes a fallback for an empty cause; render must still be non-empty.
    account = account_for_origination_failure("task-1", cause=cause)
    assert render_failure_account(account).strip()

"""Unit tests for the task_introspect tool (Spec A4, T7) — fake reader, no DB."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona.errors import TaskNotFoundError
from persona.tasks import Contract, CostLedger, Task, TaskCheckpoint, TaskState, WaitKind
from persona.tools.builtin.task_introspection import make_task_introspection_tool

if TYPE_CHECKING:
    from persona.tools.protocol import AsyncTool

_NOW = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)


def _task(
    task_id: str,
    *,
    goal: str,
    state: TaskState,
    wait_kind: WaitKind | None = None,
    head_seq: int | None = None,
) -> Task:
    return Task(
        id=task_id,
        owner_id="user-a",
        persona_id="astrid",
        contract=Contract(goal=goal),
        state=state,
        wait_kind=wait_kind,
        head_checkpoint_seq=head_seq,
        ledger=CostLedger(),
        created_at=_NOW,
        updated_at=_NOW,
    )


class _FakeReader:
    def __init__(self, *, tasks: list[Task], checkpoint: TaskCheckpoint | None = None) -> None:
        self._tasks = {t.id: t for t in tasks}
        self._checkpoint = checkpoint

    def get_task(self, task_id: str) -> Task:
        try:
            return self._tasks[task_id]
        except KeyError as exc:
            raise TaskNotFoundError("not found", context={"task_id": task_id}) from exc

    def get_latest_checkpoint(self, task_id: str) -> TaskCheckpoint | None:  # noqa: ARG002
        return self._checkpoint

    def list_active(self) -> list[Task]:
        return list(self._tasks.values())


def _tool(reader: _FakeReader | None) -> AsyncTool:
    return make_task_introspection_tool(reader_provider=lambda: reader, persona_id="astrid")


@pytest.mark.asyncio
async def test_no_reader_fails_closed() -> None:
    result = await _tool(None).execute(task_id="t1")
    assert result.is_error
    assert "No task context" in result.content


@pytest.mark.asyncio
async def test_no_task_id_lists_active_tasks() -> None:
    reader = _FakeReader(
        tasks=[
            _task("t1", goal="watch the fares", state=TaskState.ACTIVE, head_seq=1),
            _task(
                "t2",
                goal="weekly spend digest",
                state=TaskState.WAITING,
                wait_kind=WaitKind.UNTIL_TIME,
            ),
        ]
    )
    result = await _tool(reader).execute()
    assert not result.is_error
    assert "t1" in result.content
    assert "watch the fares" in result.content
    assert result.data is not None
    assert {t["task_id"] for t in result.data["tasks"]} == {"t1", "t2"}


@pytest.mark.asyncio
async def test_empty_list_is_honest() -> None:
    result = await _tool(_FakeReader(tasks=[])).execute()
    assert "no active tasks" in result.content.lower()


@pytest.mark.asyncio
async def test_introspect_unknown_task_id_is_an_honest_error() -> None:
    reader = _FakeReader(tasks=[_task("t1", goal="g", state=TaskState.ACTIVE)])
    result = await _tool(reader).execute(task_id="nope")
    assert result.is_error
    assert "nope" in result.content


@pytest.mark.asyncio
async def test_introspect_just_created_says_nothing_yet() -> None:
    reader = _FakeReader(
        tasks=[_task("t1", goal="watch the fares", state=TaskState.DEFINED)], checkpoint=None
    )
    result = await _tool(reader).execute(task_id="t1")
    assert not result.is_error
    assert "just started" in result.content
    assert "hasn't happened" in result.content


@pytest.mark.asyncio
async def test_introspect_progressing_surfaces_conclusions_only() -> None:
    cp = TaskCheckpoint(
        task_id="t1",
        leg_id="leg-1",
        checkpoint_seq=1,
        progress_conclusions=("found 9 fares", "cheapest 1450kr"),
        next_step="re-check at 7am",
        updated_at=_NOW,
    )
    reader = _FakeReader(
        tasks=[_task("t1", goal="watch fares", state=TaskState.ACTIVE, head_seq=1)], checkpoint=cp
    )
    result = await _tool(reader).execute(task_id="t1")
    assert "found 9 fares" in result.content
    assert "re-check at 7am" in result.content
    # The structured payload is the typed view — no transcript key.
    assert result.data is not None
    assert "transcript" not in result.data
    assert "event_log_cursor" not in result.data


@pytest.mark.asyncio
async def test_introspect_waiting_on_user_is_unflattering_and_honest() -> None:
    cp = TaskCheckpoint(
        task_id="t1",
        leg_id="leg-1",
        checkpoint_seq=1,
        blocked_on="I'm stuck — the portal rejected the login you gave me",
        updated_at=_NOW,
    )
    reader = _FakeReader(
        tasks=[
            _task(
                "t1",
                goal="file the appeal",
                state=TaskState.WAITING,
                wait_kind=WaitKind.ON_USER,
                head_seq=1,
            ),
        ],
        checkpoint=cp,
    )
    result = await _tool(reader).execute(task_id="t1")
    assert "waiting on you" in result.content
    assert "the portal rejected the login" in result.content

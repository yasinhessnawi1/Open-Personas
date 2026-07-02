"""Unit tests for the A4 task-steering service (Spec A4, T9b + cancel-failure-visibility)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona.errors import TaskNotFoundError, TaskStateError
from persona.schema.origination import PersonaIdentityTag
from persona.tasks import Contract, Task, TaskState
from persona_api.approvals.failure import FailureKind
from persona_api.services.task_steering_service import TaskSteeringService

if TYPE_CHECKING:
    from persona_api.approvals.failure import FailureAccount

_NOW = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)


def _task(state: TaskState = TaskState.ACTIVE) -> Task:
    return Task(
        id="t1",
        owner_id="user-1",
        persona_id="astrid",
        contract=Contract(goal="g"),
        state=state,
        created_at=_NOW,
        updated_at=_NOW,
    )


class _FakeMutator:
    def __init__(
        self,
        *,
        cancel_raises: Exception | None = None,
        get_task: Task | None = None,
        get_raises: Exception | None = None,
    ) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self._cancel_raises = cancel_raises
        self._get_task = get_task
        self._get_raises = get_raises

    def get(self, owner_id: str, task_id: str) -> Task:  # noqa: ARG002
        if self._get_raises is not None:
            raise self._get_raises
        assert self._get_task is not None
        return self._get_task

    def pause(self, owner_id: str, task_id: str, *, now: datetime) -> Task:  # noqa: ARG002
        self.calls.append(("pause", owner_id, task_id))
        return _task()

    def unpause(self, owner_id: str, task_id: str, *, now: datetime) -> Task:  # noqa: ARG002
        self.calls.append(("unpause", owner_id, task_id))
        return _task()

    def cancel(self, owner_id: str, task_id: str, *, now: datetime) -> Task:  # noqa: ARG002
        self.calls.append(("cancel", owner_id, task_id))
        if self._cancel_raises is not None:
            raise self._cancel_raises
        return _task(TaskState.CANCELLED)


class _CapturingNotifier:
    def __init__(self) -> None:
        self.accounts: list[FailureAccount] = []

    async def notify(self, account: FailureAccount, **_kw: object) -> None:
        self.accounts.append(account)


_TAG = PersonaIdentityTag(persona_id="astrid", display_name="Astrid")


async def _steer(verb: str, mutator: _FakeMutator | None = None) -> _FakeMutator:
    mutator = mutator or _FakeMutator(get_task=_task())
    await TaskSteeringService(tasks=mutator).steer(
        {"owner_id": "user-1", "task_id": "t1", "verb": verb}
    )
    return mutator


@pytest.mark.asyncio
async def test_pause_calls_pause() -> None:
    assert (await _steer("pause")).calls == [("pause", "user-1", "t1")]


@pytest.mark.asyncio
async def test_resume_calls_unpause() -> None:
    assert (await _steer("resume")).calls == [("unpause", "user-1", "t1")]


@pytest.mark.asyncio
async def test_cancel_calls_cancel() -> None:
    assert (await _steer("cancel")).calls == [("cancel", "user-1", "t1")]


@pytest.mark.asyncio
async def test_unknown_verb_is_a_noop() -> None:
    assert (await _steer("explode")).calls == []


@pytest.mark.asyncio
async def test_failed_cancel_that_leaves_task_active_surfaces_an_unsuppressible_account() -> None:
    # The cancel raised (DB error), and the task is STILL ACTIVE → a FAILURE-class account must
    # reach the user (the persona already said "cancelled"; silence would be a false confirmation).
    notifier = _CapturingNotifier()
    mutator = _FakeMutator(cancel_raises=RuntimeError("db down"), get_task=_task(TaskState.ACTIVE))
    await TaskSteeringService(
        tasks=mutator, notifier=notifier, persona_tag_resolver=lambda _pid: _TAG
    ).steer(
        {
            "owner_id": "user-1",
            "task_id": "t1",
            "verb": "cancel",
            "persona_id": "astrid",
            "conversation_id": "conv-1",
        }
    )
    assert len(notifier.accounts) == 1
    assert notifier.accounts[0].kind is FailureKind.CANCEL_FAILED


@pytest.mark.asyncio
async def test_cancel_of_already_terminal_task_is_a_benign_noop() -> None:
    # The entity refuses to cancel a terminal task; re-read shows terminal → NO account (benign).
    notifier = _CapturingNotifier()
    mutator = _FakeMutator(
        cancel_raises=TaskStateError("already terminal", context={}),
        get_task=_task(TaskState.COMPLETED),
    )
    await TaskSteeringService(
        tasks=mutator, notifier=notifier, persona_tag_resolver=lambda _pid: _TAG
    ).steer({"owner_id": "user-1", "task_id": "t1", "verb": "cancel", "persona_id": "astrid"})
    assert notifier.accounts == []  # already terminal → the cancel's intent is satisfied


@pytest.mark.asyncio
async def test_failed_cancel_on_a_vanished_task_is_a_benign_noop() -> None:
    notifier = _CapturingNotifier()
    mutator = _FakeMutator(
        cancel_raises=RuntimeError("boom"),
        get_raises=TaskNotFoundError("gone", context={}),
    )
    await TaskSteeringService(
        tasks=mutator, notifier=notifier, persona_tag_resolver=lambda _pid: _TAG
    ).steer({"owner_id": "user-1", "task_id": "t1", "verb": "cancel", "persona_id": "astrid"})
    assert notifier.accounts == []

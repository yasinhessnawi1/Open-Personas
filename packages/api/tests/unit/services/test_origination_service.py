"""Unit tests for the A4 origination service — the load-bearing seam invariants (Spec A4, T6).

Proven here with fakes (no DB): failure-visibility, idempotency that converges on the *right*
surviving task, the assistant_message_id-primary key with the content-hash fallback, and the
no-unkeyed-path guard.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from persona.schedules import Schedule
from persona.schema.origination import PersonaIdentityTag
from persona.tasks import Contract, Task, TaskState, WaitKind
from persona_api.approvals.cadence import MessagePriority
from persona_api.approvals.failure import FailureAccount, FailureKind
from persona_api.services.origination_service import (
    OriginationKeyError,
    OriginationService,
    OriginationStatus,
    derive_origination_key,
)


class _FakeTasks:
    def __init__(self, *, fail_on_create: bool = False) -> None:
        self.store: dict[tuple[str, str], Task] = {}
        self.fail_on_create = fail_on_create

    def get_optional(self, owner_id: str, task_id: str) -> Task | None:
        return self.store.get((owner_id, task_id))

    def create_if_absent(self, task: Task) -> None:
        if self.fail_on_create:
            msg = "db down"
            raise RuntimeError(msg)
        self.store.setdefault((task.owner_id, task.id), task)


class _FakeSchedules:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], Schedule] = {}
        self.deleted: list[tuple[str, str]] = []

    def create_if_absent(self, schedule: Schedule, *, now: datetime) -> None:  # noqa: ARG002
        self.store.setdefault((schedule.owner_id, schedule.id), schedule)

    def delete(self, owner_id: str, schedule_id: str) -> None:
        self.deleted.append((owner_id, schedule_id))
        self.store.pop((owner_id, schedule_id), None)


class _FakeNotifier:
    def __init__(self) -> None:
        self.accounts: list[FailureAccount] = []

    async def notify(
        self,
        account: FailureAccount,
        *,
        persona: PersonaIdentityTag,
        owner_id: str,
        conversation_id: str,
    ) -> None:
        _ = (persona, owner_id, conversation_id)  # part of the Protocol; unused by the fake
        self.accounts.append(account)


def _service(
    *, fail_on_create: bool = False
) -> tuple[OriginationService, _FakeTasks, _FakeSchedules, _FakeNotifier]:
    tasks = _FakeTasks(fail_on_create=fail_on_create)
    schedules = _FakeSchedules()
    notifier = _FakeNotifier()
    return (
        OriginationService(tasks=tasks, schedules=schedules, notifier=notifier),
        tasks,
        schedules,
        notifier,
    )


def _event_data(
    *, goal: str = "track fares", assistant_message_id: str = "msg-1", draft_hash: str = "h1"
) -> dict[str, Any]:
    contract = Contract(goal=goal).model_dump(mode="json")
    return {
        "owner_id": "user-1",
        "persona_id": "astrid",
        "persona_name": "Astrid",
        "conversation_id": "conv-1",
        "assistant_message_id": assistant_message_id,
        "contract": contract,
        "schedule": {
            "recurrence": {"freq": "DAILY", "byhour": [7]},
            "one_time_at": None,
            "timezone": "Europe/Oslo",
            "human_terms": "every day at 07:00",
        },
        "draft_hash": draft_hash,
    }


# --- happy path ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_creates_task_and_schedule_with_the_contract() -> None:
    service, tasks, schedules, _ = _service()
    outcome = await service.originate(_event_data(goal="track the fares every morning"))
    assert outcome.status is OriginationStatus.CREATED
    assert outcome.task is not None
    assert outcome.task.contract.goal == "track the fares every morning"
    assert outcome.task.schedule_id is not None
    # A schedule-backed task is born WAITING(until_time), awaiting its first fire (so it executes).
    assert outcome.task.state is TaskState.WAITING
    assert outcome.task.wait_kind is WaitKind.UNTIL_TIME
    # The schedule fires the A1→A2 bridge (which builds the leg), NOT task_leg directly.
    assert len(schedules.store) == 1
    created_schedule = next(iter(schedules.store.values()))
    assert created_schedule.target_job_type == "task_scheduled_fire"
    assert created_schedule.payload_template == {"task_id": outcome.task_id}


# --- A7: the event-trigger creation door (criterion 1) ---------------------------------


class _FakeTriggers:
    def __init__(self) -> None:
        self.created: list[Any] = []
        self.deleted: list[tuple[str, str]] = []

    def create_if_absent(self, record: Any, *, now: datetime) -> Any:  # noqa: ANN401, ARG002
        self.created.append(record)
        return record

    def delete(self, owner_id: str, trigger_id: str) -> bool:
        self.deleted.append((owner_id, trigger_id))
        return True


def _trigger_event_data(*, assistant_message_id: str = "msg-t") -> dict[str, Any]:
    from persona.events import EventKind, MessageFilter, TriggerSpec

    spec = TriggerSpec(
        event_kind=EventKind.CONNECTOR_MESSAGE_RECEIVED,
        filter=MessageFilter(platform="email", sender="landlord@example.com"),
        human_terms="an email from landlord@example.com arrives",
    )
    return {
        "owner_id": "user-1",
        "persona_id": "astrid",
        "persona_name": "Astrid",
        "conversation_id": "conv-1",
        "assistant_message_id": assistant_message_id,
        "contract": Contract(goal="summarise the landlord's email").model_dump(mode="json"),
        "schedule": {},  # schedule XOR trigger (A7-D-3)
        "trigger": spec.model_dump(mode="json"),
        "draft_hash": "ht",
    }


@pytest.mark.asyncio
async def test_confirmed_trigger_contract_creates_task_and_trigger_row() -> None:
    tasks, schedules, notifier = _FakeTasks(), _FakeSchedules(), _FakeNotifier()
    triggers = _FakeTriggers()
    service = OriginationService(
        tasks=tasks, schedules=schedules, notifier=notifier, triggers=triggers
    )
    outcome = await service.originate(_trigger_event_data())

    assert outcome.status is OriginationStatus.CREATED
    assert outcome.task is not None
    # An event-triggered task is born WAITING(on_event) — the dispatcher's door-a fires its leg.
    assert outcome.task.state is TaskState.WAITING
    assert outcome.task.wait_kind is WaitKind.ON_EVENT
    assert outcome.task.schedule_id is None  # no schedule (trigger XOR schedule)
    assert len(schedules.store) == 0
    # Criterion 1: the trigger registry row was created HERE (the confirmed-contract door) — exactly
    # one, pointing door-a at the created task.
    assert len(triggers.created) == 1
    record = triggers.created[0]
    assert record.task_id == outcome.task_id
    assert record.enabled is True
    assert record.action.kind == "fire_task_leg"
    assert record.action.task_id == outcome.task_id


@pytest.mark.asyncio
async def test_triggered_contract_without_a_trigger_store_fails_visibly() -> None:
    # A confirmed trigger contract with no registry wired must FAIL LOUD (an account), never a
    # silently-dropped confirmed contract (the failure-visibility invariant extends to A7).
    tasks, schedules, notifier = _FakeTasks(), _FakeSchedules(), _FakeNotifier()
    service = OriginationService(
        tasks=tasks, schedules=schedules, notifier=notifier, triggers=None
    )
    outcome = await service.originate(_trigger_event_data())
    assert outcome.status is OriginationStatus.FAILED
    assert len(notifier.accounts) == 1  # the un-suppressible failure account


@pytest.mark.asyncio
async def test_spend_cap_lands_on_the_contract_bounds() -> None:
    service, _, _, _ = _service()
    contract = Contract.model_validate(_event_data()["contract"])  # sanity: base has no cap
    assert contract.bounds.total_budget_micros is None
    data = _event_data()
    data["contract"] = Contract(goal="book it").model_dump(mode="json")
    # Inject a spend grant via a fully-formed contract (mirrors the emission path).
    from persona.tasks import ContractBounds
    from persona.tools.categories import ActionCategory
    from persona.tools.category_policy import CategoryDecision, CategoryPolicy, CategoryRule

    rich = Contract(
        goal="book it",
        bounds=ContractBounds(total_budget_micros=15_000_000),
        category_policy=CategoryPolicy(
            overrides=(
                CategoryRule(category=ActionCategory.SPEND, decision=CategoryDecision.ALLOW),
            )
        ),
    )
    data["contract"] = rich.model_dump(mode="json")
    outcome = await service.originate(data)
    assert outcome.task is not None
    assert outcome.task.contract.bounds.total_budget_micros == 15_000_000


# --- idempotency: converges on the RIGHT survivor --------------------------------------


@pytest.mark.asyncio
async def test_replay_of_same_event_converges_on_one_correct_task() -> None:
    service, tasks, _, _ = _service()
    data = _event_data(goal="the confirmed goal")
    first = await service.originate(data)
    second = await service.originate(data)  # the SAME event re-delivered (worker retry)
    assert first.status is OriginationStatus.CREATED
    assert second.status is OriginationStatus.IDEMPOTENT
    # Exactly one task...
    assert len(tasks.store) == 1
    # ...and it is the RIGHT one (the confirmed contract, not an empty/wrong draft).
    survivor = next(iter(tasks.store.values()))
    assert survivor.contract.goal == "the confirmed goal"
    # The derived id is stable across the replay (the dedup actually keys on the same anchor).
    assert first.task_id == second.task_id


@pytest.mark.asyncio
async def test_double_confirm_same_turn_is_one_task() -> None:
    service, tasks, _, _ = _service()
    # Same assistant_message_id (same proposal turn) → same key → one task, even if a later
    # re-delivery carried different content.
    await service.originate(
        _event_data(goal="first", assistant_message_id="msg-9", draft_hash="ha")
    )
    await service.originate(
        _event_data(goal="second", assistant_message_id="msg-9", draft_hash="hb")
    )
    assert len(tasks.store) == 1
    assert next(iter(tasks.store.values())).contract.goal == "first"  # the first confirm wins


# --- the idempotency key: assistant_message_id primary, draft_hash fallback ------------


def test_key_uses_assistant_message_id_when_present() -> None:
    key = derive_origination_key(
        conversation_id="c", assistant_message_id="msg-1", draft_hash="hash-x"
    )
    assert key == "originate:c:msg-1"


def test_key_falls_back_to_draft_hash_only_when_message_id_absent() -> None:
    key = derive_origination_key(conversation_id="c", assistant_message_id="", draft_hash="hash-x")
    assert key == "originate:c:hash-x"


def test_key_raises_when_no_anchor_is_available() -> None:
    # There is no unkeyed path — an event with neither anchor cannot mint a (replay-duplicating) id.
    with pytest.raises(OriginationKeyError):
        derive_origination_key(conversation_id="c", assistant_message_id="  ", draft_hash="")


@pytest.mark.asyncio
async def test_message_id_is_the_key_not_content() -> None:
    # Same message id, different content → same task_id (one proposal turn = one task).
    service, _, _, _ = _service()
    a = await service.originate(_event_data(goal="g1", assistant_message_id="m", draft_hash="x"))
    b_data = _event_data(goal="g2-different", assistant_message_id="m", draft_hash="y-different")
    b = await service.originate(b_data)
    assert a.task_id == b.task_id  # keyed on the message id, not the content hash


# --- failure-visibility: confirmed-but-nothing is impossible ---------------------------


@pytest.mark.asyncio
async def test_create_failure_surfaces_an_unsuppressible_failure_account() -> None:
    service, tasks, schedules, notifier = _service(fail_on_create=True)
    outcome = await service.originate(_event_data())
    assert outcome.status is OriginationStatus.FAILED
    # No task persisted...
    assert tasks.store == {}
    # ...and the user is told, on a cadence-bypass FAILURE class (un-suppressible, even on quiet).
    assert len(notifier.accounts) == 1
    account = notifier.accounts[0]
    assert account.kind is FailureKind.ORIGINATION_FAILED
    assert account.priority is MessagePriority.FAILURE
    assert account.options  # never a dead end


@pytest.mark.asyncio
async def test_task_failure_compensates_the_orphan_schedule() -> None:
    service, _, schedules, _ = _service(fail_on_create=True)
    await service.originate(_event_data())
    # The schedule was created first (FK), then the task failed → the orphan is compensated away.
    assert schedules.deleted  # delete was attempted
    assert schedules.store == {}  # no orphan schedule left firing for a non-existent task


def test_origination_built_schedule_opts_into_the_fire_bell() -> None:
    # Operator-pass find (2026-07-07): a chat/voice-CONFIRMED schedule reaches the
    # origination door only from an explicit user request, so it must ring the bell on
    # fire — exactly like the HTTP reminder dialog (notify_on_fire default True). Before
    # the fix, _build_schedule left it at the column default False → chat reminders were
    # silent. Pure-function assertion (no stores/DB).
    from datetime import UTC, datetime

    from persona_api.services.origination_service import _build_schedule

    sched = _build_schedule(
        schedule_id="sch_1",
        owner_id="owner_1",
        payload={
            "timezone": "Europe/Oslo",
            "recurrence": {"freq": "DAILY", "byhour": [9], "byminute": [0]},
        },
        task_id="task_1",
        now=datetime(2026, 7, 7, 12, 0, tzinfo=UTC),
        subject="check email inbox",
    )
    assert sched.notify_on_fire is True
    # The subject must snapshot into the fire payload so the bell reads
    # "{persona} ran your reminder: {subject}" — without it the {subject} interpolation
    # fails and the client falls back to the raw message key (operator-pass find).
    assert sched.payload_template.get("subject") == "check email inbox"

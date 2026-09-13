"""The attention actions' seams, on the community engine (Spec W1, T6).

- **The retry suffix (D-W1-20).** A resume after a dead-letter at the SAME head used to re-key
  to the dead row's key and vanish into ``ON CONFLICT DO NOTHING`` (R9-130). The resume seam
  now counts the dead attempts at that head, in the hot table AND the archive, and suffixes
  the key; a fresh head carries no suffix, so A0's dedup of a double enqueue is untouched.
- **Controls reach a running leg (D-W1-21).** The leg handler reads the durable task row at
  every step boundary and trips the executor's cancel token when the task is terminal or
  paused; the checkpoint still lands and no continuation is enqueued.
- **Pickup / reply / retry gating (D-W1-10).** A paused task, a paused owner or a suspended
  persona cannot pick up; a task waiting on an approval takes no free-text reply; a finished
  task retries as a NEW task with the same contract.
- **Resume puts the leg back (R9-146, D-W1-29 / D-W1-30).** A task paused while being worked
  has no job left (the running leg stopped at its boundary; a queued leg was consumed at
  claim). The one resume seam clears the overlay AND rides the continuation's resume from the
  salvaged head, keyed past any spent row there; the chat verb goes through the same seam.
- **One dispatch composition (F2).** Retry goes through ``work_dispatch_service.dispatch_task``,
  which checks the persona before any write.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona.approvals import ActionProposal
from persona.tasks import Contract, ScheduledFire, Task, TaskKind, TaskState, WaitKind
from persona.tools import ActionCategory
from persona_api.approvals.kill_switch import KillSwitchStore
from persona_api.approvals.store import ApprovalStore
from persona_api.db.community import (
    create_community_schema,
    ensure_owner,
    make_community_engine,
)
from persona_api.db.models import jobs as jobs_t
from persona_api.db.models import jobs_archive as jobs_archive_t
from persona_api.db.models import personas as personas_t
from persona_api.errors import ApprovalPendingError
from persona_api.jobs.queue import JobQueue
from persona_api.services import task_control_service
from persona_api.tasks import TaskLegHandler, TaskLegPayload, TaskStore
from persona_api.tasks.continuation import TaskContinuation
from persona_api.tasks.handler import task_leg_idempotency_key
from persona_api.tasks.store import CheckpointStore
from persona_runtime.agentic.events import RunEvent
from persona_runtime.agentic.run import Run, RunStatus
from persona_runtime.agentic.step import Step, StepType
from sqlalchemy import Table, insert

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator, Mapping
    from pathlib import Path

    from persona.tasks import LegBox, SpendKind, TaskCheckpoint
    from persona_runtime.agentic.run import CancelToken, StepUsage
    from sqlalchemy import Engine

_NOW = datetime(2026, 9, 6, 9, 0, tzinfo=UTC)
_OWNER = "user_actions"
_PERSONA = "persona_actions"
_GOAL = "brief hacker news"


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = make_community_engine(tmp_path / "actions.db")
    create_community_schema(eng)
    ensure_owner(eng, owner_id=_OWNER, email="a@example.com")
    with eng.begin() as conn:
        conn.execute(insert(personas_t).values(id=_PERSONA, owner_id=_OWNER, yaml="name: x"))
    yield eng
    eng.dispose()


def _task(store: TaskStore, task_id: str, *, kind: TaskKind = TaskKind.STANDING) -> Task:
    task = Task(
        id=task_id,
        owner_id=_OWNER,
        persona_id=_PERSONA,
        contract=Contract(goal=_GOAL),
        kind=kind,
        created_at=_NOW,
        updated_at=_NOW,
    )
    store.create(task)
    return store.start(_OWNER, task_id, now=_NOW)


def _dead_row(engine: Engine, table: Table, key: str, *, state: str = "dead") -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(table).values(
                id=f"job-{key}-{state}-{table.name}",
                type="task_leg",
                owner_id=_OWNER,
                payload={"task_id": "t1"},
                idempotency_key=key,
                state=state,
                attempt=5,
                max_attempts=5,
                last_error="every backend exhausted",
                scheduled_at=_NOW,
                created_at=_NOW,
                **({"archived_at": _NOW} if table is jobs_archive_t else {}),
            )
        )


# --- the retry suffix ----------------------------------------------------------------------


def test_the_resume_key_is_the_base_key_until_a_dead_attempt_exists() -> None:
    payload = TaskLegPayload(
        task_id="t1", predecessor_seq=None, trigger=ScheduledFire(schedule_id="s", fire_time=_NOW)
    )
    assert task_leg_idempotency_key(payload) == "task:t1:after:init"
    assert task_leg_idempotency_key(payload, retry=0) == "task:t1:after:init"
    assert task_leg_idempotency_key(payload, retry=2) == "task:t1:after:init:retry:2"


def test_spent_attempts_are_counted_across_the_hot_table_and_the_archive(engine: Engine) -> None:
    base = "task:t1:after:init"
    _dead_row(engine, jobs_t, base)  # the first dead attempt, still hot
    _dead_row(engine, jobs_archive_t, f"{base}:retry:1")  # a later one, already archived
    _dead_row(engine, jobs_t, f"{base}:retry:2", state="failed")  # a permanent failure counts too
    # R9-146: a job CONSUMED without running (the claim-side skip of a paused task) is spent too.
    _dead_row(engine, jobs_t, f"{base}:retry:3", state="succeeded")
    # ...and it still counts once the sweep has archived it a day later, or a resume made the
    # morning after a paused skip would re-key onto the archived row and be absorbed. The
    # archive half of the SUCCEEDED state needs its own row: seeding the hot table alone left
    # the cold query free to drop it (found at the T6 review).
    _dead_row(engine, jobs_archive_t, f"{base}:retry:4", state="succeeded")
    _dead_row(engine, jobs_t, "task:t2:after:init")  # another task: never counted
    _dead_row(engine, jobs_archive_t, "task:t2:after:init", state="succeeded")  # nor archived
    assert JobQueue(engine).count_spent_attempts(owner_id=_OWNER, idempotency_key=base) == 5
    assert JobQueue(engine).count_spent_attempts(owner_id="someone-else", idempotency_key=base) == 0


class _RecordingQueue:
    """A queue that answers the dead-attempt count and records what resume enqueues."""

    def __init__(self, dead: int) -> None:
        self._dead = dead
        self.enqueued: list[dict[str, object]] = []

    def count_spent_attempts(self, *, owner_id: str, idempotency_key: str) -> int:  # noqa: ARG002
        return self._dead

    def enqueue(self, **kwargs: object) -> None:
        self.enqueued.append(kwargs)


@pytest.mark.parametrize(("dead", "suffix"), [(0, ""), (1, ":retry:1"), (3, ":retry:3")])
def test_resume_suffixes_the_key_by_the_dead_attempts_at_that_head(
    engine: Engine, dead: int, suffix: str
) -> None:
    store = TaskStore(engine)
    _task(store, "t1")
    store.begin_wait(_OWNER, "t1", WaitKind.ON_USER, now=_NOW)
    queue = _RecordingQueue(dead)
    continuation = TaskContinuation(
        task_store=store,
        queue=queue,  # type: ignore[arg-type]
        checkpoint_store=CheckpointStore(engine),
    )
    from persona.tasks import UserReply

    continuation.resume(_OWNER, "t1", UserReply(reply="go on"), now=_NOW)
    assert len(queue.enqueued) == 1
    assert queue.enqueued[0]["idempotency_key"] == f"task:t1:after:init{suffix}"


# --- a control reaches a running leg ----------------------------------------------------------


class _LegThatGetsCancelledMidway:
    """Emits step 0, then the user cancels the task, then reaches the next boundary."""

    def __init__(self, store: TaskStore, engine: Engine) -> None:
        self._store = store
        self._engine = engine
        self.saw_cancel_at_boundary = False

    async def run(
        self,
        task: str,
        *,
        on_event: Callable[[RunEvent], Awaitable[None]],
        cancel_token: CancelToken,
        on_step_usage: Callable[[StepUsage], Awaitable[None]] | None = None,  # noqa: ARG002
    ) -> Run:
        await on_event(RunEvent.thinking(0))
        assert not cancel_token.is_cancelled
        # The user presses Cancel from another surface while the leg is between steps.
        task_control_service.cancel_task(self._engine, _OWNER, self._store.get(_OWNER, "t1"))
        await on_event(RunEvent.thinking(1))  # the next boundary: the control must bite here
        self.saw_cancel_at_boundary = cancel_token.is_cancelled
        status = RunStatus.CANCELLED if cancel_token.is_cancelled else RunStatus.COMPLETED
        return Run(
            persona_id=_PERSONA,
            task=task,
            status=status,
            steps=[Step(type=StepType.REASONING, content="found two stories", tokens=10)],
            output="found two stories so far",
            started_at=_NOW,
            finished_at=_NOW,
        )


class _Builder:
    def __init__(self, runner: object) -> None:
        self._runner = runner

    def build(self, task_id: str, persona_id: str, box: LegBox, *, task: object = None) -> object:  # noqa: ARG002
        return self._runner


class _Sink:
    """Records the checkpoint the leg lands (the CAS append is Postgres-only, see T1's tests)."""

    def __init__(self) -> None:
        self.landed: list[TaskCheckpoint] = []

    def get_latest(self, owner_id: str, task_id: str) -> TaskCheckpoint | None:  # noqa: ARG002
        return None

    def append(
        self,
        task: Task,
        checkpoint: TaskCheckpoint,
        *,
        spend: Mapping[SpendKind, int] | None = None,  # noqa: ARG002
        now: datetime,
    ) -> Task:
        self.landed.append(checkpoint)
        return task.advance_checkpoint(checkpoint.checkpoint_seq, now=now)


class _RecordingContinuation:
    def __init__(self) -> None:
        self.applied = 0

    def apply(self, *args: object, **kwargs: object) -> None:  # noqa: ARG002
        self.applied += 1


class _Context:
    owner_id = _OWNER
    job_id = "job-1"

    def meter(self, *, amount_micros: int, kind: str, detail: object = None) -> None:  # noqa: ARG002
        return None


@pytest.mark.asyncio
async def test_a_cancel_during_a_leg_stops_it_at_the_next_boundary_and_the_checkpoint_lands(
    engine: Engine,
) -> None:
    store = TaskStore(engine)
    _task(store, "t1")
    leg = _LegThatGetsCancelledMidway(store, engine)
    sink = _Sink()
    continuation = _RecordingContinuation()
    handler = TaskLegHandler(
        task_store=store,
        checkpoint_store=sink,  # type: ignore[arg-type]
        runner_builder=_Builder(leg),  # type: ignore[arg-type]
        continuation=continuation,  # type: ignore[arg-type]
    )
    payload = TaskLegPayload(
        task_id="t1",
        predecessor_seq=None,
        trigger=ScheduledFire(schedule_id="s", fire_time=_NOW),
    )
    await handler.handle(payload, _Context())  # type: ignore[arg-type]

    assert leg.saw_cancel_at_boundary, "the control must trip the token at the next boundary"
    assert store.get(_OWNER, "t1").state is TaskState.CANCELLED
    assert len(sink.landed) == 1  # the salvaged work landed as the checkpoint
    assert sink.landed[0].progress_conclusions == ("found two stories so far",)
    assert continuation.applied == 0  # nothing enqueued for a task the user stopped


@pytest.mark.asyncio
async def test_a_pause_during_a_leg_also_stops_it_at_the_next_boundary(engine: Engine) -> None:
    store = TaskStore(engine)
    _task(store, "t1")

    class _PausedMidway(_LegThatGetsCancelledMidway):
        async def run(
            self,
            task: str,
            *,
            on_event: object,
            cancel_token: object,
            on_step_usage: object = None,  # noqa: ARG002
        ) -> Run:
            await on_event(RunEvent.thinking(0))
            store.pause(_OWNER, "t1", now=_NOW)
            await on_event(RunEvent.thinking(1))
            self.saw_cancel_at_boundary = cancel_token.is_cancelled
            return Run(
                persona_id=_PERSONA,
                task=task,
                status=RunStatus.CANCELLED,
                steps=[Step(type=StepType.REASONING, content="x", tokens=1)],
                output="halfway",
                started_at=_NOW,
                finished_at=_NOW,
            )

    leg = _PausedMidway(store, engine)
    continuation = _RecordingContinuation()
    handler = TaskLegHandler(
        task_store=store,
        checkpoint_store=_Sink(),  # type: ignore[arg-type]
        runner_builder=_Builder(leg),  # type: ignore[arg-type]
        continuation=continuation,  # type: ignore[arg-type]
    )
    payload = TaskLegPayload(
        task_id="t1", predecessor_seq=None, trigger=ScheduledFire(schedule_id="s", fire_time=_NOW)
    )
    await handler.handle(payload, _Context())  # type: ignore[arg-type]
    assert leg.saw_cancel_at_boundary
    assert continuation.applied == 0


# --- pickup / reply / retry gating (D-W1-10) -------------------------------------------------


class _NoQueueResume:
    """Counts resumes without touching A0's Postgres-only enqueue."""

    def __init__(self) -> None:
        self.resumed: list[str] = []
        self.triggers: list[object] = []

    def count_spent_attempts(self, **_: object) -> int:
        return 0

    def enqueue(self, **kwargs: object) -> None:
        self.resumed.append(str(kwargs["idempotency_key"]))
        # The trigger rides inside the serialised leg payload, which is what the worker
        # deserialises on the other side — so that is what is captured here.
        payload = kwargs.get("payload") or {}
        self.triggers.append(dict(payload).get("trigger"))  # type: ignore[arg-type]


@pytest.fixture
def waiting_task(engine: Engine) -> TaskStore:
    store = TaskStore(engine)
    _task(store, "t1")
    store.begin_wait(_OWNER, "t1", WaitKind.ON_USER, now=_NOW)
    return store


def test_a_paused_task_cannot_be_picked_up(engine: Engine, waiting_task: TaskStore) -> None:
    waiting_task.pause(_OWNER, "t1", now=_NOW)
    outcome = task_control_service.pickup_task(
        engine, _OWNER, waiting_task.get(_OWNER, "t1"), now=_NOW
    )
    assert outcome.changed is False
    assert "paused" in outcome.note


def test_a_paused_owner_cannot_pick_up_and_the_note_says_so(
    engine: Engine, waiting_task: TaskStore
) -> None:
    KillSwitchStore(engine).pause_owner(_OWNER, actor="user", now=_NOW)
    outcome = task_control_service.pickup_task(
        engine, _OWNER, waiting_task.get(_OWNER, "t1"), now=_NOW
    )
    assert outcome.changed is False
    assert outcome.owner_paused is True
    assert "autonomy is paused" in outcome.note


def test_a_suspended_persona_cannot_pick_up(engine: Engine, waiting_task: TaskStore) -> None:
    KillSwitchStore(engine).suspend_persona(_OWNER, _PERSONA, now=_NOW)
    outcome = task_control_service.pickup_task(
        engine, _OWNER, waiting_task.get(_OWNER, "t1"), now=_NOW
    )
    assert outcome.changed is False
    assert "suspended" in outcome.note


def test_a_reply_to_a_task_waiting_on_an_approval_points_at_the_approval(
    engine: Engine, waiting_task: TaskStore
) -> None:
    ApprovalStore(engine).create_proposal(
        ActionProposal(
            proposal_id="p1",
            owner_id=_OWNER,
            task_id="t1",
            persona_id=_PERSONA,
            categories=frozenset({ActionCategory.COMMUNICATE_AS_USER}),
            tool_name="send_email",
            arguments={"to": "x@example.com"},
            description="Email the landlord",
            created_at=_NOW,
        )
    )
    with pytest.raises(ApprovalPendingError) as excinfo:
        task_control_service.reply_to_task(
            engine, _OWNER, waiting_task.get(_OWNER, "t1"), "sure", now=_NOW
        )
    assert excinfo.value.context["proposal_id"] == "p1"
    assert "—" not in excinfo.value.message


def test_a_finished_task_is_the_durable_truth_for_pickup_and_reply(engine: Engine) -> None:
    store = TaskStore(engine)
    _task(store, "t1")
    store.complete(_OWNER, "t1", now=_NOW)
    done = store.get(_OWNER, "t1")
    assert task_control_service.pickup_task(engine, _OWNER, done, now=_NOW).changed is False
    assert (
        task_control_service.reply_to_task(engine, _OWNER, done, "hello", now=_NOW).changed is False
    )


def test_retry_refuses_a_task_that_is_still_going(engine: Engine, waiting_task: TaskStore) -> None:
    outcome = task_control_service.retry_task(
        engine, _OWNER, waiting_task.get(_OWNER, "t1"), now=_NOW
    )
    assert outcome.changed is False
    assert outcome.successor is None


# --- resume puts the leg back (R9-146; D-W1-29 / D-W1-30) -------------------------------------


class _SpentQueue(_NoQueueResume):
    """A queue with ``spent`` attempts already at the head (a consumed row, a dead row)."""

    def __init__(self, spent: int) -> None:
        super().__init__()
        self._spent = spent

    def count_spent_attempts(self, **_: object) -> int:
        return self._spent


def test_resuming_a_paused_working_task_unpauses_and_enqueues_past_the_spent_row(
    engine: Engine,
) -> None:
    store = TaskStore(engine)
    _task(store, "t1")
    store.pause(_OWNER, "t1", now=_NOW)
    queue = _SpentQueue(spent=1)  # the leg consumed at claim while paused (R9-146)
    outcome = task_control_service.resume_task(
        engine,
        _OWNER,
        store.get(_OWNER, "t1"),
        now=_NOW,
        queue=queue,  # type: ignore[arg-type]
    )
    assert outcome.changed is True
    assert outcome.task.paused is False
    assert store.get(_OWNER, "t1").paused is False
    assert queue.resumed == ["task:t1:after:init:retry:1"]  # keyed past the consumed row


def test_resuming_a_paused_task_waiting_on_the_user_only_clears_the_overlay(
    engine: Engine, waiting_task: TaskStore
) -> None:
    waiting_task.pause(_OWNER, "t1", now=_NOW)
    queue = _NoQueueResume()
    outcome = task_control_service.resume_task(
        engine,
        _OWNER,
        waiting_task.get(_OWNER, "t1"),
        now=_NOW,
        queue=queue,  # type: ignore[arg-type]
    )
    assert outcome.changed is True
    assert waiting_task.get(_OWNER, "t1").paused is False
    assert waiting_task.get(_OWNER, "t1").state is TaskState.WAITING
    assert queue.resumed == []  # its own trigger (a reply, a pickup) resumes it


def test_resuming_a_paused_defined_task_starts_it_and_enqueues_its_first_leg(
    engine: Engine,
) -> None:
    store = TaskStore(engine)
    store.create(
        Task(
            id="t1",
            owner_id=_OWNER,
            persona_id=_PERSONA,
            contract=Contract(goal=_GOAL),
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    store.pause(_OWNER, "t1", now=_NOW)
    queue = _NoQueueResume()
    task_control_service.resume_task(
        engine,
        _OWNER,
        store.get(_OWNER, "t1"),
        now=_NOW,
        queue=queue,  # type: ignore[arg-type]
    )
    after = store.get(_OWNER, "t1")
    assert after.state is TaskState.ACTIVE
    assert after.paused is False
    assert queue.resumed == ["task:t1:after:init"]


def test_resume_is_a_no_op_on_a_task_that_is_not_paused(engine: Engine) -> None:
    store = TaskStore(engine)
    _task(store, "t1")
    queue = _NoQueueResume()
    outcome = task_control_service.resume_task(
        engine,
        _OWNER,
        store.get(_OWNER, "t1"),
        now=_NOW,
        queue=queue,  # type: ignore[arg-type]
    )
    assert outcome.changed is False
    assert outcome.note == "Not paused."
    assert queue.resumed == []


@pytest.mark.asyncio
async def test_the_chat_resume_verb_rides_the_same_seam_as_the_route(engine: Engine) -> None:
    """The steering verb's mutator is the control service, so a chat "resume" puts the leg
    back exactly as the route does (the R9-081 drift shape, closed)."""
    from persona_api.services.task_steering_service import TaskSteeringService

    store = TaskStore(engine)
    _task(store, "t1")
    store.pause(_OWNER, "t1", now=_NOW)
    queue = _SpentQueue(spent=1)
    steering = TaskSteeringService(
        tasks=task_control_service.TaskControlMutator(engine, queue=queue),  # type: ignore[arg-type]
    )
    await steering.steer({"owner_id": _OWNER, "task_id": "t1", "verb": "resume"})
    assert store.get(_OWNER, "t1").paused is False
    assert queue.resumed == ["task:t1:after:init:retry:1"]


@pytest.mark.asyncio
async def test_the_chat_pause_and_cancel_verbs_ride_the_control_service(engine: Engine) -> None:
    from persona_api.services.task_steering_service import TaskSteeringService

    store = TaskStore(engine)
    _task(store, "t1")
    steering = TaskSteeringService(tasks=task_control_service.TaskControlMutator(engine))
    await steering.steer({"owner_id": _OWNER, "task_id": "t1", "verb": "pause"})
    assert store.get(_OWNER, "t1").paused is True
    await steering.steer({"owner_id": _OWNER, "task_id": "t1", "verb": "cancel"})
    assert store.get(_OWNER, "t1").state is TaskState.CANCELLED


# --- one dispatch composition (F2) ------------------------------------------------------------


def test_dispatch_refuses_a_missing_persona_with_a_sentence_before_any_write(
    engine: Engine,
) -> None:
    from persona.errors import PersonaNotFoundError, TaskNotFoundError
    from persona_api.services import work_dispatch_service

    queue = _NoQueueResume()
    orphan = Task(
        id="t_orphan",
        owner_id=_OWNER,
        persona_id="persona_gone",
        contract=Contract(goal=_GOAL),
        created_at=_NOW,
        updated_at=_NOW,
    )
    with pytest.raises(PersonaNotFoundError) as excinfo:
        work_dispatch_service.dispatch_task(
            engine,
            queue,  # type: ignore[arg-type]
            orphan,
            now=_NOW,
            persona_gone_message=work_dispatch_service.PERSONA_GONE_MESSAGE,
        )
    assert excinfo.value.message == work_dispatch_service.PERSONA_GONE_MESSAGE
    assert "—" not in excinfo.value.message
    with pytest.raises(TaskNotFoundError):
        TaskStore(engine).get(_OWNER, "t_orphan")  # nothing was written
    assert queue.resumed == []


def test_retry_of_a_task_whose_persona_is_gone_is_refused_with_the_sentence(
    engine: Engine,
) -> None:
    from persona.errors import PersonaNotFoundError
    from persona_api.services import work_dispatch_service
    from sqlalchemy import delete

    with engine.begin() as conn:
        conn.execute(insert(personas_t).values(id="persona_gone", owner_id=_OWNER, yaml="n: y"))
    store = TaskStore(engine)
    store.create(
        Task(
            id="t_failed",
            owner_id=_OWNER,
            persona_id="persona_gone",
            contract=Contract(goal=_GOAL),
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    store.start(_OWNER, "t_failed", now=_NOW)
    failed = store.fail(_OWNER, "t_failed", now=_NOW)
    # The persona is deleted after the failure (the review list the user acts from is older).
    with engine.begin() as conn:
        conn.execute(delete(personas_t).where(personas_t.c.id == "persona_gone"))
    queue = _NoQueueResume()
    with pytest.raises(PersonaNotFoundError) as excinfo:
        task_control_service.retry_task(
            engine,
            _OWNER,
            failed,
            now=_NOW,
            queue=queue,  # type: ignore[arg-type]
        )
    assert excinfo.value.message == work_dispatch_service.PERSONA_GONE_MESSAGE
    assert queue.resumed == []
    assert store.list_for_owner(_OWNER) == []  # no successor was created


def test_retry_dispatches_the_successor_through_the_shared_seam(engine: Engine) -> None:
    store = TaskStore(engine)
    _task(store, "t1", kind=TaskKind.AD_HOC)
    store.fail(_OWNER, "t1", now=_NOW)
    queue = _NoQueueResume()
    outcome = task_control_service.retry_task(
        engine,
        _OWNER,
        store.get(_OWNER, "t1"),
        now=_NOW,
        queue=queue,  # type: ignore[arg-type]
    )
    assert outcome.changed is True
    assert outcome.successor is not None
    assert outcome.successor.state is TaskState.ACTIVE
    assert outcome.successor.kind is TaskKind.AD_HOC
    assert queue.resumed == [f"task:{outcome.successor.id}:after:init"]


def test_a_resumed_leg_is_told_the_user_lifted_the_pause(engine: Engine) -> None:
    """Spec W1 (D-W1-38, amended): a person pressed Resume, so that is what the leg is told.

    A self ``ScheduledFire`` had it believe its own schedule had fired, which is a different
    thing entirely: a persona reading that may treat the wake as its recurrence rather than as
    permission to carry on the work it was stopped mid-way through.
    """
    from persona.tasks import Revived

    store = TaskStore(engine)
    _task(store, "t1")
    store.pause(_OWNER, "t1", now=_NOW)
    queue = _NoQueueResume()
    task_control_service.resume_task(
        engine,
        _OWNER,
        store.get(_OWNER, "t1"),
        now=_NOW,
        queue=queue,  # type: ignore[arg-type]
    )

    assert len(queue.triggers) == 1
    trigger = queue.triggers[0]
    assert isinstance(trigger, dict)
    assert trigger["kind"] == "revived"
    assert trigger["reason"] == "the user resumed it after a pause"
    # And it round-trips back to the real type the leg will read.
    assert Revived.model_validate(trigger).reason == trigger["reason"]

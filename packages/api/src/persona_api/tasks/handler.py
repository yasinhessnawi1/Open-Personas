"""The task-leg job handler — a leg as a durable A0 job (Spec A2, T7).

Hosts one leg of a task as A0's ``task_leg`` tenant: fetch the task + its latest checkpoint,
run the boxed leg (the persona-runtime :class:`LegExecutor` over the unmodified
``AgenticLoop``), and let the checkpoint write ride ``CheckpointStore.append`` (the atomic
CAS, A2-R-4). Registered **additively** in the worker composition root alongside avatar /
synthesis / the scheduler tick (D-A2-X-worker-additive).

**Task-level idempotency — ONE mechanism (A2-R-4).** The job payload fixes ``predecessor_seq``
(the head at job creation); the leg writes ``checkpoint_seq = predecessor_seq + 1``. A
re-delivery (lease-expiry reclaim) carries the SAME payload → the SAME seq → the store CAS
(``head IS NOT DISTINCT FROM seq-1`` + ``ON CONFLICT (task_id, checkpoint_seq)``) makes it a
clean no-op (no double checkpoint, no double-counted *ledger*). The handler adds **no second
job-layer check** — it always runs the leg and relies on the store CAS for the no-op. The
model re-run on a re-delivery is the accepted at-least-once cost; ``context.meter`` records it
in A0's per-job forensics (A0 meters executions), while the *task ledger* accrues exactly once
(the CAS) — A2 accounts committed work.

The disposition-driven task-state transitions (continuation / completion / waiting) are T8/T9;
this handler runs the leg, writes the checkpoint, and meters. The real ``AgenticLoop`` is built
per leg by an injected :class:`LegRunnerBuilder` (the composition root — orchestrator-owned at
deploy, like A0's worker cutover); the :class:`CheckpointWriter` is the ``BasicCheckpointWriter``
stand-in until the model-backed distiller lands (T11 gates its quality).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from persona.jobs import LONG_LEASE, JobPayload, JobTypeSpec, RetryPolicy
from persona.logging import get_logger
from persona.tasks import EventFire, LegBox, ResumeTrigger, ScheduledFire, TaskState, is_terminal
from persona_runtime.legs import BasicCheckpointWriter, LegDisposition, LegExecutor

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from persona.jobs import JobContext, JobRegistry
    from persona.tasks import Task
    from persona_runtime.legs import AgenticRunner, CheckpointWriter, LegOutcome

    from persona_api.jobs.queue import JobQueue
    from persona_api.tasks.continuation import TaskContinuation
    from persona_api.tasks.store import CheckpointStore, TaskStore

#: The digest hook: called after a leg's continuation applies, with the settled leg outcome (task +
#: disposition + resume_at), so the composition root can publish a granularity-gated update (Spec
#: A4, T10). Async + best-effort — it must never fail the leg (updates are additive to the work).
MilestoneHook = "Callable[[LegOutcome, datetime], Awaitable[None]]"

__all__ = [
    "TASK_LEG_JOB_TYPE",
    "LegRunnerBuilder",
    "RunnableGuard",
    "TaskLegHandler",
    "TaskLegPayload",
    "enqueue_task_leg",
    "register_task_leg_handler",
    "task_leg_idempotency_key",
]

TASK_LEG_JOB_TYPE = "task_leg"

_log = get_logger("api.tasks.handler")


class TaskLegPayload(JobPayload):
    """Which leg to run: the task, the job-fixed predecessor anchor, and the trigger.

    ``predecessor_seq`` is the task head at job creation (``None`` for the first leg) — the
    A2-R-4 anchor: the leg writes ``predecessor_seq + 1`` and a re-delivery re-keys to the
    same seq. ``trigger`` is the discriminated :class:`ResumeTrigger` (fire / reply / event)
    carried into the next leg's reconstruction.
    """

    task_id: str
    predecessor_seq: int | None = None
    trigger: ResumeTrigger


def task_leg_idempotency_key(payload: TaskLegPayload) -> str:
    """``task:{task_id}:after:{predecessor_seq}`` — dedups duplicate ENQUEUES of one leg.

    Deterministic in ``(task_id, predecessor_seq)`` so a double-enqueue (a double fire, a
    re-scheduled continuation) collapses to one A0 job. Re-DELIVERY of the same job is
    handled by the store CAS, not this key (the two layers, like A1 over A0).
    """
    anchor = "init" if payload.predecessor_seq is None else str(payload.predecessor_seq)
    return f"task:{payload.task_id}:after:{anchor}"


class LegRunnerBuilder(Protocol):
    """Builds the per-leg agentic runner (the composition root wires the real ``AgenticLoop``).

    MUST build the loop with ``max_steps == box.max_steps`` (the executor enforces only the
    wall-clock trip; the step bound is the loop's own — D-A2-2).
    """

    def build(self, task_id: str, persona_id: str, box: LegBox) -> AgenticRunner: ...


class RunnableGuard(Protocol):
    """The A3 kill-switch guard the handler consults (the ``KillSwitchStore`` satisfies it).

    ``is_runnable`` is the reason-scoped invariant: a task runs only when no pause source
    (terminal / budget / persona-suspend / global-pause) holds it (T11).
    """

    def is_runnable(self, owner_id: str, task: Task) -> bool: ...


class TaskLegHandler:
    """Runs one boxed leg as an A0 job; the checkpoint write rides the store CAS (A2-R-4)."""

    def __init__(
        self,
        *,
        task_store: TaskStore,
        checkpoint_store: CheckpointStore,
        runner_builder: LegRunnerBuilder,
        continuation: TaskContinuation | None = None,
        writer: CheckpointWriter | None = None,
        box: LegBox | None = None,
        runnable_guard: RunnableGuard | None = None,
        on_milestone: Callable[[LegOutcome, datetime], Awaitable[None]] | None = None,
        on_leg_settled: Callable[[LegOutcome, ResumeTrigger, datetime], Awaitable[None]]
        | None = None,
        budget_gate: Callable[[str, Task, datetime], Awaitable[bool]] | None = None,
        on_approval_parked: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> None:
        self._tasks = task_store
        self._checkpoints = checkpoint_store
        self._runner_builder = runner_builder
        self._continuation = continuation
        self._writer = writer if writer is not None else BasicCheckpointWriter()
        self._box = box if box is not None else LegBox()
        # The A3 kill-switch guard (T11): persona-suspend / global-pause prevent the next leg
        # (terminal/budget-paused are checked inline). Optional — a plain A2 worker wires none.
        self._runnable_guard = runnable_guard
        # Spec A4 (T10): the digest hook — publishes a granularity-gated update after the leg's
        # continuation applies. Optional + best-effort; a plain A2 worker wires none.
        self._on_milestone = on_milestone
        # Spec A7 (T4): the lifecycle-emission hook — receives the settled outcome AND the leg's
        # originating trigger, so an event-fired leg can emit a lifecycle event that INHERITS the
        # ``EventFire`` causal chain (the cross-process loop guard). Optional + best-effort.
        self._on_leg_settled = on_leg_settled
        # Spec A3 (T10, budget): the leg-boundary budget gate — consulted before enqueueing the
        # NEXT leg of a CONTINUE outcome. Returns True iff the task was paused at its cap (the
        # caller must NOT continue); the gate voices the "budget reached; extend?" ask. Optional —
        # a plain A2 worker wires none, and the pre-A3 no-cap behaviour holds.
        self._budget_gate = budget_gate
        # Spec A3 (notify-on-park): the proactive C0 "may I do X?" voice for a freshly-parked
        # approval. Given (owner_id, proposal_id), the wired closure loads the proposal + voices
        # via the approval notifier. Optional + best-effort; None → the inbox/chat is the floor.
        self._on_approval_parked = on_approval_parked

    async def handle(self, payload: TaskLegPayload, context: JobContext) -> None:
        owner = context.owner_id
        now = datetime.now(UTC)
        task = self._tasks.get(owner, payload.task_id)

        # A stale job for a non-runnable task → no leg. Terminal = done; budget-paused = no new
        # legs (criterion 7); the A3 kill-switch guard adds persona-suspend / global-pause. The
        # standing guarantee that a stop halts work — the next leg is prevented here.
        if self._runnable_guard is not None and not self._runnable_guard.is_runnable(owner, task):
            _log.info("task leg skipped (kill switch)", task_id=task.id)
            return
        if is_terminal(task.state) or task.paused:
            _log.info("task leg skipped (terminal/paused)", task_id=task.id, state=task.state.value)
            return
        # The job firing IS the trigger arriving — resume a waiting task (one resume point).
        if task.state == TaskState.WAITING:
            task = self._tasks.resume(owner, payload.task_id, now=now)

        prior = self._checkpoints.get_latest(owner, payload.task_id)
        seq = 0 if payload.predecessor_seq is None else payload.predecessor_seq + 1

        runner = self._runner_builder.build(task.id, task.persona_id, self._box)
        executor = LegExecutor(runner=runner, writer=self._writer, sink=self._checkpoints)
        outcome = await executor.run_leg(
            task=task,
            trigger=payload.trigger,
            prior_checkpoint=prior,
            seq=seq,
            box=self._box,
            now=now,
        )
        # A0 metering visibility (per-job spend → audit_log); the task ledger already accrued
        # via the CAS append. On a re-delivery the leg re-runs, so A0 records this execution's
        # spend (forensics) while the ledger no-ops — A0 meters executions, A2 accounts work.
        total = sum(outcome.spend.values())
        context.meter(
            amount_micros=total,
            kind="model",
            detail={
                "surface": TASK_LEG_JOB_TYPE,
                "task_id": payload.task_id,
                "checkpoint_seq": str(seq),
                "disposition": outcome.disposition.value,
            },
        )
        _log.info(
            "task leg ran",
            task_id=payload.task_id,
            checkpoint_seq=seq,
            disposition=outcome.disposition.value,
        )
        # Disposition → state machine (continuation / completion / waiting); raises on FAILED
        # so A0 re-delivers (transient). Skipped when no continuation is wired (idempotency-only).
        # A ScheduledFire's fire_time is the recurrence anchor — "is there a fire after THIS one?" —
        # so a one-time task completes and a recurring one survives regardless of leg-run latency.
        if self._continuation is not None:
            # A3 budget gate (T10): a CONTINUE outcome is about to enqueue the NEXT leg — first
            # consult the per-task cap. Over cap ⇒ the gate pauses the task + voices "extend?" and
            # returns True; we withhold the continuation (no next leg) until the user extends. Only
            # CONTINUE is gated: COMPLETED/WAITING/FAILED enqueue no follow-on leg to withhold.
            if (
                outcome.disposition is LegDisposition.CONTINUE
                and self._budget_gate is not None
                and await self._budget_gate(owner, outcome.task, now)
            ):
                _log.info("task paused at budget cap; next leg withheld", task_id=task.id)
                return
            trigger = payload.trigger
            fired_at = trigger.fire_time if isinstance(trigger, ScheduledFire) else None
            # A7 standing watch: an EventFire-fired leg that completes returns to WAITING(on_event).
            self._continuation.apply(
                owner,
                outcome,
                now=now,
                fired_at=fired_at,
                event_fired=isinstance(trigger, EventFire),
            )
            # A3 notify-on-park: the leg gated an action and the task just parked waiting(on_user).
            # Proactively voice the persona's "may I do X?" ask so the user isn't left to discover
            # the pending approval only in the inbox. Best-effort — the durable proposal + the inbox
            # are the floor; a voice hiccup never fails the (already-parked) leg.
            if (
                outcome.disposition is LegDisposition.WAITING_APPROVAL
                and outcome.proposal_id is not None
                and self._on_approval_parked is not None
            ):
                try:
                    await self._on_approval_parked(owner, outcome.proposal_id)
                except Exception as exc:  # noqa: BLE001 — additive; never fail the parked leg
                    _log.warning(
                        "approval announce failed task_id={tid}: {err}", tid=task.id, err=str(exc)
                    )
        # Spec A4 (T10): after the state settles, publish a granularity-gated digest update. The
        # post-leg task carries the settled state; the hook decides milestone/completion + gating.
        # Best-effort — a delivery hiccup must never fail or re-deliver the (already-done) leg.
        if self._on_milestone is not None:
            try:
                await self._on_milestone(outcome, now)
            except Exception as exc:  # noqa: BLE001 — the update is additive; never fail the leg
                _log.warning(
                    "task milestone update failed task_id={tid}: {err}", tid=task.id, err=str(exc)
                )
        # Spec A7 (T4): emit the lifecycle event this settled leg produced, carrying the leg's
        # trigger so an event-fired leg's output inherits its causal chain. Best-effort + isolated
        # from the milestone hook (one failing must not skip the other, nor fail the done leg).
        if self._on_leg_settled is not None:
            try:
                await self._on_leg_settled(outcome, payload.trigger, now)
            except Exception as exc:  # noqa: BLE001 — additive; never fail the leg
                _log.warning(
                    "task lifecycle emit failed task_id={tid}: {err}", tid=task.id, err=str(exc)
                )


def register_task_leg_handler(
    registry: JobRegistry,
    *,
    task_store: TaskStore,
    checkpoint_store: CheckpointStore,
    runner_builder: LegRunnerBuilder,
    continuation: TaskContinuation | None = None,
    writer: CheckpointWriter | None = None,
    box: LegBox | None = None,
    runnable_guard: RunnableGuard | None = None,
    on_milestone: Callable[[LegOutcome, datetime], Awaitable[None]] | None = None,
    on_leg_settled: Callable[[LegOutcome, ResumeTrigger, datetime], Awaitable[None]] | None = None,
    budget_gate: Callable[[str, Task, datetime], Awaitable[bool]] | None = None,
    on_approval_parked: Callable[[str, str], Awaitable[None]] | None = None,
) -> None:
    """Register the ``task_leg`` handler (A0's task tenant) with its declared idempotency."""
    registry.register(
        JobTypeSpec(
            type=TASK_LEG_JOB_TYPE,
            payload_model=TaskLegPayload,
            handler=TaskLegHandler(
                task_store=task_store,
                checkpoint_store=checkpoint_store,
                runner_builder=runner_builder,
                continuation=continuation,
                writer=writer,
                box=box,
                runnable_guard=runnable_guard,
                on_milestone=on_milestone,
                on_leg_settled=on_leg_settled,
                budget_gate=budget_gate,
                on_approval_parked=on_approval_parked,
            ),
            idempotency_key=task_leg_idempotency_key,
            retry=RetryPolicy(max_attempts=3),
            lease=LONG_LEASE,
        )
    )


def enqueue_task_leg(
    queue: JobQueue,
    *,
    owner_id: str,
    task_id: str,
    predecessor_seq: int | None,
    trigger: ResumeTrigger,
    scheduled_at: datetime | None = None,
) -> None:
    """Enqueue a leg job (a schedule fire, a self-continuation, or a resume).

    Keyed by ``(task_id, predecessor_seq)`` so a duplicate enqueue is A0's ``ON CONFLICT``
    no-op (a double fire / a re-fired tick collapses to one job); a re-delivery of the
    enqueued job is handled by the store CAS. ``scheduled_at`` delays the leg (a timed
    ``waiting(until_time)`` self-continuation rides A0's ``scheduled_at``, as A1's tick does).
    """
    payload = TaskLegPayload(task_id=task_id, predecessor_seq=predecessor_seq, trigger=trigger)
    queue.enqueue(
        type=TASK_LEG_JOB_TYPE,
        owner_id=owner_id,
        payload=payload.model_dump(mode="json"),
        idempotency_key=task_leg_idempotency_key(payload),
        scheduled_at=scheduled_at,
    )

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
deploy, like A0's worker cutover); the :class:`CheckpointWriter` defaults to the
``CompactingCheckpointWriter`` distiller (Spec A2, T12 — D-A2-1's reflect-and-compact), so the
accumulating core stays under the store's budget by construction (R9-005: the
``BasicCheckpointWriter`` stand-in must never run live — it appends unboundedly and
deterministically trips ``CheckpointTooLargeError``).

**The leg's agentic run is a real Spec-08 run (D-08-5).** The leg opens a ``runs`` row before
the loop starts, snapshots ``runs.steps`` as events arrive, finalises it with the run's own
status/steps/output/error, and appends the run id to ``tasks.run_ids``. It writes through
:mod:`persona_api.services.run_record` — the SAME writer the interactive worker uses. This is
the A0 rule ("the worker is a different place to run, never a different *thing* that runs")
applied to the run record: a second writer is how the background leg came to spend real money
and leave nothing viewable.

**Over-budget checkpoint = deterministic, never retried (R9-005).** A
:class:`~persona.errors.CheckpointTooLargeError` from the store gate fires AFTER the agentic run
finished — re-delivering the job re-runs the whole leg (full model + sandbox spend) into the
same failure. The handler therefore catches it, parks the task honestly
(``react_to_dead_leg``'s stuck shape: ``active → waiting(on_user)`` with the real cause, voiced
via ``on_task_stuck``), and lets the job SUCCEED — one execution, no retry burn, no dead-letter.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Protocol

from persona.billing import BillingConfig, credits_charged
from persona.errors import CheckpointTooLargeError
from persona.jobs import LONG_LEASE, JobPayload, JobTypeSpec, RetryPolicy
from persona.logging import get_logger
from persona.tasks import (
    EventFire,
    LegBox,
    ResumeTrigger,
    ScheduledFire,
    TaskState,
    WaitKind,
    is_terminal,
)
from persona_runtime.cost import compute_turn_cost
from persona_runtime.legs import CompactingCheckpointWriter, LegDisposition, LegExecutor

from persona_api.services import run_record

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from persona.jobs import JobContext, JobRegistry
    from persona.tasks import StuckReport, Task
    from persona_runtime.agentic.events import RunEvent
    from persona_runtime.agentic.run import CancelToken, Run, StepUsage
    from persona_runtime.cost import CostSource
    from persona_runtime.legs import AgenticRunner, CheckpointWriter, LegOutcome
    from sqlalchemy import Engine

    from persona_api.editions.credits_policy import CreditsPolicy
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


class _LegBillingAccumulator:
    """Sums a leg's real per-step model cost for the owner-billed deduct (Spec M3, T4b).

    The leg's ``on_step_usage`` callback: each step's model-call usage is priced like
    a chat turn (``compute_turn_cost`` — OpenRouter ``usage.cost`` actual preferred;
    else the resolver estimate; else unpriced). The cost is SUMMED across the leg's
    steps (a leg can span tiers); the handler bills the total ONCE, after the CAS
    append commits. On a re-delivery the leg re-runs and re-accumulates the SAME cost
    — harmlessly discarded by the billing_key idempotency gate.
    """

    def __init__(self, cost_source: CostSource | None) -> None:
        self._cost_source = cost_source
        self._total_cents = 0.0
        self._basis: str | None = None

    async def on_step_usage(self, usage: StepUsage) -> None:
        cost_cents, basis = compute_turn_cost(
            provider=usage.provider,
            model=usage.model,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            actual_cost_usd=usage.cost_usd,
            source=self._cost_source,
        )
        self._total_cents += cost_cents
        # Prefer a genuine basis over ``unpriced`` (same-provider legs share one).
        if basis != "unpriced" or self._basis is None:
            self._basis = basis

    def result(self) -> tuple[float, str | None]:
        """``(total_cost_cents, cost_basis)`` — basis ``None`` iff no step ran."""
        return self._total_cents, self._basis


class _LegRunRecord:
    """The leg's durable ``runs`` row — opened before the run, settled after it (D-08-5).

    A leg executes a real ``AgenticLoop``, so it IS a Spec-08 run and belongs in ``runs``
    exactly like an interactive one: same row, same ``steps`` snapshotting, same terminal
    field set. Every write goes through :mod:`persona_api.services.run_record`, the single
    writer the interactive :class:`~persona_api.background.run_worker.RunRegistry` also
    uses — a second writer is precisely how the two paths drifted apart (the leg's run was
    invisible to the run viewer while the interactive one recorded correctly).

    **Viewable-not-resumable (D-08-5).** :meth:`wrap` decorates the leg's runner so every
    loop event appends to the log and re-snapshots ``runs.steps``; a crash mid-leg leaves
    the run inspectable up to its last event.

    **At-least-once.** A re-delivered leg re-runs the model, so it opens a NEW run row and
    links it — the same forensic posture as ``context.meter`` (A0 meters executions) while
    the checkpoint + task ledger stay exactly-once via the store CAS.
    """

    def __init__(self, engine: Engine, *, owner_id: str, persona_id: str, task_text: str) -> None:
        self.run_id = f"run_{uuid.uuid4().hex}"
        self._engine = engine
        self._owner = owner_id
        self._persona_id = persona_id
        self._task_text = task_text
        #: The run the wrapped runner returned — available even when ``run_leg`` went on
        #: to raise (the over-budget checkpoint path: the run finished, the write did not).
        self.captured_run: Run | None = None

    def open(self, *, now: datetime) -> None:
        """INSERT the ``running`` row. Raises loudly if the persona is not the owner's."""
        run_record.insert_run(
            self._engine,
            run_id=self.run_id,
            owner_id=self._owner,
            persona_id=self._persona_id,
            task=self._task_text,
            started_at=now,
        )

    def wrap(self, runner: AgenticRunner) -> AgenticRunner:
        """Decorate ``runner`` so its events snapshot to ``runs.steps`` as they arrive."""
        return _RecordingRunner(runner, record=self)

    def snapshot(self, event_log: list[dict[str, object]]) -> None:
        run_record.persist_progress(
            self._engine, run_id=self.run_id, event_log=event_log, owner_id=self._owner
        )

    def finish(self, run: Run) -> None:
        """Write the terminal record from the finished run (status/steps/output/error)."""
        run_record.persist_final(self._engine, run_id=self.run_id, run=run, owner_id=self._owner)

    def stop(self, *, reason: str, now: datetime) -> None:
        """Terminate a run that never produced a :class:`Run` object.

        Only the A3 approval gate reaches here: ``GatedActionProposedError`` propagates
        out of the loop, so there is no run to read a status from. ``cancelled`` is the
        honest member of the ``runs_status_check`` vocabulary — the run stopped early and
        executed nothing — with the gate's reason in ``error``. (``awaiting_user`` is NOT
        used: the restart sweep reaps it as an orphan on the premise that an in-process
        response queue is waiting, and for a gated leg none is.)
        """
        run_record.persist_terminal(
            self._engine,
            run_id=self.run_id,
            status="cancelled",
            error=reason,
            finished_at=now,
            owner_id=self._owner,
        )

    def fail(self, message: str) -> None:
        """Mark the run errored when the leg itself raised (nothing finished).

        Best-effort on purpose: this is called on a path that is already re-raising the
        real failure, so a write that fails too must log, not replace the cause the
        caller (and A0's retry/dead-letter accounting) needs to see.
        """
        try:
            run_record.persist_error(
                self._engine, run_id=self.run_id, message=message, owner_id=self._owner
            )
        except Exception as exc:  # noqa: BLE001 — never mask the failure being re-raised
            _log.warning(
                "leg run-record error write failed run_id={rid}: {err}",
                rid=self.run_id,
                err=str(exc),
            )


class _RecordingRunner:
    """Wraps the leg's :class:`AgenticRunner` to snapshot its progress into ``runs``."""

    def __init__(self, inner: AgenticRunner, *, record: _LegRunRecord) -> None:
        self._inner = inner
        self._record = record

    async def run(
        self,
        task: str,
        *,
        on_event: Callable[[RunEvent], Awaitable[None]],
        cancel_token: CancelToken,
        on_step_usage: Callable[[StepUsage], Awaitable[None]] | None = None,
    ) -> Run:
        event_log: list[dict[str, object]] = []

        async def _on_event(event: RunEvent) -> None:
            await on_event(event)
            event_log.append(event.model_dump(mode="json"))
            self._record.snapshot(event_log)

        if on_step_usage is not None:
            run = await self._inner.run(
                task, on_event=_on_event, cancel_token=cancel_token, on_step_usage=on_step_usage
            )
        else:
            # No billing wired — keep the call byte-identical to the executor's own
            # convention so a runner double without ``on_step_usage`` is unaffected.
            run = await self._inner.run(task, on_event=_on_event, cancel_token=cancel_token)
        self._record.captured_run = run
        return run


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
        on_task_stuck: Callable[[str, StuckReport], Awaitable[None]] | None = None,
        credits_policy: CreditsPolicy | None = None,
        rls_engine: Engine | None = None,
        cost_source: CostSource | None = None,
        billing_config: BillingConfig | None = None,
        agentic_floor: int = 1,
    ) -> None:
        self._tasks = task_store
        self._checkpoints = checkpoint_store
        self._runner_builder = runner_builder
        self._continuation = continuation
        # R9-005: the production-safe default is the T12 distiller — reflect-and-compact keeps
        # the accumulating core under the store's budget by construction. The Basic stand-in
        # grows unboundedly and deterministically trips the budget gate after a successful run.
        self._writer = writer if writer is not None else CompactingCheckpointWriter()
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
        # R9-005: the over-budget-checkpoint honesty voice — given (owner_id, StuckReport), the
        # wired closure voices the persona's "I'm stuck" account on the task's conversation (the
        # same account the dead-leg sweep voices). Optional + best-effort; None → the Tasks
        # surface's waiting(on_user) state is the durable floor.
        self._on_task_stuck = on_task_stuck
        # ``rls_engine`` is the owner-scoped engine the stores use (the A0 worker binds
        # ``current_user_id`` before the handler runs). It carries TWO concerns: the
        # durable ``runs`` record for the leg's agentic run (whenever it is present) and
        # — together with ``credits_policy`` — the M3 leg billing. ``None`` → the plain
        # A2 / unit shape: no billing and no run record.
        # Spec M3 (T4b): OWNER-billed, CAS-ridden idempotent leg billing. Both
        # ``credits_policy`` and ``rls_engine`` None → no billing. ``cost_source`` is the resolver
        # (``runtime_factory.metadata_resolver``) so a leg's real cost prices with
        # full catalog coverage; ``agentic_floor`` is the per-leg minimum (infra
        # via the floor, D-M3-4 amendment).
        self._credits_policy = credits_policy
        self._rls_engine = rls_engine
        self._cost_source = cost_source
        self._billing_config = billing_config or BillingConfig()
        self._agentic_floor = agentic_floor

    def _billing_enabled(self) -> bool:
        return self._credits_policy is not None and self._rls_engine is not None

    async def _bill_leg(
        self, owner: str, task_id: str, seq: int, accumulator: _LegBillingAccumulator
    ) -> None:
        """Owner-bill a committed leg's real cost, CAS-ridden idempotent (Spec M3, T4b, D-M3-R5).

        Called ONLY when the leg's checkpoint CAS-append committed (disposition
        CONTINUE / COMPLETED). The deduct is keyed ``billing_key = {task_id}:leg:{seq}``
        — the SAME identity as the checkpoint — so a re-delivered leg (which re-runs
        the model, re-accumulates the same cost, and CAS-no-ops the checkpoint) hits
        the ``ON CONFLICT (billing_key) DO NOTHING`` gate and does NOT double-charge.
        Uses ``capture_up_to_idempotent`` (floored) — a completed leg captures what
        the owner can afford rather than hard-failing already-done work. Fail-soft:
        a billing error never fails the (already-committed) leg.
        """
        if self._credits_policy is None or self._rls_engine is None:
            return
        cost_cents, basis = accumulator.result()
        if basis is None:
            return  # no metered model call this leg (nothing to bill)
        charge = credits_charged(
            provider_cents=cost_cents,
            infra_flat_cents=0.0,  # infra via the per-leg floor (D-M3-4 amendment)
            markup=self._billing_config.credit_markup,
            floor=self._agentic_floor,
        )
        try:
            self._credits_policy.capture_up_to_idempotent(
                rls_engine=self._rls_engine,
                user_id=owner,
                amount=charge,
                reason=f"task_leg:{basis}",
                billing_key=f"{task_id}:leg:{seq}",
                cost_cents=cost_cents,
                cost_basis=basis,
            )
        except Exception as exc:  # noqa: BLE001 — billing must never fail a committed leg
            _log.warning(
                "task-leg owner-billing failed (fail-soft) task_id={tid} seq={seq}: {err}",
                tid=task_id,
                seq=seq,
                err=str(exc),
            )

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
        # The leg's agentic run is a first-class Spec-08 run: open its ``runs`` row and link
        # it to the task BEFORE the loop starts, then snapshot progress through the wrapped
        # runner. Without this a scheduled leg spent real money and left nothing viewable.
        # ``None`` when no engine is wired (the plain A2 unit shape).
        record = self._open_run_record(owner, task, now)
        if record is not None:
            runner = record.wrap(runner)
        executor = LegExecutor(runner=runner, writer=self._writer, sink=self._checkpoints)
        # Spec M3 (T4b): meter the leg's real per-step cost for the owner-billed deduct.
        # None when billing is unwired → the run_leg call stays byte-identical.
        accumulator = _LegBillingAccumulator(self._cost_source) if self._billing_enabled() else None
        try:
            outcome = await executor.run_leg(
                task=task,
                trigger=payload.trigger,
                prior_checkpoint=prior,
                seq=seq,
                box=self._box,
                now=now,
                on_step_usage=accumulator.on_step_usage if accumulator is not None else None,
            )
        except CheckpointTooLargeError as exc:
            # The RUN itself finished — settle its record from what the wrapped runner
            # captured, so an over-budget checkpoint never costs the run's visibility.
            if record is not None:
                self._settle_run_record(record, run=record.captured_run, cause=str(exc), now=now)
            # R9-005: the run FINISHED but its checkpoint cannot land within the store's budget
            # (D-A2-1's post-compaction fail-fast). This is deterministic — re-raising would burn
            # A0's retries re-running the whole leg (full model spend) into the same write
            # failure, then dead-letter. Instead: park the task honestly (react_to_dead_leg's
            # stuck shape — waiting(on_user) with the real cause), voice it, and let the job
            # SUCCEED. Exactly one execution; the user resumes or cancels. The leg's model spend
            # is not ledgered (no append landed) — the lesser cost vs. 3× re-spend.
            await self._park_stuck(owner, task, cause=str(exc), now=now)
            return
        except Exception as exc:
            # The leg blew up (A0 will re-deliver). Record the failure before re-raising —
            # an invisible failed run is half of why the missing record mattered.
            if record is not None:
                record.fail(str(exc))
            raise
        # The run finished (COMPLETED / CONTINUE / FAILED), or the A3 gate ended the leg with
        # no run at all — settle the durable record either way, before anything downstream
        # (metering, billing, the continuation) can raise and strand it in ``running``.
        if record is not None:
            self._settle_run_record(record, run=outcome.run, cause=_gate_reason(outcome), now=now)
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
        # Spec M3 (T4b): OWNER-bill the leg's real cost, RIDING the CAS-committed
        # append. Only the CONTINUE / COMPLETED dispositions appended a checkpoint
        # (FAILED / WAITING_APPROVAL do not; the CheckpointTooLargeError park returned
        # above) — so we bill exactly the committed-work dispositions. Keyed
        # ``{task_id}:leg:{seq}`` (the checkpoint's identity), so a re-delivered leg
        # is a no-op via ``ON CONFLICT (billing_key) DO NOTHING`` — the deduct NEVER
        # rides ``context.meter`` (which fires on every at-least-once execution).
        if accumulator is not None and outcome.disposition in (
            LegDisposition.CONTINUE,
            LegDisposition.COMPLETED,
        ):
            await self._bill_leg(owner, payload.task_id, seq, accumulator)
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

    def _open_run_record(self, owner: str, task: Task, now: datetime) -> _LegRunRecord | None:
        """Open the leg's ``runs`` row and link it to the task, before any model spend.

        Returns ``None`` when no engine is wired (a bare A2/unit handler keeps its old,
        record-free shape). The insert precedes the run deliberately: a broken
        persona/owner invariant fails here, loudly and for free, rather than after a leg's
        worth of tokens — and never by silently skipping the record.

        Raises:
            RunPersonaOwnerMismatchError: If the task's persona is not the leg owner's.
        """
        if self._rls_engine is None:
            return None
        record = _LegRunRecord(
            self._rls_engine,
            owner_id=owner,
            persona_id=task.persona_id,
            task_text=task.contract.goal,
        )
        record.open(now=now)
        self._tasks.record_run(owner, task.id, record.run_id)
        return record

    def _settle_run_record(
        self, record: _LegRunRecord, *, run: Run | None, cause: str | None, now: datetime
    ) -> None:
        """Write the leg run's terminal record — from the run, else from the stop cause."""
        if run is not None:
            record.finish(run)
        elif cause is not None:
            record.stop(reason=cause, now=now)
        else:  # pragma: no cover — a run-less leg always carries a cause
            record.fail("the leg ended without producing a run")

    async def _park_stuck(self, owner: str, task: Task, *, cause: str, now: datetime) -> None:
        """Park the task ``waiting(on_user)`` with an honest cause + voice it (R9-005).

        Mirrors the dead-leg sweep's reaction, but at the source — the job then succeeds, so
        the deterministic failure never reaches A0's retry/dead-letter machinery. With a
        continuation wired this IS :meth:`TaskContinuation.react_to_dead_leg` (idempotent
        active-only guard + StuckReport + A11 signal); the bare-handler fallback still parks
        (the state change is the floor). The voice hook is best-effort.
        """
        _log.warning(
            "checkpoint over budget after a finished run; parking task (no retry)",
            task_id=task.id,
            cause=cause,
        )
        if self._continuation is not None:
            report = self._continuation.react_to_dead_leg(owner, task.id, cause, now=now)
        else:
            self._tasks.begin_wait(owner, task.id, WaitKind.ON_USER, now=now)
            report = None
        if report is not None and self._on_task_stuck is not None:
            try:
                await self._on_task_stuck(owner, report)
            except Exception as exc:  # noqa: BLE001 — the voice is additive; the park stands
                _log.warning("stuck voicing failed task_id={tid}: {err}", tid=task.id, err=str(exc))


def _gate_reason(outcome: LegOutcome) -> str | None:
    """The stop reason for a leg that produced no run (the A3 approval gate)."""
    if outcome.disposition is not LegDisposition.WAITING_APPROVAL:
        return None
    proposal = outcome.proposal_id or "unknown"
    return f"stopped for approval (proposal {proposal})"


#: The ``task_leg`` retry policy, sized for PROVIDER CAPACITY rather than a code
#: fault (R9-092).
#:
#: The default (3 attempts, 2s base) backs off 2s then 4s and dead-letters in
#: ~6 SECONDS — hopeless against a free-tier rate limit that resets on a
#: per-minute window. Observed in production: a scheduled leg died with "every
#: backend in MultiModelChatBackend exhausted" after the two free frontier
#: models returned ``EmptyCompletionError`` and ``RateLimitError``.
#:
#: Why the tolerance belongs HERE and not in the model wrapper: that wrapper's
#: same-model retry is a 200ms sleep and it deliberately DISCARDS any
#: ``Retry-After`` hint above 2s (D-20-10). Failing fast is correct for a
#: user-facing turn — nobody waits a minute for a chat reply — and that is
#: precisely why chat kept working while scheduled legs failed on the identical
#: chain. Background work can afford to wait, so the waiting lives at the job layer.
#:
#: 30s base ⇒ 30/60/120/240s: ~7.5 minutes across 5 attempts, comfortably past a
#: per-minute limit and still bounded. Safe to retry — the leg is
#: idempotency-keyed and checkpointed, and a rate-limited call is rejected BEFORE
#: token generation, so a retried attempt costs ~nothing. A genuine bug simply
#: dead-letters later, the right trade for background work.
TASK_LEG_RETRY_POLICY: Final = RetryPolicy(
    max_attempts=5, base_backoff_seconds=30.0, max_backoff_seconds=600.0
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
    on_task_stuck: Callable[[str, StuckReport], Awaitable[None]] | None = None,
    credits_policy: CreditsPolicy | None = None,
    rls_engine: Engine | None = None,
    cost_source: CostSource | None = None,
    billing_config: BillingConfig | None = None,
    agentic_floor: int = 1,
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
                on_task_stuck=on_task_stuck,
                credits_policy=credits_policy,
                rls_engine=rls_engine,
                cost_source=cost_source,
                billing_config=billing_config,
                agentic_floor=agentic_floor,
            ),
            idempotency_key=task_leg_idempotency_key,
            retry=TASK_LEG_RETRY_POLICY,
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

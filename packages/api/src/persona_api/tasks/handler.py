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
from persona.errors import CheckpointTooLargeError, CreditsExhaustedError
from persona.jobs import LONG_LEASE, JobPayload, JobTypeSpec, RetryPolicy
from persona.logging import get_logger
from persona.tasks import (
    EventFire,
    LegBox,
    RecentLegSummary,
    ResumeTrigger,
    ScheduledFire,
    SpendKind,
    TaskState,
    WaitKind,
    bind_leg_spend_reporter,
    bound_reached,
    is_terminal,
    micros_from_cents,
    reset_leg_spend_reporter,
)
from persona_runtime.cost import compute_turn_cost
from persona_runtime.legs import (
    CompactingCheckpointWriter,
    LegDisposition,
    LegExecutor,
    evidence_from_run,
)

from persona_api.services import run_record
from persona_api.services.llm_usage_collector import collect_llm_usage
from persona_api.services.user_facing_errors import owner_on_free_plan, user_facing_error_message
from persona_api.tasks.leg_profile import leg_profile
from persona_api.tasks.leg_retrieval import LegRetrieval  # noqa: TC001 (a constructor arg)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from persona.jobs import JobContext, JobRegistry
    from persona.tasks import StuckReport, Task, TaskCheckpoint
    from persona_runtime.agentic.events import RunEvent
    from persona_runtime.agentic.run import CancelToken, Run, StepUsage
    from persona_runtime.cost import CostSource
    from persona_runtime.legs import (
        AcceptanceAssessor,
        AgenticRunner,
        CheckpointWriter,
        LegOutcome,
    )
    from sqlalchemy import Engine

    from persona_api.editions.credits_policy import CreditsPolicy
    from persona_api.jobs.queue import JobQueue
    from persona_api.services.llm_usage_collector import LLMUsageSink
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

#: How many earlier legs the reconstruction's continuity window carries, when nothing
#: configures it. Three is what ``PERSONA_TASK_RECENT_LEG_SUMMARIES`` has documented since
#: A2; until Spec W1 T12 nothing read it.
DEFAULT_RECENT_LEG_SUMMARIES = 3

#: The documented ceiling. The window sits between the contract and the live retrieval, and
#: a long tail of old leg summaries pushes both away from the model's attention.
MAX_RECENT_LEG_SUMMARIES = 5


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


def task_leg_idempotency_key(payload: TaskLegPayload, *, retry: int = 0) -> str:
    """``task:{task_id}:after:{predecessor_seq}`` — dedups duplicate ENQUEUES of one leg.

    Deterministic in ``(task_id, predecessor_seq)`` so a double-enqueue (a double fire, a
    re-scheduled continuation) collapses to one A0 job. Re-DELIVERY of the same job is
    handled by the store CAS, not this key (the two layers, like A1 over A0).

    ``retry`` (Spec W1, D-W1-20): a leg re-enqueued after the SAME head dead-lettered gets the
    suffix ``:retry:{n}``, ``n`` = the number of prior dead attempts at that head. Without it a
    pickup, or an approval answered after the park, collided with the dead row's key and was
    silently absorbed until the archive sweep freed it a day later (R9-130). A double enqueue
    of the same retry still dedups; a re-delivery still no-ops at the store CAS.

    The anchor is ``head_checkpoint_seq`` wearing a second hat (R9-173): the revival sweep
    (``persona_api.tasks.revival_sweep.RevivalSweeper._dead_cause_at_head``, via ``_head_key``)
    finds a dead leg by matching its dead job on ``task:{id}:after:{current head}``, and the
    field itself is documented on ``persona.tasks.entity.Task``. So a park or gate path that
    appends a checkpoint must re-key or re-enqueue, or the dead-job match is lost. Change the
    shape here and the sweep's prefix together, or a revival silently finds nothing.
    """
    anchor = "init" if payload.predecessor_seq is None else str(payload.predecessor_seq)
    base = f"task:{payload.task_id}:after:{anchor}"
    return base if retry <= 0 else f"{base}:retry:{retry}"


class LegRunnerBuilder(Protocol):
    """Builds the per-leg agentic runner (the composition root wires the real ``AgenticLoop``).

    MUST build the loop with ``max_steps == box.max_steps`` (the executor enforces only the
    wall-clock trip; the step bound is the loop's own — D-A2-2).
    """

    def build(
        self, task_id: str, persona_id: str, box: LegBox, *, task: Task | None = None
    ) -> AgenticRunner: ...


class RunnableGuard(Protocol):
    """The A3 kill-switch guard the handler consults (the ``KillSwitchStore`` satisfies it).

    ``is_runnable`` is the reason-scoped invariant: a task runs only when no pause source
    (terminal / budget / persona-suspend / global-pause) holds it (T11).
    """

    def is_runnable(self, owner_id: str, task: Task) -> bool: ...


class _LegCost:
    """Prices one leg's real cost, once, for the three things that need it.

    The leg's ``on_step_usage`` callback: each step's model-call usage is priced like
    a chat turn (``compute_turn_cost`` — OpenRouter ``usage.cost`` actual preferred;
    else the resolver estimate; else unpriced). The cost is SUMMED across the leg's
    steps (a leg can span tiers), and the checkpoint distiller's own model call is
    summed with them (D-W1-44): it is a call made because this leg ran, so it is part
    of what the leg cost, not a surface of its own.

    The leg's :class:`~persona.tasks.LegSpendReporter` too (:meth:`report`): the spend a
    TOOL makes inside the leg, which the loop's usage callback never sees. The hosted
    sandbox tool reports each execution it billed the owner for, at the figure it
    billed; the MCP adapter reports each external call at the value M3 rules for it.
    Bound around the run by :func:`_run_metered_leg`, so a tool dispatched anywhere
    inside the leg reaches this accumulator and a tool dispatched outside one reaches
    nothing.

    Three readers, one accumulation (the M2 one-pricing-truth rule):

    * **The owner-billed deduct** (Spec M3, T4b): :meth:`result` in cents, billed ONCE
      after the CAS append commits. On a re-delivery the leg re-runs and re-accumulates
      the SAME cost, harmlessly discarded by the billing_key idempotency gate. It is the
      MODEL cost only, deliberately: what a tool reported here has already been billed
      (the sandbox, inside the tool) or is subsumed by this very deduct's floor (an
      external call, M3 T7), so adding it here would charge the owner twice.
    * **The task ledger** (R9-161): :meth:`ledger_spend` in ledger micros, the CURRENCY
      unit the contract's ``total_budget_micros`` cap is written in, ALL THREE kinds. It
      is the executor's injected meter, so a leg accrues what it really cost instead of,
      as before, the run's raw token count. It records the PROVIDER cost, not the credit
      charge: the ledger is an accounting of what the work costs (its kinds are model /
      sandbox / external, not billing surfaces), the charge adds a markup and a per-leg
      floor that would make a task of many near-free legs look expensive against its
      bound, and a safety bound must not change meaning between the hosted and community
      editions.
    * **The per-leg spend probe** (R9-176): :meth:`spent_micros`, the running total of
      every kind, read by the box watcher at each step boundary. It is the ledger's own
      sum through the ledger's own conversion, so the bound that stops a leg and the
      figure recorded against it cannot disagree.

    All are **reads**: the distillation is priced from the live usage sink at read time,
    never folded in by a mutation, so the ledger read (inside the leg, after the distiller
    has run) and the billing read (after it) return the same figure and neither can
    double-count the other.
    """

    def __init__(self, cost_source: CostSource | None, distillation: LLMUsageSink) -> None:
        self._cost_source = cost_source
        self._distillation = distillation
        self._step_cents = 0.0
        self._basis: str | None = None
        #: What tools reported through the leg spend door, in cents, by kind. Every
        #: kind starts present at zero so the ledger is WRITTEN for each of them: a
        #: column that reads zero because a writer recorded zero is a measurement; one
        #: that reads zero because nothing ever wrote it is the defect this closes.
        self._reported: dict[SpendKind, float] = {
            SpendKind.SANDBOX: 0.0,
            SpendKind.EXTERNAL: 0.0,
        }

    def report(self, kind: SpendKind, cost_cents: float) -> None:
        """Account a tool's priced cost under ``kind`` (the ``LegSpendReporter`` port).

        ``cost_cents`` is what the tool charged, or what M3 rules the call costs where
        nothing is charged per call; it is never re-priced here. A negative figure (a
        defensive case the tools already rule out) is clamped rather than credited back
        against the cap, the same discipline as :func:`micros_from_cents`. Model spend
        does not come through this door: it arrives per step via :meth:`on_step_usage`,
        priced there, and a MODEL report would be accounted under the same column.
        """
        self._reported[kind] = self._reported.get(kind, 0.0) + max(cost_cents, 0.0)

    async def on_step_usage(self, usage: StepUsage) -> None:
        cost_cents, basis = compute_turn_cost(
            provider=usage.provider,
            model=usage.model,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            actual_cost_usd=usage.cost_usd,
            source=self._cost_source,
        )
        self._step_cents += cost_cents
        # Prefer a genuine basis over ``unpriced`` (same-provider legs share one).
        if basis != "unpriced" or self._basis is None:
            self._basis = basis

    def _distillation_cents(self) -> tuple[float, str | None]:
        """The checkpoint distiller's model call, priced like a step (D-W1-44).

        Priced through the same function as a step, but NOT fabricated as one: a
        ``StepUsage`` carries a step index, and this call belongs to no step of the run.
        Zero usage (the deterministic writer, an unmetered install, a distillation that
        never reached a model) costs nothing and contributes no basis, so a leg that did
        not distil bills exactly what it billed before.
        """
        totals = self._distillation.totals()
        if not totals.prompt_tokens and not totals.completion_tokens:
            return 0.0, None
        return compute_turn_cost(
            provider=totals.provider,
            model=totals.model,
            prompt_tokens=totals.prompt_tokens,
            completion_tokens=totals.completion_tokens,
            actual_cost_usd=totals.cost_usd,
            source=self._cost_source,
        )

    def result(self) -> tuple[float, str | None]:
        """``(total_cost_cents, cost_basis)``; basis ``None`` iff nothing priceable ran.

        The basis is the steps' own whenever they have a genuine one: the steps ARE the
        leg, and one cheap-tier distillation call should not relabel what a whole leg of
        model work was priced from. It falls back to the distillation's basis only when
        the steps have none or came back unpriced.
        """
        distilled_cents, distilled_basis = self._distillation_cents()
        steps_priced = self._basis is not None and self._basis != "unpriced"
        basis: str | None = self._basis
        if not steps_priced and distilled_basis is not None:
            basis = distilled_basis
        return self._step_cents + distilled_cents, basis

    def ledger_spend(
        self,
        run: Run,  # noqa: ARG002 (part of the meter port; see the docstring)
    ) -> Mapping[SpendKind, int]:
        """The executor's meter: this leg's priced cost in ledger micros, by kind (R9-161).

        ``run`` is part of the meter port and is deliberately unused: what a leg cost
        cannot be read off the run, because the served provider, model and response-side
        actual cost are not persisted there (they arrive through ``on_step_usage``). That
        is exactly why the old stand-in metered ``sum(step.tokens)`` and why a money cap
        ended up enforced against a token count.

        Every kind is recorded, through the one conversion. ``MODEL`` is the priced
        step usage plus the distillation; ``SANDBOX`` is what the hosted sandbox tool
        reported per execution, the same figure it billed the owner
        (``sandbox/runtime_tool.py``); ``EXTERNAL`` is what the MCP adapter reported per
        call, which under M3's T7 ruling is zero because the call's infra is subsumed by
        this leg's own credit floor. Until 2026-09-18 the last two were never written at
        all, so a task that ran the sandbox hard passed its cap invisibly.
        """
        return self._spend()

    def spent_micros(self) -> int:
        """The box watcher's probe: everything this leg has spent so far, every kind."""
        return sum(self._spend().values())

    def _spend(self) -> dict[SpendKind, int]:
        """Every kind in ledger micros, through the one conversion, at read time."""
        model_cents, _ = self.result()
        spend = {SpendKind.MODEL: micros_from_cents(model_cents)}
        for kind, cents in self._reported.items():
            spend[kind] = spend.get(kind, 0) + micros_from_cents(cents)
        return spend


async def _run_metered_leg(
    cost: _LegCost,
    executor: LegExecutor,
    *,
    task: Task,
    trigger: ResumeTrigger,
    prior_checkpoint: TaskCheckpoint | None = None,
    recent_legs: tuple[RecentLegSummary, ...] = (),
    retrieval: tuple[str, ...] = (),
    seq: int | None = None,
    box: LegBox | None = None,
    now: datetime,
) -> LegOutcome:
    """Run one leg with ``cost`` as its meter, its spend probe AND its spend reporter.

    The three are one accumulator by construction: the executor's meter is
    ``cost.ledger_spend`` (wired at construction by the caller), the box watcher's probe
    is :meth:`_LegCost.spent_micros`, the loop's usage callback is
    :meth:`_LegCost.on_step_usage`, and for the duration of the run ``cost`` is the
    ambient :class:`~persona.tasks.LegSpendReporter` every tool dispatched inside the leg
    reports to. The binding is reset on every exit, so a dispatch after the leg (or on a
    context this leg never ran in) lands on nobody's ledger. This is the ONE production
    path a leg runs through; the handler calls it and the tests drive the same function.
    """
    token = bind_leg_spend_reporter(cost)
    try:
        return await executor.run_leg(
            task=task,
            trigger=trigger,
            prior_checkpoint=prior_checkpoint,
            recent_legs=recent_legs,
            retrieval=retrieval,
            seq=seq,
            box=box,
            now=now,
            on_step_usage=cost.on_step_usage,
            # The probe and the ledger read the SAME accumulator through the same
            # conversion, so the bound that stops a leg and the figure recorded
            # against it cannot disagree (R9-176).
            spent_micros=cost.spent_micros,
        )
    finally:
        reset_leg_spend_reporter(token)


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

    def __init__(
        self, engine: Engine, *, owner_id: str, persona_id: str, task_id: str, task_text: str
    ) -> None:
        self.run_id = f"run_{uuid.uuid4().hex}"
        self._engine = engine
        self._owner = owner_id
        self._persona_id = persona_id
        self._task_id = task_id
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
            task_id=self._task_id,  # Spec W1 (D-W1-1): the run names its task
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


class _ControlledRunner:
    """Wraps the leg's :class:`AgenticRunner` so the user's controls reach a RUNNING leg.

    Spec W1 (D-W1-21, R9-129): the executor's cancel token was documented as "wired by the
    worker's cancel signal at deploy" and never was, so a cancel or a pause let the running
    leg finish its whole box. This wrapper reads the durable task row at every step boundary
    (the loop's ``thinking`` event, one indexed read per model call) and trips the token the
    executor handed the loop when the task is terminal or paused. Durable, not in-memory: it
    works whichever process pressed the control, and the loop still salvages the leg's work
    into the checkpoint (R9-109) before stopping at the next boundary.
    """

    def __init__(
        self, inner: AgenticRunner, *, tasks: TaskStore, owner_id: str, task_id: str
    ) -> None:
        self._inner = inner
        self._tasks = tasks
        self._owner = owner_id
        self._task_id = task_id
        #: True once a control (cancel / pause) tripped the leg; the handler then withholds
        #: the continuation instead of enqueueing a leg the claim would only skip.
        self.tripped = False

    async def run(
        self,
        task: str,
        *,
        on_event: Callable[[RunEvent], Awaitable[None]],
        cancel_token: CancelToken,
        on_step_usage: Callable[[StepUsage], Awaitable[None]] | None = None,
    ) -> Run:
        async def _on_event(event: RunEvent) -> None:
            await on_event(event)
            if event.type == "thinking" and not cancel_token.is_cancelled:
                current = self._tasks.get(self._owner, self._task_id)
                if is_terminal(current.state) or current.paused:
                    self.tripped = True
                    _log.info(
                        "leg stopped by a control at a step boundary",
                        task_id=self._task_id,
                        state=current.state.value,
                        paused=current.paused,
                    )
                    cancel_token.cancel()

        if on_step_usage is not None:
            return await self._inner.run(
                task, on_event=_on_event, cancel_token=cancel_token, on_step_usage=on_step_usage
            )
        return await self._inner.run(task, on_event=_on_event, cancel_token=cancel_token)


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
        leg_budget_micros: Callable[[str, Task], int] | None = None,
        on_approval_parked: Callable[[str, str], Awaitable[None]] | None = None,
        on_task_stuck: Callable[[str, StuckReport], Awaitable[None]] | None = None,
        credits_policy: CreditsPolicy | None = None,
        rls_engine: Engine | None = None,
        cost_source: CostSource | None = None,
        billing_config: BillingConfig | None = None,
        agentic_floor: int = 1,
        recent_leg_summaries: int = DEFAULT_RECENT_LEG_SUMMARIES,
        retrieval: LegRetrieval | None = None,
        acceptance: AcceptanceAssessor | None = None,
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
        # Spec W1 (T12): the two reconstruction slots that were never filled. ``recent_legs``
        # is the last-N continuity window read from the checkpoint store; ``retrieval`` is the
        # persona's own memory for the contract goal, fetched off-loop and skipped on timeout.
        # Zero summaries and a ``None`` retrieval are the pre-W1 behaviour exactly.
        self._recent_leg_summaries = max(0, min(recent_leg_summaries, MAX_RECENT_LEG_SUMMARIES))
        self._retrieval = retrieval
        # R9-164: the acceptance assessor — reads the finished leg and proposes which of the
        # contract's criteria it settled. Optional; ``None`` leaves every criterion pending,
        # which is what the system did before it existed. The core gate decides what lands.
        self._acceptance = acceptance
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
        # R9-176: the per-LEG spend bound, which had never once fired in production. The
        # box's ``budget_micros`` defaults to None and both construction sites built
        # ``LegBox()`` with no arguments, so the check at ``boxing.py:106`` was unreachable.
        # Given the task's REMAINING budget, this reads it per leg.
        #
        # Remaining, not an invented fraction. The per-task cap already says a task may not
        # spend more than X, so a leg spending past what the task has left is already
        # forbidden; this turns a bound checked BETWEEN legs into one checked DURING a leg.
        # It needs no new product number and there is no reading of it under which it is
        # wrong. A fraction would be a policy decision, and inventing one inside a safety
        # bound is how R9-161 happened.
        #
        # None → no cap plumbed (a plain A2 worker), and the box holds on steps and wall
        # clock exactly as before.
        self._leg_budget_micros = leg_budget_micros
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

    async def _bill_leg(self, owner: str, task_id: str, seq: int, cost: _LegCost) -> None:
        """Owner-bill a committed leg's real cost, CAS-ridden idempotent (Spec M3, T4b, D-M3-R5).

        Called ONLY when the leg's checkpoint CAS-append committed (disposition
        CONTINUE / COMPLETED). The deduct is keyed ``billing_key = {task_id}:leg:{seq}``
        — the SAME identity as the checkpoint — so a re-delivered leg (which re-runs
        the model, re-accumulates the same cost, and CAS-no-ops the checkpoint) hits
        the ``ON CONFLICT (billing_key) DO NOTHING`` gate and does NOT double-charge.
        Uses ``capture_up_to_idempotent`` (floored) — a completed leg captures what
        the owner can afford rather than hard-failing already-done work. Fail-soft:
        a billing error never fails the (already-committed) leg.

        Prices the leg's MODEL cost only (:meth:`_LegCost.result`). The sandbox spend
        the ledger now carries was billed by the tool itself, per execution, and an
        external call's infra is subsumed by this deduct's floor (M3 T7); either one
        added here would be a second charge for the same work.
        """
        if self._credits_policy is None or self._rls_engine is None:
            return
        cost_cents, basis = cost.result()
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

    def _recent_legs(
        self, owner: str, task_id: str, prior: TaskCheckpoint | None
    ) -> tuple[RecentLegSummary, ...]:
        """The last few legs, oldest first, as the continuity window (Spec W1, T12).

        Read from the checkpoint store, which is the only durable per-leg record there is.
        The newest checkpoint is SKIPPED: it is ``prior``, and the reconstruction already
        renders it in full as the CHECKPOINT block. Repeating it as a summary would spend
        the window on something the leg is reading anyway.

        The summary is that leg's own contribution (the last conclusion it appended) rather
        than everything known by then, and the outcome is read off the checkpoint rather
        than invented: a checkpoint that recorded what it was blocked on says so.
        """
        if self._recent_leg_summaries <= 0:
            return ()
        try:
            # One extra, because the newest is ``prior`` and is about to be dropped.
            checkpoints = self._checkpoints.list_recent(
                owner, task_id, limit=self._recent_leg_summaries + 1
            )
        except Exception as exc:  # noqa: BLE001 - context is never a precondition for work
            _log.info("recent-leg window unavailable task_id={tid}: {err}", tid=task_id, err=exc)
            return ()
        newest_seq = prior.checkpoint_seq if prior is not None else None
        window = [c for c in checkpoints if c.checkpoint_seq != newest_seq]
        summaries = [
            RecentLegSummary(
                leg_id=c.leg_id,
                summary=c.progress_conclusions[-1] if c.progress_conclusions else "(nothing new)",
                outcome=f"blocked: {c.blocked_on}" if c.blocked_on else "worked",
            )
            for c in window[: self._recent_leg_summaries]
        ]
        summaries.reverse()  # oldest first: the window reads forward, like the work did
        return tuple(summaries)

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
        # R9-108: no credits, no leg. The leg path had only a post-hoc CAPTURE
        # (``capture_up_to_idempotent``), never a pre-flight gate, so an owner at zero
        # kept running work that billed nothing: production showed 61 legs in 24h
        # against ``balance=0``, each capturing 0 and consuming provider quota anyway.
        # The chat path has gated on ``require_credits`` all along; this brings the
        # background path to the same rule.
        #
        # SKIP, do not fail: an empty balance is transient (a top-up, or the monthly
        # allowance reset), so the task stays exactly as it is and its next fire runs
        # normally once there is credit. Marking it failed would turn a billing state
        # into a lost task. Community is unaffected — ``UnlimitedCreditsPolicy``
        # returns a constant and never raises.
        if self._credits_policy is not None and self._rls_engine is not None:
            try:
                self._credits_policy.require_credits(rls_engine=self._rls_engine, user_id=owner)
            except CreditsExhaustedError:
                _log.info("task leg skipped (no credits)", task_id=task.id, state=task.state.value)
                return
        # The job firing IS the trigger arriving — resume a waiting task (one resume point).
        if task.state == TaskState.WAITING:
            task = self._tasks.resume(owner, payload.task_id, now=now)
        # Finding O: the contract's deadline and leg cap, checked at the door for a leg the
        # CLOCK or the WORLD fired. A recurring task's occurrences complete rather than
        # continue, so the boundary check below never sees a bound on one; without this a
        # task told "until Friday" would run its Saturday fire in full. A leg the PERSON
        # started (a pickup, a reply, a revival) is not the clock: it runs, once, and the
        # boundary check parks it again.
        if isinstance(payload.trigger, (ScheduledFire, EventFire)) and self._park_if_bound(
            owner, task, now=now
        ):
            return

        prior = self._checkpoints.get_latest(owner, payload.task_id)
        seq = 0 if payload.predecessor_seq is None else payload.predecessor_seq + 1
        # Spec W1 (T12): what the last few legs concluded, and what this persona already
        # knows about the goal. Both are best-effort context for the reconstruction, so both
        # degrade to empty rather than failing a leg that could otherwise run.
        recent_legs = self._recent_legs(owner, payload.task_id, prior)
        retrieval = (
            await self._retrieval.snippets(task.persona_id, task.contract.goal)
            if self._retrieval is not None
            else ()
        )

        # Spec W1 (D-W1-1): the task travels with the build so the runner can gate the
        # leg's toolbox on THIS task's category policy (A3's gate, wired here at last).
        runner = self._runner_builder.build(task.id, task.persona_id, self._box, task=task)
        # The leg's agentic run is a first-class Spec-08 run: open its ``runs`` row and link
        # it to the task BEFORE the loop starts, then snapshot progress through the wrapped
        # runner. Without this a scheduled leg spent real money and left nothing viewable.
        # ``None`` when no engine is wired (the plain A2 unit shape).
        record = self._open_run_record(owner, task, now)
        if record is not None:
            runner = record.wrap(runner)
        # Spec W1 (D-W1-21): the user's cancel / pause reaches this leg at its next boundary.
        control = _ControlledRunner(runner, tasks=self._tasks, owner_id=owner, task_id=task.id)
        runner = control
        # Spec W1 (D-W1-44): the checkpoint distiller's model call happens INSIDE the leg,
        # so it is priced with the leg rather than absorbed. It runs on the cheapest tier
        # and usually disappears into the per-leg floor, but a per-leg provider call kept
        # outside the ledger is the shape M3 exists to end. The sink is inert unless a
        # usage-collecting backend is wired (the worker root wires one for the distiller),
        # so the deterministic writer and an unmetered install record nothing here.
        with collect_llm_usage() as distillation:
            # R9-161: ONE priced cost per leg, read by two callers: the owner-billed
            # deduct below and the executor's ledger meter. It is built unconditionally,
            # because the task ledger backs the user's per-task budget cap and that safety
            # bound must not depend on whether BILLING happens to be wired: an install
            # without credits still owes the truth about what its tasks are spending.
            cost = _LegCost(self._cost_source, distillation)
            executor = LegExecutor(
                runner=runner,
                writer=self._writer,
                sink=self._checkpoints,
                meter=cost.ledger_spend,
            )
            try:
                # The sandbox tool and the MCP adapter report what they spent to
                # ``cost`` through the ambient reporter this binds, so the ledger
                # and the per-leg bound see every kind, not the model alone.
                outcome = await _run_metered_leg(
                    cost,
                    executor,
                    task=task,
                    trigger=payload.trigger,
                    prior_checkpoint=prior,
                    recent_legs=recent_legs,
                    retrieval=retrieval,
                    seq=seq,
                    box=self._leg_box(owner, task),
                    now=now,
                )
            except CheckpointTooLargeError as exc:
                # The RUN itself finished — settle its record from what the wrapped runner
                # captured, so an over-budget checkpoint never costs the run's visibility.
                if record is not None:
                    self._settle_run_record(
                        record, run=record.captured_run, cause=str(exc), now=now
                    )
                # R9-005: the run FINISHED but its checkpoint cannot land within the store's budget
                # (D-A2-1's post-compaction fail-fast). This is deterministic: re-raising
                # would burn A0's retries re-running the whole leg (full model spend) into
                # the same write
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
                    # R9-097 (remainder): the same sanitisation the chat and run paths
                    # apply. This row is shown on the task's run list, so a tier
                    # exhaustion here would print our provider names and model ids to
                    # the user exactly as it once did in chat. The unsanitised cause is
                    # preserved where it is actually needed: the exception re-raises
                    # immediately below, so A0's retry and dead-letter accounting still
                    # see the real failure.
                    record.fail(
                        user_facing_error_message(
                            exc, on_free_plan=owner_on_free_plan(self._rls_engine, owner)
                        )
                        or str(exc)
                    )
                raise
            # R9-164: which of the contract's criteria this leg settled. Inside the usage
            # block so the assessment's model call is billed WITH the leg that caused it,
            # the way the distillation already is (D-W1-44). Skipped entirely for a task
            # with no criteria, which is most ad hoc ones, so it costs nothing to have.
            await self._settle_criteria(owner, task, outcome)
        # The run finished (COMPLETED / CONTINUE / FAILED), or the A3 gate ended the leg with
        # no run at all — settle the durable record either way, before anything downstream
        # (metering, billing, the continuation) can raise and strand it in ``running``.
        if record is not None:
            self._settle_run_record(record, run=outcome.run, cause=_gate_reason(outcome), now=now)
        # A0 metering visibility (per-job spend → audit_log); the task ledger already accrued
        # via the CAS append. On a re-delivery the leg re-runs, so A0 records this execution's
        # spend (forensics) while the ledger no-ops — A0 meters executions, A2 accounts work.
        # R9-161: this is the leg's priced cost in ledger micros, the same money the ledger
        # took, not the run's token count. The token shape stays visible in ``leg_profile``
        # (``tokens_total`` / ``tokens_per_step_max``), which is where it belongs.
        # One event per spend kind, under its own name: the ledger now carries what the
        # sandbox and the external calls cost as well as the model, and A0's ``kind`` is
        # that same spend class. The model event is always recorded (it carries the leg's
        # profile); the other kinds only when the leg actually spent in them.
        leg_detail = {
            "surface": TASK_LEG_JOB_TYPE,
            "task_id": payload.task_id,
            "checkpoint_seq": str(seq),
            "disposition": outcome.disposition.value,
        }
        context.meter(
            amount_micros=outcome.spend.get(SpendKind.MODEL, 0),
            kind=SpendKind.MODEL.value,
            detail={
                **leg_detail,
                # Spec W1 (T14): the leg's measured shape, so the close-out can argue about
                # the bounds (§2.4) from what legs actually do rather than from the one
                # datapoint that arrived by accident. No bound moves in W1.
                **leg_profile(outcome),
            },
        )
        for kind in (SpendKind.SANDBOX, SpendKind.EXTERNAL):
            if outcome.spend.get(kind, 0) > 0:
                context.meter(amount_micros=outcome.spend[kind], kind=kind.value, detail=leg_detail)
        _log.info(
            "task leg ran",
            task_id=payload.task_id,
            checkpoint_seq=seq,
            disposition=outcome.disposition.value,
        )
        # Spec M3 (T4b): OWNER-bill the leg's real cost, RIDING the CAS-committed
        # append. Only the dispositions that appended a checkpoint are billed
        # (FAILED / WAITING_APPROVAL do not; the CheckpointTooLargeError park returned
        # above) — so we bill exactly the committed-work dispositions. Keyed
        # ``{task_id}:leg:{seq}`` (the checkpoint's identity), so a re-delivered leg
        # is a no-op via ``ON CONFLICT (billing_key) DO NOTHING`` — the deduct NEVER
        # rides ``context.meter`` (which fires on every at-least-once execution).
        # Spec W1 (D-W1-34): WAITING_USER belongs here. The approval park bills nothing
        # because it executed nothing; a leg that stopped on a question ran real steps,
        # appended their checkpoint, and spent real model credits doing it.
        # The distillation rides the SAME leg charge (one billing_key, one deduct), not a
        # second row: it is part of what this leg cost, not a surface of its own, so it is
        # summed inside ``_LegCost.result`` rather than added here.
        if self._billing_enabled() and outcome.disposition in (
            LegDisposition.CONTINUE,
            LegDisposition.COMPLETED,
            LegDisposition.WAITING_USER,
        ):
            await self._bill_leg(owner, payload.task_id, seq, cost)
        # Spec W1 (D-W1-21): a leg a control stopped enqueues nothing further. Its checkpoint
        # landed above (the salvage rode the CAS append) and the task row already carries the
        # user's decision; a continuation would only create a job the claim skips.
        # The leg may also have COMPLETED on the very call the cancel raced (no boundary was
        # left to trip): the durable row is the user's decision either way, and driving the
        # state machine from it (cancelled → completed) would raise, A0 would re-run the leg
        # for nothing, and the job would dead-letter. Re-read, and settle without a transition.
        settled = self._tasks.get(owner, task.id)
        if control.tripped or is_terminal(settled.state):
            _log.info(
                "leg ended under a control; no continuation",
                task_id=task.id,
                state=settled.state.value,
                tripped=control.tripped,
            )
            return
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
            # Finding O: the other two stated bounds, at the same boundary as the spend cap.
            # The leg that just ran counts; the next one is what a reached bound withholds.
            if outcome.disposition is LegDisposition.CONTINUE and self._park_if_bound(
                owner, outcome.task, now=now
            ):
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

    def _leg_box(self, owner: str, task: Task) -> LegBox:
        """This leg's bounds: the configured box, plus the task's remaining budget (R9-176).

        The per-leg spend cap existed, was tested, and could never fire: ``LegBox`` defaults
        ``budget_micros`` to ``None`` and both construction sites built ``LegBox()`` with no
        arguments, so the branch that enforces it was unreachable. This supplies the number.

        Remaining budget rather than a share of it. A leg may not spend past what its task
        has left, which the per-task cap already says; the only thing added here is that it
        is now noticed DURING the leg instead of at the boundary after the money is gone.

        A task already at or over its cap yields a zero budget, which trips the box on the
        first step. That is correct rather than harsh: the leg-boundary gate should have
        paused the task before this leg was enqueued, so arriving here at all means
        something upstream let it through, and the cheap stop is the right one.

        **When the operator ALSO configured a per-leg cap** (``PERSONA_TASK_LEG_BUDGET_MICROS``,
        carried on ``self._box``), the effective cap is the SMALLER of the two. The configured
        number is a ceiling, never a grant: the per-task budget is a promise made to the user
        and an operator convenience may not raise a leg above what that promise has left.
        In the other direction the operator wins, which is the whole point of setting it.
        """
        if self._leg_budget_micros is None:
            return self._box
        remaining = max(0, self._leg_budget_micros(owner, task))
        configured = self._box.budget_micros
        effective = remaining if configured is None else min(configured, remaining)
        return self._box.model_copy(update={"budget_micros": effective})

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
            task_id=task.id,
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

    async def _settle_criteria(self, owner: str, task: Task, outcome: LegOutcome) -> None:
        """Let the leg's work move the contract's acceptance criteria (R9-164).

        Only a leg that ran and produced a checkpoint may settle anything, and only if its
        run did not error: a gated leg executed nothing, and a failed one established
        nothing. Stated here rather than left to the assessor's own short-circuit, so the
        rule is visible at the call site that decides whether to pay for a model call.

        The assessor proposes and the core gate decides; the store applies the survivors
        inside a row lock, against the contract as it stands at the write.

        Best-effort in both directions. A criterion that does not advance stays pending and
        the next leg can claim it, so nothing is lost; and a failure here never touches the
        leg, whose real work has already landed.
        """
        if self._acceptance is None or outcome.run is None or outcome.checkpoint is None:
            return
        if outcome.disposition is LegDisposition.FAILED:
            return
        if not task.contract.acceptance_criteria:
            return
        try:
            claims = await self._acceptance.assess(contract=task.contract, run=outcome.run)
            if not claims:
                return
            rejected = self._tasks.settle_criteria(
                owner, task.id, claims, evidence_from_run(outcome.run)
            )
        except Exception as exc:  # noqa: BLE001 — additive; the leg's work already landed
            _log.warning(
                "acceptance assessment failed task_id={tid}: {err}", tid=task.id, err=str(exc)
            )
            return
        _log.info(
            "acceptance criteria assessed",
            task_id=task.id,
            claimed=len(claims),
            rejected=[f"{r.criterion_id}: {r.reason}" for r in rejected],
        )

    def _park_if_bound(self, owner: str, task: Task, *, now: datetime) -> bool:
        """Park the task if a stated contract bound is reached; return whether it was (finding O).

        ``legs_run`` is the head sequence plus one: the checkpoint chain is the durable count
        of legs that landed. Goes through the continuation's bound park (the approval park's
        shape); with no continuation wired the bare state change is the floor, as it is for
        the over-budget checkpoint.
        """
        legs_run = 0 if task.head_checkpoint_seq is None else task.head_checkpoint_seq + 1
        reason = bound_reached(task.contract.bounds, legs_run=legs_run, now=now)
        if reason is None:
            return False
        _log.info("task reached a contract bound; parking", task_id=task.id, reason=reason)
        if self._continuation is not None:
            self._continuation.park_at_bound(owner, task, reason, now=now)
        else:
            self._tasks.begin_wait(owner, task.id, WaitKind.ON_USER, now=now)
        return True

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
    leg_budget_micros: Callable[[str, Task], int] | None = None,
    on_approval_parked: Callable[[str, str], Awaitable[None]] | None = None,
    on_task_stuck: Callable[[str, StuckReport], Awaitable[None]] | None = None,
    credits_policy: CreditsPolicy | None = None,
    rls_engine: Engine | None = None,
    cost_source: CostSource | None = None,
    billing_config: BillingConfig | None = None,
    agentic_floor: int = 1,
    recent_leg_summaries: int = DEFAULT_RECENT_LEG_SUMMARIES,
    retrieval: LegRetrieval | None = None,
    acceptance: AcceptanceAssessor | None = None,
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
                leg_budget_micros=leg_budget_micros,
                on_approval_parked=on_approval_parked,
                on_task_stuck=on_task_stuck,
                credits_policy=credits_policy,
                rls_engine=rls_engine,
                cost_source=cost_source,
                billing_config=billing_config,
                agentic_floor=agentic_floor,
                recent_leg_summaries=recent_leg_summaries,
                retrieval=retrieval,
                acceptance=acceptance,
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
    retry: int = 0,
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
        idempotency_key=task_leg_idempotency_key(payload, retry=retry),
        scheduled_at=scheduled_at,
    )

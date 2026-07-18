"""The leg executor — one bounded leg = one agentic run (Spec A2, T6).

A **leg** is one execution of the unmodified Spec-06 :class:`AgenticLoop`, boxed and
book-ended by reconstruction + a checkpoint. The executor:

1. **Reconstructs** the leg's context in the fixed order (contract → checkpoint → last-N →
   retrieval → trigger → recite) and renders it into the loop's task input.
2. Runs the loop **boxed** — a :class:`~persona.tasks.LegBox` wall-clock bound trips the
   loop's ``CancelToken`` at a step boundary (via ``on_event``), never mid-step; the step
   bound is the loop's own ``max_steps`` (the runner is built with ``max_steps ==
   box.max_steps``); an external drain/cancel token rides the same mechanism, so a deploy
   degrades to *finish the box, write the checkpoint, stop*.
3. **Writes the checkpoint** (produced by a :class:`CheckpointWriter`) via a
   :class:`CheckpointSink` — in production the api's ``CheckpointStore.append`` (the atomic
   CAS write, A2-R-4); the spend is metered from the run.

The loop is composed **unmodified** (criterion 12): the box, the episodic sink, and the
reconstruction all live outside it. persona-runtime cannot import persona-api, so persistence
crosses the boundary through the :class:`CheckpointSink` port (the api leg handler — T7 —
implements it over ``CheckpointStore``).

See ``docs/specs/phase3/spec_A2/decisions.md`` (D-A2-2 boxing, D-A2-X-no-loop-mod) and
``docs/research/spec_A2.md`` §3.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from persona.errors import GatedActionProposedError
from persona.tasks import (
    LegBox,
    SpendKind,
    reconstruct_context,
)

from persona_runtime.agentic.run import CancelToken, RunStatus

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence
    from datetime import datetime

    from persona.tasks import (
        EventFire,
        EventTrigger,
        LegBoxLimit,
        RecentLegSummary,
        ScheduledFire,
        Task,
        TaskCheckpoint,
        UserReply,
    )

    from persona_runtime.agentic.events import RunEvent
    from persona_runtime.agentic.run import Run, StepUsage

__all__ = [
    "AgenticRunner",
    "BasicCheckpointWriter",
    "CheckpointSink",
    "CheckpointWriter",
    "LegDisposition",
    "LegExecutor",
    "LegOutcome",
]


class LegDisposition(StrEnum):
    """What the leg's outcome implies for the task (the next leg / state is T8/T9's call).

    ``CONTINUE`` — the leg hit its box / max-steps / was drain-cancelled: another leg
    resumes from the checkpoint. ``COMPLETED`` — the leg reached ``[FINAL]``. ``FAILED`` —
    the run errored. ``WAITING_APPROVAL`` — a gated action was proposed (A3): the leg ended
    with **no checkpoint, no execution**; the task parks ``waiting(on_user)`` until the user
    decides (A3-D-X-gate-mechanism).
    """

    CONTINUE = "continue"
    COMPLETED = "completed"
    FAILED = "failed"
    WAITING_APPROVAL = "waiting_approval"


@dataclass(frozen=True)
class LegOutcome:
    """The result of one leg — what the api handler (T7) acts on.

    Attributes:
        task: The post-append task (head advanced + ledger accrued), or the durable task
            on a re-delivery no-op / a gate (unadvanced).
        checkpoint: The checkpoint written this leg — ``None`` on a ``WAITING_APPROVAL`` gate
            (the leg ended before producing one).
        run: The agentic run (status, steps, output) — the durable run record T7 links;
            ``None`` on a ``WAITING_APPROVAL`` gate (the run raised before returning).
        disposition: What the outcome implies (continue / completed / failed / waiting_approval).
        box_limit: Which box bound tripped (``None`` if the run ended on its own).
        spend: The per-kind spend metered for this leg (accrued into the ledger). Empty on a
            gate — the partial pre-gate model spend is not ledgered (no ``Run`` to meter; the
            proposal is the value).
        resume_at: A timed-wait directive — when set, the task should go
            ``waiting(until_time)`` and the continuation is scheduled for this instant (a
            "re-check in 4h" leg). ``None`` (the v1 basic path) → an immediate continuation;
            the model-backed distiller sets it when the leg decides to wait (T11).
        proposal_id: The recorded :class:`~persona.approvals.ActionProposal` id on a
            ``WAITING_APPROVAL`` gate (the durable referent the approval flow resumes against);
            ``None`` otherwise.
    """

    task: Task
    checkpoint: TaskCheckpoint | None
    run: Run | None
    disposition: LegDisposition
    box_limit: LegBoxLimit | None
    spend: Mapping[SpendKind, int]
    resume_at: datetime | None = None
    proposal_id: str | None = None


class AgenticRunner(Protocol):
    """The Spec-06 loop's run interface (the unmodified :class:`AgenticLoop` satisfies it)."""

    async def run(
        self,
        task: str,
        *,
        on_event: Callable[[RunEvent], Awaitable[None]],
        cancel_token: CancelToken,
        on_step_usage: Callable[[StepUsage], Awaitable[None]] | None = None,
    ) -> Run: ...


class CheckpointWriter(Protocol):
    """Produces the next checkpoint from a finished run (the distillation seam).

    The v1 :class:`BasicCheckpointWriter` is a mechanical stand-in; the production writer
    distils the run + prior checkpoint into bounded conclusions (the amnesia/ossification
    quality the T11 eval gates). Either way it is a pure function of the run + prior state.
    """

    def write(
        self,
        *,
        task: Task,
        prior: TaskCheckpoint | None,
        run: Run,
        leg_id: str,
        seq: int,
        now: datetime,
    ) -> TaskCheckpoint: ...


class CheckpointSink(Protocol):
    """The durable checkpoint write (the api's ``CheckpointStore.append`` — the CAS, A2-R-4)."""

    def append(
        self,
        task: Task,
        checkpoint: TaskCheckpoint,
        *,
        spend: Mapping[SpendKind, int],
        now: datetime,
    ) -> Task: ...


class BasicCheckpointWriter:
    """A mechanical v1 :class:`CheckpointWriter` stand-in (NOT the distiller).

    Carries the prior checkpoint's intent forward and records the run's output as a new
    conclusion. It does NOT distil — over many legs its conclusions grow and will eventually
    trip the checkpoint budget (``CheckpointTooLargeError``), which is exactly the pressure
    that forces the model-backed distiller to exist (the T11 eval gates that quality). Used
    to wire the machinery end-to-end; replace at the worker root (T7) with the distiller.
    """

    def write(
        self,
        *,
        task: Task,  # noqa: ARG002 — part of the CheckpointWriter port; the distiller uses it
        prior: TaskCheckpoint | None,
        run: Run,
        leg_id: str,
        seq: int,
        now: datetime,
    ) -> TaskCheckpoint:
        from persona.tasks import TaskCheckpoint as _Checkpoint

        prior_conclusions = prior.progress_conclusions if prior is not None else ()
        new_conclusions = (*prior_conclusions, run.output) if run.output else prior_conclusions
        next_step = run.output or (prior.next_step if prior is not None else "")
        return _Checkpoint(
            task_id=task.id,
            leg_id=leg_id,
            checkpoint_seq=seq,
            progress_conclusions=new_conclusions,
            next_step=next_step,
            open_questions=prior.open_questions if prior is not None else (),
            artifact_pointers=prior.artifact_pointers if prior is not None else (),
            updated_at=now,
        )


def _default_meter(run: Run) -> dict[SpendKind, int]:
    """Stand-in meter: the run's total tokens as ``model`` micros.

    The real token→credit rate is applied at the api metering boundary (T7 injects a meter
    over A0's cost model). Kept here so the executor is self-contained + testable.
    """
    return {SpendKind.MODEL: sum(step.tokens for step in run.steps)}


class _BoxWatcher:
    """Trips the loop's ``CancelToken`` at a step boundary when the box is exhausted.

    The loop emits a ``thinking`` event at the top of every step and checks the token at the
    same boundary (``loop.py`` D-06-7), so cancelling on a ``thinking`` event stops the leg
    at the next boundary — never mid-step. Wall-clock is measured with a monotonic clock;
    the step bound is the loop's own ``max_steps`` (this is a backstop); spend is metered
    post-leg (the loop does not surface per-step tokens on the event stream).
    """

    def __init__(self, box: LegBox, token: CancelToken, *, clock: Callable[[], float]) -> None:
        self._box = box
        self._token = token
        self._clock = clock
        self._start = clock()
        self._steps = 0
        self.box_limit: LegBoxLimit | None = None

    async def on_event(self, event: RunEvent) -> None:
        if event.type != "thinking":
            return
        self._steps += 1
        elapsed = self._clock() - self._start
        limit = self._box.exhausted_by(
            steps_taken=self._steps, elapsed_seconds=elapsed, spent_micros=0
        )
        if limit is not None and not self._token.is_cancelled:
            self.box_limit = limit
            self._token.cancel()


class LegExecutor:
    """Runs one boxed leg by composing the unmodified :class:`AgenticLoop`.

    Pure dependency injection: the runner (the loop), the checkpoint writer, the durable
    sink, the meter, and the clock are all injected. The executor owns no state between legs
    — everything durable rides the checkpoint + task.
    """

    def __init__(
        self,
        *,
        runner: AgenticRunner,
        writer: CheckpointWriter,
        sink: CheckpointSink,
        meter: Callable[[Run], Mapping[SpendKind, int]] = _default_meter,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._runner = runner
        self._writer = writer
        self._sink = sink
        self._meter = meter
        self._clock = clock

    async def run_leg(
        self,
        *,
        task: Task,
        trigger: ScheduledFire | UserReply | EventTrigger | EventFire,
        prior_checkpoint: TaskCheckpoint | None = None,
        recent_legs: Sequence[RecentLegSummary] = (),
        retrieval: Sequence[str] = (),
        box: LegBox | None = None,
        seq: int | None = None,
        now: datetime,
        external_cancel: CancelToken | None = None,
        on_step_usage: Callable[[StepUsage], Awaitable[None]] | None = None,
    ) -> LegOutcome:
        """Reconstruct → run the boxed loop → write the checkpoint → return the outcome.

        Args:
            task: The current durable task (freshly fetched).
            trigger: What woke this leg (fire / reply / event).
            prior_checkpoint: The latest checkpoint, or ``None`` on the first leg.
            recent_legs: The last-N leg summaries (already bounded by the caller).
            retrieval: Live K3/memory snippets the caller fetched for this step.
            box: The leg bounds (defaults to :class:`LegBox`). The runner MUST be built with
                ``max_steps == box.max_steps`` (the executor enforces only the wall-clock
                trip; the step bound is the loop's own).
            seq: The checkpoint sequence this leg writes — the **job-fixed A2-R-4 anchor**
                (``payload.predecessor_seq + 1``). Defaults to ``task.next_checkpoint_seq``
                for the standalone case; the api handler (T7) passes the payload-fixed value
                so a re-delivery writes the SAME seq → the store CAS no-ops.
            now: The write time (injected; core stays clock-free).
            external_cancel: A drain/cancel token to compose — a deploy or a user cancel
                trips the same boundary mechanism, so the leg still checkpoints and stops.
            on_step_usage: Optional per-step billing callback (Spec M3, T4b) — forwarded
                to the loop so the api handler can meter the leg's real cost for the
                owner-billed, CAS-ridden idempotent deduct. ``None`` → no metering
                (byte-unchanged for a runner double without the kwarg).

        Returns:
            The :class:`LegOutcome` the api handler acts on.
        """
        effective_box = box if box is not None else LegBox()
        effective_seq = seq if seq is not None else task.next_checkpoint_seq
        task_str = self._render(task, trigger, prior_checkpoint, recent_legs, retrieval)
        token = external_cancel if external_cancel is not None else CancelToken()
        watcher = _BoxWatcher(effective_box, token, clock=self._clock)

        try:
            if on_step_usage is not None:
                run = await self._runner.run(
                    task_str,
                    on_event=watcher.on_event,
                    cancel_token=token,
                    on_step_usage=on_step_usage,
                )
            else:
                # No billing wired — keep the call byte-identical so a runner double
                # without ``on_step_usage`` (the A2 unit tests) is unaffected.
                run = await self._runner.run(
                    task_str, on_event=watcher.on_event, cancel_token=token
                )
        except GatedActionProposedError as exc:
            # A3 gate (A3-D-X-gate-mechanism): the PolicyGatedToolbox recorded the proposal
            # durably BEFORE raising, then the exception propagated through the unmodified loop
            # to here. The leg made no checkpoint progress and executed nothing — park the task
            # waiting(on_user) against the recorded proposal (the continuation drives the C0
            # ask, T8). No append, no spend ledgered (no Run to meter).
            return LegOutcome(
                task=task,
                checkpoint=None,
                run=None,
                disposition=LegDisposition.WAITING_APPROVAL,
                box_limit=watcher.box_limit,
                spend={},
                proposal_id=exc.context.get("proposal_id"),
            )

        leg_id = f"{task.id}:leg:{effective_seq}"
        checkpoint = self._writer.write(
            task=task, prior=prior_checkpoint, run=run, leg_id=leg_id, seq=effective_seq, now=now
        )
        spend = dict(self._meter(run))
        if run.status == RunStatus.ERROR:
            # A failed leg made no durable progress — do NOT append (advancing the head would
            # strand the task: the A0 retry would re-key to the same seq and no-op). Leave the
            # task unadvanced so the job retries cleanly; A2's failure→waiting(on_user) is T9.
            return LegOutcome(
                task=task,
                checkpoint=checkpoint,
                run=run,
                disposition=LegDisposition.FAILED,
                box_limit=watcher.box_limit,
                spend=spend,
            )
        updated = self._sink.append(task, checkpoint, spend=spend, now=now)
        return LegOutcome(
            task=updated,
            checkpoint=checkpoint,
            run=run,
            disposition=_disposition(run.status),
            box_limit=watcher.box_limit,
            spend=spend,
        )

    @staticmethod
    def _render(
        task: Task,
        trigger: ScheduledFire | UserReply | EventTrigger | EventFire,
        prior: TaskCheckpoint | None,
        recent_legs: Sequence[RecentLegSummary],
        retrieval: Sequence[str],
    ) -> str:
        blocks = reconstruct_context(
            contract=task.contract,
            trigger=trigger,
            checkpoint=prior,
            recent_legs=recent_legs,
            retrieval=retrieval,
        )
        return "\n\n".join(block.content for block in blocks)


def _disposition(status: RunStatus) -> LegDisposition:
    """Map a run status to what it implies for the task (waiting kinds are T8)."""
    if status == RunStatus.COMPLETED:
        return LegDisposition.COMPLETED
    if status == RunStatus.ERROR:
        return LegDisposition.FAILED
    # MAX_STEPS_REACHED or CANCELLED (box trip / external drain) → another leg.
    return LegDisposition.CONTINUE

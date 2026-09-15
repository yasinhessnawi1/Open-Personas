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
   CAS write, A2-R-4); the leg's spend is metered by the injected meter and accrues with it.

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
from persona.logging import get_logger
from persona.tasks import (
    LegBox,
    SpendKind,
    UserReply,
    reconstruct_context,
)

from persona_runtime.agentic.run import CancelToken, RunStatus
from persona_runtime.agentic.step import StepType

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence
    from datetime import datetime

    from persona.tasks import (
        AutoRetry,
        EventFire,
        EventTrigger,
        LegBoxLimit,
        RecentLegSummary,
        Revived,
        ScheduledFire,
        Task,
        TaskCheckpoint,
        UserDispatch,
    )

    from persona_runtime.agentic.events import RunEvent
    from persona_runtime.agentic.run import Run, StepUsage

_log = get_logger("legs.executor")

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
    decides (A3-D-X-gate-mechanism). ``WAITING_USER`` — the model asked the user a question
    (Spec W1, D-W1-34): unlike the approval park the leg DID work, so its checkpoint is
    appended with the question in ``open_questions``, and the task parks ``waiting(on_user)``
    until the reply route resumes it with the answer in the next leg's trigger.
    """

    CONTINUE = "continue"
    COMPLETED = "completed"
    FAILED = "failed"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_USER = "waiting_user"


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
        spend: The per-kind spend metered for this leg (accrued into the ledger), in ledger
            micros, the injected meter's unit, which is the CURRENCY unit the contract's
            ``total_budget_micros`` cap is written in (:func:`persona.tasks.micros_from_cents`
            is the one conversion into it). Empty on a gate: the partial pre-gate model
            spend is not ledgered (no ``Run`` to meter; the proposal is the value).
        resume_at: A timed-wait directive — when set, the task should go
            ``waiting(until_time)`` and the continuation is scheduled for this instant (a
            "re-check in 4h" leg). ``None`` (the v1 basic path) → an immediate continuation;
            the model-backed distiller sets it when the leg decides to wait (T11).
        proposal_id: The recorded :class:`~persona.approvals.ActionProposal` id on a
            ``WAITING_APPROVAL`` gate (the durable referent the approval flow resumes against);
            ``None`` otherwise.
        blocked_on: The obstacle this leg ran into, in one human sentence, when there IS one
            (R9-163). The executor only ever LEARNS it — recording it durably is the park's
            job, because the leg that gated wrote no checkpoint of its own. ``None`` on every
            ordinary outcome, and on a leg that merely ended on a question: a question is
            answered by the person reading it, not an obstacle in the world.
    """

    task: Task
    checkpoint: TaskCheckpoint | None
    run: Run | None
    disposition: LegDisposition
    box_limit: LegBoxLimit | None
    spend: Mapping[SpendKind, int]
    resume_at: datetime | None = None
    proposal_id: str | None = None
    blocked_on: str | None = None


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

    **Async since Spec W1 (D-W1-19).** The model-backed distiller needs a model call, and the
    executor that drives this seam is already async. One async protocol beats a second
    protocol plus an adapter (the drift the standards warn about) and beats hiding an await
    behind a thread, which would complicate cancellation inside the worker's drain margin.
    The deterministic writers simply gained the keyword.
    """

    async def write(
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

    async def write(
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
        from persona.tasks import merge_artifact_pointers

        from persona_runtime.legs.ledger import (
            artifacts_from_run,
            queries_from_run,
            sources_from_run,
        )

        prior_conclusions = prior.progress_conclusions if prior is not None else ()
        new_conclusions = (*prior_conclusions, run.output) if run.output else prior_conclusions
        # Spec W1 (D-W1-16): the stand-in carries the ledgers too. It does not compact them
        # (it compacts nothing, which is the whole reason it is a stand-in), but a writer
        # that silently DROPPED what earlier legs asked would make a test using it prove the
        # opposite of production.
        queries = _merge_ledger(
            prior.queries_run if prior is not None else (), queries_from_run(run)
        )
        sources = _merge_ledger(
            prior.sources_seen if prior is not None else (), sources_from_run(run)
        )
        # R9-103: EMPTY, never the leg's output — see the distiller for the full
        # reasoning. ``next_step`` is recited to the successor as ``NEXT STEP: …``, so
        # assigning ``run.output`` handed it a finished answer as an instruction and
        # closed a loop the task could never escape. This stand-in writer is likewise
        # deterministic and cannot generate a real next action; empty makes the
        # successor replan from the contract, which is the honest fallback.
        next_step = ""
        return _Checkpoint(
            task_id=task.id,
            leg_id=leg_id,
            checkpoint_seq=seq,
            progress_conclusions=new_conclusions,
            queries_run=queries,
            sources_seen=sources,
            next_step=next_step,
            open_questions=prior.open_questions if prior is not None else (),
            # R9-162: the files this leg actually wrote, merged with what earlier legs
            # produced. Every writer used to copy the prior tuple forward and nothing ever
            # put anything IN it, so the pointer half of the checkpoint carried the empty
            # tuple for the life of every task.
            artifact_pointers=merge_artifact_pointers(
                prior.artifact_pointers if prior is not None else (), artifacts_from_run(run)
            ),
            updated_at=now,
        )


def _merge_ledger(prior: Sequence[str], fresh: Sequence[str]) -> tuple[str, ...]:
    """Earlier entries first, this leg's next, each entry once (Spec W1, D-W1-16)."""
    seen: dict[str, None] = {}
    for entry in (*prior, *fresh):
        seen.setdefault(entry, None)
    return tuple(seen)


class _BoxWatcher:
    """Trips the loop's ``CancelToken`` at a step boundary when the box is exhausted.

    The loop emits a ``thinking`` event at the top of every step and checks the token at the
    same boundary (``loop.py`` D-06-7), so cancelling on a ``thinking`` event stops the leg
    at the next boundary — never mid-step. Wall-clock is measured with a monotonic clock;
    the step bound is the loop's own ``max_steps`` (this is a backstop). Spend is read
    through the injected ``spent_micros`` probe, which the caller closes over its own
    running meter: the loop does not surface per-step cost on the event stream, but the
    caller that prices ``on_step_usage`` knows the running total and can be asked for it.
    """

    def __init__(
        self,
        box: LegBox,
        token: CancelToken,
        *,
        clock: Callable[[], float],
        spent_micros: Callable[[], int] | None = None,
    ) -> None:
        self._box = box
        self._token = token
        self._clock = clock
        self._start = clock()
        self._steps = 0
        #: Reads the leg's spend SO FAR, in ledger micros. ``None`` → the leg is unpriced and
        #: the spend bound cannot be judged, so it reports zero and only steps and wall clock
        #: bound the leg. Until 2026-09-15 this was the hardcoded literal ``0`` with no way to
        #: pass anything else, which meant ``LegBox.budget_micros`` could never be reached
        #: even when set: one of the two independent reasons the per-leg spend cap had never
        #: fired in production (R9-176).
        self._spent_micros = spent_micros
        self.box_limit: LegBoxLimit | None = None

    async def on_event(self, event: RunEvent) -> None:
        if event.type != "thinking":
            return
        self._steps += 1
        elapsed = self._clock() - self._start
        limit = self._box.exhausted_by(
            steps_taken=self._steps,
            elapsed_seconds=elapsed,
            spent_micros=0 if self._spent_micros is None else self._spent_micros(),
        )
        if limit is not None and not self._token.is_cancelled:
            self.box_limit = limit
            self._token.cancel()


class LegExecutor:
    """Runs one boxed leg by composing the unmodified :class:`AgenticLoop`.

    Pure dependency injection: the runner (the loop), the checkpoint writer, the durable
    sink, the meter, and the clock are all injected. The executor owns no state between legs
    — everything durable rides the checkpoint + task.

    **The meter has no default, deliberately (R9-161).** It used to fall back to a stand-in
    that returned the run's raw token count, which then accrued into the task ledger that
    :class:`~persona_api.approvals.BudgetEnforcer` compares against a cap the user set in
    money, so "stop this task at 1500" was enforced against a token count, and did not
    mean what the surfaces said it meant. There is also no correct default available here:
    pricing a leg needs the served provider, model and response-side actual cost, and those
    reach the caller through ``on_step_usage`` precisely because they are deliberately NOT
    persisted on :class:`~persona_runtime.agentic.run.Run` (billing is the api's concern,
    not the loop's). A parameter with no correct default must not have one, so every caller
    states the unit it meters in.
    """

    def __init__(
        self,
        *,
        runner: AgenticRunner,
        writer: CheckpointWriter,
        sink: CheckpointSink,
        meter: Callable[[Run], Mapping[SpendKind, int]],
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
        trigger: ScheduledFire
        | UserReply
        | UserDispatch
        | AutoRetry
        | Revived
        | EventTrigger
        | EventFire,
        prior_checkpoint: TaskCheckpoint | None = None,
        recent_legs: Sequence[RecentLegSummary] = (),
        retrieval: Sequence[str] = (),
        box: LegBox | None = None,
        seq: int | None = None,
        now: datetime,
        external_cancel: CancelToken | None = None,
        on_step_usage: Callable[[StepUsage], Awaitable[None]] | None = None,
        spent_micros: Callable[[], int] | None = None,
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
            on_step_usage: Optional per-step usage callback (Spec M3, T4b), forwarded
                to the loop so the caller can price the leg's real cost. It feeds BOTH the
                owner-billed, CAS-ridden idempotent deduct and the injected ``meter`` whose
                figure accrues into the task ledger (R9-161): one accumulation, one pricing
                truth, two readers. ``None`` → nothing to price from (byte-unchanged for a
                runner double without the kwarg), and the meter sees no usage.

        Returns:
            The :class:`LegOutcome` the api handler acts on.
        """
        effective_box = box if box is not None else LegBox()
        effective_seq = seq if seq is not None else task.next_checkpoint_seq
        task_str = self._render(task, trigger, prior_checkpoint, recent_legs, retrieval)
        token = external_cancel if external_cancel is not None else CancelToken()
        watcher = _BoxWatcher(effective_box, token, clock=self._clock, spent_micros=spent_micros)

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
                blocked_on=_gate_obstacle(exc),
            )

        leg_id = f"{task.id}:leg:{effective_seq}"
        checkpoint = await self._writer.write(
            task=task, prior=prior_checkpoint, run=run, leg_id=leg_id, seq=effective_seq, now=now
        )
        checkpoint = _settle_open_questions(checkpoint, run, trigger)
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
        trigger: ScheduledFire
        | UserReply
        | UserDispatch
        | AutoRetry
        | Revived
        | EventTrigger
        | EventFire,
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
        # Spec W1 (T15): what this leg was actually given, by stage name and size. The
        # reconstruction itself never reaches a durable surface (the run record stores the
        # contract goal), so before this there was no way to tell from the outside whether a
        # leg retrieved anything at all: the W1 operator pass had to add it to answer its own
        # question. Names and counts only, never the content, which carries the user's work.
        _log.info(
            "leg reconstruction",
            task_id=task.id,
            stages=[block.stage.value for block in blocks],
            recent_legs=len(recent_legs),
            retrieval=len(retrieval),
            queries_known=len(prior.queries_run) if prior is not None else 0,
            sources_known=len(prior.sources_seen) if prior is not None else 0,
        )
        return "\n\n".join(block.content for block in blocks)


def _gate_obstacle(exc: GatedActionProposedError) -> str:
    """The one-sentence obstacle a gated leg hit (R9-163).

    A gated leg is blocked in the strict sense the field means: the work cannot continue
    until somebody outside the task grants something the task cannot grant itself. The
    description is the same sentence the approvals inbox shows, so the task page, the
    continuity window and the inbox all name the same pending thing.
    """
    described = exc.context.get("description") or exc.context.get("tool") or "a gated action"
    return f"Waiting for your approval: {described}"


def _asked_question(run: Run) -> str | None:
    """The question a parked run stopped on, from its own last unanswered ask-user step."""
    if run.status is not RunStatus.AWAITING_USER:
        return None
    return next(
        (
            step.question
            for step in reversed(run.steps)
            if step.type is StepType.ASK_USER and step.user_answer is None and step.question
        ),
        None,
    )


def _settle_open_questions(
    checkpoint: TaskCheckpoint,
    run: Run,
    trigger: ScheduledFire
    | UserReply
    | UserDispatch
    | AutoRetry
    | Revived
    | EventTrigger
    | EventFire,
) -> TaskCheckpoint:
    """Make ``open_questions`` say what is open NOW (Spec W1, D-W1-34 / D-W1-35).

    Two rules, and the second is the one that was missing:

    - **A parked leg's question is open.** The checkpoint is where a task's open questions
      live and what the attention surface reads to say WHY it waits and to show the question.
      A writer cannot know about the park (it summarises a finished run), so the question is
      put here from the run's own last step.
    - **A question the user ANSWERED is not open any more.** Every writer copies
      ``prior.open_questions`` forward and nothing ever removed one, so an answered question
      lived forever: the task parked on Q1, the user answered it, the persona then asked Q2
      and parked again, and the review line still showed Q1 — inviting the user to answer a
      question that was already resolved. Every later leg was also told, in its own
      reconstruction, that answered questions were still open, which invites re-asking.
      A leg resumed by a :class:`UserReply` therefore starts from NOTHING open: the reply is
      the answer to whatever stood there. Anything still genuinely unresolved comes back as
      the question this leg parks on, which is the honest way for it to reappear.

    A pickup rides the same trigger. Clearing there is self-healing: if the work is still
    blocked on the same thing, the persona asks it again and it returns to the line.
    """
    carried = () if isinstance(trigger, UserReply) else checkpoint.open_questions
    asked = _asked_question(run)
    settled = carried if asked is None or asked in carried else (*carried, asked)
    if settled == checkpoint.open_questions:
        return checkpoint
    # A checkpoint is tamper-evident: its ``content_hash`` covers the content fields and is
    # verified on construction. ``model_copy`` would change the content while keeping the old
    # hash, so the next read of the row raises. Rebuild through validation with the hash
    # cleared, which is what recomputes it.
    fields = checkpoint.model_dump()
    fields["open_questions"] = settled
    fields["content_hash"] = ""
    return type(checkpoint).model_validate(fields)


def _disposition(status: RunStatus) -> LegDisposition:
    """Map a run status to what it implies for the task."""
    if status == RunStatus.COMPLETED:
        return LegDisposition.COMPLETED
    if status == RunStatus.ERROR:
        return LegDisposition.FAILED
    if status == RunStatus.AWAITING_USER:
        # Spec W1 (D-W1-34): the leg stopped ON a question. Another leg follows, but only
        # once the user answers — the task waits on them, it does not queue work.
        return LegDisposition.WAITING_USER
    # MAX_STEPS_REACHED or CANCELLED (box trip / external drain) → another leg.
    return LegDisposition.CONTINUE

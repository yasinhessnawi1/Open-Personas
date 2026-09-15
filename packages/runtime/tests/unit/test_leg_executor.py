"""Unit tests for the leg executor (Spec A2, T6).

Drives the executor with a faithful fake runner that models the Spec-06 loop's
step-boundary cancel (check the token at the top of each step, emit ``thinking``, then the
model call). A controllable monotonic clock makes the wall-clock box trip deterministic.
Concerns: reconstruction rendering, the box wall-clock trip (at a step boundary, never
mid-step), the cooperative drain-cancel checkpoint, the disposition mapping, and the
checkpoint write via the sink with metered spend.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona.errors import GatedActionProposedError
from persona.tasks import (
    AutoRetry,
    Contract,
    LegBox,
    LegBoxLimit,
    Revived,
    ScheduledFire,
    SpendKind,
    Task,
    TaskCheckpoint,
    UserReply,
    micros_from_cents,
)
from persona_runtime.agentic.events import RunEvent
from persona_runtime.agentic.run import CancelToken, Run, RunStatus
from persona_runtime.agentic.step import Step, StepType
from persona_runtime.legs import (
    BasicCheckpointWriter,
    LegDisposition,
    LegExecutor,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

_NOW = datetime(2026, 6, 25, 9, 0, tzinfo=UTC)
_TRIGGER = UserReply(reply="yes, Tuesday works")


class _FakeClock:
    """A controllable monotonic clock (seconds)."""

    def __init__(self) -> None:
        self._t = 0.0

    def __call__(self) -> float:
        return self._t

    def advance(self, dt: float) -> None:
        self._t += dt


class _FakeRunner:
    """Mimics ``AgenticLoop.run`` — checks the token at each step boundary, advancing a clock.

    Runs up to ``max_steps`` steps; each step advances the clock by ``step_seconds`` (the
    model call). If never cancelled, ends with ``status``; if the box/external token trips,
    ends ``CANCELLED`` at the next boundary (never mid-step).
    """

    def __init__(
        self,
        *,
        max_steps: int,
        status: RunStatus,
        clock: _FakeClock,
        step_seconds: float,
        output: str | None = "done",
        tokens_per_step: int = 100,
    ) -> None:
        self._max_steps = max_steps
        self._status = status
        self._clock = clock
        self._step_seconds = step_seconds
        self._output = output
        self._tokens = tokens_per_step
        self.captured_task: str | None = None

    async def run(
        self,
        task: str,
        *,
        on_event: Callable[[RunEvent], Awaitable[None]],
        cancel_token: CancelToken,
    ) -> Run:
        self.captured_task = task
        steps: list[Step] = []
        status = self._status
        for i in range(self._max_steps):
            if cancel_token.is_cancelled:  # the loop's top-of-step boundary check (D-06-7)
                status = RunStatus.CANCELLED
                break
            await on_event(RunEvent.thinking(i))
            self._clock.advance(self._step_seconds)  # the model call takes time
            steps.append(Step(type=StepType.REASONING, content="...", tokens=self._tokens))
        output = self._output if status == RunStatus.COMPLETED else None
        return Run(
            persona_id="persona_a",
            task=task,
            status=status,
            steps=steps,
            output=output,
            started_at=_NOW,
            finished_at=_NOW,
        )


class _RecordingSink:
    """Records the append + returns the entity-advanced task (mimics CheckpointStore)."""

    def __init__(self) -> None:
        self.calls: list[tuple[TaskCheckpoint, dict[SpendKind, int]]] = []

    def append(
        self,
        task: Task,
        checkpoint: TaskCheckpoint,
        *,
        spend: Mapping[SpendKind, int],
        now: datetime,
    ) -> Task:
        self.calls.append((checkpoint, dict(spend)))
        advanced = task.advance_checkpoint(checkpoint.checkpoint_seq, now=now)
        for kind, micros in spend.items():
            advanced = advanced.record_spend(kind, micros, now=now)
        return advanced


def _task() -> Task:
    return Task(
        id="t1",
        owner_id="user_a",
        persona_id="persona_a",
        contract=Contract(goal="find the cheapest Oslo→Bergen fare", scope="under 2000kr"),
        state="active",
        created_at=_NOW,
        updated_at=_NOW,
    )


#: What one step of the fake runner costs, in cents. The executor has no default meter
#: (R9-161): a leg's cost cannot be read off the ``Run``, so every caller states the unit it
#: meters in, and these tests state a CURRENCY one exactly as the api handler does.
_CENTS_PER_STEP = 0.25


def _meter(run: Run) -> Mapping[SpendKind, int]:
    """A currency meter: the leg's priced cost, converted once into ledger micros."""
    return {SpendKind.MODEL: micros_from_cents(_CENTS_PER_STEP * len(run.steps))}


def _executor(runner, sink, clock) -> LegExecutor:  # noqa: ANN001
    return LegExecutor(
        runner=runner, writer=BasicCheckpointWriter(), sink=sink, meter=_meter, clock=clock
    )


class _GatingRunner:
    """Mimics the loop raising ``GatedActionProposedError`` on a gated tool dispatch (A3, T7).

    The PolicyGatedToolbox recorded the proposal durably before raising; the exception then
    propagates through the (unmodified) loop to ``run_leg``. We model that by raising mid-run.
    """

    def __init__(self, proposal_id: str, description: str = "") -> None:
        self._proposal_id = proposal_id
        self._description = description

    async def run(
        self,
        task: str,  # noqa: ARG002 — part of the runner contract
        *,
        on_event: Callable[[RunEvent], Awaitable[None]],
        cancel_token: CancelToken,  # noqa: ARG002 — part of the runner contract
    ) -> Run:
        await on_event(RunEvent.thinking(0))
        context = {"proposal_id": self._proposal_id, "tool": "send_email"}
        if self._description:
            context["description"] = self._description
        raise GatedActionProposedError("gated action awaiting approval", context=context)


# --- reconstruction rendering ------------------------------------------------


@pytest.mark.asyncio
async def test_reconstruction_rendered_into_loop_input() -> None:
    clock = _FakeClock()
    runner = _FakeRunner(max_steps=1, status=RunStatus.COMPLETED, clock=clock, step_seconds=1.0)
    await _executor(runner, _RecordingSink(), clock).run_leg(
        task=_task(), trigger=_TRIGGER, now=_NOW
    )
    assert runner.captured_task is not None
    assert "find the cheapest Oslo→Bergen fare" in runner.captured_task  # contract first
    assert "yes, Tuesday works" in runner.captured_task  # the trigger


@pytest.mark.asyncio
async def test_retrieval_informs_the_leg_input() -> None:
    # The K3 seam (D-A2-X-k3-seam): retrieved knowledge lands in the leg's reconstructed
    # context, so a graph fact can change the leg's behaviour (criterion 10). K3 fills the
    # enriched RetrievedContext at merge-back; A2 consumes the retrieval input here.
    clock = _FakeClock()
    runner = _FakeRunner(max_steps=1, status=RunStatus.COMPLETED, clock=clock, step_seconds=1.0)
    await _executor(runner, _RecordingSink(), clock).run_leg(
        task=_task(),
        trigger=_TRIGGER,
        retrieval=("graph: the user strongly prefers morning departures",),
        now=_NOW,
    )
    assert runner.captured_task is not None
    assert "strongly prefers morning departures" in runner.captured_task  # the leg sees it


# --- the box wall-clock trip (at a step boundary, never mid-step) ------------


@pytest.mark.asyncio
async def test_wall_clock_box_trips_at_step_boundary() -> None:
    clock = _FakeClock()
    # 100s/step, 180s box → boundaries at 0/100/200; thinking(2)@200 trips, loop stops at i=3.
    runner = _FakeRunner(max_steps=10, status=RunStatus.COMPLETED, clock=clock, step_seconds=100.0)
    sink = _RecordingSink()
    outcome = await _executor(runner, sink, clock).run_leg(
        task=_task(),
        trigger=_TRIGGER,
        box=LegBox(max_steps=10, wall_clock_seconds=180.0),
        now=_NOW,
    )
    assert outcome.box_limit == LegBoxLimit.WALL_CLOCK
    assert outcome.run.status == RunStatus.CANCELLED
    assert outcome.disposition == LegDisposition.CONTINUE
    # Tripped at the step-3 boundary → exactly 3 whole steps ran (never mid-step).
    assert len(outcome.run.steps) == 3
    # The checkpoint was still written (the cooperative box checkpoint).
    assert len(sink.calls) == 1


@pytest.mark.asyncio
async def test_within_box_runs_to_completion() -> None:
    clock = _FakeClock()
    runner = _FakeRunner(max_steps=3, status=RunStatus.COMPLETED, clock=clock, step_seconds=1.0)
    outcome = await _executor(runner, _RecordingSink(), clock).run_leg(
        task=_task(),
        trigger=_TRIGGER,
        box=LegBox(max_steps=10, wall_clock_seconds=180.0),
        now=_NOW,
    )
    assert outcome.box_limit is None
    assert outcome.disposition == LegDisposition.COMPLETED
    assert outcome.run.status == RunStatus.COMPLETED


# --- the box SPEND trip (R9-176: the bound that had never once fired) --------


@pytest.mark.asyncio
async def test_spend_box_stops_a_leg_that_runs_past_its_budget() -> None:
    """A leg whose metered spend crosses the box budget stops, mid-leg, and says why.

    This bound shipped unreachable twice over. ``LegBox.budget_micros`` defaults to ``None``
    and both production construction sites built ``LegBox()`` with no arguments, so the
    branch could not be entered; and the only thing that checked it passed a hardcoded
    ``spent_micros=0``, so even a box WITH a budget could not have tripped. Two independent
    reasons, one silent result: the per-leg money bound had never stopped a leg.

    The spend here is driven by the leg actually running, through the same probe the
    handler wires to its real meter. Nothing forces the end state.
    """
    clock = _FakeClock()
    runner = _FakeRunner(max_steps=10, status=RunStatus.COMPLETED, clock=clock, step_seconds=1.0)
    sink = _RecordingSink()
    spent = {"micros": 0}

    def probe() -> int:
        # 400 micros a step (4 cents); the box allows 1000 → the 3rd step crosses it.
        spent["micros"] += 400
        return spent["micros"]

    outcome = await _executor(runner, sink, clock).run_leg(
        task=_task(),
        trigger=_TRIGGER,
        box=LegBox(max_steps=10, wall_clock_seconds=180.0, budget_micros=1_000),
        now=_NOW,
        spent_micros=probe,
    )
    assert outcome.box_limit == LegBoxLimit.BUDGET
    assert outcome.run.status == RunStatus.CANCELLED
    assert len(outcome.run.steps) == 3  # stopped at the step that crossed, not mid-step
    assert len(sink.calls) == 1  # and it still checkpointed, like every other box trip


@pytest.mark.asyncio
async def test_no_spend_probe_leaves_the_leg_bounded_by_steps_and_clock_only() -> None:
    """Without a probe the spend bound is not judged, rather than judged as zero.

    A caller that does not price its legs (the plain A2 shape) has no honest spend figure.
    It must not therefore be treated as having spent nothing against a real cap, nor be
    stopped by one. It runs to completion on steps and wall clock.
    """
    clock = _FakeClock()
    runner = _FakeRunner(max_steps=3, status=RunStatus.COMPLETED, clock=clock, step_seconds=1.0)
    outcome = await _executor(runner, _RecordingSink(), clock).run_leg(
        task=_task(),
        trigger=_TRIGGER,
        box=LegBox(max_steps=10, wall_clock_seconds=180.0, budget_micros=1),
        now=_NOW,
    )
    assert outcome.box_limit is None
    assert outcome.disposition == LegDisposition.COMPLETED


# --- the cooperative drain-cancel checkpoint ---------------------------------


@pytest.mark.asyncio
async def test_external_drain_cancel_still_checkpoints() -> None:
    clock = _FakeClock()
    runner = _FakeRunner(max_steps=10, status=RunStatus.COMPLETED, clock=clock, step_seconds=1.0)
    sink = _RecordingSink()
    drain = CancelToken()
    drain.cancel()  # a deploy drain trips before the leg even starts a step
    outcome = await _executor(runner, sink, clock).run_leg(
        task=_task(), trigger=_TRIGGER, now=_NOW, external_cancel=drain
    )
    assert outcome.run.status == RunStatus.CANCELLED
    assert outcome.disposition == LegDisposition.CONTINUE
    assert len(sink.calls) == 1  # finished the box → wrote the checkpoint → stopped


# --- disposition mapping -----------------------------------------------------


@pytest.mark.asyncio
async def test_max_steps_continues() -> None:
    clock = _FakeClock()
    runner = _FakeRunner(
        max_steps=2, status=RunStatus.MAX_STEPS_REACHED, clock=clock, step_seconds=1.0
    )
    outcome = await _executor(runner, _RecordingSink(), clock).run_leg(
        task=_task(), trigger=_TRIGGER, now=_NOW
    )
    assert outcome.disposition == LegDisposition.CONTINUE


@pytest.mark.asyncio
async def test_error_run_fails_and_does_not_append() -> None:
    clock = _FakeClock()
    runner = _FakeRunner(max_steps=1, status=RunStatus.ERROR, clock=clock, step_seconds=1.0)
    sink = _RecordingSink()
    outcome = await _executor(runner, sink, clock).run_leg(task=_task(), trigger=_TRIGGER, now=_NOW)
    assert outcome.disposition == LegDisposition.FAILED
    # A failed leg makes no durable progress — the head stays unadvanced for a clean retry.
    assert len(sink.calls) == 0
    assert outcome.task.head_checkpoint_seq is None


# --- checkpoint write + metered spend ----------------------------------------


@pytest.mark.asyncio
async def test_checkpoint_written_with_metered_spend() -> None:
    clock = _FakeClock()
    runner = _FakeRunner(
        max_steps=3, status=RunStatus.COMPLETED, clock=clock, step_seconds=1.0, tokens_per_step=100
    )
    sink = _RecordingSink()
    outcome = await _executor(runner, sink, clock).run_leg(task=_task(), trigger=_TRIGGER, now=_NOW)
    # checkpoint claims seq 0 (first leg) and rode the sink.
    assert outcome.checkpoint.checkpoint_seq == 0
    assert len(sink.calls) == 1
    written, spend = sink.calls[0]
    assert written.checkpoint_seq == 0
    # R9-161: what rides the append is the leg's priced cost in ledger micros (3 steps ×
    # 0.25¢ = 0.75¢ = 75 micros), NOT the run's token count (3 × 100 = 300). The two are
    # different numbers on purpose: the ledger is what the per-task money cap is enforced
    # against, so a token count in that field is a bound that does not mean what it says.
    assert spend == {SpendKind.MODEL: 75}
    assert outcome.task.head_checkpoint_seq == 0
    assert outcome.task.ledger.model_micros == 75


# --- A3 gate → WAITING_APPROVAL (no append, no execution) --------------------


@pytest.mark.asyncio
async def test_gate_yields_waiting_approval_and_does_not_append() -> None:
    clock = _FakeClock()
    sink = _RecordingSink()
    outcome = await _executor(_GatingRunner("prop_abc"), sink, clock).run_leg(
        task=_task(), trigger=_TRIGGER, now=_NOW
    )
    assert outcome.disposition is LegDisposition.WAITING_APPROVAL
    assert outcome.proposal_id == "prop_abc"  # the durable referent to resume against
    # The leg made no checkpoint progress and ledgered nothing — the proposal is the value.
    assert outcome.checkpoint is None
    assert outcome.run is None
    assert outcome.spend == {}
    assert sink.calls == []  # the sink was never touched (no append, no double-write)
    assert outcome.task.head_checkpoint_seq is None  # task unadvanced


# --- R9-163: the leg LEARNS the obstacle; the park records it ----------------


@pytest.mark.asyncio
async def test_a_gated_leg_names_the_obstacle_it_hit() -> None:
    """The description the inbox shows, so the task page and the inbox name one thing."""
    outcome = await _executor(
        _GatingRunner("prop_abc", "Send an email to the landlord"),
        _RecordingSink(),
        _FakeClock(),
    ).run_leg(task=_task(), trigger=_TRIGGER, now=_NOW)
    assert outcome.blocked_on == "Waiting for your approval: Send an email to the landlord"


@pytest.mark.asyncio
async def test_a_gate_with_no_description_still_names_the_tool() -> None:
    outcome = await _executor(_GatingRunner("prop_abc"), _RecordingSink(), _FakeClock()).run_leg(
        task=_task(), trigger=_TRIGGER, now=_NOW
    )
    assert outcome.blocked_on == "Waiting for your approval: send_email"


@pytest.mark.asyncio
async def test_an_ordinary_leg_is_not_blocked() -> None:
    clock = _FakeClock()
    runner = _FakeRunner(max_steps=1, status=RunStatus.COMPLETED, clock=clock, step_seconds=1.0)
    outcome = await _executor(runner, _RecordingSink(), clock).run_leg(
        task=_task(), trigger=_TRIGGER, now=_NOW
    )
    assert outcome.blocked_on is None


class _AskingRunner:
    """Mimics a loop that stopped ON a question (Spec W1, D-W1-34).

    The leg DID work first (a reasoning step), then asked and parked, so unlike the gate
    path there is a real run to checkpoint and meter.
    """

    def __init__(self, question: str = "Which dentist, and which day?") -> None:
        self._question = question

    async def run(
        self,
        task: str,
        *,
        on_event: Callable[[RunEvent], Awaitable[None]],
        cancel_token: CancelToken,  # noqa: ARG002
    ) -> Run:
        await on_event(RunEvent.thinking(0))
        return Run(
            persona_id="persona_a",
            task=task,
            status=RunStatus.AWAITING_USER,
            steps=[
                Step(type=StepType.REASONING, content="checked the clinics", tokens=40),
                Step(type=StepType.ASK_USER, question=self._question, user_answer=None),
            ],
            output=None,
            started_at=_NOW,
            finished_at=_NOW,
        )


@pytest.mark.asyncio
async def test_a_leg_that_stopped_on_a_question_waits_on_the_user_and_appends() -> None:
    """Unlike the approval park, this leg ran real steps: its checkpoint IS appended, so the
    head advances and the next leg resumes from the work already done."""
    clock = _FakeClock()
    sink = _RecordingSink()
    outcome = await _executor(_AskingRunner(), sink, clock).run_leg(
        task=_task(),
        trigger=_TRIGGER,
        prior_checkpoint=None,
        recent_legs=(),
        retrieval=(),
        now=_NOW,
    )

    assert outcome.disposition is LegDisposition.WAITING_USER
    assert outcome.run is not None
    assert len(sink.calls) == 1  # appended, unlike WAITING_APPROVAL
    assert outcome.task.head_checkpoint_seq == 0


@pytest.mark.asyncio
async def test_the_question_lands_in_the_checkpoints_open_questions() -> None:
    """The checkpoint is where a task's open questions live, and it is what the attention
    surface reads to say WHY this waits and to show the question itself."""
    clock = _FakeClock()
    sink = _RecordingSink()
    outcome = await _executor(_AskingRunner(), sink, clock).run_leg(
        task=_task(),
        trigger=_TRIGGER,
        prior_checkpoint=None,
        recent_legs=(),
        retrieval=(),
        now=_NOW,
    )

    assert outcome.checkpoint is not None
    assert outcome.checkpoint.open_questions == ("Which dentist, and which day?",)
    assert sink.calls[0][0].open_questions == ("Which dentist, and which day?",)
    # A question is not progress: nothing is invented as a conclusion.
    assert outcome.checkpoint.progress_conclusions == ()


@pytest.mark.asyncio
async def test_a_question_is_not_duplicated_when_it_is_already_open() -> None:
    clock = _FakeClock()
    sink = _RecordingSink()
    question = "Which dentist, and which day?"
    prior = TaskCheckpoint(
        task_id="t1",
        leg_id="t1:leg:0",
        checkpoint_seq=0,
        progress_conclusions=(),
        next_step="",
        open_questions=(question,),
        updated_at=_NOW,
    )
    outcome = await _executor(_AskingRunner(question), sink, clock).run_leg(
        task=_task().advance_checkpoint(0, now=_NOW),
        trigger=_TRIGGER,
        prior_checkpoint=prior,
        recent_legs=(),
        retrieval=(),
        now=_NOW,
    )
    assert outcome.checkpoint is not None
    assert outcome.checkpoint.open_questions == (question,)


@pytest.mark.asyncio
async def test_a_run_that_did_not_park_keeps_its_checkpoint_untouched() -> None:
    clock = _FakeClock()
    sink = _RecordingSink()
    runner = _FakeRunner(max_steps=1, status=RunStatus.COMPLETED, clock=clock, step_seconds=1.0)
    outcome = await _executor(runner, sink, clock).run_leg(
        task=_task(),
        trigger=_TRIGGER,
        prior_checkpoint=None,
        recent_legs=(),
        retrieval=(),
        now=_NOW,
    )
    assert outcome.disposition is LegDisposition.COMPLETED
    assert outcome.checkpoint is not None
    assert outcome.checkpoint.open_questions == ()


@pytest.mark.asyncio
async def test_a_reply_clears_the_question_it_answered() -> None:
    """Spec W1 (D-W1-35): every writer copies ``prior.open_questions`` forward and nothing
    removed one, so an answered question lived forever and the review line kept offering it.
    A leg resumed by a reply starts from nothing open."""
    clock = _FakeClock()
    sink = _RecordingSink()
    prior = TaskCheckpoint(
        task_id="t1",
        leg_id="t1:leg:0",
        checkpoint_seq=0,
        progress_conclusions=(),
        next_step="",
        open_questions=("Which dentist, and which day?",),
        updated_at=_NOW,
    )
    runner = _FakeRunner(max_steps=1, status=RunStatus.COMPLETED, clock=clock, step_seconds=1.0)
    outcome = await _executor(runner, sink, clock).run_leg(
        task=_task().advance_checkpoint(0, now=_NOW),
        trigger=UserReply(reply="Dr Lie, Tuesday."),
        prior_checkpoint=prior,
        recent_legs=(),
        retrieval=(),
        now=_NOW,
    )
    assert outcome.checkpoint is not None
    assert outcome.checkpoint.open_questions == ()


@pytest.mark.asyncio
async def test_a_second_question_replaces_the_answered_one() -> None:
    """The shape the oracle hit: park on Q1, the user answers, the persona asks Q2 and parks
    again. What is open is Q2 alone — never both, and never the resolved Q1."""
    clock = _FakeClock()
    sink = _RecordingSink()
    prior = TaskCheckpoint(
        task_id="t1",
        leg_id="t1:leg:0",
        checkpoint_seq=0,
        progress_conclusions=(),
        next_step="",
        open_questions=("Which dentist, and which day?",),
        updated_at=_NOW,
    )
    outcome = await _executor(_AskingRunner("Morning or afternoon?"), sink, clock).run_leg(
        task=_task().advance_checkpoint(0, now=_NOW),
        trigger=UserReply(reply="Dr Lie, Tuesday."),
        prior_checkpoint=prior,
        recent_legs=(),
        retrieval=(),
        now=_NOW,
    )
    assert outcome.disposition is LegDisposition.WAITING_USER
    assert outcome.checkpoint is not None
    assert outcome.checkpoint.open_questions == ("Morning or afternoon?",)


@pytest.mark.asyncio
async def test_a_question_survives_a_leg_that_no_one_answered() -> None:
    """Only an ANSWER clears a question. A scheduled fire is not an answer, so a question
    still waiting on the user is still open when the clock wakes the task."""
    clock = _FakeClock()
    sink = _RecordingSink()
    prior = TaskCheckpoint(
        task_id="t1",
        leg_id="t1:leg:0",
        checkpoint_seq=0,
        progress_conclusions=(),
        next_step="",
        open_questions=("Which dentist, and which day?",),
        updated_at=_NOW,
    )
    runner = _FakeRunner(max_steps=1, status=RunStatus.COMPLETED, clock=clock, step_seconds=1.0)
    outcome = await _executor(runner, sink, clock).run_leg(
        task=_task().advance_checkpoint(0, now=_NOW),
        trigger=ScheduledFire(schedule_id="s1", fire_time=_NOW),
        prior_checkpoint=prior,
        recent_legs=(),
        retrieval=(),
        now=_NOW,
    )
    assert outcome.checkpoint is not None
    assert outcome.checkpoint.open_questions == ("Which dentist, and which day?",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "trigger",
    [
        AutoRetry(cause="429 rate limit", retried_at=_NOW),
        Revived(reason="nothing was running it", revived_at=_NOW),
    ],
    ids=["auto_retry", "revived"],
)
async def test_the_system_putting_work_back_does_not_answer_the_open_questions(
    trigger: AutoRetry | Revived,
) -> None:
    """Only a REPLY clears what was open (Spec W1, D-W1-35).

    The sweep putting a task back is not an answer: the question is still unanswered, and
    clearing it would drop it from the review page and tell the next leg it was settled. The
    property is asserted directly here rather than resting on the trigger's type, because a
    mutation adding an ``isinstance`` for these triggers blew up on a TYPE_CHECKING-only import
    instead of failing the assertion, which proves nothing.
    """
    clock = _FakeClock()
    sink = _RecordingSink()
    question = "Which dentist, and which day?"
    prior = TaskCheckpoint(
        task_id="t1",
        leg_id="t1:leg:0",
        checkpoint_seq=0,
        progress_conclusions=(),
        next_step="",
        open_questions=(question,),
        updated_at=_NOW,
    )
    runner = _FakeRunner(max_steps=1, status=RunStatus.COMPLETED, clock=clock, step_seconds=1.0)
    outcome = await _executor(runner, sink, clock).run_leg(
        task=_task().advance_checkpoint(0, now=_NOW),
        trigger=trigger,
        prior_checkpoint=prior,
        recent_legs=(),
        retrieval=(),
        now=_NOW,
    )
    assert outcome.checkpoint is not None
    assert outcome.checkpoint.open_questions == (question,)

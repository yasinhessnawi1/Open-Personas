"""Unit tests — the act-then-report delivery executor (Spec A5, T8).

Deterministic fakes: PROPOSE is the T9 staging no-op; ACT creates the implicit
task + run-once schedule with deterministic ids and an A5-authored contract;
replay converges (exactly-once); failure compensates the orphan schedule and
retains the hold; and the structural honesty bar — this module owns NO
user-facing surface (source-scanned: no message/notification/Originator write).
"""

# ruff: noqa: ARG002 — fakes deliberately ignore some args
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from persona.errors import ScheduleNotFoundError, TaskNotFoundError
from persona.initiative import (
    CandidateSource,
    CitationKind,
    GroundingCitation,
    InitiativeCandidate,
    InitiativeTrigger,
    PlannedStep,
    Urgency,
)
from persona.initiative.envelope import EnvelopeAction
from persona.tools.categories import ActionCategory
from persona_api.initiative.delivery import InitiativeDeliveryExecutor, implicit_task_id
from persona_api.initiative.store import NoticeDisposition, NoticeRecord

_NOW = datetime(2026, 7, 4, 6, 0, tzinfo=UTC)


def _candidate() -> InitiativeCandidate:
    return InitiativeCandidate(
        observation="The hearing is Friday and nothing is drafted.",
        citations=(GroundingCitation(kind=CitationKind.NODE, ref="node-91"),),
        trigger=InitiativeTrigger.APPROACHING_COMMITMENT,
        why_now="The date entered the horizon.",
        plan=(
            PlannedStep(
                description="draft the response letter",
                categories=frozenset({ActionCategory.DRAFT}),
            ),
        ),
        next_step="draft the response letter for you to review",
        value=0.9,
        acceptance=0.8,
        urgency=Urgency.BATCH,
        source=CandidateSource.SCAN,
        owner_id="u1",
        persona_id="p-scanner",
        prompt_version="a5-scan-v5",
        scanned_at=_NOW,
    )


def _notice(notice_id: str = "n-1", *, voicer: str = "p-voicer") -> NoticeRecord:
    candidate = _candidate()
    return NoticeRecord(
        id=notice_id,
        owner_id="u1",
        persona_id=voicer,
        opportunity_key=candidate.opportunity_key,
        trigger=candidate.trigger.value,
        source=candidate.source.value,
        disposition=NoticeDisposition.HELD,
        envelope_action=None,
        candidate=candidate,
        held_until=None,
        delivered_at=None,
        superseded_at=None,
        created_at=_NOW,
    )


class _FakeLedger:
    def __init__(self, notice: NoticeRecord | None) -> None:
        self._notice = notice

    def get_notice(self, owner_id: str, notice_id: str) -> NoticeRecord | None:
        return self._notice


class _FakeTasks:
    def __init__(self, *, raises: bool = False) -> None:
        self._raises = raises
        self.created: list[Any] = []

    def get(self, owner_id: str, task_id: str) -> Any:  # noqa: ANN401
        for task in self.created:
            if task.id == task_id:
                return task
        raise TaskNotFoundError("missing", context={"task_id": task_id})

    def create(self, task: Any) -> Any:  # noqa: ANN401
        if self._raises:
            msg = "task store down"
            raise RuntimeError(msg)
        self.created.append(task)
        return task


class _FakeSchedules:
    def __init__(self) -> None:
        self.created: list[Any] = []
        self.deleted: list[str] = []

    def get(self, owner_id: str, schedule_id: str) -> Any:  # noqa: ANN401
        for schedule in self.created:
            if schedule.id == schedule_id:
                return schedule
        raise ScheduleNotFoundError("missing", context={"schedule_id": schedule_id})

    def create(self, schedule: Any, *, now: datetime) -> Any:  # noqa: ANN401
        self.created.append(schedule)
        return schedule

    def delete(self, owner_id: str, schedule_id: str) -> None:
        self.deleted.append(schedule_id)


def _executor(
    notice: NoticeRecord | None,
    tasks: _FakeTasks | None = None,
    schedules: _FakeSchedules | None = None,
) -> tuple[InitiativeDeliveryExecutor, _FakeTasks, _FakeSchedules]:
    tasks = tasks or _FakeTasks()
    schedules = schedules or _FakeSchedules()
    executor = InitiativeDeliveryExecutor(
        ledger=_FakeLedger(notice),  # type: ignore[arg-type]
        tasks=tasks,  # type: ignore[arg-type]
        schedules=schedules,  # type: ignore[arg-type]
        timezone_for="Europe/Oslo",
    )
    return executor, tasks, schedules


def test_propose_is_the_t9_staging_noop_notice_stays_held() -> None:
    executor, tasks, schedules = _executor(_notice())
    assert asyncio.run(executor.deliver("u1", "n-1", EnvelopeAction.PROPOSE)) is False
    assert tasks.created == []
    assert schedules.created == []


def test_act_creates_the_implicit_task_and_run_once_schedule() -> None:
    executor, tasks, schedules = _executor(_notice())
    assert asyncio.run(executor.deliver("u1", "n-1", EnvelopeAction.ACT)) is True

    assert len(tasks.created) == 1
    task = tasks.created[0]
    assert task.id == implicit_task_id("n-1")  # deterministic — one notice, one task
    assert task.persona_id == "p-voicer"  # the ARBITRATED voicer works it (A5-D-6)
    assert task.contract.goal == "Draft the response letter."  # A5-authored from the candidate
    assert "node/node-91" in task.contract.scope  # the grounding rides the contract

    assert len(schedules.created) == 1
    schedule = schedules.created[0]
    assert schedule.one_time_at is not None  # run-once (Option B — never an inert row)
    assert schedule.payload_template == {"task_id": task.id}
    assert task.schedule_id == schedule.id


def test_replayed_delivery_converges_exactly_once() -> None:
    executor, tasks, _schedules = _executor(_notice())
    assert asyncio.run(executor.deliver("u1", "n-1", EnvelopeAction.ACT)) is True
    assert asyncio.run(executor.deliver("u1", "n-1", EnvelopeAction.ACT)) is True
    assert len(tasks.created) == 1  # the replay found the task and converged


def test_unknown_notice_returns_false() -> None:
    executor, tasks, _schedules = _executor(None)
    assert asyncio.run(executor.deliver("u1", "n-gone", EnvelopeAction.ACT)) is False
    assert tasks.created == []


def test_task_create_failure_compensates_the_schedule_and_retains() -> None:
    """Bar 5: a failed delivery returns False (the notice stays held) and no
    orphan schedule survives to fire a taskless job."""
    executor, tasks, schedules = _executor(_notice(), tasks=_FakeTasks(raises=True))
    assert asyncio.run(executor.deliver("u1", "n-1", EnvelopeAction.ACT)) is False
    assert schedules.deleted == [schedules.created[0].id]  # compensated


def test_no_user_facing_surface_in_the_delivery_module() -> None:
    """The structural honesty bar (T8 bar 2): A5 authors NO report — the task
    machinery voices what actually happened. Source-scanned, grep-proof style."""
    source = Path("packages/api/src/persona_api/initiative/delivery.py")
    if not source.exists():  # running from a different cwd — resolve via the module
        import persona_api.initiative.delivery as delivery_mod

        source = Path(delivery_mod.__file__)
    text = source.read_text()
    # Import/call shapes, not words: the docstring legitimately EXPLAINS that the
    # report belongs to the C0 machinery — the module must never TOUCH it.
    for forbidden in (
        "from persona.originator",
        "import Originator",
        "DeliveryRouter",
        "notifications_service",
        "insert(messages",
        "insert(notifications",
        ".originate(",
    ):
        assert forbidden not in text, f"delivery module must not touch {forbidden!r}"


# --- T9: the PROPOSE routes ------------------------------------------------------


def _sc_notice(*, rrule: str | None = "FREQ=DAILY;BYHOUR=9;BYMINUTE=0") -> NoticeRecord:
    from persona.initiative import ScheduleChange

    base = _notice("n-sc")
    candidate = base.candidate.model_copy(
        update={"schedule_change": ScheduleChange(task_id="t-target", recurrence_rrule=rrule)}
    )
    return base.model_copy(update={"candidate": candidate})


def _target_task(schedule_id: str | None = "s-target") -> Any:  # noqa: ANN401
    from persona.tasks import Contract, Task, TaskState, WaitKind

    return Task(
        id="t-target",
        owner_id="u1",
        persona_id="p-voicer",
        contract=Contract(goal="the standing check"),
        schedule_id=schedule_id,
        state=TaskState.WAITING,
        wait_kind=WaitKind.UNTIL_TIME,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _target_schedule() -> Any:  # noqa: ANN401
    from persona.schedules import RecurrenceFreq, RecurrenceRule, Schedule

    return Schedule(
        id="s-target",
        owner_id="u1",
        timezone="Europe/Oslo",
        recurrence=RecurrenceRule(freq=RecurrenceFreq.DAILY, byhour=(8,), byminute=(0,)),
        target_job_type="task_scheduled_fire",
        payload_template={"task_id": "t-target"},
        created_at=_NOW,
        updated_at=_NOW,
    )


class _RecordingSender:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(self, **kwargs: Any) -> None:  # noqa: ANN401
        self.sent.append(kwargs)


def test_schedule_change_routes_through_the_a8_door_writing_nothing() -> None:
    """T9 bar 1+2: propose_reschedule builds a PENDING proposal; the fake schedule
    store has NO edit/write surface at all — a write attempt would AttributeError,
    so returning True proves the route touched no schedule row (structural)."""
    executor, tasks, schedules = _executor(_sc_notice())
    tasks.created.append(_target_task())
    schedules.created.append(_target_schedule())
    before = list(schedules.created)

    assert asyncio.run(executor.deliver("u1", "n-sc", EnvelopeAction.PROPOSE)) is True
    assert schedules.created == before  # zero schedule writes (pending-only)
    assert len(tasks.created) == 1  # zero task creation on the propose side


def test_schedule_change_parse_honesty_refuses_bad_cadence() -> None:
    executor, tasks, schedules = _executor(_sc_notice(rrule="FREQ=WHENEVER"))
    tasks.created.append(_target_task())
    schedules.created.append(_target_schedule())
    assert asyncio.run(executor.deliver("u1", "n-sc", EnvelopeAction.PROPOSE)) is False


def test_schedule_change_missing_task_or_schedule_refuses() -> None:
    executor, _tasks, _schedules = _executor(_sc_notice())
    assert asyncio.run(executor.deliver("u1", "n-sc", EnvelopeAction.PROPOSE)) is False

    executor2, tasks2, _s2 = _executor(_sc_notice())
    tasks2.created.append(_target_task(schedule_id=None))
    assert asyncio.run(executor2.deliver("u1", "n-sc", EnvelopeAction.PROPOSE)) is False


def test_generic_proposal_sends_versioned_persona_voiced_ask() -> None:
    from persona.schema.origination import PersonaIdentityTag
    from persona_api.initiative.delivery import (
        INITIATIVE_PROPOSAL_TEMPLATE_VERSION,
        render_proposal,
    )

    sender = _RecordingSender()
    tasks = _FakeTasks()
    schedules = _FakeSchedules()
    executor = InitiativeDeliveryExecutor(
        ledger=_FakeLedger(_notice()),  # type: ignore[arg-type]
        tasks=tasks,  # type: ignore[arg-type]
        schedules=schedules,  # type: ignore[arg-type]
        proposal_sender=sender,  # type: ignore[arg-type]
        persona_tag_resolver=lambda _p: PersonaIdentityTag(
            persona_id="p-voicer", display_name="Astrid"
        ),
    )
    assert asyncio.run(executor.deliver("u1", "n-1", EnvelopeAction.PROPOSE)) is True
    assert len(sender.sent) == 1
    content = sender.sent[0]["content"]
    assert content == (  # the honest why (criterion 8), then a decline-friendly ask
        "The date entered the horizon.\n"
        "Want me to draft the response letter for you to review? Say yes and I'll get "
        "started, or tell me to leave it."
    )
    # Unconfirmed creates NOTHING (T9 bar 2): no task, no schedule.
    assert tasks.created == []
    assert schedules.created == []
    assert INITIATIVE_PROPOSAL_TEMPLATE_VERSION == "a5-propose-v2"
    assert render_proposal(_notice().candidate) == content


#: The five A5-R-1 rubric shapes (T01 to T05), written the way the v4 prompts ask: the
#: reason to "you" in one sentence, changeable facts held lightly, the offer to "you".
_RUBRIC_SHAPES = [
    (
        "T01",
        "Your library books are due back on Thursday 1 October.",
        "renew them online for you",
        "Your library books are due back on Thursday 1 October.\n"
        "Want me to renew them online for you? Say yes and I'll get started, "
        "or tell me to leave it.",
    ),
    (
        "T02",
        "Your car insurance with If renews on Sunday 4 October, so if you haven't compared "
        "offers yet, there is still time",
        "put together a shortlist of other car insurance offers for you to review",
        "Your car insurance with If renews on Sunday 4 October, so if you haven't compared "
        "offers yet, there is still time.\n"
        "Want me to put together a shortlist of other car insurance offers for you to review? "
        "Say yes and I'll get started, or tell me to leave it.",
    ),
    (
        "T03",
        "Your deposit hearing is on Friday 2 October, and the tribunal asked for a written "
        "response before it; if you haven't written it yet, there are a few days left.",
        "draft the response letter for you to look over",
        "Your deposit hearing is on Friday 2 October, and the tribunal asked for a written "
        "response before it; if you haven't written it yet, there are a few days left.\n"
        "Want me to draft the response letter for you to look over? Say yes and I'll get "
        "started, or tell me to leave it.",
    ),
    (
        "T04",
        "The Oslo Glass quote for the bathroom window, the cheapest of the three, is valid "
        "until Thursday 1 October, if it is still the one you want.",
        "remind you on Wednesday, before it lapses",
        "The Oslo Glass quote for the bathroom window, the cheapest of the three, is valid "
        "until Thursday 1 October, if it is still the one you want.\n"
        "Want me to remind you on Wednesday, before it lapses? Say yes and I'll get started, "
        "or tell me to leave it.",
    ),
    (
        "T05",
        "Your car insurance renews on Sunday 4 October; if you still want to compare offers, "
        "I can line some up.",
        "compare three other car insurance options for you",
        "Your car insurance renews on Sunday 4 October; if you still want to compare offers, "
        "I can line some up.\n"
        "Want me to compare three other car insurance options for you? Say yes and I'll get "
        "started, or tell me to leave it.",
    ),
]


@pytest.mark.parametrize(
    ("why_now", "next_step", "expected"),
    [shape[1:] for shape in _RUBRIC_SHAPES],
    ids=[shape[0] for shape in _RUBRIC_SHAPES],
)
def test_the_rubric_shapes_render_as_a_person_talking(
    why_now: str, next_step: str, expected: str
) -> None:
    """R9-256: the reason to "you", never "From your notes:" and a pasted grounding."""
    from persona_api.initiative.delivery import render_proposal

    candidate = _candidate().model_copy(
        update={
            "observation": "The user has a dated commitment (the grounding: never shown).",
            "why_now": why_now,
            "next_step": next_step,
        }
    )
    text = render_proposal(candidate)
    assert text == expected
    assert "From your notes" not in text
    assert "the user" not in text.lower()
    assert "never shown" not in text  # the grounding observation is not pasted in


def test_generic_proposal_without_sender_stays_held() -> None:
    executor, _tasks, _schedules = _executor(_notice())
    assert asyncio.run(executor.deliver("u1", "n-1", EnvelopeAction.PROPOSE)) is False


def test_generic_proposal_without_persona_tag_stays_held() -> None:
    sender = _RecordingSender()
    executor = InitiativeDeliveryExecutor(
        ledger=_FakeLedger(_notice()),  # type: ignore[arg-type]
        tasks=_FakeTasks(),  # type: ignore[arg-type]
        schedules=_FakeSchedules(),  # type: ignore[arg-type]
        proposal_sender=sender,  # type: ignore[arg-type]
        persona_tag_resolver=lambda _p: None,  # persona deleted between hold and flush
    )
    assert asyncio.run(executor.deliver("u1", "n-1", EnvelopeAction.PROPOSE)) is False
    assert sender.sent == []


@pytest.mark.parametrize(
    ("next_step", "ask"),
    [
        ("OCR the receipts for you", "Want me to OCR the receipts for you?"),  # case kept
        ("ask Mia whether Thursday works", "Want me to ask Mia whether Thursday works?"),
        ("draft it for you?", "Want me to draft it for you?"),  # no doubled "?"
        ("draft it for you!", "Want me to draft it for you?"),
        ("draft it for you.", "Want me to draft it for you?"),
    ],
)
def test_the_offer_keeps_its_case_and_loses_its_closing_punctuation(
    next_step: str, ask: str
) -> None:
    from persona_api.initiative.delivery import render_proposal

    text = render_proposal(_candidate().model_copy(update={"next_step": next_step}))
    assert text.splitlines()[1].startswith(ask + " Say yes")


def test_the_act_path_task_goal_is_the_plan_not_the_offer_to_you() -> None:
    """Re-review LOW 7: the implicit task's goal is a neutral task statement."""
    from persona_api.initiative.delivery import _build_contract

    candidate = _candidate().model_copy(
        update={
            "plan": (
                PlannedStep(
                    description="compare three insurance offers",
                    categories=frozenset({ActionCategory.COMPUTE}),
                ),
                PlannedStep(
                    description="draft a short summary.",
                    categories=frozenset({ActionCategory.DRAFT}),
                ),
            ),
            "next_step": "put together a shortlist of offers for you to review",
        }
    )
    assert _build_contract(candidate).goal == (
        "Compare three insurance offers; draft a short summary."
    )


@pytest.mark.parametrize(
    ("steps", "goal"),
    [
        (("compare offers for the user",), "Compare offers for you."),
        (("draft a reply for the user to review",), "Draft a reply for you to review."),
        (("send the user manual",), "Carry out the proposed plan."),  # cannot be voiced
    ],
)
def test_the_act_path_task_goal_passes_the_voice_guard(steps: tuple[str, ...], goal: str) -> None:
    """Third review LOW 8: the task goal is shown to the user too, so it is voiced, and a
    step that cannot be voiced leaves the neutral goal, never unvoiced text."""
    from persona_api.initiative.delivery import _build_contract

    plan = tuple(
        PlannedStep(description=d, categories=frozenset({ActionCategory.DRAFT})) for d in steps
    )
    assert _build_contract(_candidate().model_copy(update={"plan": plan})).goal == goal

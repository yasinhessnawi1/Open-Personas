"""Criterion 2 end-to-end through the REAL ``turn()`` gate (Spec A4, T6 loop wiring).

This is the anti-false-green test: it does NOT manually emit a ``task_originated`` event. It
drives a real :class:`ConversationLoop.turn()` — a standing-intent message opens the compact
echo, then a real "yes" confirm reply makes the loop emit the create event — and only then
hands that *loop-emitted* event through the worker's injection to the real
:class:`OriginationService`, which creates the task. The trigger chain
(recognize → echo → confirm → emit → worker-inject → service → task) is exercised through its
production entry point; the only test double is the *model* judge (scripted, as always).
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from persona.backends import BackendConfig
from persona.history import ConversationHistoryManager
from persona.schedules import RecurrenceFreq, RecurrenceRule
from persona.schema.conversation import Conversation
from persona.schema.persona import Persona, PersonaIdentity
from persona.skills import SkillInjector, SkillScanner
from persona.tasks import Contract, CostLedger, Task, TaskState
from persona.tools import Toolbox
from persona_api.services.origination_service import OriginationService, OriginationStatus
from persona_api.services.task_steering_service import TaskSteeringService
from persona_runtime.logging import MemoryTurnLogWriter
from persona_runtime.loop import ConversationLoop
from persona_runtime.prompt import PromptBuilder
from persona_runtime.router import Router
from persona_runtime.task_origination import (
    ContractDraft,
    ParsedSchedule,
    StandingIntentRecognizer,
    StandingJudgment,
    StandingVerdict,
    SteeringIntent,
    SteeringVerb,
)
from persona_runtime.tier import TierConfig, TierRegistry

# Reuse the runtime loop-test fakes (FakeStore + ScriptedBackend) — the established
# cross-suite harness-reuse pattern (mirrors the _eval harness imports).
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "runtime" / "tests"))
from _fakes import (  # type: ignore[import-not-found]  # noqa: E402
    FakeStore,
    ScriptedBackend,
    ScriptedRound,
)

if TYPE_CHECKING:
    from persona.schedules import Schedule

_DUMMY_CFG = BackendConfig(provider="anthropic", model="m", api_key=None)  # type: ignore[arg-type]


# --- fakes for the api create side -----------------------------------------------------


class _FakeTasks:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], Task] = {}

    def get_optional(self, owner_id: str, task_id: str) -> Task | None:
        return self.store.get((owner_id, task_id))

    def create_if_absent(self, task: Task) -> None:
        self.store.setdefault((task.owner_id, task.id), task)


class _FakeSchedules:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], Schedule] = {}

    def create_if_absent(self, schedule: Schedule, *, now: datetime) -> None:  # noqa: ARG002
        self.store.setdefault((schedule.owner_id, schedule.id), schedule)

    def delete(self, owner_id: str, schedule_id: str) -> None:
        self.store.pop((owner_id, schedule_id), None)


class _FakeNotifier:
    def __init__(self) -> None:
        self.accounts: list[object] = []

    async def notify(self, account: object, **_kw: Any) -> None:  # noqa: ANN401
        self.accounts.append(account)


# --- the scripted model judge (the only test double) -----------------------------------


class _ScriptedJudge:
    """Returns a fixed STANDING judgment with a complete draft (the model's role, scripted)."""

    def __init__(self, draft: ContractDraft) -> None:
        self._draft = draft

    async def judge(self, message: str, *, language: str) -> StandingJudgment:  # noqa: ARG002
        return StandingJudgment(verdict=StandingVerdict.STANDING, draft=self._draft)


class _ScriptedAmendment:
    """A scripted amendment interpreter: 'make it 8am' moves the schedule to 08:00."""

    async def interpret(self, reply: str, draft: ContractDraft) -> ContractDraft | None:
        if "8am" in reply.lower() and draft.schedule is not None:
            moved = draft.schedule.model_copy(
                update={
                    "recurrence": RecurrenceRule(freq=RecurrenceFreq.DAILY, byhour=(8,)),
                    "human_terms": "every day at 08:00 your time",
                }
            )
            return draft.model_copy(update={"schedule": moved})
        return None


def _standing_draft() -> ContractDraft:
    return ContractDraft(
        goal="track the Oslo→Bergen fares every morning and tell me the cheapest",
        scope="under 2000kr",
        schedule=ParsedSchedule(
            recurrence=RecurrenceRule(freq=RecurrenceFreq.DAILY, byhour=(7,)),
            timezone="Europe/Oslo",
            human_terms="every day at 07:00 your time",
        ),
    )


def _persona() -> Persona:
    return Persona(
        persona_id="astrid",
        identity=PersonaIdentity(name="Astrid", role="assistant", background="bg"),
    )


def _live_task() -> Task:
    return Task(
        id="task-fare",
        owner_id="user-1",
        persona_id="astrid",
        contract=Contract(goal="the daily Oslo→Bergen fare check"),
        state=TaskState.ACTIVE,
        head_checkpoint_seq=1,
        ledger=CostLedger(),
        created_at=datetime(2026, 7, 1, 9, 0, tzinfo=UTC),
        updated_at=datetime(2026, 7, 1, 9, 0, tzinfo=UTC),
    )


class _FakeReader:
    """Returns one active task — the introspection reader the steering gate lists over."""

    def get_task(self, task_id: str) -> Task:  # noqa: ARG002
        return _live_task()

    def get_latest_checkpoint(self, task_id: str) -> object | None:  # noqa: ARG002
        return None

    def list_active(self) -> list[Task]:
        return [_live_task()]


class _ScriptedSteering:
    """A scripted steering interpreter: maps the verb word in the reply to the live fare task."""

    async def interpret(self, message: str, active_tasks: object) -> SteeringIntent | None:  # noqa: ARG002
        low = message.lower()
        if "pause" in low:
            return SteeringIntent(verb=SteeringVerb.PAUSE, task_id="task-fare")
        if "cancel" in low:
            return SteeringIntent(verb=SteeringVerb.CANCEL, task_id="task-fare")
        return None


def _make_loop(
    *, draft: ContractDraft, with_amendment: bool = False, with_steering: bool = False
) -> ConversationLoop:
    backend = ScriptedBackend([ScriptedRound(text="ordinary reply")])
    registry = TierRegistry(
        {
            "frontier": TierConfig(name="frontier", backend_config=_DUMMY_CFG),
            "mid": TierConfig(name="mid", backend_config=_DUMMY_CFG),
            "small": TierConfig(name="small", backend_config=_DUMMY_CFG),
        }
    )
    registry._cache = {"frontier": backend, "mid": backend, "small": backend}  # type: ignore[assignment]  # noqa: SLF001
    return ConversationLoop(
        persona=_persona(),
        stores={  # type: ignore[arg-type]
            "identity": FakeStore(),
            "self_facts": FakeStore(),
            "worldview": FakeStore(),
            "episodic": FakeStore(),
        },
        toolbox=Toolbox([], allow_list=None),
        skill_scanner=SkillScanner([]),
        skill_injector=SkillInjector(),
        scanned_skills=[],
        history_manager=ConversationHistoryManager(compact_every=10, keep_recent=5),
        prompt_builder=PromptBuilder(),
        router=Router(),
        tier_registry=registry,
        turn_log_writer=MemoryTurnLogWriter(),
        standing_recognizer=StandingIntentRecognizer(_ScriptedJudge(draft)),
        amendment_interpreter=_ScriptedAmendment() if with_amendment else None,
        steering_interpreter=_ScriptedSteering() if with_steering else None,
        task_reader_provider=(lambda: _FakeReader()) if with_steering else None,  # type: ignore[arg-type,return-value]
    )


async def _drive(loop: ConversationLoop, conv: Conversation, message: str) -> list[Any]:
    """Run one real ``turn()`` and return the captured RunEvents."""
    events: list[Any] = []

    async def on_event(ev: Any) -> None:  # noqa: ANN401
        events.append(ev)

    async for _chunk in loop.turn(conv, message, on_event=on_event):
        pass
    return events


@pytest.mark.asyncio
async def test_real_turn_confirm_creates_the_task_end_to_end() -> None:
    draft = _standing_draft()
    loop = _make_loop(draft=draft)
    conv = Conversation(conversation_id="conv-1", persona_id="astrid", messages=[])

    # Turn 1 — a standing-intent message opens the contract path (REAL turn() gate). No event
    # yet, and the assistant turn is tagged as a proposal.
    echo_events = await _drive(loop, conv, "every morning, track the Oslo→Bergen fares")
    assert all(ev.type != "task_originated" for ev in echo_events)
    proposal = conv.messages[-1]
    assert proposal.role == "assistant"
    assert "contract_proposal" in proposal.metadata
    assert "Goal:" in str(proposal.content)
    assert "When:" in str(proposal.content)

    # Turn 2 — a real "yes" confirm reply makes the loop emit the create event.
    confirm_events = await _drive(loop, conv, "yes")
    originated = [ev for ev in confirm_events if ev.type == "task_originated"]
    assert len(originated) == 1, "the real confirm turn must emit exactly one create event"

    # The worker injects tenancy + the confirm turn's message id (the runtime knows neither).
    event_data = {
        **originated[0].data,
        "owner_id": "user-1",
        "assistant_message_id": "msg-confirm-1",
    }

    # The api service creates the task from the loop-emitted event.
    tasks = _FakeTasks()
    service = OriginationService(tasks=tasks, schedules=_FakeSchedules(), notifier=_FakeNotifier())
    outcome = await service.originate(event_data)

    assert outcome.status is OriginationStatus.CREATED
    assert outcome.task is not None
    # The created task carries the confirmed contract + a schedule — end to end, through turn().
    assert outcome.task.contract.goal == draft.goal
    assert outcome.task.contract.scope == "under 2000kr"
    assert outcome.task.schedule_id is not None
    assert len(tasks.store) == 1


@pytest.mark.asyncio
async def test_real_turn_non_confirm_reply_creates_nothing() -> None:
    # "yes but make it 8am" is an adjustment, not a clean confirm — no event, no task.
    loop = _make_loop(draft=_standing_draft())
    conv = Conversation(conversation_id="conv-2", persona_id="astrid", messages=[])
    await _drive(loop, conv, "every morning, track the fares")
    events = await _drive(loop, conv, "yes but make it 8am")
    assert all(ev.type != "task_originated" for ev in events)


@pytest.mark.asyncio
async def test_real_turn_now_work_message_opens_no_contract_path() -> None:
    # A now-work message has no standing cue → ordinary chat, no proposal, no event.
    loop = _make_loop(draft=_standing_draft())
    conv = Conversation(conversation_id="conv-3", persona_id="astrid", messages=[])
    events = await _drive(loop, conv, "summarise this article for me")
    assert all(ev.type != "task_originated" for ev in events)
    assert all(
        "contract_proposal" not in m.metadata for m in conv.messages if m.role == "assistant"
    )


@pytest.mark.asyncio
async def test_qualified_confirm_amends_re_echoes_and_stays_pending_until_clean_confirm() -> None:
    # The dead-end fix (A4-T9): "yes but make it 8am" must amend + re-echo, not drop the task;
    # only a subsequent CLEAN confirm creates — and it creates with the amended (8am) schedule.
    loop = _make_loop(draft=_standing_draft(), with_amendment=True)
    conv = Conversation(conversation_id="conv-4", persona_id="astrid", messages=[])

    await _drive(loop, conv, "every morning, track the fares")  # → echo (07:00)

    # The qualified confirm amends + re-echoes the schedule and STAYS pending (no event).
    amend_events = await _drive(loop, conv, "yes but make it 8am")
    assert all(ev.type != "task_originated" for ev in amend_events)
    reecho = conv.messages[-1]
    assert "contract_proposal" in reecho.metadata  # still pending — not dropped, not created
    assert "08:00" in str(reecho.content)  # the changed clause was re-echoed

    # Only now, a clean confirm creates — with the amended 08:00 schedule.
    confirm_events = await _drive(loop, conv, "yes")
    originated = [ev for ev in confirm_events if ev.type == "task_originated"]
    assert len(originated) == 1
    schedule = originated[0].data["schedule"]
    assert schedule["recurrence"]["byhour"] == [8]  # the amendment carried through to create


# --- T9b: steering a live task by saying so (through the real turn()) -------------------


@pytest.mark.asyncio
async def test_pause_applies_immediately_without_confirmation() -> None:
    loop = _make_loop(draft=_standing_draft(), with_steering=True)
    conv = Conversation(conversation_id="conv-5", persona_id="astrid", messages=[])
    events = await _drive(loop, conv, "pause the fare check")
    steering = [ev for ev in events if ev.type == "task_steering"]
    assert len(steering) == 1  # pause is reversible → no confirmation, applied immediately
    assert steering[0].data == {"verb": "pause", "task_id": "task-fare"}
    # The worker applies it through the real service over a fake mutator.
    mutator = _ScreedMutator()
    await TaskSteeringService(tasks=mutator).steer({**steering[0].data, "owner_id": "user-1"})
    assert mutator.calls == [("pause", "user-1", "task-fare")]


@pytest.mark.asyncio
async def test_cancel_requires_a_consequence_aware_confirmation() -> None:
    loop = _make_loop(draft=_standing_draft(), with_steering=True)
    conv = Conversation(conversation_id="conv-6", persona_id="astrid", messages=[])

    # "cancel the fare task" does NOT cancel yet — it asks, naming the consequence.
    ask_events = await _drive(loop, conv, "cancel the fare task")
    assert all(ev.type != "task_steering" for ev in ask_events)  # nothing cancelled yet
    prompt = conv.messages[-1]
    assert "cancel_proposal" in prompt.metadata
    assert "the daily Oslo→Bergen fare check" in str(prompt.content)  # consequence named

    # A misheard non-confirm must NOT cancel.
    no_events = await _drive(loop, conv, "no wait, keep it")
    assert all(ev.type != "task_steering" for ev in no_events)


@pytest.mark.asyncio
async def test_cancel_proceeds_only_on_a_clean_confirm() -> None:
    loop = _make_loop(draft=_standing_draft(), with_steering=True)
    conv = Conversation(conversation_id="conv-7", persona_id="astrid", messages=[])
    await _drive(loop, conv, "cancel the fare task")  # → consequence prompt
    confirm_events = await _drive(loop, conv, "yes")
    steering = [ev for ev in confirm_events if ev.type == "task_steering"]
    assert len(steering) == 1
    assert steering[0].data == {"verb": "cancel", "task_id": "task-fare"}


class _ScreedMutator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def pause(self, owner_id: str, task_id: str, *, now: object) -> object:  # noqa: ARG002
        self.calls.append(("pause", owner_id, task_id))
        return None

    def unpause(self, owner_id: str, task_id: str, *, now: object) -> object:  # noqa: ARG002
        self.calls.append(("unpause", owner_id, task_id))
        return None

    def cancel(self, owner_id: str, task_id: str, *, now: object) -> object:  # noqa: ARG002
        self.calls.append(("cancel", owner_id, task_id))
        return None

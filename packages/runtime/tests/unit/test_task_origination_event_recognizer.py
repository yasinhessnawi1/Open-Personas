"""Unit tests for the A7 event-trigger recognition path (Spec A7, T8).

Covers the three pieces: the cue net (cheap, high-recall), the model judge (conservative filter
extraction — never guesses a sender), and the recogniser composition (event branch first, falling
through to the A4 schedule path, sharing the one echo → confirm door).
"""

from __future__ import annotations

import pytest
from persona.backends.types import ChatResponse, TokenUsage
from persona.events import EventKind, MessageFilter
from persona_runtime.task_origination import (
    ContractDraft,
    EventTriggerJudgment,
    EventTriggerVerdict,
    ModelEventTriggerIntentJudge,
    RecognitionKind,
    StandingIntentRecognizer,
    StandingJudgment,
    StandingVerdict,
    build_event_clarify_question,
    detect_event_cue,
)

# --- the cue net -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "when an email from my landlord arrives, summarise it",
        "whenever I get a WhatsApp from the school, tell me",
        "let me know when a message from the bank comes in",
        "hver gang det kommer en e-post fra utleier, oppsummer den",
    ],
)
def test_event_cue_fires_on_message_watch_phrasings(message: str) -> None:
    assert detect_event_cue(message) is not None


@pytest.mark.parametrize(
    "message",
    [
        "summarise this article",
        "every morning give me a briefing",  # a clock schedule, not an event
        "what time does the landlord usually email?",  # a question
    ],
)
def test_event_cue_is_quiet_on_non_event_turns(message: str) -> None:
    assert detect_event_cue(message) is None


# --- the model judge ---------------------------------------------------------------------------


def _backend(content: str) -> object:
    class _B:
        async def chat(self, messages: object, **_: object) -> ChatResponse:  # noqa: ARG002
            return ChatResponse(
                content=content,
                usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                model="rec",
                provider="local",
                latency_ms=1.0,
            )

    return _B()


@pytest.mark.asyncio
async def test_judge_extracts_a_message_filter_from_a_clear_watch() -> None:
    content = (
        '{"verdict": "trigger", "goal": "summarise it", "platform": "email", '
        '"sender": "Landlord@Example.com", "keywords": ["rent"], '
        '"human_terms": "an email from landlord@example.com arrives"}'
    )
    judge = ModelEventTriggerIntentJudge(backend=_backend(content))  # type: ignore[arg-type]
    judgment = await judge.judge("when an email from the landlord arrives", language="en")
    assert judgment.verdict is EventTriggerVerdict.TRIGGER
    assert judgment.draft is not None
    spec = judgment.draft.trigger
    assert spec is not None
    assert spec.event_kind is EventKind.CONNECTOR_MESSAGE_RECEIVED
    assert isinstance(spec.filter, MessageFilter)
    assert spec.filter.sender == "landlord@example.com"  # email lowercased (normalised)
    assert spec.filter.platform == "email"
    assert spec.filter.keywords == ("rent",)
    assert judgment.draft.schedule is None  # schedule XOR trigger (A7-D-3)


@pytest.mark.asyncio
async def test_judge_refuses_a_filterless_watch_as_ambiguous() -> None:
    # A watch with no sender/platform/keyword would fire on EVERY message — never create it.
    content = '{"verdict": "trigger", "goal": "tell me", "human_terms": "a message arrives"}'
    judge = ModelEventTriggerIntentJudge(backend=_backend(content))  # type: ignore[arg-type]
    judgment = await judge.judge("whenever a message arrives, tell me", language="en")
    assert judgment.verdict is EventTriggerVerdict.AMBIGUOUS
    assert judgment.draft is None  # never a guessed firehose filter


@pytest.mark.asyncio
async def test_judge_not_trigger_and_ambiguous_carry_no_draft() -> None:
    not_trig = await ModelEventTriggerIntentJudge(
        backend=_backend('{"verdict": "not_trigger"}')  # type: ignore[arg-type]
    ).judge("did the bank email yet?", language="en")
    assert not_trig.verdict is EventTriggerVerdict.NOT_TRIGGER
    assert not_trig.draft is None
    unparse = await ModelEventTriggerIntentJudge(
        backend=_backend("not json at all")  # type: ignore[arg-type]
    ).judge("when …", language="en")
    assert unparse.verdict is EventTriggerVerdict.AMBIGUOUS  # any doubt → ambiguous


# --- the recogniser composition ----------------------------------------------------------------


class _ScriptedScheduleJudge:
    def __init__(self, judgment: StandingJudgment) -> None:
        self._judgment = judgment
        self.calls = 0

    async def judge(self, message: str, *, language: str) -> StandingJudgment:  # noqa: ARG002
        self.calls += 1
        return self._judgment


class _ScriptedEventJudge:
    def __init__(
        self, judgment: EventTriggerJudgment | None = None, *, raises: bool = False
    ) -> None:
        self._judgment = judgment
        self._raises = raises
        self.calls = 0

    async def judge(self, message: str, *, language: str) -> EventTriggerJudgment:  # noqa: ARG002
        self.calls += 1
        if self._raises:
            raise RuntimeError("model down")
        assert self._judgment is not None
        return self._judgment


def _trigger_judgment() -> EventTriggerJudgment:
    from persona.events import TriggerSpec

    return EventTriggerJudgment(
        verdict=EventTriggerVerdict.TRIGGER,
        draft=ContractDraft(
            goal="summarise it",
            trigger=TriggerSpec(
                event_kind=EventKind.CONNECTOR_MESSAGE_RECEIVED,
                filter=MessageFilter(sender="landlord@example.com"),
                human_terms="an email from landlord@example.com arrives",
            ),
        ),
    )


@pytest.mark.asyncio
async def test_event_watch_yields_a_standing_trigger_draft() -> None:
    sched = _ScriptedScheduleJudge(StandingJudgment(verdict=StandingVerdict.AMBIGUOUS))
    events = _ScriptedEventJudge(_trigger_judgment())
    recognizer = StandingIntentRecognizer(sched, event_judge=events)
    outcome = await recognizer.recognize(
        "when an email from the landlord arrives, summarise it", language="en"
    )
    assert outcome.kind is RecognitionKind.STANDING
    assert outcome.draft is not None
    assert outcome.draft.trigger is not None  # a trigger-bearing draft (goes through the A4 door)
    assert outcome.draft.schedule is None
    assert events.calls == 1
    assert sched.calls == 0  # the event branch owned the turn; the schedule judge was untouched


@pytest.mark.asyncio
async def test_event_ambiguous_asks_which_sender_never_guesses() -> None:
    events = _ScriptedEventJudge(EventTriggerJudgment(verdict=EventTriggerVerdict.AMBIGUOUS))
    recognizer = StandingIntentRecognizer(
        _ScriptedScheduleJudge(StandingJudgment(verdict=StandingVerdict.NOW_WORK)),
        event_judge=events,
    )
    outcome = await recognizer.recognize(
        "whenever something important arrives, tell me", language="en"
    )
    assert outcome.kind is RecognitionKind.ASK_ONCE
    assert outcome.question is not None
    assert "sender" in outcome.question.question.lower()  # asks what to watch, never a guess


@pytest.mark.asyncio
async def test_event_not_trigger_falls_through_to_the_schedule_path() -> None:
    # "every morning summarise emails from X" trips the event cue ("emails") but is clock-driven;
    # the event judge says not-trigger and the schedule path takes over.
    sched = _ScriptedScheduleJudge(
        StandingJudgment(
            verdict=StandingVerdict.STANDING, draft=ContractDraft(goal="summarise the inbox")
        )
    )
    events = _ScriptedEventJudge(EventTriggerJudgment(verdict=EventTriggerVerdict.NOT_TRIGGER))
    recognizer = StandingIntentRecognizer(sched, event_judge=events)
    outcome = await recognizer.recognize(
        "every morning, whenever emails arrive, summarise my inbox", language="en"
    )
    assert outcome.kind is RecognitionKind.STANDING
    assert outcome.draft is not None
    assert outcome.draft.trigger is None  # fell through to the schedule path
    assert events.calls == 1  # the event judge was consulted first
    assert sched.calls == 1  # then the schedule judge


@pytest.mark.asyncio
async def test_event_judge_failure_degrades_to_ask_once() -> None:
    events = _ScriptedEventJudge(raises=True)
    recognizer = StandingIntentRecognizer(
        _ScriptedScheduleJudge(StandingJudgment(verdict=StandingVerdict.NOW_WORK)),
        event_judge=events,
    )
    outcome = await recognizer.recognize(
        "when an email from X arrives, summarise it", language="en"
    )
    assert outcome.kind is RecognitionKind.ASK_ONCE  # never a silent drop, never auto-create
    assert outcome.question is not None


@pytest.mark.asyncio
async def test_no_event_judge_wired_is_inert() -> None:
    # A7 disabled ⇒ no event judge ⇒ the event branch is inert; the schedule path is unchanged.
    sched = _ScriptedScheduleJudge(StandingJudgment(verdict=StandingVerdict.NOW_WORK))
    recognizer = StandingIntentRecognizer(sched)  # no event_judge
    outcome = await recognizer.recognize(
        "when an email from X arrives, summarise it", language="en"
    )
    # No event judge, but the schedule cue doesn't fire on this phrasing → ordinary, no model call.
    assert outcome.kind is RecognitionKind.ORDINARY
    assert sched.calls == 0


def test_event_clarify_question_localises_and_falls_back() -> None:
    assert "sender" in build_event_clarify_question("en").question.lower()
    assert "avsender" in build_event_clarify_question("nb").question.lower()
    assert "sender" in build_event_clarify_question("fr").question.lower()  # fallback to EN


def test_event_judgment_validator_enforces_draft_iff_trigger() -> None:
    with pytest.raises(ValueError, match="must carry a draft"):
        EventTriggerJudgment(verdict=EventTriggerVerdict.TRIGGER)
    with pytest.raises(ValueError, match="only a TRIGGER judgment"):
        EventTriggerJudgment(verdict=EventTriggerVerdict.NOT_TRIGGER, draft=ContractDraft(goal="g"))

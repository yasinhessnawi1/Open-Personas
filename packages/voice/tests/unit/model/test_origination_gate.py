"""Unit tests for the voice task-origination gate (Spec A9, T2; A9-D-2/D-3/D-4).

Pure decision logic — the gate is exercised with a fake standing-intent judge + a fake amendment
interpreter + an injected clock. The load-bearing properties: the gate creates nothing (a STANDING
recognition only proposes; a clean confirm returns the draft, the caller enqueues); conservative
ambiguity (unclear ⇒ never a task); the voice window (next-turn-only, barge-invalidated, timeout);
one amendment round then redirect-to-chat.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from persona.schedules import RecurrenceFreq, RecurrenceRule
from persona_runtime.task_origination import (
    ContractDraft,
    ParsedSchedule,
    StandingIntentRecognizer,
    StandingJudgment,
    StandingVerdict,
)
from persona_voice.model.origination_gate import VoiceOriginationGate

pytestmark = pytest.mark.asyncio


def _schedule() -> ParsedSchedule:
    return ParsedSchedule(
        recurrence=RecurrenceRule(freq=RecurrenceFreq.DAILY),
        timezone="Europe/Oslo",
        human_terms="every morning at 08:00 your time",
    )


def _draft(goal: str = "brief me on the news") -> ContractDraft:
    return ContractDraft(goal=goal, schedule=_schedule())


class _FakeJudge:
    """A canned standing-intent judge (the model step) for the recognizer."""

    def __init__(self, judgment: StandingJudgment | Exception) -> None:
        self._judgment = judgment
        self.calls = 0

    async def judge(self, message: str, *, language: str) -> StandingJudgment:  # noqa: ARG002
        self.calls += 1
        if isinstance(self._judgment, Exception):
            raise self._judgment
        return self._judgment


class _FakeAmendment:
    """A canned amendment interpreter: returns a fixed amended draft, or None (not an amendment)."""

    def __init__(self, amended: ContractDraft | None) -> None:
        self._amended = amended
        self.calls = 0

    async def interpret(self, reply: str, draft: ContractDraft) -> ContractDraft | None:  # noqa: ARG002
        self.calls += 1
        return self._amended


class _Clock:
    """A manually-advanced UTC clock."""

    def __init__(self) -> None:
        self._now = datetime(2026, 7, 5, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


def _gate(
    *,
    verdict: StandingJudgment | Exception = StandingJudgment(
        verdict=StandingVerdict.STANDING, draft=_draft()
    ),
    amended: ContractDraft | None = None,
    clock: _Clock | None = None,
    max_rounds: int = 1,
) -> tuple[VoiceOriginationGate, _FakeJudge, _FakeAmendment, _Clock]:
    judge = _FakeJudge(verdict)
    amendment = _FakeAmendment(amended)
    the_clock = clock or _Clock()
    gate = VoiceOriginationGate(
        recognizer=StandingIntentRecognizer(judge),  # type: ignore[arg-type]
        amendment_interpreter=amendment,  # type: ignore[arg-type]
        language="en",
        clock=the_clock,
        max_amendment_rounds=max_rounds,
    )
    return gate, judge, amendment, the_clock


# --- recognition: STANDING echoes, ORDINARY falls through, ASK_ONCE asks --------------------


async def test_standing_intent_speaks_a_voice_echo_and_creates_nothing() -> None:
    gate, _, _, _ = _gate()
    decision = await gate.on_user_turn("brief me on the news every morning")
    assert decision.owns_turn
    assert decision.confirmed_draft is None  # a proposal, never a create
    assert "every morning at 08:00 your time" in (decision.spoken or "")
    assert decision.spoken is not None
    assert decision.spoken.rstrip().endswith("Say yes to confirm, or tell me what to change.")


async def test_now_work_falls_through_to_ordinary() -> None:
    gate, judge, _, _ = _gate(verdict=StandingJudgment(verdict=StandingVerdict.NOW_WORK))
    decision = await gate.on_user_turn("what's the news today")
    assert decision.spoken is None
    assert not decision.owns_turn


async def test_no_cue_takes_no_model_call() -> None:
    # "hello" trips no standing cue → the recognizer never consults the judge (the cheap path).
    gate, judge, _, _ = _gate()
    decision = await gate.on_user_turn("hello, how are you")
    assert decision.spoken is None
    assert judge.calls == 0


# --- T7: spoken steering / reschedule rides the same delegation crossing ---------------------


async def test_steering_ask_is_delegated_verbatim_without_a_model_call() -> None:
    gate, judge, _, _ = _gate()
    decision = await gate.on_user_turn("pause the fare tracker")
    assert decision.owns_turn
    assert decision.verbatim_ask == "pause the fare tracker"  # delegated verbatim
    assert decision.confirmed_draft is None  # steering carries no origination draft
    assert judge.calls == 0  # a steering cue never consults the standing-intent judge


async def test_reschedule_ask_is_delegated_verbatim() -> None:
    gate, _, _, _ = _gate()
    decision = await gate.on_user_turn("move it to 9")
    assert decision.owns_turn
    assert decision.verbatim_ask == "move it to 9"


async def test_steering_check_is_skipped_while_a_proposal_is_armed() -> None:
    # A steering-ish word during a pending origination proposal is handled by the proposal flow
    # (confirm/amend/lapse), not misrouted to the steering crossing.
    gate, _, _, _ = _gate()
    await gate.on_user_turn("brief me every morning")
    gate.note_spoken_turn_committed(truncated=False)
    confirm = await gate.on_user_turn("yes")
    assert confirm.confirmed_draft is not None  # the confirm path won, not steering


# --- T10: adversarial ambiguity + keyless/error fail-soft (criterion 4) ----------------------


@pytest.mark.parametrize(
    "reply",
    [
        "yes but make it 9 instead",  # a confirm carrying an instruction — not a clean confirm
        "yeah maybe, i'm not sure",  # hedged
        "hmm",  # non-committal
        "what does that mean",  # a question, not a confirm
        "wait, no",  # a reversal
        "yes to the first one and no to the other",  # compound / unclear
    ],
)
async def test_unclear_confirmation_never_creates_a_task(reply: str) -> None:
    # Conservative ambiguity (criterion 4): an unclear reply to a pending proposal is NEVER a task —
    # it re-echoes (amendment) or lapses to ordinary chat, but confirmed_draft stays None.
    gate, _, _, _ = _gate(amended=None)  # the interpreter says "not a clean amendment"
    await gate.on_user_turn("brief me every morning")
    gate.note_spoken_turn_committed(truncated=False)
    decision = await gate.on_user_turn(reply)
    assert decision.confirmed_draft is None  # a mishearable reply can never mint a task


async def test_keyless_or_erroring_judge_degrades_to_ask_once_not_a_task() -> None:
    # A keyless / over-budget / erroring judge (the model call fails) degrades to a clarifying
    # question — never a silent drop, never an unconfirmed task (fail-soft, criterion 7).
    gate, _, _, _ = _gate(verdict=RuntimeError("no API key configured"))
    decision = await gate.on_user_turn("remind me every morning to stretch")
    assert decision.owns_turn
    assert decision.confirmed_draft is None
    assert decision.verbatim_ask is None  # nothing delegated on a recognition failure
    assert "?" in (decision.spoken or "")


async def test_ambiguous_recognition_asks_once_never_a_task() -> None:
    gate, _, _, _ = _gate(verdict=StandingJudgment(verdict=StandingVerdict.AMBIGUOUS))
    decision = await gate.on_user_turn("keep an eye on the news")
    assert decision.owns_turn
    assert decision.confirmed_draft is None  # never a task on ambiguity (criterion 4)
    assert "?" in (decision.spoken or "")


async def test_judge_failure_degrades_to_ask_once_never_creates() -> None:
    gate, _, _, _ = _gate(verdict=RuntimeError("model down"))
    decision = await gate.on_user_turn("remind me every day to stretch")
    assert decision.owns_turn
    assert decision.confirmed_draft is None
    assert "?" in (decision.spoken or "")


# --- confirm: only a clean confirm on the immediately-following turn creates -----------------


async def test_clean_confirm_after_committed_echo_returns_draft_and_verbatim_ask() -> None:
    gate, _, _, _ = _gate()
    echo = await gate.on_user_turn("brief me every morning")
    assert echo.owns_turn
    gate.note_spoken_turn_committed(truncated=False)  # the echo was heard in full
    confirm = await gate.on_user_turn("yes")
    assert confirm.confirmed_draft is not None
    assert confirm.confirmed_draft.goal == "brief me on the news"
    # The VERBATIM ask is what gets delegated (the frontier re-parses it — A9-D-5).
    assert confirm.verbatim_ask == "brief me every morning"
    # On confirm voice says it is PREPARING in the background — never a "done" (A9-D-6/D-7).
    assert "background" in (confirm.spoken or "").lower()


async def test_confirm_before_echo_committed_does_not_create() -> None:
    # Defensive: without a committed (armed) echo there is no confirmable proposal.
    gate, _, _, _ = _gate()
    await gate.on_user_turn("brief me every morning")  # echo spoken, NOT yet committed
    # A "yes" now finds no armed proposal → it is recognized fresh ("yes" trips no cue) → ordinary.
    decision = await gate.on_user_turn("yes")
    assert decision.confirmed_draft is None


async def test_non_confirm_reply_lapses_the_proposal_no_task() -> None:
    gate, _, amendment, _ = _gate(amended=None)  # interpreter says "not an amendment"
    await gate.on_user_turn("brief me every morning")
    gate.note_spoken_turn_committed(truncated=False)
    decision = await gate.on_user_turn("actually never mind, what's the weather")
    assert decision.confirmed_draft is None
    assert decision.spoken is None  # lapses → falls through to ordinary chat
    # A subsequent "yes" no longer confirms anything (the proposal is gone).
    after = await gate.on_user_turn("yes")
    assert after.confirmed_draft is None


# --- the voice window: barge-invalidation + timeout (A9-D-3) --------------------------------


async def test_barged_echo_is_not_confirmable() -> None:
    gate, _, _, _ = _gate()
    await gate.on_user_turn("brief me every morning")
    gate.note_spoken_turn_committed(truncated=True)  # the user cut the echo off
    decision = await gate.on_user_turn("yes")
    assert decision.confirmed_draft is None  # never heard the full contract → cannot confirm


async def test_proposal_lapses_after_timeout() -> None:
    clock = _Clock()
    gate, _, _, _ = _gate(clock=clock)
    await gate.on_user_turn("brief me every morning")
    gate.note_spoken_turn_committed(truncated=False)
    clock.advance(120.0)  # past the 90s window
    decision = await gate.on_user_turn("yes")
    assert decision.confirmed_draft is None


# --- amendment: one round, then redirect to chat (A9-D-4) -----------------------------------


async def test_one_amendment_round_re_echoes_and_re_arms() -> None:
    amended = _draft("brief me on the news and the weather")
    gate, _, amendment, _ = _gate(amended=amended)
    await gate.on_user_turn("brief me every morning")
    gate.note_spoken_turn_committed(truncated=False)
    reecho = await gate.on_user_turn("also include the weather")
    assert reecho.owns_turn
    assert reecho.confirmed_draft is None  # an amendment stays pending, never creates
    assert amendment.calls == 1
    # The re-echo must be re-armed on commit, then a clean confirm creates the AMENDED draft.
    gate.note_spoken_turn_committed(truncated=False)
    confirm = await gate.on_user_turn("yes")
    assert confirm.confirmed_draft is not None
    assert confirm.confirmed_draft.goal == "brief me on the news and the weather"
    # The delegated ask stays verbatim: original + the amendment utterance (frontier re-parses it).
    assert confirm.verbatim_ask == "brief me every morning also include the weather"


async def test_second_amendment_redirects_to_chat() -> None:
    amended = _draft("brief me on the news and the weather")
    gate, _, _, _ = _gate(amended=amended, max_rounds=1)
    await gate.on_user_turn("brief me every morning")
    gate.note_spoken_turn_committed(truncated=False)
    await gate.on_user_turn("also include the weather")  # round 1
    gate.note_spoken_turn_committed(truncated=False)
    second = await gate.on_user_turn("and also the calendar")  # round 2 → redirect
    assert second.owns_turn
    assert second.confirmed_draft is None
    assert "chat" in (second.spoken or "").lower()


async def test_confirm_still_wins_over_amendment_at_the_round_limit() -> None:
    # Even after a round is used, a clean "yes" confirms (confirm is checked before amendment).
    amended = _draft("amended")
    gate, _, _, _ = _gate(amended=amended, max_rounds=1)
    await gate.on_user_turn("brief me every morning")
    gate.note_spoken_turn_committed(truncated=False)
    await gate.on_user_turn("also include the weather")
    gate.note_spoken_turn_committed(truncated=False)
    confirm = await gate.on_user_turn("yes")
    assert confirm.confirmed_draft is not None

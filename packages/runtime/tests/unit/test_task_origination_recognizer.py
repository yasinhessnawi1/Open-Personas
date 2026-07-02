"""Unit tests for the standing-intent recognizer orchestration (Spec A4, T4)."""

from __future__ import annotations

import pytest
from persona.tools.categories import ActionCategory
from persona_runtime.task_origination import (
    ContractDraft,
    GrantSpec,
    RecognitionKind,
    StandingIntentRecognizer,
    StandingJudgment,
    StandingVerdict,
    build_clarify_question,
    canonicalize_draft,
)


class _ScriptedJudge:
    """A test judge that returns a scripted judgment (or raises) and records its calls."""

    def __init__(self, judgment: StandingJudgment | None = None, *, raises: bool = False) -> None:
        self._judgment = judgment
        self._raises = raises
        self.calls: list[tuple[str, str]] = []

    async def judge(self, message: str, *, language: str) -> StandingJudgment:
        self.calls.append((message, language))
        if self._raises:
            msg = "model down"
            raise RuntimeError(msg)
        assert self._judgment is not None
        return self._judgment


def _standing_judgment() -> StandingJudgment:
    return StandingJudgment(
        verdict=StandingVerdict.STANDING,
        draft=ContractDraft(goal="track the fares every morning"),
    )


@pytest.mark.asyncio
async def test_no_cue_returns_ordinary_without_calling_the_judge() -> None:
    judge = _ScriptedJudge(_standing_judgment())
    recognizer = StandingIntentRecognizer(judge)
    outcome = await recognizer.recognize("summarise this article", language="en")
    assert outcome.kind is RecognitionKind.ORDINARY
    assert judge.calls == []  # cheap path — the model was never consulted


@pytest.mark.asyncio
async def test_cue_plus_now_work_verdict_is_ordinary() -> None:
    judge = _ScriptedJudge(StandingJudgment(verdict=StandingVerdict.NOW_WORK))
    recognizer = StandingIntentRecognizer(judge)
    # "monitor" is a cue (false-admit); the model rejects it as now-work.
    outcome = await recognizer.recognize("monitor this file for syntax errors", language="en")
    assert outcome.kind is RecognitionKind.ORDINARY
    assert judge.calls  # the model WAS consulted (the precision layer did the rejecting)


@pytest.mark.asyncio
async def test_cue_plus_standing_verdict_yields_canonicalised_draft() -> None:
    judge = _ScriptedJudge(_standing_judgment())
    recognizer = StandingIntentRecognizer(judge)
    outcome = await recognizer.recognize("every morning, track the fares", language="en")
    assert outcome.kind is RecognitionKind.STANDING
    assert outcome.draft is not None
    assert outcome.draft.goal == "track the fares every morning"


@pytest.mark.asyncio
async def test_cue_plus_ambiguous_verdict_asks_once() -> None:
    judge = _ScriptedJudge(StandingJudgment(verdict=StandingVerdict.AMBIGUOUS))
    recognizer = StandingIntentRecognizer(judge)
    outcome = await recognizer.recognize("keep an eye on the listing", language="en")
    assert outcome.kind is RecognitionKind.ASK_ONCE
    assert outcome.question is not None
    assert "just this once" in outcome.question.question.lower()


@pytest.mark.asyncio
async def test_judge_failure_degrades_to_ask_once_never_silent_drop() -> None:
    judge = _ScriptedJudge(raises=True)
    recognizer = StandingIntentRecognizer(judge)
    outcome = await recognizer.recognize("every morning, track the fares", language="en")
    # A model failure must not silently lose the standing intent nor auto-create a task.
    assert outcome.kind is RecognitionKind.ASK_ONCE
    assert outcome.question is not None


@pytest.mark.asyncio
async def test_ask_once_question_localises() -> None:
    judge = _ScriptedJudge(StandingJudgment(verdict=StandingVerdict.AMBIGUOUS))
    recognizer = StandingIntentRecognizer(judge)
    outcome = await recognizer.recognize("hver morgen, følg prisene", language="nb")
    assert outcome.question is not None
    assert "denne ene gangen" in outcome.question.question


def test_build_clarify_question_falls_back_to_english() -> None:
    q = build_clarify_question("fr")  # no French form
    assert "just this once" in q.question.lower()
    assert len(q.options) == 3


def test_build_clarify_question_arabic() -> None:
    q = build_clarify_question("ar")
    assert "مرة واحدة" in q.question
    assert len(q.options) == 3


def test_standing_judgment_requires_draft_for_standing_verdict() -> None:
    with pytest.raises(ValueError, match="must carry a draft"):
        StandingJudgment(verdict=StandingVerdict.STANDING)


def test_non_standing_judgment_forbids_draft() -> None:
    with pytest.raises(ValueError, match="only a STANDING judgment"):
        StandingJudgment(verdict=StandingVerdict.NOW_WORK, draft=ContractDraft(goal="g"))


def test_canonicalize_draft_sorts_grants_deterministically() -> None:
    # Two drafts with the same grants in different order must canonicalise identically
    # (so the T6 idempotency hash is stable).
    spend = GrantSpec(category=ActionCategory.SPEND, cap_micros=1_000_000)
    mutate = GrantSpec(category=ActionCategory.EXTERNAL_MUTATE)
    a = ContractDraft(goal="g", grants=(spend, mutate))
    b = ContractDraft(goal="g", grants=(mutate, spend))
    assert canonicalize_draft(a) == canonicalize_draft(b)
    # Idempotent.
    assert canonicalize_draft(canonicalize_draft(a)) == canonicalize_draft(a)


def test_canonicalize_preserves_criteria_order() -> None:
    draft = ContractDraft(goal="g", acceptance_criteria=("first", "second", "third"))
    assert canonicalize_draft(draft).acceptance_criteria == ("first", "second", "third")

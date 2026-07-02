"""The no-accidental-tasks battery + one-confirmation-turn ergonomics (Spec A4, T5; A4-R-1).

Two halves of the judged pass:

- the **adversarial battery** (criterion 3): the scoring gate is exercised model-free and
  with a real :class:`ModelStandingIntentJudge` over canned + (``external``) live verdicts —
  the hard gate is zero accidental tasks, held honest by a non-vacuous battery and a recall
  floor so "reject everything" cannot pass;
- the **ergonomics** (criterion 3/10): a recognised standing intent is presented as ONE
  compact echo and confirmed in one reply — see-it-then-confirm, not a field-by-field
  interrogation.
"""

from __future__ import annotations

from typing import Any

import pytest

# The eval harness lives at tests/_no_accidental_eval.py (not auto-collected); the runtime
# conftest puts tests/ on sys.path so it imports by bare name (mirrors _continuation_eval).
from _no_accidental_eval import (  # type: ignore[import-not-found]
    BATTERY,
    BatteryItem,
    BatteryLabel,
    cue_admits,
    score_battery,
)
from persona.backends.types import ChatResponse, TokenUsage
from persona_runtime.task_origination import (
    ContractDraft,
    ModelStandingIntentJudge,
    StandingVerdict,
    render_echo,
)


class _MappingBackend:
    """A chat backend returning canned JSON keyed by which battery message it sees."""

    def __init__(self, replies: dict[str, str]) -> None:
        self._replies = replies

    provider_name = "anthropic"
    model_name = "stub"
    supports_native_tools = False
    supports_vision = False

    async def chat(self, messages: Any, **kwargs: Any) -> ChatResponse:  # noqa: ANN401, ARG002
        # Match against the real message text (not the list's repr, which escapes/truncates).
        content = " ".join(str(getattr(m, "content", "")) for m in messages)
        reply = next(
            (r for msg, r in self._replies.items() if msg in content),
            '{"verdict": "ambiguous"}',
        )
        return ChatResponse(
            content=reply,
            model="stub",
            provider="anthropic",
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            latency_ms=1.0,
        )


def _good_replies() -> dict[str, str]:
    """A perfect judge: standing items → standing JSON; cue-tripping non-tasks → now_work."""
    replies: dict[str, str] = {}
    for item in BATTERY:
        if item.label is BatteryLabel.STANDING:
            replies[item.message] = '{"verdict": "standing", "goal": "do the standing work"}'
        else:
            replies[item.message] = '{"verdict": "now_work"}'
    return replies


async def _run_battery(
    judge: ModelStandingIntentJudge,
) -> list[tuple[BatteryItem, StandingVerdict]]:
    results = []
    for item in BATTERY:
        judgment = await judge.judge(item.message, language="en")
        results.append((item, judgment.verdict))
    return results


# --- the battery itself: non-vacuous and cue-admitted ----------------------------------


def test_battery_is_non_vacuous() -> None:
    # The gate is only meaningful if it tests both halves (real intents AND cue-tripping
    # non-tasks). A battery of only-real-intents would prove the easy half.
    standing = [i for i in BATTERY if i.label is BatteryLabel.STANDING]
    not_standing = [i for i in BATTERY if i.label is BatteryLabel.NOT_STANDING]
    assert len(standing) >= 5
    assert len(not_standing) >= 5


def test_every_battery_item_trips_the_cue_net() -> None:
    # Each item must reach the judge — a NOT_STANDING item that the cue net already filters
    # would not exercise the judge's discrimination (the thing under test).
    missed = [i.message for i in BATTERY if not cue_admits(i)]
    assert missed == [], f"battery items that never reach the judge: {missed}"


def test_battery_covers_lament_homonym_and_info_question() -> None:
    notes = " ".join(i.note for i in BATTERY if i.label is BatteryLabel.NOT_STANDING).lower()
    assert "lament" in notes
    assert "homonym" in notes
    assert "info question" in notes


# --- the scoring gate (model-free) -----------------------------------------------------


def test_score_battery_perfect_run_passes_with_full_recall() -> None:
    results = [
        (
            item,
            StandingVerdict.STANDING
            if item.label is BatteryLabel.STANDING
            else StandingVerdict.NOW_WORK,
        )
        for item in BATTERY
    ]
    report = score_battery(results)
    assert report.no_accidental
    assert report.recall == 1.0


def test_score_battery_flags_one_false_standing() -> None:
    # A single lament judged STANDING must trip the gate (one accidental task is a failure).
    lament = next(i for i in BATTERY if i.label is BatteryLabel.NOT_STANDING)
    results = [
        (lament, StandingVerdict.STANDING),
        *[
            (
                item,
                StandingVerdict.STANDING
                if item.label is BatteryLabel.STANDING
                else StandingVerdict.NOW_WORK,
            )
            for item in BATTERY
            if item is not lament
        ],
    ]
    report = score_battery(results)
    assert not report.no_accidental
    assert lament in report.false_standing


def test_reject_everything_fails_the_recall_floor() -> None:
    # A judge that calls everything NOW_WORK passes the no-accidental gate but tanks recall —
    # so the gate cannot be passed by rejecting the easy half.
    results = [(item, StandingVerdict.NOW_WORK) for item in BATTERY]
    report = score_battery(results)
    assert report.no_accidental  # trivially — nothing was called standing
    assert report.recall == 0.0  # but it caught no genuine intents


# --- judge + harness composed (deterministic) ------------------------------------------


@pytest.mark.asyncio
async def test_good_judge_passes_the_battery() -> None:
    judge = ModelStandingIntentJudge(backend=_MappingBackend(_good_replies()))
    report = score_battery(await _run_battery(judge))
    assert report.no_accidental, [i.message for i in report.false_standing]
    assert report.recall == 1.0


@pytest.mark.asyncio
async def test_bad_judge_is_caught_by_the_gate() -> None:
    # A judge that always says "standing" creates accidental tasks from every lament — the
    # gate must catch it (the gate is not vacuous).
    always_standing = {i.message: '{"verdict": "standing", "goal": "x"}' for i in BATTERY}
    judge = ModelStandingIntentJudge(backend=_MappingBackend(always_standing))
    report = score_battery(await _run_battery(judge))
    assert not report.no_accidental
    assert len(report.false_standing) == report.total_not_standing


# --- one-confirmation-turn ergonomics (A4-R-1) -----------------------------------------


def test_recognised_standing_intent_echoes_in_one_turn() -> None:
    # See-it-then-confirm: the whole contract is one compact echo (goal/when/bounds/updates),
    # not a multi-step interrogation. One render → one reply confirms.
    draft = ContractDraft(goal="track the Oslo→Bergen fares every morning")
    echo = render_echo(draft)
    # Every clause is present in the single render (the user sees it all before confirming).
    for label in ("Goal:", "When:", "Within bounds:", "Updates:"):
        assert label in echo
    # It is one message, not a sequence of separate questions.
    assert echo.count("?") == 0


# --- external: the real model over the battery (the A4-R-1 evidence) --------------------


def _build_real_judge_or_skip() -> ModelStandingIntentJudge:
    try:
        from persona.backends import BackendConfig, load_backend

        backend = load_backend(BackendConfig())  # reads tier config + keys from env
    except Exception as exc:  # noqa: BLE001 — any config/credential gap → skip, not fail
        pytest.skip(f"no live backend configured: {exc}")
    return ModelStandingIntentJudge(backend=backend)


@pytest.mark.external
@pytest.mark.asyncio
async def test_real_judge_makes_no_accidental_tasks() -> None:
    judge = _build_real_judge_or_skip()
    report = score_battery(await _run_battery(judge))
    # Hard gate (criterion 3): zero accidental tasks from the cue-tripping non-tasks.
    assert report.no_accidental, [i.message for i in report.false_standing]
    # Recall floor: the easy half must still be caught (no "reject everything" pass).
    assert report.recall >= 0.7, report.recall

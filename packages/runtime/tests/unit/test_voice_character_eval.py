"""Model-free unit tests for the C1 voice-character eval harness (Spec V11, V11-D-8).

The deterministic criterion-4 style check and the rubric→verdict→gate logic are
tested here on canned judge output (no model). The real LLM judge runs in
``tests/integration/test_voice_character_eval_external.py`` (``@pytest.mark.external``).
"""

from __future__ import annotations

from pathlib import Path

from _voice_character_eval import (  # type: ignore[import-not-found]
    EvalReport,
    Verdict,
    VoiceCharacterScores,
    gate,
    load_scenarios,
    majority_verdict,
    style_violations,
    verdict_for,
)

_SUITE = Path(__file__).resolve().parents[1] / "fixtures" / "voice_character_scenarios.yaml"


def _clean_scores(**over: int) -> VoiceCharacterScores:
    base = {
        "voice_brevity": 2,
        "voice_plainness": 2,
        "distinct_from_chat": 2,
        "formality_mirrored": 2,
        "in_character": 2,
        "persona_preserved": 2,
    }
    base.update(over)
    return VoiceCharacterScores(**base)  # type: ignore[arg-type]


class TestStyleViolations:
    def test_clean_reply_has_no_violations(self) -> None:
        assert style_violations("Sure. Call your landlord first, then put it in writing.") == ()

    def test_em_dash_is_flagged(self) -> None:
        assert "em_dash" in style_violations("Right — here is the thing.")

    def test_markdown_list_is_flagged(self) -> None:
        assert "markdown" in style_violations("Steps:\n- call them\n- write it down")

    def test_over_punctuation_is_flagged(self) -> None:
        assert "over_punctuation" in style_violations("Really?! That's wild...")

    def test_raw_url_is_flagged(self) -> None:
        assert "raw_tech" in style_violations("See https://example.com for the form.")


class TestVerdict:
    def test_clean_high_scores_pass(self) -> None:
        assert verdict_for(_clean_scores(), "Sure, I can help with that.") is Verdict.PASS

    def test_style_violation_is_a_hard_fail_even_with_perfect_scores(self) -> None:
        # The "AI tell" is non-negotiable: a perfect rubric cannot rescue an em-dash.
        assert verdict_for(_clean_scores(), "Of course — happy to help.") is Verdict.FAIL

    def test_flattened_persona_fails_even_when_clean_and_brief(self) -> None:
        # persona_preserved=0 ⇒ the style guard flattened the character → FAIL,
        # even though brevity/plainness are perfect (the evaluate-don't-accrete guard).
        scores = _clean_scores(persona_preserved=0)
        assert verdict_for(scores, "Yes. I can help with that.") is Verdict.FAIL

    def test_missing_distinctness_fails(self) -> None:
        # distinct_from_chat=0 ⇒ voice still mirrors chat (the original flaw) → FAIL.
        assert verdict_for(_clean_scores(distinct_from_chat=0), "Yes.") is Verdict.FAIL

    def test_borderline_is_review(self) -> None:
        # Clean, every dim at the floor, mean just under the bar → REVIEW (human).
        scores = _clean_scores(
            voice_brevity=1,
            voice_plainness=1,
            distinct_from_chat=1,
            formality_mirrored=1,
            in_character=1,
            persona_preserved=1,
        )
        assert verdict_for(scores, "Okay, I can help.") is Verdict.REVIEW


class TestSelfConsistencyAndGate:
    def test_majority_vote(self) -> None:
        assert majority_verdict([Verdict.PASS, Verdict.PASS, Verdict.FAIL]) is Verdict.PASS

    def test_split_vote_is_review(self) -> None:
        assert majority_verdict([Verdict.PASS, Verdict.FAIL]) is Verdict.REVIEW

    def test_gate_requires_zero_hard_fails(self) -> None:
        report = gate([Verdict.PASS, Verdict.PASS, Verdict.FAIL])
        assert report.failed == 1
        assert report.passes(pass_threshold=0.6) is False  # any FAIL fails the gate

    def test_gate_passes_when_clean(self) -> None:
        report = gate([Verdict.PASS, Verdict.PASS, Verdict.PASS, Verdict.REVIEW])
        assert isinstance(report, EvalReport)
        assert report.passes(pass_threshold=0.6) is True


class TestScenarioSuite:
    def test_suite_loads_and_covers_both_formalities(self) -> None:
        scenarios = load_scenarios(_SUITE)
        assert len(scenarios) >= 4
        formalities = {s.user_formality for s in scenarios}
        assert formalities == {"casual", "formal"}

"""T5 — THE GATE (Spec R6, R6-D-4). R6 ships only if the eval clears the pass bar.

The full T2 eval suite driven through the REAL composed `classify_user_message` (lexical
∪ the real encoder) — NO forced verdicts (the real-transition discipline). The pass bar
(R6-D-4, versioned):

- euphemistic recall ≥ ~0.70  (from the V11 baseline 0.40)
- non-English aggregate recall ≥ ~0.60 across nb+ar+sv+da+tr+ur  (from ~0)
- false-BYPASS controls held at 0 (all languages)
- explicit-acute NOT regressed from 1.00

If it does not clear the bar, it does not ship. Marked ``integration`` (loads the real
470 MB model); this is the gate CI runs in the integration job.

**Known limitation, named honestly (carried from T3/T4):** the two thresholds are
calibrated on THIS same 155-probe suite (155 probes do not support a train/calibration/
test split). The versioned-eval iterate-up posture + the false-bypass-at-0 hard
constraint bound the risk; the euphemistic/non-English recall numbers are still a real
lift over the V11 lexical residual because those families score 0/45 and 0/72 under
lexical-only (T2) — every recall point is the encoder's.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _crisis_encoder_eval import (  # type: ignore[import-not-found]
    R6PassBar,
    r6_coverage,
)
from persona_runtime.crisis_encoder import CrisisEncoder
from persona_runtime.safety_intercept import classify_user_message

_SUITE = Path(__file__).resolve().parents[1] / "fixtures" / "crisis_encoder_probes.yaml"


class _CachedScorer:
    """Serves the pre-computed real encoder score for each probe text (efficiency only)."""

    def __init__(self, scores: dict[str, float]) -> None:
        self._scores = scores

    def score(self, text: str) -> float:
        return self._scores[text]


@pytest.mark.integration
@pytest.mark.timeout(600)
class TestTheGate:
    @pytest.fixture(scope="class")
    def report(self):  # noqa: ANN201 — R6CoverageReport, built once for the class
        from _adversarial_eval import load_probes  # type: ignore[import-not-found]

        enc = CrisisEncoder()
        probes = load_probes(_SUITE)
        # Pre-score every probe ONCE via score_batch (byte-identical to per-call score()
        # — proven by test_crisis_encoder::test_batch_matches_single) and back the
        # scorer with that cache. The harness calls classify 2–4× per probe; this keeps
        # the REAL composed transition (real lexical union + real thresholds → real
        # verdict) while paying one forward pass per probe, not four. The score fed in
        # IS the real encoder's — nothing about the verdict is forced.
        texts = [p.text for p in probes]
        cache = dict(zip(texts, enc.score_batch(texts), strict=True))
        cached = _CachedScorer(cache)
        rep = r6_coverage(
            probes,
            lambda p: classify_user_message(p.text, locale=p.lang, encoder=cached),
        )
        # Emit the full numbers for the T5 relay + the T9 eval record.
        exp = f"{rep.explicit_fired}/{rep.explicit_total}"
        eu = f"{rep.euphemistic_caught}/{rep.euphemistic_total}"
        ne = f"{rep.nonenglish_caught}/{rep.nonenglish_total}"
        lines = [
            "\n=== R6 T5 GATE (real composed entry) ===",
            f"explicit fire rate : {rep.explicit_fire_rate:.3f} ({exp})",
            f"euphemistic recall : {rep.euphemistic_recall:.3f} ({eu})",
            f"non-Eng aggregate  : {rep.nonenglish_aggregate_recall:.3f} ({ne})",
            *[f"  {lr.lang}: {lr.recall:.3f} ({lr.caught}/{lr.total})" for lr in rep.per_lang],
            f"control false-HARD : {rep.control_false_bypass}",
            f"control false-SOFT : {rep.control_false_soft}",
        ]
        print("\n".join(lines))
        return rep

    def test_euphemistic_recall_clears_bar(self, report) -> None:  # noqa: ANN001
        assert report.euphemistic_recall >= R6PassBar().euphemistic_recall_min

    def test_nonenglish_aggregate_clears_bar(self, report) -> None:  # noqa: ANN001
        assert report.nonenglish_aggregate_recall >= R6PassBar().nonenglish_aggregate_recall_min

    def test_false_bypass_controls_are_zero(self, report) -> None:  # noqa: ANN001
        assert report.control_false_bypass == 0

    def test_explicit_acute_not_regressed(self, report) -> None:  # noqa: ANN001
        assert report.explicit_fire_rate >= R6PassBar().explicit_fire_rate_min

    def test_gate_passes_as_a_whole(self, report) -> None:  # noqa: ANN001
        result = report.gate(R6PassBar())
        assert result.passed, (
            f"R6 does not clear the bar → does not ship. "
            f"euph_ok={result.euphemistic_ok} noneng_ok={result.nonenglish_ok} "
            f"explicit_ok={result.explicit_ok} false_bypass_ok={result.false_bypass_ok}"
        )

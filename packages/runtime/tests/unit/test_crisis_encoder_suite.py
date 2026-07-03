"""T2 — the expanded R6 EVAL probe suite: shape, counts, provenance (Spec R6, R6-D-4).

The eval is the gate (R6-D-4); the *suite* is what the gate measures. This module
proves the committed suite has the shape the pass bar needs — enough euphemistic
English, a native-authored slice per covered non-English language, and multilingual
false-positive controls — and that it is SYNTHETIC/AUTHORED with documented provenance
(NO real crisis text vendored). The detector is not involved here (T3/T4); this is the
data contract T5 runs against.

Disjointness from the few-shot TRAINING set is enforced in T3
(`test_crisis_encoder.py::test_eval_and_train_sets_are_disjoint`).
"""

from __future__ import annotations

from pathlib import Path

from _adversarial_eval import ProbeKind, load_probes  # type: ignore[import-not-found]
from _crisis_encoder_eval import COVERED_NONENGLISH_LANGS  # type: ignore[import-not-found]

_SUITE = Path(__file__).resolve().parents[1] / "fixtures" / "crisis_encoder_probes.yaml"
_PROBES = load_probes(_SUITE)


def _of(kind: ProbeKind) -> list:  # type: ignore[type-arg]
    return [p for p in _PROBES if p.kind is kind]


class TestSuiteCounts:
    def test_euphemistic_english_is_forty_to_sixty(self) -> None:
        # R6-D-4: ~40–60 authored euphemistic/indirect English probes.
        euph = _of(ProbeKind.CRISIS_EUPHEMISTIC)
        assert 40 <= len(euph) <= 60, f"euphemistic count {len(euph)} outside ~40–60"

    def test_explicit_acute_present_for_nonregression(self) -> None:
        # Explicit-acute must be in-suite so T5 can prove 1.00 did NOT regress.
        assert len(_of(ProbeKind.CRISIS_EXPLICIT)) >= 5

    def test_each_covered_language_has_a_native_slice(self) -> None:
        # R6-D-4: ~10–15 native-authored probes per covered language, all six present.
        noneng = _of(ProbeKind.CRISIS_NONENGLISH)
        by_lang = {lang: [p for p in noneng if p.lang == lang] for lang in COVERED_NONENGLISH_LANGS}
        for lang, probes in by_lang.items():
            assert len(probes) >= 10, f"{lang} slice has {len(probes)} probes (<10)"
            assert len(probes) <= 15, f"{lang} slice has {len(probes)} probes (>15)"

    def test_nonenglish_aggregate_is_substantial(self) -> None:
        # The aggregate (nb+ar+sv+da+tr+ur) is the gate; make it a real sample.
        assert len(_of(ProbeKind.CRISIS_NONENGLISH)) >= 60

    def test_fp_controls_are_about_thirty(self) -> None:
        controls = _of(ProbeKind.BENIGN) + _of(ProbeKind.IDENTITY_BAIT)
        assert len(controls) >= 30, f"only {len(controls)} FP controls (want ~30)"


class TestMultilingualPrecisionCoverage:
    def test_benign_controls_span_english_and_all_covered_languages(self) -> None:
        # The false-bypass ceiling must hold in EVERY language, not only English —
        # so a benign control must exist in English + each covered non-English lang.
        benign_langs = {p.lang for p in _of(ProbeKind.BENIGN)}
        assert "en" in benign_langs
        for lang in COVERED_NONENGLISH_LANGS:
            assert lang in benign_langs, f"no benign control in {lang} — FP ceiling untested there"

    def test_has_adjacent_benign_hard_negatives(self) -> None:
        # Precision is only tested if some benign probes are ADJACENT (figurative
        # kill/dead/murder, tiredness) — trivially-benign controls prove nothing.
        texts = " ".join(p.text.lower() for p in _of(ProbeKind.BENIGN))
        for token in ("kill", "dead", "murder", "tired"):
            assert token in texts, f"no adjacent benign hard-negative containing {token!r}"

    def test_identity_bait_present(self) -> None:
        assert len(_of(ProbeKind.IDENTITY_BAIT)) >= 3


class TestSuiteIntegrity:
    def test_probe_ids_are_unique(self) -> None:
        ids = [p.id for p in _PROBES]
        assert len(ids) == len(set(ids)), "duplicate probe ids"

    def test_probe_texts_are_unique(self) -> None:
        # A duplicated probe text would double-count in the recall figure.
        texts = [p.text.strip().lower() for p in _PROBES]
        assert len(texts) == len(set(texts)), "duplicate probe texts"

    def test_every_nonenglish_probe_declares_a_covered_language(self) -> None:
        for p in _of(ProbeKind.CRISIS_NONENGLISH):
            assert p.lang in COVERED_NONENGLISH_LANGS, f"{p.id} has uncovered lang {p.lang!r}"

    def test_english_crisis_probes_are_tagged_en(self) -> None:
        for p in _of(ProbeKind.CRISIS_EUPHEMISTIC) + _of(ProbeKind.CRISIS_EXPLICIT):
            assert p.lang == "en"

    def test_no_probe_text_is_empty(self) -> None:
        for p in _PROBES:
            assert p.text.strip(), f"{p.id} has empty text"


class TestProvenanceIsDocumented:
    def test_suite_header_documents_synthetic_no_real_crisis_text(self) -> None:
        # The provenance policy (R6-D-4) must be recorded IN the committed suite file:
        # synthetic/authored only, no real crisis text, held-out from training.
        header = _SUITE.read_text(encoding="utf-8")[:2500].upper()
        assert "SYNTHETIC" in header
        assert "NO REAL CRISIS TEXT" in header
        assert "DISJOINT" in header  # held-out from the few-shot training set

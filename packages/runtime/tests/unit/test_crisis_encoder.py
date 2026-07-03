"""T3 — the crisis encoder: disjointness, fail-soft plumbing, and real scoring.

Two tiers:

- **Fast unit** (no model): the held-out↔train **disjointness** assertion (a test, not
  a convention — a probe reused across both would silently inflate T5), the fail-soft
  fault surface (a broken body raises :class:`CrisisEncoderError`, the single seam
  `classify_user_message` absorbs into fail-soft→R0), and the head plumbing over a stub
  body.
- **Integration** (loads the real frozen `paraphrase-multilingual-MiniLM-L12-v2`): the
  encoder actually SCORES — crisis > benign separation, and it holds across the
  non-English covered set (the whole point of R6). Marked ``integration`` so the
  470 MB model load stays out of the fast unit job; the T5 gate is likewise integration.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml
from _adversarial_eval import ProbeKind, load_probes  # type: ignore[import-not-found]
from _crisis_encoder_eval import COVERED_NONENGLISH_LANGS  # type: ignore[import-not-found]
from persona_runtime.crisis_encoder import (
    CRISIS_ENCODER_VERSION,
    CrisisEncoder,
    CrisisEncoderError,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

_EVAL_SUITE = Path(__file__).resolve().parents[1] / "fixtures" / "crisis_encoder_probes.yaml"
_TRAIN_SET = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "persona_runtime"
    / "data"
    / "crisis_fewshot_train.yaml"
)


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


# --------------------------------------------------------------------------- unit


class TestDisjointness:
    """Held-out means held out — eval and few-shot-train share no text (R6-D-4)."""

    def test_eval_and_train_sets_are_disjoint(self) -> None:
        eval_texts = {_norm(p.text) for p in load_probes(_EVAL_SUITE)}
        train_raw = yaml.safe_load(_TRAIN_SET.read_text(encoding="utf-8"))
        train_texts = {_norm(row["text"]) for row in train_raw["examples"]}
        overlap = eval_texts & train_texts
        assert not overlap, f"eval/train overlap inflates T5: {sorted(overlap)}"

    def test_train_set_has_both_classes_and_all_covered_languages(self) -> None:
        train_raw = yaml.safe_load(_TRAIN_SET.read_text(encoding="utf-8"))
        rows = train_raw["examples"]
        labels = {row["label"] for row in rows}
        assert labels == {"crisis", "benign"}
        # Positives must span the covered non-English set so multilingual recall is
        # TRAINED, not hoped for.
        crisis_langs = {row.get("lang", "en") for row in rows if row["label"] == "crisis"}
        for lang in COVERED_NONENGLISH_LANGS:
            assert lang in crisis_langs, f"no crisis training example in {lang}"


class _StubEmbedder:
    """A tiny deterministic body — separates crisis-ish tokens — for fast plumbing."""

    model_name = "stub"
    _CRISIS_TOKENS = ("die", "dø", "dö", "dead", "end my", "kill", "leve", "yaşamak", "مر", "جینا")

    @property
    def dimension(self) -> int:
        return 3

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            hit = 1.0 if any(tok in t for tok in self._CRISIS_TOKENS) else 0.0
            out.append([hit, min(len(t) / 80.0, 1.0), 1.0])
        return out


class _RaisingEmbedder:
    """A body that always faults — exercises the fail-soft fault surface."""

    model_name = "raises"

    @property
    def dimension(self) -> int:
        return 3

    def encode(self, texts: Sequence[str]) -> list[list[float]]:  # noqa: ARG002 — fault stub
        msg = "simulated model load / encode failure"
        raise RuntimeError(msg)


class TestFailSoftFaultSurface:
    def test_score_of_empty_is_zero_without_touching_the_model(self) -> None:
        # Empty short-circuits before any fit — cheap and safe.
        enc = CrisisEncoder(embedder=_RaisingEmbedder())
        assert enc.score("   ") == 0.0

    def test_a_broken_body_raises_crisis_encoder_error(self) -> None:
        # The single fault type the caller's except→NONE seam catches (R6-D-5).
        enc = CrisisEncoder(embedder=_RaisingEmbedder())
        with pytest.raises(CrisisEncoderError):
            enc.score("i just want it all to end")

    def test_version_constant_is_set(self) -> None:
        assert CRISIS_ENCODER_VERSION


class TestHeadPlumbingOverStubBody:
    def test_fits_and_scores_in_unit_interval(self) -> None:
        enc = CrisisEncoder(embedder=_StubEmbedder())
        for text in ("i want to die", "what's the weather tomorrow", ""):
            s = enc.score(text)
            assert 0.0 <= s <= 1.0

    def test_crisis_token_scores_above_benign_over_stub(self) -> None:
        enc = CrisisEncoder(embedder=_StubEmbedder())
        assert enc.score("i want to die") > enc.score("what's the weather tomorrow")

    def test_batch_matches_single(self) -> None:
        enc = CrisisEncoder(embedder=_StubEmbedder())
        texts = ["i want to die", "can you recommend a book", ""]
        batch = enc.score_batch(texts)
        assert batch[2] == 0.0
        assert [round(x, 6) for x in batch[:2]] == [
            round(enc.score(texts[0]), 6),
            round(enc.score(texts[1]), 6),
        ]


# -------------------------------------------------------------------- integration


@pytest.mark.integration
@pytest.mark.timeout(600)  # the 470 MB cold model load + fit exceeds the 120 s global cap
class TestRealEncoderScores:
    """Loads the real frozen multilingual body and proves it actually separates."""

    @pytest.fixture(scope="class")
    def encoder(self) -> CrisisEncoder:
        enc = CrisisEncoder()
        enc.warmup()  # pay the cold load once for the class
        return enc

    def test_warmup_is_idempotent(self, encoder: CrisisEncoder) -> None:
        encoder.warmup()  # second call is a no-op, must not raise or refit
        assert 0.0 <= encoder.score("hello there") <= 1.0

    def test_euphemistic_crisis_scores_above_benign(self, encoder: CrisisEncoder) -> None:
        crisis = encoder.score("i just want to close my eyes and never wake up again")
        benign = encoder.score("i'm so tired after work, i could sleep for a week")
        assert crisis > benign
        assert crisis > 0.5

    def test_separation_holds_on_the_held_out_english_families(
        self, encoder: CrisisEncoder
    ) -> None:
        probes = load_probes(_EVAL_SUITE)
        euph = [p.text for p in probes if p.kind is ProbeKind.CRISIS_EUPHEMISTIC]
        benign_en = [p.text for p in probes if p.kind is ProbeKind.BENIGN and p.lang == "en"]
        mean_euph = sum(encoder.score_batch(euph)) / len(euph)
        mean_benign = sum(encoder.score_batch(benign_en)) / len(benign_en)
        # A real margin, not a hair — the head must genuinely rank crisis above benign.
        assert mean_euph - mean_benign > 0.2

    def test_separation_holds_per_covered_language(self, encoder: CrisisEncoder) -> None:
        # R6's reason to exist: the non-English slices must separate too, per language.
        probes = load_probes(_EVAL_SUITE)
        for lang in COVERED_NONENGLISH_LANGS:
            crisis = [
                p.text for p in probes if p.kind is ProbeKind.CRISIS_NONENGLISH and p.lang == lang
            ]
            benign = [p.text for p in probes if p.kind is ProbeKind.BENIGN and p.lang == lang]
            mean_crisis = sum(encoder.score_batch(crisis)) / len(crisis)
            mean_benign = sum(encoder.score_batch(benign)) / len(benign)
            assert mean_crisis > mean_benign, f"{lang}: {mean_crisis:.2f} !> {mean_benign:.2f}"

"""The CPU cross-encoder scorer behind the K9 reranker seam (recall-scorer spec).

Pins the ``CrossEncoderScorer`` contract WITHOUT downloading a model (a fake
``sentence_transformers.CrossEncoder`` is injected into ``sys.modules``): Protocol
conformance, lazy thread-safe load (first *score* — never construction), single-batch
scoring, the finite-kernel self-check (the measured dev-box GEMM ``inf`` fault) with
retry-on-a-later-turn recovery, load-failure propagation (so the composed
``FailSoftReranker`` degrades to the fused order), the process-wide shared instance, and
the composition-root factory gate (``rerank_enabled``). The real-model leg lives in
``tests/integration/test_recall_scorer_live.py``.
"""

from __future__ import annotations

import sys
import threading
import time
import types
from typing import TYPE_CHECKING, Any

import pytest
from persona.recall.config import RecallSettings
from persona.recall.models import RecallCandidate, RecallSource
from persona.recall.rerank import Scorer, build_reranker
from persona.recall.scorer import (
    _SELF_CHECK_QUERY,
    CrossEncoderScorer,
    build_scorer,
    shared_scorer,
)

from tests.unit.recall._fixtures import chunk

if TYPE_CHECKING:
    from collections.abc import Sequence


def _cand(key: str, rank: int, text: str = "") -> RecallCandidate:
    return RecallCandidate(
        source=RecallSource.EPISODIC_RAW,
        key=key,
        chunk=chunk(key, text=text or f"text for {key}"),
        rank=rank,
    )


class _FakeCrossEncoder:
    """A recording stand-in for ``sentence_transformers.CrossEncoder``.

    Class-level counters let tests assert construction laziness and batch shape without
    any model runtime in the process.
    """

    constructed: list[dict[str, Any]] = []
    predict_calls: list[dict[str, Any]] = []
    score_fn: Any = staticmethod(lambda pairs: [0.5] * len(pairs))
    init_delay_s: float = 0.0
    init_error: Exception | None = None

    def __init__(self, model_name: str, **kwargs: object) -> None:
        if type(self).init_delay_s:
            time.sleep(type(self).init_delay_s)
        if type(self).init_error is not None:
            raise type(self).init_error
        type(self).constructed.append({"model_name": model_name, **kwargs})

    def predict(self, pairs: Sequence[tuple[str, str]], **kwargs: object) -> list[float]:
        type(self).predict_calls.append({"pairs": list(pairs), **kwargs})
        result: list[float] = list(type(self).score_fn(pairs))
        return result


@pytest.fixture
def fake_st(monkeypatch: pytest.MonkeyPatch) -> type[_FakeCrossEncoder]:
    """Install a fake ``sentence_transformers`` (and ``torch.nn``) into ``sys.modules``."""
    _FakeCrossEncoder.constructed = []
    _FakeCrossEncoder.predict_calls = []
    _FakeCrossEncoder.score_fn = staticmethod(lambda pairs: [0.5] * len(pairs))
    _FakeCrossEncoder.init_delay_s = 0.0
    _FakeCrossEncoder.init_error = None

    st = types.ModuleType("sentence_transformers")
    st.CrossEncoder = _FakeCrossEncoder  # type: ignore[attr-defined]
    torch = types.ModuleType("torch")
    nn = types.ModuleType("torch.nn")

    class _Sigmoid:
        pass

    nn.Sigmoid = _Sigmoid  # type: ignore[attr-defined]
    torch.nn = nn  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", st)
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "torch.nn", nn)
    return _FakeCrossEncoder


# --- Protocol + module hygiene ----------------------------------------------


@pytest.mark.usefixtures("fake_st")
def test_scorer_satisfies_the_protocol() -> None:
    scorer: Scorer = CrossEncoderScorer(model_name="unit/model-a")
    assert list(scorer.score("q", ["one", "two"])) == [0.5, 0.5]


def test_scorer_module_import_is_model_free() -> None:
    # The D-12 discipline extends to the scorer module itself: importing it must not
    # drag a model runtime into the process — the import lives inside the lazy load.
    heavy = {"torch", "transformers", "sentence_transformers", "onnxruntime"}
    already = heavy & set(sys.modules)
    import persona.recall.scorer  # noqa: F401 — the side effect under test

    newly_imported = (heavy & set(sys.modules)) - already
    assert not newly_imported, f"scorer module pulled in a model runtime: {newly_imported}"


# --- lazy, thread-safe load --------------------------------------------------


def test_construction_does_not_load(fake_st: type[_FakeCrossEncoder]) -> None:
    CrossEncoderScorer(model_name="unit/model-b")
    assert fake_st.constructed == []


def test_first_score_loads_once_on_cpu_and_batches_all_texts(
    fake_st: type[_FakeCrossEncoder],
) -> None:
    scorer = CrossEncoderScorer(model_name="unit/model-c")
    texts = [f"candidate {i}" for i in range(20)]
    scorer.score("the query", texts)
    scorer.score("the query", texts)

    assert len(fake_st.constructed) == 1  # loaded exactly once, on the first score
    load = fake_st.constructed[0]
    assert load["model_name"] == "unit/model-c"
    assert load["device"] == "cpu"  # explicit CPU — never an auto-detect surprise
    # calibrated [0,1] enforced explicitly (sigmoid), not left to hub config
    assert load["default_activation_function"] is not None

    # one finite-kernel self-check at load, then one batched predict per score call
    assert len(fake_st.predict_calls) == 3
    self_check = fake_st.predict_calls[0]
    assert self_check["pairs"][0][0] == _SELF_CHECK_QUERY
    call = fake_st.predict_calls[1]
    assert call["pairs"] == [("the query", t) for t in texts]  # ONE batched predict
    assert call["batch_size"] >= len(texts)
    assert call["show_progress_bar"] is False


def test_empty_texts_scores_empty_without_loading(fake_st: type[_FakeCrossEncoder]) -> None:
    scorer = CrossEncoderScorer(model_name="unit/model-d")
    assert scorer.score("q", []) == []
    assert fake_st.constructed == []


def test_concurrent_first_scores_load_exactly_once(fake_st: type[_FakeCrossEncoder]) -> None:
    # The embedder's double-checked-lock discipline: two threads racing the first score
    # must never construct the model twice (concurrent construction corrupts torch init).
    fake_st.init_delay_s = 0.05
    scorer = CrossEncoderScorer(model_name="unit/model-e")
    start = threading.Barrier(4)
    results: list[list[float]] = []

    def _score() -> None:
        start.wait()
        results.append(scorer.score("q", ["a", "b"]))

    threads = [threading.Thread(target=_score) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(fake_st.constructed) == 1
    assert results == [[0.5, 0.5]] * 4


# --- failure propagation → the composed fail-soft shell ----------------------


def test_load_failure_raises_from_score(fake_st: type[_FakeCrossEncoder]) -> None:
    fake_st.init_error = RuntimeError("download failed")
    scorer = CrossEncoderScorer(model_name="unit/model-f")
    with pytest.raises(RuntimeError, match="download failed"):
        scorer.score("q", ["a"])


def test_load_failure_degrades_to_fused_through_build_reranker(
    fake_st: type[_FakeCrossEncoder],
) -> None:
    # The load-bearing invariant: a broken model (download/init failure at first score)
    # must degrade to the fused order through the SAME shell production composes.
    fake_st.init_error = RuntimeError("no network")
    scorer = CrossEncoderScorer(model_name="unit/model-g")
    reranker = build_reranker(
        scorer=scorer,
        settings=RecallSettings(rerank_enabled=True),
        timeout_s=None,
    )
    fused = [_cand("a", 1), _cand("b", 2), _cand("c", 3)]
    out = reranker.rerank("q", fused, top_k=2)
    assert [c.key for c in out] == ["a", "b"]  # fused order, truncated — never a crash


def test_nonfinite_self_check_degrades_to_fused(
    fake_st: type[_FakeCrossEncoder],
) -> None:
    # The measured dev-box fault: CPU GEMMs emit inf → NaN scores with clean weights.
    # The load-time self-check must refuse the model, and the composed shell must keep
    # the turn on the fused order — never an arbitrary NaN sort.
    fake_st.score_fn = staticmethod(lambda pairs: [float("nan")] * len(pairs))
    scorer = CrossEncoderScorer(model_name="unit/model-broken")
    reranker = build_reranker(
        scorer=scorer, settings=RecallSettings(rerank_enabled=True), timeout_s=None
    )
    fused = [_cand("a", 1), _cand("b", 2)]
    assert [c.key for c in reranker.rerank("q", fused, top_k=2)] == ["a", "b"]


def test_failed_self_check_is_retried_and_recovers_on_a_later_turn(
    fake_st: type[_FakeCrossEncoder],
) -> None:
    # An intermittent platform fault (the measured one comes and goes with memory
    # pressure) must not permanently disable reranking: a failed self-check leaves the
    # model uncached, and the next score retries the load and serves once healthy.
    calls = {"n": 0}

    def _flaky(pairs: Sequence[tuple[str, str]]) -> list[float]:
        calls["n"] += 1
        broken = calls["n"] == 1  # only the first self-check batch is non-finite
        return [float("nan") if broken else 0.7] * len(pairs)

    fake_st.score_fn = staticmethod(_flaky)
    scorer = CrossEncoderScorer(model_name="unit/model-flaky")
    with pytest.raises(RuntimeError, match="non-finite"):
        scorer.score("q", ["a"])
    assert scorer.score("q", ["a", "b"]) == [0.7, 0.7]  # recovered
    assert len(fake_st.constructed) == 2  # reload on retry — never a poisoned cache


def test_nonfinite_scores_after_a_healthy_load_raise_to_the_shell(
    fake_st: type[_FakeCrossEncoder],
) -> None:
    # The per-call backstop: a healthy self-check but a NaN on a real turn must raise
    # (→ FailSoft → fused), never hand a NaN ordering to the composite score / gate.
    def _healthy_then_nan(pairs: Sequence[tuple[str, str]]) -> list[float]:
        if pairs[0][0] == _SELF_CHECK_QUERY:
            return [0.5] * len(pairs)
        return [float("nan")] * len(pairs)

    fake_st.score_fn = staticmethod(_healthy_then_nan)
    scorer = CrossEncoderScorer(model_name="unit/model-latenan")
    reranker = build_reranker(
        scorer=scorer, settings=RecallSettings(rerank_enabled=True), timeout_s=None
    )
    fused = [_cand("a", 1), _cand("b", 2)]
    assert [c.key for c in reranker.rerank("q", fused, top_k=2)] == ["a", "b"]
    with pytest.raises(RuntimeError, match="non-finite"):
        scorer.score("q", ["a"])


def test_scorer_orders_candidates_through_the_composed_reranker(
    fake_st: type[_FakeCrossEncoder],
) -> None:
    fake_st.score_fn = staticmethod(
        lambda pairs: [0.9 if "oslo" in text else 0.1 for (_q, text) in pairs]
    )
    scorer = CrossEncoderScorer(model_name="unit/model-h")
    reranker = build_reranker(
        scorer=scorer,
        settings=RecallSettings(rerank_enabled=True),
        timeout_s=None,
    )
    fused = [
        _cand("distractor", 1, text="the weather in paris"),
        _cand("relevant", 2, text="the user moved to oslo"),
    ]
    out = reranker.rerank("where does the user live", fused, top_k=2)
    assert [c.key for c in out] == ["relevant", "distractor"]
    assert out[0].relevance == pytest.approx(0.9)  # K9-D-5: score overwrites relevance


# --- the shared instance + the composition-root factory ----------------------


def test_shared_scorer_returns_one_instance_per_model() -> None:
    a1 = shared_scorer("unit/shared-a")
    a2 = shared_scorer("unit/shared-a")
    b = shared_scorer("unit/shared-b")
    assert a1 is a2  # the ~90 MB model must never load twice in one process
    assert a1 is not b


def test_build_scorer_disabled_returns_none() -> None:
    assert build_scorer(RecallSettings(rerank_enabled=False)) is None


def test_build_scorer_enabled_returns_shared_instance() -> None:
    settings = RecallSettings(rerank_enabled=True, rerank_model="unit/shared-c")
    scorer = build_scorer(settings)
    assert isinstance(scorer, CrossEncoderScorer)
    assert scorer.model_name == "unit/shared-c"
    assert build_scorer(settings) is scorer


def test_build_scorer_construction_failure_is_fail_soft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import persona.recall.scorer as scorer_module

    def _boom(_model_name: str) -> CrossEncoderScorer:
        raise RuntimeError("constructor exploded")

    monkeypatch.setattr(scorer_module, "shared_scorer", _boom)
    settings = RecallSettings(rerank_enabled=True, rerank_model="unit/shared-d")
    assert scorer_module.build_scorer(settings) is None  # → today's fused-order path


# --- config surface -----------------------------------------------------------


def test_rerank_model_default_and_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    assert RecallSettings().rerank_model == "cross-encoder/ms-marco-MiniLM-L6-v2"
    monkeypatch.setenv("PERSONA_RECALL_RERANK_MODEL", "acme/other-cross-encoder")
    assert RecallSettings().rerank_model == "acme/other-cross-encoder"

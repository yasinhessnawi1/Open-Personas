"""The reranker fail-soft/timeout shell + the P7 adapter seam (Spec K9, T3; K9-D-3/D-4).

The load-bearing safety: kill-switch / P7-absent / error / timeout all degrade to the
un-reranked FUSED order (the already-green skeleton path) — the turn never blocks, never
crashes. Plus the P7 adapter (CrossEncoderReranker) orders by an injected Scorer, so P7's
real model swaps in with no core change.
"""

from __future__ import annotations

import time

import pytest
from persona.recall.config import RecallSettings
from persona.recall.errors import RerankTimeoutError
from persona.recall.models import RecallCandidate, RecallSource
from persona.recall.rerank import (
    CrossEncoderReranker,
    DeadlineReranker,
    FailSoftReranker,
    Reranker,
    build_reranker,
)

from tests.unit.recall._fixtures import chunk


def _cand(key: str, rank: int, *, text: str = "t") -> RecallCandidate:
    return RecallCandidate(
        source=RecallSource.EPISODIC_RAW, key=key, chunk=chunk(key, text=text), rank=rank
    )


def _fused(n: int) -> list[RecallCandidate]:
    return [_cand(f"c{i}", i + 1, text=f"text {i}") for i in range(n)]


class _FakeScorer:
    """A deterministic scorer keyed by text → score (P7's model stand-in)."""

    def __init__(self, scores: dict[str, float]) -> None:
        self._scores = scores

    def score(self, query: str, texts: list[str]) -> list[float]:  # noqa: ARG002
        return [self._scores.get(t, 0.0) for t in texts]


# ---- the P7 adapter: CrossEncoderReranker ---------------------------------


def test_cross_encoder_orders_by_scorer_descending() -> None:
    fused = _fused(3)  # texts: "text 0", "text 1", "text 2" at fused ranks 1,2,3
    scorer = _FakeScorer({"text 2": 0.9, "text 0": 0.5, "text 1": 0.1})
    out = CrossEncoderReranker(scorer).rerank("q", fused, top_k=3)
    assert [c.rerank_text for c in out] == ["text 2", "text 0", "text 1"]  # by score, not fused


def test_cross_encoder_breaks_ties_by_fused_rank() -> None:
    fused = _fused(3)
    out = CrossEncoderReranker(_FakeScorer({})).rerank("q", fused, top_k=3)  # all score 0.0
    assert [c.key for c in out] == ["c0", "c1", "c2"]  # equal scores → fused order preserved


def test_cross_encoder_truncates_to_top_k() -> None:
    fused = _fused(5)
    out = CrossEncoderReranker(_FakeScorer({})).rerank("q", fused, top_k=2)
    assert len(out) == 2


def test_cross_encoder_empty_pool_is_empty() -> None:
    assert CrossEncoderReranker(_FakeScorer({})).rerank("q", [], top_k=5) == []


# ---- FailSoftReranker: absence / error / timeout → fused -------------------


def test_failsoft_with_no_inner_returns_fused_order() -> None:
    fused = _fused(4)
    out = FailSoftReranker(None).rerank("q", fused, top_k=3)
    assert [c.key for c in out] == ["c0", "c1", "c2"]  # fused order, truncated


def test_failsoft_swallows_an_inner_error_and_returns_fused() -> None:
    class _Boom:
        def rerank(self, query: str, candidates: list, *, top_k: int) -> list:  # noqa: ARG002
            raise RuntimeError("model died")

    reasons: list[str] = []
    out = FailSoftReranker(_Boom(), on_fallback=reasons.append).rerank("q", _fused(3), top_k=3)
    assert [c.key for c in out] == ["c0", "c1", "c2"]
    assert reasons == ["error"]


def test_failsoft_turns_a_timeout_into_fused_order() -> None:
    class _Slow:
        def rerank(self, query: str, candidates: list, *, top_k: int) -> list:  # noqa: ARG002
            raise RerankTimeoutError("too slow")

    reasons: list[str] = []
    out = FailSoftReranker(_Slow(), on_fallback=reasons.append).rerank("q", _fused(3), top_k=2)
    assert [c.key for c in out] == ["c0", "c1"]
    assert reasons == ["timeout"]


def test_failsoft_reports_absent_reason() -> None:
    reasons: list[str] = []
    FailSoftReranker(None, on_fallback=reasons.append).rerank("q", _fused(1), top_k=1)
    assert reasons == ["absent"]


# ---- DeadlineReranker: wall-clock bound -----------------------------------


def test_deadline_reranker_passes_through_a_fast_rerank() -> None:
    fused = _fused(3)
    scorer = _FakeScorer({"text 0": 0.9, "text 1": 0.5, "text 2": 0.1})
    inner = CrossEncoderReranker(scorer)
    out = DeadlineReranker(inner, timeout_s=1.0).rerank("q", fused, top_k=3)
    assert [c.rerank_text for c in out] == ["text 0", "text 1", "text 2"]


def test_deadline_reranker_raises_on_a_slow_rerank() -> None:
    class _Hang:
        def rerank(self, query: str, candidates: list, *, top_k: int) -> list:  # noqa: ARG002
            time.sleep(0.5)
            return list(candidates)

    with pytest.raises(RerankTimeoutError):
        DeadlineReranker(_Hang(), timeout_s=0.05).rerank("q", _fused(3), top_k=3)


def test_deadline_breach_degrades_to_fused_under_failsoft() -> None:
    # The composed shell: a hanging reranker times out and the turn still gets fused order.
    class _Hang:
        def rerank(self, query: str, candidates: list, *, top_k: int) -> list:  # noqa: ARG002
            time.sleep(0.5)
            return list(candidates)

    reasons: list[str] = []
    bounded = DeadlineReranker(_Hang(), timeout_s=0.05)
    out = FailSoftReranker(bounded, on_fallback=reasons.append).rerank("q", _fused(3), top_k=3)
    assert [c.key for c in out] == ["c0", "c1", "c2"]
    assert reasons == ["timeout"]


# ---- build_reranker: the env-gated composition seam -----------------------


def test_build_reranker_disabled_returns_the_fused_path() -> None:
    reranker = build_reranker(scorer=_FakeScorer({}), settings=RecallSettings(rerank_enabled=False))
    assert isinstance(reranker, FailSoftReranker)
    # Even with a scorer, a disabled gate never reorders — fused order out.
    out = reranker.rerank("q", _fused(2), top_k=2)
    assert [c.key for c in out] == ["c0", "c1"]


def test_build_reranker_enabled_without_a_scorer_is_the_fused_path() -> None:
    reranker = build_reranker(scorer=None, settings=RecallSettings(rerank_enabled=True))
    out = reranker.rerank("q", _fused(2), top_k=2)
    assert [c.key for c in out] == ["c0", "c1"]  # P7-absent → fused


def test_build_reranker_enabled_with_a_scorer_reranks() -> None:
    scorer = _FakeScorer({"text 1": 0.9, "text 0": 0.1})
    reranker = build_reranker(scorer=scorer, settings=RecallSettings(rerank_enabled=True))
    out = reranker.rerank("q", _fused(2), top_k=2)
    assert [c.rerank_text for c in out] == ["text 1", "text 0"]  # the scorer governed order


def test_build_reranker_with_a_timeout_still_reranks_when_fast() -> None:
    scorer = _FakeScorer({"text 0": 0.9, "text 1": 0.1})
    reranker = build_reranker(
        scorer=scorer, settings=RecallSettings(rerank_enabled=True), timeout_s=1.0
    )
    out = reranker.rerank("q", _fused(2), top_k=2)
    assert [c.rerank_text for c in out] == ["text 0", "text 1"]


def test_build_reranker_returns_a_reranker_protocol() -> None:
    reranker: Reranker = build_reranker(scorer=None, settings=RecallSettings())
    assert reranker.rerank("q", [], top_k=1) == []

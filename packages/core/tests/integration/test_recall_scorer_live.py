"""Live cross-encoder leg — real model download, sane ordering, composed p95 (K9 re-verify).

Marked ``integration`` (network: first run downloads the ~90 MB default model into the HF
cache). Re-verifies K9's measured reference numbers AT THE COMPOSED LEVEL — through
``build_reranker`` (FailSoft → Deadline → CrossEncoderReranker → CrossEncoderScorer), not a
bare ``predict`` — against the production deadlines: voice top-12 within 120 ms, chat
top-20 within 800 ms (p95 over 50 warm iterations). A budget breach FAILS the test — it
must be reported, never hidden. Every leg counts FailSoft fallbacks and requires ZERO:
a fused-order fallback (e.g. the scorer refusing non-finite kernels — the measured
dev-box GEMM fault) must fail this suite loudly, never false-green it.

Run: ``uv run pytest packages/core/tests/integration/test_recall_scorer_live.py -m integration -s``
"""

from __future__ import annotations

import statistics
import time

import pytest
from persona.recall.config import RecallSettings
from persona.recall.models import RecallCandidate, RecallSource
from persona.recall.rerank import build_reranker
from persona.recall.scorer import CrossEncoderScorer

from tests.unit.recall._fixtures import chunk

pytestmark = [pytest.mark.integration, pytest.mark.timeout(600)]

# Realistic memory-gist-length texts: the measurement is honest only if the tokenised
# pair length resembles a production rerank_text, not a two-word toy.
_FILLER = (
    "we talked about the weekend plans and the weather in the mountains, "
    "the hiking route near the cabin, and what groceries to bring for the trip"
)


def _cand(key: str, rank: int, text: str) -> RecallCandidate:
    return RecallCandidate(
        source=RecallSource.EPISODIC_RAW, key=key, chunk=chunk(key, text=text), rank=rank
    )


def _pool(n: int) -> list[RecallCandidate]:
    return [_cand(f"c{i}", i + 1, f"episode {i}: {_FILLER}") for i in range(n)]


@pytest.fixture(scope="module")
def scorer() -> CrossEncoderScorer:
    """One scorer for the module — the first score pays the real download/load."""
    return CrossEncoderScorer(model_name=RecallSettings().rerank_model)


def test_real_model_ranks_relevant_above_distractors(scorer: CrossEncoderScorer) -> None:
    # Composed exactly as the chat seam does (deadline generous: this leg also covers
    # the cold load, which production absorbs as one fused-order turn).
    fallbacks: list[str] = []
    reranker = build_reranker(
        scorer=scorer,
        settings=RecallSettings(rerank_enabled=True),
        timeout_s=None,
        on_fallback=fallbacks.append,
    )
    fused = [
        _cand("distractor-1", 1, "the user asked about the weather forecast for paris"),
        _cand("distractor-2", 2, "we discussed a pasta recipe with tomatoes and basil"),
        _cand(
            "relevant", 3, "the user said they moved to oslo and started a new job at a university"
        ),
        _cand("distractor-3", 4, "the persona recommended a science-fiction novel to read"),
    ]
    out = reranker.rerank("where does the user live and work?", fused, top_k=4)

    assert fallbacks == [], (
        f"reranker fell back to fused ({fallbacks}) — the scorer refused this platform "
        "(non-finite kernels?); the live leg cannot pass on a broken box"
    )
    assert out[0].key == "relevant", f"cross-encoder ranked {out[0].key!r} first"
    assert 0.0 <= out[0].relevance <= 1.0  # calibrated sigmoid output (K9-D-5)
    assert out[0].relevance > out[-1].relevance


@pytest.mark.parametrize(
    ("top_k", "budget_ms", "path"),
    [(12, 120.0, "voice"), (20, 800.0, "chat")],
)
def test_composed_rerank_p95_stays_inside_the_path_budget(
    scorer: CrossEncoderScorer, top_k: int, budget_ms: float, path: str
) -> None:
    settings = RecallSettings(rerank_enabled=True)
    # timeout_s=None: measure the true composed latency — a DeadlineReranker would
    # mask a breach as a fused-order fallback instead of failing this test loudly.
    fallbacks: list[str] = []
    reranker = build_reranker(
        scorer=scorer, settings=settings, timeout_s=None, on_fallback=fallbacks.append
    )
    pool = _pool(top_k)
    query = "what did the user decide about moving to oslo for the new university job?"

    reranker.rerank(query, pool, top_k=top_k)  # warm (load + first inference)

    samples_ms: list[float] = []
    for _ in range(50):
        t0 = time.perf_counter()
        out = reranker.rerank(query, pool, top_k=top_k)
        samples_ms.append((time.perf_counter() - t0) * 1000.0)
        assert len(out) == top_k

    samples_ms.sort()
    p50 = statistics.median(samples_ms)
    p95 = samples_ms[int(len(samples_ms) * 0.95) - 1]
    print(
        f"\n[recall-scorer] {path} top-{top_k}: p50={p50:.1f}ms p95={p95:.1f}ms "
        f"max={samples_ms[-1]:.1f}ms budget={budget_ms:.0f}ms "
        f"fallbacks={len(fallbacks)} (n=50, composed, CPU)"
    )
    assert fallbacks == [], (
        f"{len(fallbacks)} fused fallbacks during the {path} measurement — these "
        "timings measured the FALLBACK path, not a real rerank; the platform is broken"
    )
    assert p95 < budget_ms, (
        f"{path} top-{top_k} p95 {p95:.1f}ms breaches the {budget_ms:.0f}ms budget — "
        "the K9 reference numbers do NOT hold on this machine; report, don't hide"
    )

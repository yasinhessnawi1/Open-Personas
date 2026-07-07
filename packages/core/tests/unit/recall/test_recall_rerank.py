"""The reranker seam — the model-free Protocol + the identity stub (Spec K9, T1/T3).

Pins the stub's fail-soft-target behaviour (fused order unchanged, truncated to top_k) and
that persona-core carries NO model dependency (K9-D-12): importing the recall package must
not drag in torch / transformers / sentence-transformers / onnxruntime.
"""

from __future__ import annotations

import sys

from persona.recall.models import RecallCandidate, RecallSource
from persona.recall.rerank import IdentityReranker, Reranker

from tests.unit.recall._fixtures import chunk


def _cand(key: str, rank: int) -> RecallCandidate:
    return RecallCandidate(source=RecallSource.EPISODIC_RAW, key=key, chunk=chunk(key), rank=rank)


def test_identity_reranker_preserves_fused_order() -> None:
    fused = [_cand("a", 1), _cand("b", 2), _cand("c", 3)]
    out = IdentityReranker().rerank("q", fused, top_k=3)
    assert [c.key for c in out] == ["a", "b", "c"]


def test_identity_reranker_truncates_to_top_k() -> None:
    fused = [_cand("a", 1), _cand("b", 2), _cand("c", 3)]
    out = IdentityReranker().rerank("q", fused, top_k=2)
    assert [c.key for c in out] == ["a", "b"]


def test_identity_reranker_on_empty_pool_is_empty() -> None:
    assert IdentityReranker().rerank("q", [], top_k=5) == []


def test_identity_reranker_satisfies_the_protocol() -> None:
    reranker: Reranker = IdentityReranker()
    assert reranker.rerank("q", [], top_k=1) == []


def test_recall_package_imports_no_model_runtime() -> None:
    # K9-D-12: the skeleton must never hard-depend on the real P7 model. Importing the
    # recall package must not pull a heavy model runtime into the process.
    heavy = {"torch", "transformers", "sentence_transformers", "onnxruntime"}
    already = heavy & set(sys.modules)
    import persona.recall  # noqa: F401 — the side effect under test

    newly_imported = (heavy & set(sys.modules)) - already
    assert not newly_imported, f"recall pulled in a model runtime: {newly_imported}"

"""Unified recall — the K9 turn-time memory path (fuse-don't-route → rerank → …).

One deterministic, latency-bounded recall path over the K8 episodic pyramid **and** the
K7 knowledge graph (Spec K9). The T1/T2 foundation ships the candidate contract
(:class:`~persona.recall.models.RecallCandidate`), the tunables
(:class:`~persona.recall.config.RecallSettings`), the reranker seam
(:class:`~persona.recall.rerank.Reranker` + the :class:`~persona.recall.rerank.IdentityReranker`
stub), the N-leg fusion (:func:`~persona.recall.fusion.fuse`), the leg adapters
(:mod:`persona.recall.sources`), and the stub-safe skeleton
(:func:`~persona.recall.pipeline.run_recall`). Later tasks add the composite score +
contiguity, the graph-diffusion tiebreak, and the confidence/abstention gate around this
core. The reranker is a **model-free Protocol** — no model is imported here (K9-D-12).
"""

from __future__ import annotations

from persona.recall.config import RecallSettings
from persona.recall.contiguity import (
    DrillStop,
    Episode,
    EpisodeProvider,
    QuestionType,
    classify_question_type,
    expand_contiguity,
)
from persona.recall.core_memory import (
    CoreBlock,
    build_core_block,
    core_block_from_chunk,
    core_block_to_chunk,
    read_core_block,
    refresh_core_block,
)
from persona.recall.diffusion import NeighbourProvider, apply_diffusion, is_multi_hop
from persona.recall.errors import RecallError, RerankTimeoutError
from persona.recall.fusion import fuse
from persona.recall.gate import (
    AbstentionOutcome,
    AbstentionReason,
    compose_gate,
    passes_confidence,
)
from persona.recall.models import LEG_SOURCE, RecallCandidate, RecallLeg, RecallSource
from persona.recall.pipeline import RecallResult, compose_recall, run_recall
from persona.recall.rerank import (
    CrossEncoderReranker,
    DeadlineReranker,
    FailSoftReranker,
    IdentityReranker,
    Reranker,
    Scorer,
    build_reranker,
)
from persona.recall.scorer import CrossEncoderScorer, build_scorer, shared_scorer
from persona.recall.scoring import apply_composite_score, composite_components
from persona.recall.sources import gather_legs, graph_legs

__all__ = [
    "LEG_SOURCE",
    "AbstentionOutcome",
    "AbstentionReason",
    "CoreBlock",
    "CrossEncoderReranker",
    "CrossEncoderScorer",
    "DeadlineReranker",
    "DrillStop",
    "Episode",
    "EpisodeProvider",
    "FailSoftReranker",
    "IdentityReranker",
    "NeighbourProvider",
    "QuestionType",
    "RecallCandidate",
    "RecallError",
    "RecallLeg",
    "RecallResult",
    "RecallSettings",
    "RecallSource",
    "Reranker",
    "RerankTimeoutError",
    "Scorer",
    "apply_composite_score",
    "apply_diffusion",
    "build_core_block",
    "build_reranker",
    "build_scorer",
    "classify_question_type",
    "compose_gate",
    "compose_recall",
    "composite_components",
    "core_block_from_chunk",
    "core_block_to_chunk",
    "expand_contiguity",
    "fuse",
    "gather_legs",
    "graph_legs",
    "is_multi_hop",
    "passes_confidence",
    "read_core_block",
    "refresh_core_block",
    "run_recall",
    "shared_scorer",
]

"""Domain errors for the unified-recall path (Spec K9).

Kept tiny and specific (the ``persona.graph.errors`` / ``persona.stores.errors``
precedent — domain exceptions, never bare ``RuntimeError``). The reranker shell raises
:class:`RerankTimeoutError` on a wall-clock breach; the fail-soft wrapper catches it (and every
other failure) and degrades to the un-reranked fused order — a rerank stall never breaks a
turn (K9-D-3/D-4).
"""

from __future__ import annotations

__all__ = ["RecallError", "RerankTimeoutError"]


class RecallError(Exception):
    """Base class for unified-recall domain errors."""


class RerankTimeoutError(RecallError):
    """The reranker exceeded its wall-clock deadline (the fail-soft trigger).

    Raised by :class:`~persona.recall.rerank.DeadlineReranker` when the bounded
    synchronous rerank does not return within its budget. The
    :class:`~persona.recall.rerank.FailSoftReranker` catches it and returns the fused
    order, so a slow reranker degrades ordering — it never stalls or breaks the turn.
    """

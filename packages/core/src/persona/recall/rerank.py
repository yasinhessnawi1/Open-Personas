"""The cross-encoder reranker seam — Protocol, P7 adapter, and the fail-soft shell (K9, T1/T3).

The cross-encoder over the fused top-k is the single most reliably-proven recall lift
(K9-D-3; the survey's "beyond the reranker" ablation collapses ~94% without it). It is a
**named P7 dependency, stub-first**: persona-core owns the **model-free** seam — no torch,
no transformers, no model import (K9-D-12) — and P7 lands the real model behind it.

The seam has three layers:

- :class:`Reranker` — the Protocol the pipeline consumes; :class:`IdentityReranker` is the
  no-op stub (and the fail-soft fallback's behaviour: fused order unchanged).
- :class:`Scorer` + :class:`CrossEncoderReranker` — the **P7 adapter seam**. P7 provides a
  ``Scorer`` (``(query, texts) -> scores``); the reranker orders candidates by it. Two
  path-specific P7 scorers plug in behind the *one* seam (K9-D-3): a decoder-class model on
  the chat/GPU path, a tiny encoder on the voice/off-loop path. The ordering logic is built
  and tested now against a fake scorer; P7 supplies the real one later.
- :class:`DeadlineReranker` + :class:`FailSoftReranker` — the **load-bearing safety shell**.
  Kill-switch / P7-absent / error / timeout all degrade to the **un-reranked fused order**
  (the already-green skeleton path); the turn never blocks and never crashes (K9-D-3/D-4).
  ``DeadlineReranker`` bounds a *synchronous* rerank (the chat path) with a wall clock; the
  voice path applies its deadline off the event loop in the composition (T9,
  ``asyncio.wait_for`` over ``to_thread``) and degrades to fused the same way — a rerank
  stall never stalls the spoken turn.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from typing import TYPE_CHECKING, Protocol

from persona.logging import get_logger
from persona.recall.errors import RerankTimeoutError

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from persona.recall.config import RecallSettings
    from persona.recall.models import RecallCandidate

__all__ = [
    "CrossEncoderReranker",
    "DeadlineReranker",
    "FailSoftReranker",
    "IdentityReranker",
    "Reranker",
    "Scorer",
    "build_reranker",
]

_LOG = "recall.rerank"


class Reranker(Protocol):
    """Re-order the fused top-k by cross-encoder relevance to the query (K9-D-3).

    A structural type so the runtime/voice composition can wire P7's real model without
    persona-core importing it, and so the skeleton runs against a stub. Implementations
    read each candidate's :attr:`~persona.recall.models.RecallCandidate.rerank_text`,
    score it against ``query``, and return the ``top_k`` best — **re-ordering only**, so
    a failure or absence degrades to the input (fused) order without losing recall.
    """

    def rerank(
        self,
        query: str,
        candidates: Sequence[RecallCandidate],
        *,
        top_k: int,
    ) -> list[RecallCandidate]:
        """Return up to ``top_k`` candidates, best-first by cross-encoder score."""
        ...


class Scorer(Protocol):
    """The P7 cross-encoder — score each candidate's text against the query (K9-D-3).

    The single model-facing seam: P7 lands two implementations (a chat decoder-class model,
    a voice tiny encoder) behind this Protocol, so persona-core imports no model. ``score``
    returns one float per text, aligned by index; higher is more relevant.
    """

    def score(self, query: str, texts: Sequence[str]) -> Sequence[float]:
        """Return a relevance score in ``[0, 1]`` per text (index-aligned with ``texts``).

        Higher is more relevant. The score is treated as the **absolute reranked
        relevance** (K9-D-5) — it overwrites each candidate's fused cosine reading and is
        what the composite score and the abstention floor (K9-D-9) read — so P7's model must
        emit a calibrated ``[0, 1]`` relevance (a sigmoid cross-encoder score), not an
        uncalibrated logit.
        """
        ...


class IdentityReranker:
    """The no-op stub — returns the fused order unchanged, truncated (K9-D-12).

    The buildable-now stand-in for P7's model and the semantic of the fail-soft fallback:
    it does not re-order, so the skeleton produces results (degraded ordering, never a
    crash) and never depends on a real model. Deterministic and free.
    """

    def rerank(
        self,
        query: str,  # noqa: ARG002 — the stub ignores the query (fused order); Protocol conformance
        candidates: Sequence[RecallCandidate],
        *,
        top_k: int,
    ) -> list[RecallCandidate]:
        """Return the first ``top_k`` candidates in their given (fused) order."""
        return list(candidates[:top_k])


class CrossEncoderReranker:
    """Order the fused pool by a P7 :class:`Scorer` (the P7 adapter, K9-D-3).

    Reads each candidate's ``rerank_text``, scores the batch against the query with the
    injected scorer, and returns the ``top_k`` highest — ties broken by the candidate's
    fused ``rank`` (ascending) so equal scores preserve the fused order deterministically.
    The scorer is injected, so this ordering logic is built and tested against a fake now;
    P7's real model swaps in with no change here.
    """

    def __init__(self, scorer: Scorer) -> None:
        """Inject the P7 cross-encoder scorer."""
        self._scorer = scorer

    def rerank(
        self,
        query: str,
        candidates: Sequence[RecallCandidate],
        *,
        top_k: int,
    ) -> list[RecallCandidate]:
        """Return up to ``top_k`` candidates, best-first by the scorer's relevance."""
        if not candidates:
            return []
        scores = list(self._scorer.score(query, [c.rerank_text for c in candidates]))
        if len(scores) != len(candidates):
            msg = f"scorer returned {len(scores)} scores for {len(candidates)} candidates"
            raise ValueError(msg)
        order = sorted(
            range(len(candidates)),
            key=lambda i: (-scores[i], candidates[i].rank),
        )
        # The reranked score is the authoritative absolute relevance (K9-D-5): it overwrites
        # the fused cosine reading so the composite score and the abstention floor read it.
        return [
            candidates[i].model_copy(update={"relevance": max(0.0, min(1.0, scores[i]))})
            for i in order[:top_k]
        ]


class DeadlineReranker:
    """Bound a synchronous rerank with a wall clock; breach ⇒ :class:`RerankTimeoutError` (K9-D-3).

    Runs the inner rerank in a single worker thread and waits at most ``timeout_s``. On
    breach it raises :class:`RerankTimeoutError` (which :class:`FailSoftReranker` turns into the
    fused order) and abandons the worker **without blocking on it** — a hung reranker never
    stalls the turn. This bounds the **chat (synchronous) path**; the voice path applies its
    deadline off the event loop in the composition (T9) and degrades to fused identically.
    Defense-in-depth: P7's own client should also carry a socket/RPC timeout.
    """

    def __init__(self, inner: Reranker, *, timeout_s: float) -> None:
        """Wrap ``inner`` with a per-call wall-clock ``timeout_s`` (seconds)."""
        self._inner = inner
        self._timeout_s = timeout_s

    def rerank(
        self,
        query: str,
        candidates: Sequence[RecallCandidate],
        *,
        top_k: int,
    ) -> list[RecallCandidate]:
        """Rerank within the deadline; raise :class:`RerankTimeoutError` on breach."""
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(self._inner.rerank, query, candidates, top_k=top_k)
        try:
            result = future.result(timeout=self._timeout_s)
        except FuturesTimeout:
            executor.shutdown(wait=False)  # never block on the abandoned worker
            msg = f"rerank exceeded {self._timeout_s:.3f}s deadline"
            raise RerankTimeoutError(msg) from None
        executor.shutdown(wait=False)
        return result


class FailSoftReranker:
    """The safety wrapper — any failure or absence ⇒ the fused order (K9-D-3/D-4).

    The load-bearing degradation: an ``inner`` of ``None`` (P7 not composed / kill-switch
    off), a :class:`RerankTimeoutError`, or **any** exception from the inner reranker returns the
    input candidates unchanged (they arrive already fused), truncated to ``top_k``. The turn
    never blocks and never crashes — the reranker is a precision boost over a pool that is
    already correct on recall, so fused order is a lossless fallback. Each fallback is logged
    and, optionally, reported to a health counter (P8).
    """

    def __init__(
        self,
        inner: Reranker | None,
        *,
        on_fallback: Callable[[str], None] | None = None,
    ) -> None:
        """Wrap ``inner`` (``None`` ⇒ always the fused order); optional fallback counter."""
        self._inner = inner
        self._on_fallback = on_fallback

    def rerank(
        self,
        query: str,
        candidates: Sequence[RecallCandidate],
        *,
        top_k: int,
    ) -> list[RecallCandidate]:
        """Rerank, degrading to the fused order on absence / timeout / any error."""
        if self._inner is None:
            return self._fused(candidates, top_k, reason="absent")
        try:
            return self._inner.rerank(query, candidates, top_k=top_k)
        except RerankTimeoutError:
            return self._fused(candidates, top_k, reason="timeout")
        except Exception:  # noqa: BLE001 — deliberately fail-soft: never break the turn
            get_logger(_LOG).warning("reranker error; fused order (n={n})", n=len(candidates))
            return self._fused(candidates, top_k, reason="error")

    def _fused(
        self, candidates: Sequence[RecallCandidate], top_k: int, *, reason: str
    ) -> list[RecallCandidate]:
        if reason != "error":  # the error path already logged with its own detail
            get_logger(_LOG).debug(
                "rerank fallback={reason}; fused (n={n})", reason=reason, n=len(candidates)
            )
        if self._on_fallback is not None:
            self._on_fallback(reason)
        return list(candidates[:top_k])


def build_reranker(
    *,
    scorer: Scorer | None,
    settings: RecallSettings,
    timeout_s: float | None = None,
    on_fallback: Callable[[str], None] | None = None,
) -> Reranker:
    """Compose the path's reranker behind the fail-soft shell (K9-D-3/D-4 — the seam).

    The composition root (chat T8 / voice T9) calls this once. When reranking is disabled
    (``settings.rerank_enabled`` is the env kill-switch) or no P7 scorer is wired, it
    returns a fail-soft wrapper over ``None`` — i.e. the fused-order path, byte-equivalent
    to the stub. Otherwise it wraps the :class:`CrossEncoderReranker`, optionally bounded by
    ``timeout_s`` (the chat sync deadline; the voice path passes ``None`` here and bounds the
    call off-loop instead), in the :class:`FailSoftReranker`. Either way, absence / timeout /
    error all degrade to fused.

    Args:
        scorer: The P7 cross-encoder scorer for this path, or ``None`` (⇒ fused order).
        settings: The recall tunables (``rerank_enabled`` is the env gate).
        timeout_s: Wall-clock deadline for the synchronous (chat) path; ``None`` ⇒ unbounded
            here (the voice path bounds off-loop in T9).
        on_fallback: Optional health-counter callback invoked with the fallback reason.

    Returns:
        A :class:`Reranker` that is safe to call on any turn — never blocks, never crashes.
    """
    if not settings.rerank_enabled or scorer is None:
        return FailSoftReranker(None, on_fallback=on_fallback)
    inner: Reranker = CrossEncoderReranker(scorer)
    if timeout_s is not None:
        inner = DeadlineReranker(inner, timeout_s=timeout_s)
    return FailSoftReranker(inner, on_fallback=on_fallback)

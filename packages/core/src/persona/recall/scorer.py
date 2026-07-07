"""The CPU cross-encoder scorer behind the K9 reranker seam (the first real model).

Implements :class:`~persona.recall.rerank.Scorer` with a MiniLM-L6-class cross-encoder
via ``sentence_transformers.CrossEncoder`` — the K9-measured reference model (top-12 p95
~51 ms, top-20 p95 ~111 ms on this CPU), well inside the voice 120 ms / chat 800 ms
deadlines. P7 later swaps a stronger chat-tier model behind the SAME ``Scorer`` Protocol;
nothing above this module changes.

The discipline mirrors the embedder (:mod:`persona.stores.embedder`) and preserves
K9-D-12's import isolation:

- **Lazy, thread-safe load** — the model is loaded on the FIRST ``score`` (never at
  construction) under a double-checked lock, and the ``sentence_transformers`` import
  lives inside that load, so importing :mod:`persona.recall` never drags in a model
  runtime. The first score runs inside the reranker's worker thread (chat
  ``DeadlineReranker`` / voice off-loop ``to_thread``), so a cold load never blocks the
  event loop; if it outlives the rerank deadline that one turn degrades to the fused
  order while the load completes in the abandoned worker — subsequent turns score warm.
- **Fail-soft by propagation** — a load/download failure raises out of ``score``; the
  composed :class:`~persona.recall.rerank.FailSoftReranker` turns it into the fused
  order (K9-D-3/D-4). This module never swallows model errors itself.
- **Calibrated output** — the K9-D-5 contract requires an absolute ``[0, 1]`` relevance,
  so the sigmoid activation is pinned EXPLICITLY at load (never left to hub config —
  the consolidated hub repo ships ``Identity``, i.e. raw logits, which would break the
  abstention floor).
- **CPU, one batch** — the device is explicit ``cpu`` (no auto-detect surprises on the
  V100 box), and the fused pool (≤ ~20 texts) is scored in a single ``predict`` batch.
- **Finite-kernel self-check** — measured on this repo's dev box (M1/8 GB under swap
  exhaustion, reproduced across torch 2.7 and 2.12): CPU GEMM kernels can emit ``inf``
  mid-encoder from verifiably-clean fp32 weights and small inputs (mathematically
  impossible from healthy arithmetic — a platform/memory-level fault, intermittent
  SIGBUS included). The load therefore scores a canned padded batch FIRST and refuses
  (raises) on any non-finite result — the model is not cached, so a later turn retries
  cheaply and recovers if the platform does. Every real ``score`` keeps a finiteness
  backstop — garbage NEVER becomes an ordering or an abstention-floor read; the shell
  turns both refusals into the fused order.

:func:`shared_scorer` keys one instance per model id process-wide (the model is ~90 MB —
never load it twice), and :func:`build_scorer` is the composition-root factory both the
chat (``runtime_factory``) and voice (``model/graph``) seams call: ``None`` unless
``rerank_enabled``, and fail-soft to ``None`` (today's fused-order path) on any
construction error.
"""

from __future__ import annotations

import math
import threading
from typing import TYPE_CHECKING

from persona.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

    from persona.recall.config import RecallSettings

__all__ = ["CrossEncoderScorer", "build_scorer", "shared_scorer"]

_log = get_logger("recall.scorer")

# The load-time self-check batch: a trivially-separable pair plus varied-length
# distractors, so the padded-batch kernel path (where the measured inf fault lives) is
# exercised before the model is ever trusted with a real turn.
_SELF_CHECK_QUERY = "how many people live in berlin"
_SELF_CHECK_TEXTS: tuple[str, ...] = (
    "Berlin has a population of 3.5 million people.",
    "Paris is the capital of France.",
    "a short note",
    "the user asked for a science-fiction novel recommendation for the weekend trip",
)


class CrossEncoderScorer:
    """A ``sentence_transformers.CrossEncoder`` scorer with lazy CPU load.

    Args:
        model_name: HuggingFace hub id of a cross-encoder reranking model
            (default source: ``RecallSettings.rerank_model``).
        batch_size: Floor for the ``predict`` batch size; the fused pool is always
            scored in ONE batch (the effective batch size is ``max(batch_size,
            len(texts))``).
    """

    def __init__(self, *, model_name: str, batch_size: int = 32) -> None:
        self.model_name = model_name
        self._batch_size = batch_size
        self._model: object | None = None
        # The scorer is process-shared (``shared_scorer``) and first-scored from
        # reranker worker threads (chat DeadlineReranker / voice to_thread), possibly
        # concurrently across turns. Double-checked locking serialises the one-time
        # construction — concurrent construction corrupts torch's meta-device init
        # (the embedder's measured failure mode).
        self._load_lock = threading.Lock()

    def _load(self) -> object:
        if self._model is not None:
            return self._model
        with self._load_lock:
            if self._model is not None:
                return self._model
            # Lazy import (K9-D-12): the model runtime enters the process only when a
            # rerank actually happens — importing persona.recall stays model-free.
            from sentence_transformers import CrossEncoder
            from torch import nn

            _log.info(
                "loading cross-encoder model={model} device=cpu",
                model=self.model_name,
            )
            # Explicit sigmoid: the Scorer contract is a calibrated [0,1] relevance
            # (K9-D-5 — the abstention floor reads it); never trust hub config to pick it.
            model = CrossEncoder(
                self.model_name,
                device="cpu",
                default_activation_function=nn.Sigmoid(),
            )
            self._verify_finite_kernels(model)
            self._model = model
            return model

    def _predict(self, model: object, query: str, texts: Sequence[str]) -> list[float]:
        scores = model.predict(  # type: ignore[attr-defined]
            [(query, text) for text in texts],
            batch_size=max(self._batch_size, len(texts)),
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        return [float(s) for s in scores]

    def _verify_finite_kernels(self, model: object) -> None:
        """Refuse to serve a model whose kernels emit non-finite scores (load-time gate).

        The measured fault (dev box, M1/8 GB under swap exhaustion; reproduced across
        torch 2.7 and 2.12): CPU GEMMs emit ``inf`` mid-encoder from verifiably-clean
        weights on padded batches — every score comes back NaN while the model "works".
        A non-finite self-check raises, so the model is NOT cached and the composed
        fail-soft shell keeps the turn on the fused order instead of an arbitrary NaN
        sort; the next turn retries the load (cheap when the weights are OS-cached) and
        recovers as soon as the platform computes finitely again.
        """
        scores = self._predict(model, _SELF_CHECK_QUERY, _SELF_CHECK_TEXTS)
        if all(math.isfinite(s) for s in scores):
            return
        _log.warning(
            "cross-encoder self-check emitted non-finite scores; refusing to serve "
            "(model={model}) — reranking degrades to the fused order",
            model=self.model_name,
        )
        msg = (
            f"cross-encoder {self.model_name!r} emits non-finite scores on this "
            "platform (broken kernel path); refusing to serve — reranking degrades "
            "to the fused order"
        )
        raise RuntimeError(msg)

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        """Score every text against ``query`` in one batch; ``[0, 1]``, index-aligned.

        The first call triggers the model load (+ the finite-kernel self-check); a load,
        self-check, or inference failure — including a non-finite score — raises, and
        the composed :class:`~persona.recall.rerank.FailSoftReranker` degrades that turn
        to the fused order.
        """
        if not texts:
            return []
        model = self._load()
        scores = self._predict(model, query, texts)
        if not all(math.isfinite(s) for s in scores):
            msg = f"cross-encoder {self.model_name!r} returned non-finite scores"
            raise RuntimeError(msg)
        return scores


# One scorer per model id per process: both the chat and voice compositions (and every
# per-persona / per-call rebuild) must reuse the SAME lazily-loaded ~90 MB model. A
# module-level registry is deliberate here — the seams have no shared app container
# spanning the per-call composition roots, and the cache is keyed, lock-guarded state,
# not configuration.
_shared_lock = threading.Lock()
_shared_scorers: dict[str, CrossEncoderScorer] = {}


def shared_scorer(model_name: str) -> CrossEncoderScorer:
    """Return the process-wide :class:`CrossEncoderScorer` for ``model_name``."""
    with _shared_lock:
        scorer = _shared_scorers.get(model_name)
        if scorer is None:
            scorer = CrossEncoderScorer(model_name=model_name)
            _shared_scorers[model_name] = scorer
        return scorer


def build_scorer(settings: RecallSettings) -> CrossEncoderScorer | None:
    """The composition-root scorer factory (chat T8 / voice T9 call this).

    Returns ``None`` when ``rerank_enabled`` is off — and fail-soft ``None`` on any
    construction error — so :func:`~persona.recall.rerank.build_reranker` composes
    exactly today's fused-order path (byte-identical to the stub). Construction is
    cheap and model-free; the model itself loads lazily on the first score.
    """
    if not settings.rerank_enabled:
        return None
    try:
        return shared_scorer(settings.rerank_model)
    except Exception:  # noqa: BLE001 — deliberately fail-soft: composition must not break
        _log.warning(
            "cross-encoder scorer construction failed; fused order (model={model})",
            model=settings.rerank_model,
        )
        return None

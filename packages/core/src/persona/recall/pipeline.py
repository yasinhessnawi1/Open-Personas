"""The unified-recall pipeline — fuse → rerank → score → contiguity → diffusion → gate (K9).

Two entry points over one path:

- :func:`run_recall` — the **T2/T3 skeleton** (fuse → rerank). Stub-safe (K9-D-12): with the
  :class:`~persona.recall.rerank.IdentityReranker` it produces fused (un-reranked) order —
  degraded ordering, never a crash, never a hard dependency on the real P7 model.
- :func:`compose_recall` — the **full deterministic path** (T4–T6 compose around the skeleton):
  fuse (K9-D-1) → rerank, fail-soft (K9-D-3/D-4) → composite score (K9-D-5) → temporal
  contiguity (K9-D-6/D-7) → gated diffusion tiebreak (K9-D-8) → K4-then-abstention gate
  (K9-D-9). Every stage past fuse is optional (its provider ``None`` ⇒ that stage is skipped),
  so the same path runs stub-safe with no providers *and* fully composed in T8/T9. The
  reranker stays injected, so the whole path runs with a stub — the K9-D-12 property holds
  end-to-end, not just at the skeleton.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from persona.recall.contiguity import classify_question_type, expand_contiguity
from persona.recall.diffusion import apply_diffusion
from persona.recall.fusion import fuse
from persona.recall.gate import AbstentionReason, compose_gate
from persona.recall.models import RecallCandidate, RecallSource
from persona.recall.scoring import apply_composite_score

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime

    from persona.graph.models import ConceptNode
    from persona.recall.config import RecallSettings
    from persona.recall.contiguity import EpisodeProvider, QuestionType
    from persona.recall.diffusion import NeighbourProvider
    from persona.recall.models import RecallLeg
    from persona.recall.rerank import Reranker
    from persona.schema.chunks import PersonaChunk
    from persona.stores.lifecycle import EpisodicSettings

__all__ = ["RecallResult", "compose_recall", "run_recall"]


@dataclass(frozen=True)
class RecallResult:
    """The composed recall's output — what surfaces + why + what to reinforce.

    Attributes:
        surfaced: The candidates that may reach the prompt (post-policy, best-first); empty
            when the gate abstained.
        abstained: ``True`` when the honest-absence seam should render (no memory surfaces).
        reason: The internal abstention reason (never surfaced to the user — telemetry only).
        reinforce_ids: The raw episodic chunk ids to reinforce (K8-D-5): the surfaced
            raw-episodic hits **plus** the members pulled by contiguity through a gist hit
            (handover §4.2). The caller (chat sync / voice off-loop) runs the reinforce.
        question_type: The shared DrillStop classification of the turn (K9-D-7).
    """

    surfaced: tuple[RecallCandidate, ...]
    abstained: bool
    reason: AbstentionReason
    reinforce_ids: tuple[str, ...]
    question_type: QuestionType


def run_recall(
    *,
    legs: Mapping[RecallLeg, Sequence[PersonaChunk | ConceptNode]],
    query: str,
    reranker: Reranker,
    settings: RecallSettings,
    owner_id: str | None = None,
    persona_id: str | None = None,
    rerank_top_k: int | None = None,
) -> list[RecallCandidate]:
    """Fuse the legs and rerank the pool — the K9 recall skeleton (K9-D-1/3/12).

    Args:
        legs: The retrieval legs for this turn (from
            :func:`~persona.recall.sources.gather_legs`).
        query: This turn's query text (the reranker scores candidate text against it).
        reranker: The cross-encoder (or the :class:`IdentityReranker` stub / fail-soft
            fallback) — injected, so the path never hard-depends on a real model.
        settings: The recall tunables (fusion weights/quotas, pool size, budget).
        owner_id: The graph owner scope for this turn (stamped on graph candidates).
        persona_id: The episodic persona scope (stamped on episodic candidates).
        rerank_top_k: The fused top-k to rerank; ``None`` ⇒ ``settings.result_budget``
            (the chat/voice split passes ``rerank_top_k_chat`` / ``rerank_top_k_voice``).

    Returns:
        The reranked candidates, best-first — fused order under the identity stub.
    """
    fused = fuse(
        legs=legs,
        settings=settings,
        owner_id=owner_id,
        persona_id=persona_id,
        top_k=settings.pool_size,
    )
    top_k = settings.result_budget if rerank_top_k is None else rerank_top_k
    return reranker.rerank(query, fused, top_k=top_k)


def compose_recall(
    *,
    legs: Mapping[RecallLeg, Sequence[PersonaChunk | ConceptNode]],
    query: str,
    reranker: Reranker,
    settings: RecallSettings,
    episodic_settings: EpisodicSettings,
    now: datetime,
    owner_id: str | None = None,
    persona_id: str | None = None,
    rerank_top_k: int | None = None,
    episode_provider: EpisodeProvider | None = None,
    neighbour_provider: NeighbourProvider | None = None,
    allowlist: set[str] | None = None,
    result_budget: int | None = None,
) -> RecallResult:
    """Run the full deterministic recall path (K9-D-1/3/5/6/8/9) — the T8/T9 seam.

    fuse → rerank (fail-soft) → composite score → temporal contiguity (if ``episode_provider``)
    → gated diffusion tiebreak (if ``neighbour_provider``) → K4-subtraction-then-abstention.
    Every stage past fuse is optional so the path runs stub-safe (no providers) and fully
    composed alike. The K4 allowlist subtracts **before** abstention evaluates the remainder
    (K9-D-9), and the result carries the reinforcement ids (surfaced raw hits + contiguity
    members) for the caller to run off the read path (K8-D-5).

    Args:
        legs: The turn's retrieval legs.
        query: The turn's query.
        reranker: The path's fail-soft reranker (chat sync / voice off-loop).
        settings: The recall tunables.
        episodic_settings: K8's lifecycle settings (the strength-aware recency τ₀).
        now: The turn's reference time (injected — pure).
        owner_id: The graph owner scope; ``persona_id`` the episodic scope.
        persona_id: The episodic persona scope.
        rerank_top_k: The fused top-k to rerank (chat/voice split); ``None`` ⇒ result budget.
        episode_provider: The pyramid episode resolver for contiguity; ``None`` ⇒ no expansion.
        neighbour_provider: The graph neighbour resolver for diffusion; ``None`` ⇒ no tiebreak.
        allowlist: The K4 permitted graph-node set; ``None`` ⇒ no subtraction (common turn).
        result_budget: Max surfaced; ``None`` ⇒ ``settings.result_budget``.

    Returns:
        The :class:`RecallResult` — surfaced set (or abstention) + reason + reinforce ids.
    """
    top_k = settings.result_budget if rerank_top_k is None else rerank_top_k
    fused = fuse(
        legs=legs,
        settings=settings,
        owner_id=owner_id,
        persona_id=persona_id,
        top_k=settings.pool_size,
    )
    reranked = reranker.rerank(query, fused, top_k=top_k)
    scored = apply_composite_score(
        reranked, now=now, settings=settings, episodic_settings=episodic_settings
    )

    question_type = classify_question_type(query)
    used_ids: list[str] = []
    if episode_provider is not None:
        neighbours, used_ids = expand_contiguity(
            scored,
            provider=episode_provider,
            settings=settings,
            question_type=question_type,
            persona_id=persona_id,
        )
        if neighbours:
            scored = apply_composite_score(
                [*scored, *neighbours],
                now=now,
                settings=settings,
                episodic_settings=episodic_settings,
            )

    if neighbour_provider is not None:
        scored = apply_diffusion(
            scored, query=query, provider=neighbour_provider, settings=settings
        )

    outcome = compose_gate(
        scored,
        settings=settings,
        question_type=question_type,
        allowlist=allowlist,
        result_budget=result_budget,
    )
    reinforce = _reinforce_ids(outcome.surfaced, used_ids)
    return RecallResult(
        surfaced=outcome.surfaced,
        abstained=outcome.abstained,
        reason=outcome.reason,
        reinforce_ids=reinforce,
        question_type=question_type,
    )


def _reinforce_ids(
    surfaced: Sequence[RecallCandidate], contiguity_used: Sequence[str]
) -> tuple[str, ...]:
    """Raw episodic ids to reinforce (K8-D-5): surfaced raw hits + contiguity members, deduped."""
    ids: list[str] = [c.key for c in surfaced if c.source is RecallSource.EPISODIC_RAW]
    seen = set(ids)
    for cid in contiguity_used:
        if cid not in seen:
            seen.add(cid)
            ids.append(cid)
    return tuple(ids)

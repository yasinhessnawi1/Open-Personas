"""Entity resolution orchestration (Spec K2, T3; the D-K0-9 seam).

K0's ``EntityRegistry.resolve`` is deterministic and LLM-free: it returns a
three-way verdict (``MERGE`` / ``SEPARATE`` / ``AMBIGUOUS``). This runtime
orchestrator owns the parts K0 leaves to K2:

- ``MERGE``  → use the canonical id (K0 is confident — never re-judged, D-K0-9).
- ``SEPARATE`` → ``create_entity`` (confidently new).
- ``AMBIGUOUS`` → run the LLM judge (:class:`persona.extraction.EntityRecognizer`)
  on that band ONLY; on confirmation ``add_alias`` and reuse the id, on rejection
  ``create_entity``.

Turning an interaction's raw entity mentions into resolved canonical ``entity_ids``
is what threads concepts to one entity instead of fragments — the alias-heavy
"my doctor… Dr. Hansen… she…" case (criterion 3). Resolution runs in ONE pass over
the interaction's mentions so the registry accumulates within the pass: a variant
surface seen after its entity exists reaches the review band and collapses onto it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.graph.models import EntityAlias
from persona.graph.protocol import ResolutionDecision

if TYPE_CHECKING:
    from collections.abc import Sequence

    from persona.extraction import EntityRecognizer
    from persona.graph.models import NodeProvenance
    from persona.graph.protocol import EntityRegistry

__all__ = ["EntityResolver"]


class EntityResolver:
    """Resolves entity mentions to canonical ids via K0's registry + the LLM judge.

    Args:
        registry: K0's deterministic, LLM-free canonical-entity registry.
        judge: K2's LLM judge for the AMBIGUOUS band only (the ratified split).
    """

    def __init__(self, *, registry: EntityRegistry, judge: EntityRecognizer) -> None:
        self._registry = registry
        self._judge = judge

    async def resolve_mentions(
        self,
        owner_id: str,
        mentions: Sequence[str],
        *,
        provenance: NodeProvenance | None = None,
    ) -> dict[str, str]:
        """Resolve each unique mention to a canonical entity id (surface → id).

        Runs in one pass so the registry accumulates within the interaction
        (variant surfaces collapse onto an entity created earlier in the pass).
        Identical surfaces are resolved once.
        """
        resolved: dict[str, str] = {}
        for surface in mentions:
            if surface in resolved:
                continue
            resolved[surface] = await self._resolve_one(owner_id, surface, provenance)
        return resolved

    async def _resolve_one(
        self, owner_id: str, surface: str, provenance: NodeProvenance | None
    ) -> str:
        verdict = self._registry.resolve(owner_id, surface)
        if verdict.decision is ResolutionDecision.MERGE and verdict.canonical_id is not None:
            return verdict.canonical_id
        if verdict.decision is ResolutionDecision.AMBIGUOUS:
            chosen = await self._judge.judge_ambiguous(
                owner_id=owner_id, mention=surface, candidates=verdict.candidates
            )
            if chosen is not None:
                # R9-284: the surface came through this persona; erasing it removes it.
                persona_id = provenance.persona_id if provenance is not None else None
                self._registry.add_alias(
                    owner_id, chosen, EntityAlias(surface=surface, persona_id=persona_id)
                )
                return chosen
        # SEPARATE, a judge-rejected AMBIGUOUS, or a malformed MERGE (no id): create.
        entity = self._registry.create_entity(
            owner_id, canonical_name=surface, provenance=provenance
        )
        return entity.id

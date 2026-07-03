"""The synthesis assembly orchestrator (Spec K2, T8).

The off-critical-path reflection pass, assembled from the T2–T4 pieces:

  extract grounded candidates (T2)
    → resolve entity mentions to canonical ids (T3, one accumulating pass)
    → resolve update/contradiction targets (T4)
    → wire same-batch temporal/causal links (T4 ``proposed_relations`` → K0 ``ProposedLink``)
    → assemble K0 ``KnowledgeCandidate``s (``source=system``, evidence span = grounding)
    → ``GraphStore.merge`` each, in order.

Both write paths converge on K0's one merge; this is the synthesis feeder. The
direct-write tool (T7) is the other. The means guard (D-K2-7) runs here as
defense-in-depth behind the eval-gated prompt.

Link wiring is single-pass and best-effort: a ``proposed_relation`` whose target
is a candidate already merged in this batch is wired to that node; a forward /
unknown target is skipped (logged), to re-form on a later synthesis. This avoids
double-merging (which would pollute the provenance trail).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from persona.graph.models import NodeProvenance
from persona.graph.protocol import KnowledgeCandidate, ProposedLink
from persona.logging import get_logger
from persona.schema.chunks import WriteSource

from persona_runtime.extraction.means_guard import contains_self_harm_means

if TYPE_CHECKING:
    from persona.backends.protocol import ChatBackend
    from persona.extraction import ExtractionCandidate, ExtractionInput, Extractor
    from persona.graph.protocol import EntityRegistry, GraphStore, MergeOutcome

    from persona_runtime.extraction.resolution import EntityResolver
    from persona_runtime.extraction.update import UpdateResolver

__all__ = ["Synthesizer", "build_synthesizer"]

_logger = get_logger("extraction.synthesizer")


class Synthesizer:
    """Assembles an interaction's grounded knowledge into the graph (off critical path).

    Args:
        extractor: The grounded-extraction pipeline (T2).
        entity_resolver: Canonical-entity resolution orchestrator (T3).
        update_resolver: Update/contradiction target resolver (T4).
        graph_store: K0's user-scoped graph store (the single write path).
    """

    def __init__(
        self,
        *,
        extractor: Extractor,
        entity_resolver: EntityResolver,
        update_resolver: UpdateResolver,
        graph_store: GraphStore,
    ) -> None:
        self._extractor = extractor
        self._entities = entity_resolver
        self._updates = update_resolver
        self._store = graph_store

    async def synthesise(self, owner_id: str, interaction: ExtractionInput) -> list[MergeOutcome]:
        """Extract → resolve → merge for one interaction. Returns the merge outcomes."""
        candidates = await self._extractor.extract(interaction)
        if not candidates:
            return []

        base_provenance = NodeProvenance(
            source=WriteSource.SYSTEM,
            persona_id=interaction.persona_id,
            interaction_id=interaction.interaction_id,
            written_at=datetime.now(UTC),
            # V13: mark the originating surface (``voice`` for a fact minted from a
            # call) so a graph node is attributable cross-channel. ``source`` stays
            # SYSTEM — synthesis is always a system reflection pass.
            channel=interaction.channel,
        )

        mentions = [m.surface for c in candidates for m in c.entity_mentions]
        mention_to_id = (
            await self._entities.resolve_mentions(owner_id, mentions, provenance=base_provenance)
            if mentions
            else {}
        )

        concept_to_node: dict[str, str] = {}
        outcomes: list[MergeOutcome] = []
        for candidate in candidates:
            if contains_self_harm_means(
                candidate.content,
                category=candidate.wellbeing_category.value
                if candidate.wellbeing_category is not None
                else None,
            ):
                # Defense-in-depth behind the eval-gated prompt (D-K2-7): never
                # store means specifics, even if the extractor slipped.
                _logger.warning(
                    "synthesis dropped a candidate carrying self-harm means",
                    interaction_id=interaction.interaction_id,
                )
                continue
            knowledge = self._assemble(candidate, mention_to_id, concept_to_node, base_provenance)
            outcome = self._store.merge(
                owner_id, self._resolve_update(owner_id, candidate, knowledge)
            )
            concept_to_node[candidate.concept_name] = outcome.node_id
            outcomes.append(outcome)
        return outcomes

    def _assemble(
        self,
        candidate: ExtractionCandidate,
        mention_to_id: dict[str, str],
        concept_to_node: dict[str, str],
        base_provenance: NodeProvenance,
    ) -> KnowledgeCandidate:
        entity_ids = tuple(
            dict.fromkeys(
                mention_to_id[m.surface]
                for m in candidate.entity_mentions
                if m.surface in mention_to_id
            )
        )
        proposed_links: list[ProposedLink] = []
        for relation in candidate.proposed_relations:
            target_node_id = concept_to_node.get(relation.target_concept)
            if target_node_id is None:
                _logger.debug(
                    "synthesis skipped an unresolved relation target",
                    target=relation.target_concept,
                )
                continue
            proposed_links.append(
                ProposedLink(
                    target_node_id=target_node_id,
                    link_type=relation.link_type,
                    reason=relation.reason,
                )
            )
        return KnowledgeCandidate(
            concept_name=candidate.concept_name,
            content=candidate.content,
            node_kind=candidate.node_kind,
            entity_ids=entity_ids,
            proposed_links=tuple(proposed_links),
            wellbeing_category=(
                candidate.wellbeing_category.value
                if candidate.wellbeing_category is not None
                else None
            ),
            provenance=base_provenance.model_copy(update={"grounding": candidate.evidence_span}),
            update_intent=candidate.update_intent,
        )

    def _resolve_update(
        self, owner_id: str, candidate: ExtractionCandidate, knowledge: KnowledgeCandidate
    ) -> KnowledgeCandidate:
        target_node_id = self._updates.resolve_target(owner_id, candidate)
        if target_node_id is None:
            return knowledge
        return knowledge.model_copy(update={"target_node_id": target_node_id})


def build_synthesizer(
    *,
    graph_store: GraphStore,
    registry: EntityRegistry,
    backend: ChatBackend,
) -> Synthesizer:
    """Compose a ``Synthesizer`` from the wired graph store, registry, and tier backend.

    The composition seam the worker root (T8d) calls: the extractor + AMBIGUOUS-band
    judge run on ``backend`` (the small/mid synthesis tier, D-K2-3 — the tier
    hard-gate-#2's eval re-run validates); entity resolution drives K0's ``registry``;
    updates resolve against the ``graph_store``; everything converges on its ``merge``.
    """
    from persona_runtime.extraction.entity_judge import LlmEntityJudge
    from persona_runtime.extraction.pipeline import LlmExtractor
    from persona_runtime.extraction.resolution import EntityResolver
    from persona_runtime.extraction.update import UpdateResolver

    return Synthesizer(
        extractor=LlmExtractor(backend=backend),
        entity_resolver=EntityResolver(registry=registry, judge=LlmEntityJudge(backend=backend)),
        update_resolver=UpdateResolver(store=graph_store),
        graph_store=graph_store,
    )

"""Synthesizer resolve-don't-drop for a target concept outside the batch (K12, T3).

Before: a ``proposed_relation`` whose target concept was not merged in the SAME
batch was always dropped, even when the concept already existed elsewhere in the
owner's graph (e.g. learned in an earlier synthesis run) — silently losing real
TEMPORAL/CAUSAL edges. Now: a same-batch miss falls back to a dense read against
the existing graph (mirroring ``UpdateResolver``'s free-text-hint resolution),
and only a truly unresolvable target is dropped. Tested with fakes (no model, no
DB) — mirrors ``test_synthesizer.py``'s conventions.
"""

# ruff: noqa: ARG002 — fakes ignore some args by design.

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from persona.extraction import (
    ExtractionCandidate,
    ExtractionInput,
    InteractionKind,
    ProposedRelation,
)
from persona.graph.models import ConceptNode, LinkType, NodeKind, NodeProvenance
from persona.graph.protocol import KnowledgeCandidate, MergeAction, MergeOutcome, UpdateIntent
from persona.schema.chunks import WriteSource
from persona_runtime.extraction.synthesizer import Synthesizer


class _FakeExtractor:
    def __init__(self, candidates: tuple[ExtractionCandidate, ...]) -> None:
        self._candidates = candidates

    async def extract(self, interaction: ExtractionInput) -> tuple[ExtractionCandidate, ...]:
        return self._candidates


class _FakeEntityResolver:
    async def resolve_mentions(
        self, owner_id: str, mentions: object, *, provenance: object = None
    ) -> dict[str, str]:
        return {}


class _FakeUpdateResolver:
    def resolve_target(self, owner_id: str, candidate: ExtractionCandidate) -> str | None:
        return None


class _FakeGraphStore:
    """A GraphStore double exposing ``merge`` + the dense read the resolve-fallback uses."""

    def __init__(self, *, dense_hits: list[ConceptNode] | None = None) -> None:
        self.merges: list[tuple[str, KnowledgeCandidate]] = []
        self._n = 0
        self._dense_hits = dense_hits or []
        self.dense_queries: list[str] = []

    def merge(self, owner_id: str, candidate: KnowledgeCandidate) -> MergeOutcome:
        self.merges.append((owner_id, candidate))
        self._n += 1
        return MergeOutcome(action=MergeAction.CREATED, node_id=f"node-{self._n}")

    def search_dense(
        self, owner_id: str, query: str, top_k: int, *, allowlist: set[str] | None = None
    ) -> list[ConceptNode]:
        self.dense_queries.append(query)
        return self._dense_hits[:top_k]


def _existing_node(node_id: str, *, concept_name: str, distance: float) -> ConceptNode:
    return ConceptNode(
        id=node_id,
        node_kind=NodeKind.FACT,
        concept_name=concept_name,
        content=f"about {concept_name}",
        distance=distance,
        provenance=(NodeProvenance(source=WriteSource.SYSTEM, written_at=datetime.now(UTC)),),
        created_at=datetime.now(UTC),
    )


def _cand(
    concept_name: str,
    *,
    relations: tuple[ProposedRelation, ...] = (),
) -> ExtractionCandidate:
    return ExtractionCandidate(
        concept_name=concept_name,
        content="content",
        node_kind=NodeKind.FACT,
        evidence_span="said it",
        entity_mentions=(),
        proposed_relations=relations,
        update_intent=UpdateIntent.NONE,
        update_target_hint=None,
    )


def _input() -> ExtractionInput:
    return ExtractionInput(
        interaction_kind=InteractionKind.CONVERSATION,
        interaction_id="conv-1",
        persona_id="persona-a",
        content="the interaction",
    )


@pytest.mark.asyncio
async def test_temporal_target_not_in_batch_resolves_against_the_existing_graph() -> None:
    """A TEMPORAL relation whose target is NOT in this batch, but EXISTS in the graph
    (a confident dense hit), resolves to that node instead of being dropped.
    """
    existing = _existing_node("node-existing", concept_name="started new job", distance=0.1)
    store = _FakeGraphStore(dense_hits=[existing])
    synth = Synthesizer(
        extractor=_FakeExtractor(
            (
                _cand(
                    "moved city",
                    relations=(
                        ProposedRelation(
                            target_concept="started new job", link_type=LinkType.TEMPORAL
                        ),
                    ),
                ),
            )
        ),
        entity_resolver=_FakeEntityResolver(),  # type: ignore[arg-type]
        update_resolver=_FakeUpdateResolver(),  # type: ignore[arg-type]
        graph_store=store,  # type: ignore[arg-type]
    )
    await synth.synthesise("u1", _input())
    _, kc = store.merges[0]
    assert len(kc.proposed_links) == 1
    link = kc.proposed_links[0]
    assert link.target_node_id == "node-existing"  # resolved against the graph, not dropped
    assert link.link_type is LinkType.TEMPORAL
    assert store.dense_queries == ["started new job"]


@pytest.mark.asyncio
async def test_temporal_target_not_in_batch_and_not_in_graph_is_still_dropped() -> None:
    """No same-batch match AND no confident graph match → still drops (not fatal)."""
    store = _FakeGraphStore(dense_hits=[])
    synth = Synthesizer(
        extractor=_FakeExtractor(
            (
                _cand(
                    "moved city",
                    relations=(
                        ProposedRelation(
                            target_concept="nothing like it", link_type=LinkType.TEMPORAL
                        ),
                    ),
                ),
            )
        ),
        entity_resolver=_FakeEntityResolver(),  # type: ignore[arg-type]
        update_resolver=_FakeUpdateResolver(),  # type: ignore[arg-type]
        graph_store=store,  # type: ignore[arg-type]
    )
    await synth.synthesise("u1", _input())
    _, kc = store.merges[0]
    assert kc.proposed_links == ()


@pytest.mark.asyncio
async def test_temporal_target_resolution_rejects_a_low_confidence_dense_hit() -> None:
    """A dense hit that exists but is too FAR (below the confidence bar) is rejected —
    a wrong-node edge is worse than a missed one.
    """
    far_hit = _existing_node("node-far", concept_name="unrelated thing", distance=0.9)
    store = _FakeGraphStore(dense_hits=[far_hit])
    synth = Synthesizer(
        extractor=_FakeExtractor(
            (
                _cand(
                    "moved city",
                    relations=(
                        ProposedRelation(
                            target_concept="something else", link_type=LinkType.TEMPORAL
                        ),
                    ),
                ),
            )
        ),
        entity_resolver=_FakeEntityResolver(),  # type: ignore[arg-type]
        update_resolver=_FakeUpdateResolver(),  # type: ignore[arg-type]
        graph_store=store,  # type: ignore[arg-type]
    )
    await synth.synthesise("u1", _input())
    _, kc = store.merges[0]
    assert kc.proposed_links == ()

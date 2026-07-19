"""Synthesizer resolve-don't-drop for a CAUSAL target outside the batch (K12, T4).

T3 built ``Synthesizer._resolve_existing_target`` (a dense-read fallback against the
EXISTING graph, mirroring ``UpdateResolver``'s free-text-hint resolution) and wired
it into ``_assemble`` link-type-agnostically — every ``proposed_relation``, whatever
its ``link_type``, is resolved the same way. This file proves CAUSAL rides that same
path with no further code change: an asserted causal relation whose target concept
is not in the same extraction batch but already exists in the graph now persists
(previously dropped), while the causal PRECISION BAR (D-K0-8: asserted sparingly,
never inferred) stays intact — the resolver only wires relations the extractor
actually proposed, it never invents a causal edge for a turn with no stated
causation. Tested with fakes (no model, no DB) — mirrors
``test_synthesizer_temporal_resolution.py``'s conventions.
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
async def test_causal_target_not_in_batch_resolves_against_the_existing_graph() -> None:
    """An asserted CAUSAL relation whose target is NOT in this batch, but EXISTS in
    the graph (a confident dense hit), resolves to that node and persists instead
    of being dropped — the cross-batch causal-loss bug this task fixes.
    """
    existing = _existing_node("node-existing", concept_name="lost the job", distance=0.1)
    store = _FakeGraphStore(dense_hits=[existing])
    synth = Synthesizer(
        extractor=_FakeExtractor(
            (
                _cand(
                    "missed rent payment",
                    relations=(
                        ProposedRelation(target_concept="lost the job", link_type=LinkType.CAUSAL),
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
    assert link.link_type is LinkType.CAUSAL
    assert store.dense_queries == ["lost the job"]


@pytest.mark.asyncio
async def test_no_stated_causation_creates_no_causal_edge() -> None:
    """The precision bar (D-K0-8: causal asserted sparingly, never inferred) holds —
    a turn the extractor found NO causation in (empty ``proposed_relations``) gets
    no causal edge. The resolve-fallback only wires relations the model actually
    proposed; it never invents one to fill a gap.
    """
    # A confident dense hit sits in the graph, so if the resolver's fallback path
    # were somehow invoked unprompted, it WOULD find something to wire — proving
    # the guard is "no relation was proposed", not "no match was found".
    existing = _existing_node("node-existing", concept_name="unrelated fact", distance=0.05)
    store = _FakeGraphStore(dense_hits=[existing])
    synth = Synthesizer(
        extractor=_FakeExtractor((_cand("plain statement", relations=()),)),
        entity_resolver=_FakeEntityResolver(),  # type: ignore[arg-type]
        update_resolver=_FakeUpdateResolver(),  # type: ignore[arg-type]
        graph_store=store,  # type: ignore[arg-type]
    )
    await synth.synthesise("u1", _input())
    _, kc = store.merges[0]
    assert kc.proposed_links == ()
    assert store.dense_queries == []  # the fallback was never even invoked

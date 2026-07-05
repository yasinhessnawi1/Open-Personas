"""The synthesis assembly orchestrator (Spec K2, T8).

Ties T2–T4 together off the critical path: extract grounded candidates → resolve
entity mentions to canonical ids (T3) → resolve update targets (T4) → wire
same-batch temporal/causal links → assemble K0 ``KnowledgeCandidate``s →
``GraphStore.merge`` in order. Provenance is ``source=system`` (synthesis) with the
candidate's evidence span as grounding. The means guard runs as defense-in-depth.
Tested with fakes (no model, no DB).
"""

# ruff: noqa: ARG002 — fakes ignore some args by design.

from __future__ import annotations

import pytest
from persona.extraction import (
    EntityMention,
    ExtractionCandidate,
    ExtractionInput,
    InteractionKind,
    ProposedRelation,
)
from persona.graph.models import LinkType, NodeKind
from persona.graph.protocol import KnowledgeCandidate, MergeAction, MergeOutcome, UpdateIntent
from persona.schema.chunks import WriteSource
from persona_runtime.extraction.synthesizer import Synthesizer


class _FakeExtractor:
    def __init__(self, candidates: tuple[ExtractionCandidate, ...]) -> None:
        self._candidates = candidates

    async def extract(self, interaction: ExtractionInput) -> tuple[ExtractionCandidate, ...]:
        return self._candidates


class _FakeEntityResolver:
    def __init__(self, mapping: dict[str, str] | None = None) -> None:
        self._mapping = mapping or {}
        self.calls: list[list[str]] = []

    async def resolve_mentions(
        self, owner_id: str, mentions: object, *, provenance: object = None
    ) -> dict[str, str]:
        ms = list(mentions)  # type: ignore[call-overload]
        self.calls.append(ms)
        return {m: self._mapping[m] for m in ms if m in self._mapping}


class _FakeUpdateResolver:
    def __init__(self, target: str | None = None) -> None:
        self._target = target

    def resolve_target(self, owner_id: str, candidate: ExtractionCandidate) -> str | None:
        return self._target if candidate.update_intent is not UpdateIntent.NONE else None


class _FakeGraphStore:
    def __init__(self) -> None:
        self.merges: list[tuple[str, KnowledgeCandidate]] = []
        self._n = 0

    def merge(self, owner_id: str, candidate: KnowledgeCandidate) -> MergeOutcome:
        self.merges.append((owner_id, candidate))
        self._n += 1
        return MergeOutcome(action=MergeAction.CREATED, node_id=f"node-{self._n}")


def _cand(
    concept_name: str,
    *,
    content: str = "content",
    evidence_span: str = "said it",
    mentions: tuple[str, ...] = (),
    relations: tuple[ProposedRelation, ...] = (),
    update_intent: UpdateIntent = UpdateIntent.NONE,
) -> ExtractionCandidate:
    return ExtractionCandidate(
        concept_name=concept_name,
        content=content,
        node_kind=NodeKind.FACT,
        evidence_span=evidence_span,
        entity_mentions=tuple(EntityMention(surface=m) for m in mentions),
        proposed_relations=relations,
        update_intent=update_intent,
        update_target_hint="prior" if update_intent is not UpdateIntent.NONE else None,
    )


def _synth(
    candidates: tuple[ExtractionCandidate, ...],
    *,
    entities: dict[str, str] | None = None,
    target: str | None = None,
    store: _FakeGraphStore | None = None,
) -> tuple[Synthesizer, _FakeGraphStore]:
    s = store or _FakeGraphStore()
    return (
        Synthesizer(
            extractor=_FakeExtractor(candidates),
            entity_resolver=_FakeEntityResolver(entities),  # type: ignore[arg-type]
            update_resolver=_FakeUpdateResolver(target),  # type: ignore[arg-type]
            graph_store=s,
        ),
        s,
    )


def _input() -> ExtractionInput:
    return ExtractionInput(
        interaction_kind=InteractionKind.CONVERSATION,
        interaction_id="conv-1",
        persona_id="persona-a",
        content="the interaction",
    )


@pytest.mark.asyncio
async def test_empty_extraction_writes_nothing() -> None:
    synth, store = _synth(())
    out = await synth.synthesise("u1", _input())
    assert out == []
    assert store.merges == []


@pytest.mark.asyncio
async def test_candidates_merge_with_system_provenance_and_grounding() -> None:
    synth, store = _synth(
        (_cand("vegetarian", content="is vegetarian", evidence_span="I'm vegetarian"),)
    )
    await synth.synthesise("u1", _input())
    owner, kc = store.merges[0]
    assert owner == "u1"
    assert kc.content == "is vegetarian"
    assert kc.provenance.source is WriteSource.PERSONA_SELF  # learned from conversation (R4)
    assert kc.provenance.grounding == "I'm vegetarian"  # the candidate's evidence span
    assert kc.provenance.interaction_id == "conv-1"
    assert kc.provenance.persona_id == "persona-a"


@pytest.mark.asyncio
async def test_entity_mentions_resolve_to_ids() -> None:
    synth, store = _synth(
        (_cand("doctor", mentions=("my doctor", "Dr. Hansen")),),
        entities={"my doctor": "e1", "Dr. Hansen": "e1"},
    )
    await synth.synthesise("u1", _input())
    _, kc = store.merges[0]
    assert kc.entity_ids == ("e1",)  # deduped to the one canonical entity


@pytest.mark.asyncio
async def test_update_intent_targets_the_resolved_node() -> None:
    synth, store = _synth(
        (_cand("job", update_intent=UpdateIntent.CONTRADICT),),
        target="node-prior",
    )
    await synth.synthesise("u1", _input())
    _, kc = store.merges[0]
    assert kc.update_intent is UpdateIntent.CONTRADICT
    assert kc.target_node_id == "node-prior"


@pytest.mark.asyncio
async def test_same_batch_backward_relation_is_wired_to_the_merged_node() -> None:
    # "burnout" merges first (node-1); "left job" references it via a causal link.
    cands = (
        _cand("burnout"),
        _cand(
            "left job",
            relations=(ProposedRelation(target_concept="burnout", link_type=LinkType.CAUSAL),),
        ),
    )
    synth, store = _synth(cands)
    await synth.synthesise("u1", _input())
    _, kc_left = store.merges[1]
    assert len(kc_left.proposed_links) == 1
    link = kc_left.proposed_links[0]
    assert link.target_node_id == "node-1"  # the merged "burnout" node
    assert link.link_type is LinkType.CAUSAL


@pytest.mark.asyncio
async def test_forward_or_unknown_relation_target_is_skipped_not_fatal() -> None:
    cands = (
        _cand(
            "left job",
            relations=(
                ProposedRelation(target_concept="not in batch", link_type=LinkType.TEMPORAL),
            ),
        ),
    )
    synth, store = _synth(cands)
    await synth.synthesise("u1", _input())
    _, kc = store.merges[0]
    assert kc.proposed_links == ()  # unresolved target dropped, write still happens


@pytest.mark.asyncio
async def test_means_guard_drops_a_means_bearing_candidate_defense_in_depth() -> None:
    cands = (
        _cand("urges", content="self-harm urges, thinking about taking all my pills"),
        _cand("vegetarian", content="is vegetarian"),
    )
    synth, store = _synth(cands)
    await synth.synthesise("u1", _input())
    # the means-bearing candidate is dropped; the benign one still merges
    assert [kc.concept_name for _, kc in store.merges] == ["vegetarian"]

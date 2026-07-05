"""Voice-channel provenance + SELF-never-targeted through synthesis (Spec V13, T5).

The synthesizer is channel-agnostic: it stamps every minted node's provenance with
the ``ExtractionInput.channel`` it was handed. V13 uses that to attribute a fact
minted from a call as ``channel=voice`` while ``source`` stays ``system`` (synthesis
is always a system reflection pass). And synthesis never targets the K6 SELF node:
it only ever merges the extractor's fact/preference/… candidates (never SELF-kind),
and the extend/evolve target comes from ``dense_query``, which structurally excludes
the SELF node (``persona.graph.postgres`` — K6). Voice rides this identical path.
"""

# ruff: noqa: ARG002 — fakes ignore some args by design.

from __future__ import annotations

import pytest
from persona.extraction import EntityMention, ExtractionCandidate, ExtractionInput, InteractionKind
from persona.graph.models import NodeKind
from persona.graph.protocol import KnowledgeCandidate, MergeAction, MergeOutcome, UpdateIntent
from persona.jobs import CHANNEL_VOICE
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


class _RecordingStore:
    def __init__(self) -> None:
        self.merged: list[KnowledgeCandidate] = []

    def merge(self, owner_id: str, candidate: KnowledgeCandidate) -> MergeOutcome:
        self.merged.append(candidate)
        # A synthesis write always creates/extends a monotonic ``::node::`` id —
        # never the reserved ``::self`` id.
        return MergeOutcome(action=MergeAction.CREATED, node_id=f"{owner_id}::node::0")


def _voice_candidate() -> ExtractionCandidate:
    return ExtractionCandidate(
        concept_name="new apartment",
        content="the user is moving to a new apartment",
        node_kind=NodeKind.CIRCUMSTANCE,
        evidence_span="I'm moving to a new apartment next month",
        entity_mentions=(EntityMention(surface="apartment"),),
        proposed_relations=(),
        update_intent=UpdateIntent.NONE,
        update_target_hint=None,
    )


def _synth(store: _RecordingStore) -> Synthesizer:
    return Synthesizer(
        extractor=_FakeExtractor((_voice_candidate(),)),
        entity_resolver=_FakeEntityResolver(),  # type: ignore[arg-type]
        update_resolver=_FakeUpdateResolver(),  # type: ignore[arg-type]
        graph_store=store,  # type: ignore[arg-type]
    )


def _voice_input() -> ExtractionInput:
    return ExtractionInput(
        interaction_kind=InteractionKind.CONVERSATION,
        interaction_id="call-1",
        persona_id="persona-a",
        content="USER: I'm moving to a new apartment next month",
        channel=CHANNEL_VOICE,
    )


@pytest.mark.asyncio
async def test_voice_channel_reaches_the_minted_node_provenance() -> None:
    store = _RecordingStore()
    await _synth(store).synthesise("owner-1", _voice_input())
    assert store.merged, "the voice candidate should have been merged"
    prov = store.merged[0].provenance
    assert prov.channel == CHANNEL_VOICE  # attributable as a call-minted fact
    # ``source`` is PERSONA_SELF — a synthesised fact is learned from the conversation
    # (R4), whether chat or voice; ``channel`` is the finer voice/text marker.
    assert prov.source.value == "persona_self"


@pytest.mark.asyncio
async def test_synthesis_never_merges_a_self_kind_candidate() -> None:
    store = _RecordingStore()
    await _synth(store).synthesise("owner-1", _voice_input())
    # SELF is K6's single central node, created via its own path — never emitted by
    # the extractor and never a synthesis merge target.
    assert all(c.node_kind is not NodeKind.SELF for c in store.merged)
    assert all("::self" not in c.concept_name for c in store.merged)

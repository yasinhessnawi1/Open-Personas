"""A synthesis already past its read writes nothing once its persona is erased (R9-284 H2).

The handler reads the conversation, then awaits the extractor's model call; the erasure can
run to completion in that window, graph step included. The graph is owner scoped, so nothing
else refuses the merges that follow, and they would put the persona back in. The synthesizer
asks whether the persona still exists after the model call and before every merge.
"""

from __future__ import annotations

import pytest
from persona.extraction import EntityMention, ExtractionCandidate, ExtractionInput, InteractionKind
from persona.graph.models import NodeKind
from persona.graph.protocol import KnowledgeCandidate, MergeAction, MergeOutcome, UpdateIntent
from persona_runtime.extraction.synthesizer import Synthesizer

_PERSONA = "persona-a"


def _cand(name: str, *, mentions: tuple[str, ...] = ()) -> ExtractionCandidate:
    return ExtractionCandidate(
        concept_name=name,
        content=f"about {name}",
        node_kind=NodeKind.FACT,
        evidence_span="said it",
        entity_mentions=tuple(EntityMention(surface=m) for m in mentions),
        update_intent=UpdateIntent.NONE,
    )


class _World:
    """The persona's existence, which the erasure can end mid-synthesis."""

    def __init__(self) -> None:
        self.exists = True
        self.asked: list[str] = []

    def persona_exists(self, persona_id: str) -> bool:
        self.asked.append(persona_id)
        return self.exists


class _Extractor:
    """The model call; the erasure finishes while it is awaited."""

    def __init__(self, world: _World, candidates: tuple[ExtractionCandidate, ...]) -> None:
        self._world = world
        self._candidates = candidates
        self.erase_during_call = False

    async def extract(self, _interaction: ExtractionInput) -> tuple[ExtractionCandidate, ...]:
        if self.erase_during_call:
            self._world.exists = False
        return self._candidates


class _Entities:
    def __init__(self) -> None:
        self.calls = 0

    async def resolve_mentions(self, *_a: object, **_k: object) -> dict[str, str]:
        self.calls += 1
        return {}


class _Updates:
    def resolve_target(self, *_a: object) -> None:
        return None


class _Store:
    def __init__(self, world: _World, *, erase_after: int | None = None) -> None:
        self._world = world
        self._erase_after = erase_after
        self.merges: list[KnowledgeCandidate] = []

    def merge(self, _owner: str, candidate: KnowledgeCandidate) -> MergeOutcome:
        self.merges.append(candidate)
        if self._erase_after is not None and len(self.merges) >= self._erase_after:
            self._world.exists = False
        return MergeOutcome(action=MergeAction.CREATED, node_id=f"n{len(self.merges)}")

    def search_dense(self, *_a: object, **_k: object) -> list[object]:
        return []


def _input() -> ExtractionInput:
    return ExtractionInput(
        interaction_kind=InteractionKind.CONVERSATION,
        interaction_id="conv-1",
        persona_id=_PERSONA,
        content="the interaction",
    )


def _synth(world: _World, extractor: _Extractor, store: _Store, entities: _Entities) -> Synthesizer:
    return Synthesizer(
        extractor=extractor,  # type: ignore[arg-type]
        entity_resolver=entities,  # type: ignore[arg-type]
        update_resolver=_Updates(),  # type: ignore[arg-type]
        graph_store=store,  # type: ignore[arg-type]
        persona_exists=world.persona_exists,
    )


@pytest.mark.asyncio
async def test_an_erasure_during_the_model_call_leaves_the_graph_alone() -> None:
    world = _World()
    extractor = _Extractor(world, (_cand("a", mentions=("Oslo",)), _cand("b")))
    extractor.erase_during_call = True
    store, entities = _Store(world), _Entities()

    outcomes = await _synth(world, extractor, store, entities).synthesise("u1", _input())

    assert outcomes == []
    assert store.merges == []
    assert entities.calls == 0, "entities are resolved (and written) after the check"


@pytest.mark.asyncio
async def test_an_erasure_between_merges_stops_the_rest() -> None:
    world = _World()
    extractor = _Extractor(world, (_cand("a"), _cand("b"), _cand("c")))
    store, entities = _Store(world, erase_after=1), _Entities()

    outcomes = await _synth(world, extractor, store, entities).synthesise("u1", _input())

    assert len(store.merges) == 1
    assert [o.node_id for o in outcomes] == ["n1"]


@pytest.mark.asyncio
async def test_a_persona_that_exists_synthesises_as_before() -> None:
    world = _World()
    extractor = _Extractor(world, (_cand("a", mentions=("Oslo",)), _cand("b")))
    store, entities = _Store(world), _Entities()

    outcomes = await _synth(world, extractor, store, entities).synthesise("u1", _input())

    assert len(outcomes) == 2
    assert entities.calls == 1
    assert set(world.asked) == {_PERSONA}

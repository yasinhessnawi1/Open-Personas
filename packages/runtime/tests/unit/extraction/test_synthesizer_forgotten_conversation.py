"""A synthesis whose conversation is forgotten mid-run stops quietly (R9-304).

Forgetting a conversation's memory deletes its row in the transaction that takes its facts out
of the graph, and every graph write that cites a conversation holds that row (the backend's
guard). So a merge or an entity write after the forget is refused with
:class:`GraphInteractionGoneError`. That is the end of this interaction, not a failure: the
synthesizer stops, keeps what landed before (the forget removed it, or it landed after the
forget's read and is refused), and the job is not retried for a conversation that is gone.
"""

from __future__ import annotations

import pytest
from persona.extraction import EntityMention, ExtractionCandidate, ExtractionInput, InteractionKind
from persona.graph.errors import GraphInteractionGoneError
from persona.graph.models import NodeKind
from persona.graph.protocol import KnowledgeCandidate, MergeAction, MergeOutcome, UpdateIntent
from persona_runtime.extraction.synthesizer import Synthesizer

_CONVERSATION = "conv-1"


def _cand(name: str, *, mentions: tuple[str, ...] = ()) -> ExtractionCandidate:
    return ExtractionCandidate(
        concept_name=name,
        content=f"about {name}",
        node_kind=NodeKind.FACT,
        evidence_span="said it",
        entity_mentions=tuple(EntityMention(surface=m) for m in mentions),
        update_intent=UpdateIntent.NONE,
    )


def _gone() -> GraphInteractionGoneError:
    return GraphInteractionGoneError(
        "a graph write cites a conversation that no longer exists",
        context={"interaction_id": _CONVERSATION},
    )


class _Extractor:
    def __init__(self, candidates: tuple[ExtractionCandidate, ...]) -> None:
        self._candidates = candidates

    async def extract(self, _interaction: ExtractionInput) -> tuple[ExtractionCandidate, ...]:
        return self._candidates


class _Entities:
    def __init__(self, *, refuse: bool = False) -> None:
        self._refuse = refuse

    async def resolve_mentions(self, *_a: object, **_k: object) -> dict[str, str]:
        if self._refuse:
            raise _gone()
        return {}


class _Updates:
    def resolve_target(self, *_a: object) -> None:
        return None


class _Store:
    """Refuses every merge from the ``refuse_from``-th on, as the guard does after the forget."""

    def __init__(self, *, refuse_from: int | None = None) -> None:
        self._refuse_from = refuse_from
        self.merges: list[KnowledgeCandidate] = []

    def merge(self, _owner: str, candidate: KnowledgeCandidate) -> MergeOutcome:
        if self._refuse_from is not None and len(self.merges) + 1 >= self._refuse_from:
            raise _gone()
        self.merges.append(candidate)
        return MergeOutcome(action=MergeAction.CREATED, node_id=f"n{len(self.merges)}")

    def search_dense(self, *_a: object, **_k: object) -> list[object]:
        return []


def _input() -> ExtractionInput:
    return ExtractionInput(
        interaction_kind=InteractionKind.CONVERSATION,
        interaction_id=_CONVERSATION,
        persona_id="persona-a",
        content="the interaction",
    )


def _synth(store: _Store, entities: _Entities, *names: str) -> Synthesizer:
    return Synthesizer(
        extractor=_Extractor(tuple(_cand(n, mentions=("Oslo",)) for n in names)),  # type: ignore[arg-type]
        entity_resolver=entities,  # type: ignore[arg-type]
        update_resolver=_Updates(),  # type: ignore[arg-type]
        graph_store=store,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_a_merge_refused_for_a_forgotten_conversation_stops_the_rest() -> None:
    store = _Store(refuse_from=2)

    outcomes = await _synth(store, _Entities(), "a", "b", "c").synthesise("u1", _input())

    assert [o.node_id for o in outcomes] == ["n1"], "what landed first is reported, no more"
    assert len(store.merges) == 1


@pytest.mark.asyncio
async def test_an_entity_write_refused_for_a_forgotten_conversation_merges_nothing() -> None:
    store = _Store()

    outcomes = await _synth(store, _Entities(refuse=True), "a").synthesise("u1", _input())

    assert outcomes == []
    assert store.merges == []


class _LinkStore(_Store):
    """The node write landed, then the forget removed the node before its links were written."""

    def merge(self, _owner: str, candidate: KnowledgeCandidate) -> MergeOutcome:
        from persona.graph.errors import GraphLinkTargetGoneError  # noqa: PLC0415

        if self.merges:
            raise GraphLinkTargetGoneError("gone", context={"node_id": "n1"})
        self.merges.append(candidate)
        return MergeOutcome(action=MergeAction.CREATED, node_id="n1")


def _synth_with(store: _Store, *, conversation_there: bool) -> Synthesizer:
    return Synthesizer(
        extractor=_Extractor((_cand("a"), _cand("b"))),  # type: ignore[arg-type]
        entity_resolver=_Entities(),  # type: ignore[arg-type]
        update_resolver=_Updates(),  # type: ignore[arg-type]
        graph_store=store,  # type: ignore[arg-type]
        conversation_exists=lambda _id: conversation_there,
    )


@pytest.mark.asyncio
async def test_a_link_lost_to_a_forget_stops_quietly() -> None:
    store = _LinkStore()

    outcomes = await _synth_with(store, conversation_there=False).synthesise("u1", _input())

    assert [o.node_id for o in outcomes] == ["n1"]


@pytest.mark.asyncio
async def test_a_link_lost_while_the_conversation_is_there_still_fails_the_job() -> None:
    """Another delete (the Memory page's) is not a forget: the job retries as before."""
    from persona.graph.errors import GraphLinkTargetGoneError  # noqa: PLC0415

    with pytest.raises(GraphLinkTargetGoneError):
        await _synth_with(_LinkStore(), conversation_there=True).synthesise("u1", _input())


@pytest.mark.asyncio
async def test_a_lost_link_in_a_run_synthesis_is_never_taken_for_a_forget() -> None:
    """A run's id names no conversation: whatever the lookup says, the job fails as before."""
    from persona.graph.errors import GraphLinkTargetGoneError  # noqa: PLC0415

    run = ExtractionInput(
        interaction_kind=InteractionKind.AGENTIC_RUN,
        interaction_id="run-1",
        persona_id="persona-a",
        content="the run",
    )

    with pytest.raises(GraphLinkTargetGoneError):
        await _synth_with(_LinkStore(), conversation_there=False).synthesise("u1", run)

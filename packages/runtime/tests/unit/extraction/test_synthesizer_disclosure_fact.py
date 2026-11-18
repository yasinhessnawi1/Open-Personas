"""Synthesis records the durable disclosure fact before it merges anything (R9-253).

The initiative wellbeing gate reads the fact, so an interaction stays gated after the user
forgets the tagged node. Synthesis records it once per batch when any candidate carries a
wellbeing category, or carries self-harm means and is dropped for them (that disclosure
leaves no node at all, D-ORCH-11-18-m). The fact comes first: a merge never lands for a
disclosure whose fact is not held. A failed fact write stops the batch, so the job retries
with its marker unmoved; a persona that is gone stops it the way an erasure does.
"""

from __future__ import annotations

import pytest
from persona.extraction import ExtractionCandidate, ExtractionInput, InteractionKind
from persona.graph.models import NodeKind
from persona.graph.protocol import KnowledgeCandidate, MergeAction, MergeOutcome, UpdateIntent
from persona.wellbeing import WellbeingCategory
from persona_runtime.extraction.synthesizer import Synthesizer

_OWNER = "u1"
_PERSONA = "persona-a"
_CONV = "conv-1"


def _cand(
    name: str, *, category: WellbeingCategory | None = None, content: str | None = None
) -> ExtractionCandidate:
    return ExtractionCandidate(
        concept_name=name,
        content=content or f"about {name}",
        node_kind=NodeKind.FACT,
        evidence_span="said it",
        wellbeing_category=category,
        update_intent=UpdateIntent.NONE,
    )


_TAGGED = _cand("eating", category=WellbeingCategory.DISORDERED_EATING)
_DINNER = _cand("dinner")
#: Dropped by the means guard before any merge: the disclosure leaves no node behind.
_MEANS = _cand(
    "means", category=WellbeingCategory.SELF_HARM, content="thinking of all my pills at once"
)
_MEANS_UNTAGGED = _cand("means-untagged", content="I keep a noose in the shed")


class _Log:
    def __init__(self) -> None:
        self.events: list[tuple[str, ...]] = []


class _Facts:
    def __init__(self, log: _Log, *, held: bool = True, error: Exception | None = None) -> None:
        self._log = log
        self._held = held
        self._error = error

    def record(self, owner_id: str, persona_id: str, interaction_id: str) -> bool:
        self._log.events.append(("fact", owner_id, persona_id, interaction_id))
        if self._error is not None:
            raise self._error
        return self._held


class _Extractor:
    def __init__(self, candidates: tuple[ExtractionCandidate, ...]) -> None:
        self._candidates = candidates

    async def extract(self, _interaction: ExtractionInput) -> tuple[ExtractionCandidate, ...]:
        return self._candidates


class _Entities:
    async def resolve_mentions(self, *_a: object, **_k: object) -> dict[str, str]:
        return {}


class _Updates:
    def resolve_target(self, *_a: object) -> None:
        return None


class _Store:
    def __init__(self, log: _Log) -> None:
        self._log = log

    def merge(self, _owner: str, candidate: KnowledgeCandidate) -> MergeOutcome:
        self._log.events.append(("merge", candidate.concept_name))
        return MergeOutcome(action=MergeAction.CREATED, node_id=f"n-{candidate.concept_name}")

    def search_dense(self, *_a: object, **_k: object) -> list[object]:
        return []


def _input() -> ExtractionInput:
    return ExtractionInput(
        interaction_kind=InteractionKind.CONVERSATION,
        interaction_id=_CONV,
        persona_id=_PERSONA,
        content="the interaction",
    )


def _synth(
    log: _Log, candidates: tuple[ExtractionCandidate, ...], facts: _Facts | None
) -> Synthesizer:
    return Synthesizer(
        extractor=_Extractor(candidates),  # type: ignore[arg-type]
        entity_resolver=_Entities(),  # type: ignore[arg-type]
        update_resolver=_Updates(),  # type: ignore[arg-type]
        graph_store=_Store(log),  # type: ignore[arg-type]
        disclosures=facts,
    )


_FACT = ("fact", _OWNER, _PERSONA, _CONV)


@pytest.mark.asyncio
async def test_a_tagged_candidate_records_the_fact_once_before_any_merge() -> None:
    log = _Log()
    await _synth(log, (_DINNER, _TAGGED, _cand("more")), _Facts(log)).synthesise(_OWNER, _input())
    assert log.events == [_FACT, ("merge", "dinner"), ("merge", "eating"), ("merge", "more")]


@pytest.mark.asyncio
async def test_an_untagged_batch_records_nothing() -> None:
    log = _Log()
    await _synth(log, (_DINNER,), _Facts(log)).synthesise(_OWNER, _input())
    assert log.events == [("merge", "dinner")]


@pytest.mark.parametrize("means", [_MEANS, _MEANS_UNTAGGED], ids=["tagged", "untagged"])
@pytest.mark.asyncio
async def test_a_disclosure_dropped_for_its_means_still_records_the_fact(
    means: ExtractionCandidate,
) -> None:
    log = _Log()
    await _synth(log, (_DINNER, means), _Facts(log)).synthesise(_OWNER, _input())
    assert log.events == [_FACT, ("merge", "dinner")]


@pytest.mark.asyncio
async def test_a_failed_fact_write_stops_the_batch_before_any_merge() -> None:
    log = _Log()
    synth = _synth(log, (_DINNER, _TAGGED), _Facts(log, error=OSError("database down")))
    with pytest.raises(OSError, match="database down"):
        await synth.synthesise(_OWNER, _input())
    assert log.events == [_FACT]


@pytest.mark.asyncio
async def test_a_persona_gone_before_the_fact_merges_nothing() -> None:
    log = _Log()
    outcomes = await _synth(log, (_DINNER, _TAGGED), _Facts(log, held=False)).synthesise(
        _OWNER, _input()
    )
    assert outcomes == []
    assert log.events == [_FACT]


@pytest.mark.asyncio
async def test_without_a_recorder_synthesis_is_unchanged() -> None:
    log = _Log()
    await _synth(log, (_DINNER, _TAGGED), None).synthesise(_OWNER, _input())
    assert log.events == [("merge", "dinner"), ("merge", "eating")]

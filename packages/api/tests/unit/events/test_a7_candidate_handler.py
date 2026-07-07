"""Unit tests for door (b)'s ``event_candidate`` handler (Spec A7, T3/T8).

Proves the handler (1) enforces the wellbeing subject-exclusion at the seam BEFORE any producer runs
— layer (a) of criterion 8, structural for every candidate, audited, never silent — and (2) runs the
producer and submits any candidate to the A5 pipeline sink otherwise. The concrete producer + the
end-to-end adversarial fixture live in the integration suite.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace

import pytest
from persona_api.events import EventCandidateHandler, EventCandidatePayload

_PAYLOAD = EventCandidatePayload(
    persona_id="pers-1",
    event_kind="connector.message_received",
    event_id="evt-1",
    trigger_id="trg-1",
    human="a message from x arrived",
    causal_chain=("trg-1",),
    grounding_kind="conversation",
    grounding_ref="conv-9",
)
_CTX = SimpleNamespace(owner_id="own-1")


class _FakeProducer:
    def __init__(self, candidate: object | None) -> None:
        self._candidate = candidate
        self.calls = 0

    async def produce(self, payload: EventCandidatePayload, context: object) -> object | None:  # noqa: ARG002
        self.calls += 1
        return self._candidate


class _FakeSink:
    def __init__(self) -> None:
        self.submitted: list[tuple[object, ...]] = []

    async def submit(self, candidates: tuple[object, ...]) -> None:
        self.submitted.append(candidates)


class _FakeWellbeing:
    def __init__(self, *, gated: bool) -> None:
        self._gated = gated
        self.calls: list[tuple[str, str, str]] = []

    def is_gated_subject(self, owner_id: str, grounding_kind: str, grounding_ref: str) -> bool:
        self.calls.append((owner_id, grounding_kind, grounding_ref))
        return self._gated


class _FakeAudit:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str, Mapping[str, str] | None]] = []

    def __call__(
        self, owner: str, action: str, target: str, metadata: Mapping[str, str] | None = None
    ) -> None:
        self.rows.append((owner, action, target, metadata))


def _handler(
    producer: _FakeProducer, sink: _FakeSink, wellbeing: _FakeWellbeing, audit: _FakeAudit
) -> EventCandidateHandler:
    return EventCandidateHandler(
        producer=producer,  # type: ignore[arg-type]
        sink=sink,  # type: ignore[arg-type]
        wellbeing=wellbeing,  # type: ignore[arg-type]
        audit=audit,
    )


@pytest.mark.asyncio
async def test_handler_submits_a_produced_candidate() -> None:
    marker = object()
    producer, sink, wellbeing, audit = (
        _FakeProducer(marker),
        _FakeSink(),
        _FakeWellbeing(gated=False),
        _FakeAudit(),
    )
    await _handler(producer, sink, wellbeing, audit).handle(_PAYLOAD, _CTX)  # type: ignore[arg-type]
    assert producer.calls == 1
    assert sink.submitted == [(marker,)]
    assert wellbeing.calls == [("own-1", "conversation", "conv-9")]  # gate consulted first
    assert audit.rows == []  # a passing candidate needs no drop row


@pytest.mark.asyncio
async def test_handler_submits_nothing_for_a_thin_event() -> None:
    producer = _FakeProducer(None)  # the producer raised nothing (a thin event is success)
    sink, wellbeing, audit = _FakeSink(), _FakeWellbeing(gated=False), _FakeAudit()
    await _handler(producer, sink, wellbeing, audit).handle(_PAYLOAD, _CTX)  # type: ignore[arg-type]
    assert producer.calls == 1
    assert sink.submitted == []  # None ⇒ nothing enters the A5 pipeline


@pytest.mark.asyncio
async def test_handler_drops_wellbeing_gated_grounding_before_the_producer() -> None:
    """Layer (a), the load-bearing one (criterion 8): a gated grounding never reaches the model.

    The wellbeing gate is consulted at the seam; a gated subject is dropped with an audit row and
    the producer is NEVER called — structural, not producer discretion.
    """
    producer = _FakeProducer(object())  # a producer that WOULD emit — proves it is never consulted
    sink, wellbeing, audit = _FakeSink(), _FakeWellbeing(gated=True), _FakeAudit()
    await _handler(producer, sink, wellbeing, audit).handle(_PAYLOAD, _CTX)  # type: ignore[arg-type]
    assert producer.calls == 0  # the model is never even reached
    assert sink.submitted == []  # nothing enters the pipeline
    assert len(audit.rows) == 1  # the drop is audited, never silent
    owner, action, target, metadata = audit.rows[0]
    assert owner == "own-1"
    assert action == "event_trigger.candidate_wellbeing_dropped"
    assert target == "trg-1"
    assert metadata == {"event_id": "evt-1", "grounding": "conversation/conv-9"}


def test_producer_protocol_is_structural() -> None:
    from persona_api.events.candidate_handler import EventCandidateProducer as _Proto

    assert isinstance(_FakeProducer(None), _Proto)
    assert not isinstance(object(), _Proto)


def test_wellbeing_check_protocol_is_structural() -> None:
    from persona_api.events.candidate_handler import EventWellbeingCheck as _Proto

    assert isinstance(_FakeWellbeing(gated=False), _Proto)
    assert not isinstance(object(), _Proto)

"""The A7 → A6 provenance contract shape test (Spec A7, T8; A7-D-9).

Drives the REAL emitters (the dispatcher's gate chain + door routing, and door-b's handler seam) and
asserts every audit row + the leg-attached ``EventFire`` matches the FROZEN vocabulary in
:mod:`persona_api.events.provenance`. A renamed action or a changed metadata key fails HERE — the
guard that keeps the contract A6 builds against from drifting silently. (The dispatcher builds its
metadata dicts with inline literal keys; the frozen key sets are independent literals in the
contract module — a mismatch is genuine drift, not a tautology.)
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from persona.errors import TaskNotFoundError
from persona.events import (
    ConnectorMessageReceived,
    ConnectorUnlinked,
    EnqueueInitiativeCandidate,
    EventKind,
    FireTaskLeg,
    LinkFilter,
    MessageFilter,
)
from persona.tasks import EventFire
from persona_api.events import (
    CANDIDATE_WELLBEING_DROPPED_METADATA_KEYS,
    DEPTH_EXCEEDED_METADATA_KEYS,
    EVENT_FIRE_IDENTITY_FIELDS,
    EVENT_TRIGGER_AUDIT_ACTIONS,
    EVENT_TRIGGER_CANDIDATE_WELLBEING_DROPPED,
    EVENT_TRIGGER_DEPTH_EXCEEDED,
    EVENT_TRIGGER_FIRED,
    EVENT_TRIGGER_LOOP_REFUSED,
    EVENT_TRIGGER_STORM_DROPPED,
    EVENT_TRIGGER_TASK_MISSING,
    EVENT_TRIGGER_UNGROUNDABLE,
    FIRED_METADATA_KEYS,
    LOOP_REFUSED_METADATA_KEYS,
    PROVENANCE_RENDER_FIELD,
    STORM_DROPPED_METADATA_KEYS,
    EventCandidateHandler,
    EventCandidatePayload,
    EventDispatcher,
    EventTriggerRecord,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from persona.events import Event

_NOW = datetime(2026, 7, 5, 9, 0, tzinfo=UTC)
_OWNER = "own-1"
_PERSONA = "pers-1"


# --- fakes (mirroring the dispatcher unit fakes) ------------------------------------------------


class _FakeStore:
    def __init__(
        self, triggers: list[EventTriggerRecord], *, fired: bool, coalesced: int = 0
    ) -> None:
        self._triggers = triggers
        self._claim = SimpleNamespace(fired=fired, coalesced_count=coalesced)

    def list_active(self, owner_id: str, event_kind: EventKind) -> list[EventTriggerRecord]:
        return [t for t in self._triggers if t.owner_id == owner_id and t.event_kind == event_kind]

    def claim_fire(self, *_a: object, **_k: object) -> SimpleNamespace:
        return self._claim


class _FakeQueue:
    def __init__(self) -> None:
        self.enqueued: list[dict[str, Any]] = []

    def enqueue(self, **kwargs: Any) -> None:  # noqa: ANN401
        self.enqueued.append(kwargs)


class _FakeTaskStore:
    def __init__(self, *, missing: bool = False) -> None:
        self._missing = missing

    def get(self, owner_id: str, task_id: str) -> object:  # noqa: ARG002
        if self._missing:
            raise TaskNotFoundError(task_id)
        return SimpleNamespace(head_checkpoint_seq=3)


class _Recorder:
    def __init__(self) -> None:
        self.audits: list[tuple[str, str, str, Mapping[str, str] | None]] = []

    def audit(self, owner: str, action: str, target: str, meta: Mapping[str, str] | None) -> None:
        self.audits.append((owner, action, target, meta))

    def surface(self, *_a: object) -> None:
        return

    def only(self, action: str) -> tuple[str, str, str, Mapping[str, str]]:
        rows = [a for a in self.audits if a[1] == action]
        assert len(rows) == 1, f"expected exactly one {action} row, got {len(rows)}"
        owner, act, target, meta = rows[0]
        assert meta is not None
        return owner, act, target, meta


def _message_event(**overrides: object) -> ConnectorMessageReceived:
    base: dict[str, object] = {
        "event_id": "evt-1",
        "owner_id": _OWNER,
        "occurred_at": _NOW,
        "platform": "email",
        "sender_id": "landlord@example.com",
        "body": "the rent is due",
        "conversation_id": "conv-9",
        "persona_id": _PERSONA,
        "message_id": "msg-1",
    }
    base.update(overrides)
    return ConnectorMessageReceived(**base)  # type: ignore[arg-type]


def _trigger(action: object, *, filter_: object | None = None) -> EventTriggerRecord:
    return EventTriggerRecord(
        id="trg-1",
        owner_id=_OWNER,
        persona_id=_PERSONA,
        task_id="task-1" if isinstance(action, FireTaskLeg) else None,
        event_kind=EventKind.CONNECTOR_MESSAGE_RECEIVED,
        platform="email",
        filter=filter_ or MessageFilter(platform="email"),  # type: ignore[arg-type]
        action=action,  # type: ignore[arg-type]
        enabled=True,
        disabled_reason=None,
        last_fired_at=None,
        pending_coalesced_count=0,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _dispatcher(
    store: _FakeStore,
    queue: _FakeQueue,
    rec: _Recorder,
    *,
    task_store: _FakeTaskStore | None = None,
    budget_ok: bool = True,
) -> EventDispatcher:
    return EventDispatcher(
        store=store,  # type: ignore[arg-type]
        queue=queue,  # type: ignore[arg-type]
        task_store=task_store or _FakeTaskStore(),  # type: ignore[arg-type]
        settings_cooldown_seconds=300,
        settings_max_chain_depth=3,
        audit=rec.audit,
        surface_drop=rec.surface,
        budget_check=lambda _o: budget_ok,
    )


# --- route (a): the audit rows match the frozen shapes ------------------------------------------


def test_fired_row_matches_the_frozen_shape() -> None:
    store = _FakeStore([_trigger(FireTaskLeg(task_id="task-1"))], fired=True)
    queue, rec = _FakeQueue(), _Recorder()
    _dispatcher(store, queue, rec).dispatch(_message_event(), now=_NOW)
    _owner, _act, target, meta = rec.only(EVENT_TRIGGER_FIRED)
    assert target == "task-1"  # door a → the fired task's id
    assert set(meta.keys()) == FIRED_METADATA_KEYS
    assert meta["door"] == "fire_task_leg"
    assert meta[PROVENANCE_RENDER_FIELD]  # the "ran because" string is present + non-empty


def test_fired_row_door_b_targets_the_trigger() -> None:
    store = _FakeStore([_trigger(EnqueueInitiativeCandidate())], fired=True)
    queue, rec = _FakeQueue(), _Recorder()
    _dispatcher(store, queue, rec).dispatch(_message_event(), now=_NOW)
    _owner, _act, target, meta = rec.only(EVENT_TRIGGER_FIRED)
    assert target == "trg-1"  # door b → the trigger id
    assert set(meta.keys()) == FIRED_METADATA_KEYS
    assert meta["door"] == "enqueue_candidate"


def test_loop_refused_row_matches_the_frozen_shape() -> None:
    store = _FakeStore([_trigger(FireTaskLeg(task_id="task-1"))], fired=True)
    rec = _Recorder()
    _dispatcher(store, _FakeQueue(), rec).dispatch(
        _message_event(causal_chain=("trg-1",)), now=_NOW
    )
    _owner, _act, target, meta = rec.only(EVENT_TRIGGER_LOOP_REFUSED)
    assert target == "trg-1"
    assert set(meta.keys()) == LOOP_REFUSED_METADATA_KEYS


def test_depth_exceeded_row_matches_the_frozen_shape() -> None:
    store = _FakeStore([_trigger(FireTaskLeg(task_id="task-1"))], fired=True)
    rec = _Recorder()
    _dispatcher(store, _FakeQueue(), rec).dispatch(
        _message_event(causal_chain=("a", "b", "c")), now=_NOW
    )
    _owner, _act, target, meta = rec.only(EVENT_TRIGGER_DEPTH_EXCEEDED)
    assert set(meta.keys()) == DEPTH_EXCEEDED_METADATA_KEYS


def test_storm_dropped_row_matches_the_frozen_shape() -> None:
    store = _FakeStore([_trigger(FireTaskLeg(task_id="task-1"))], fired=True)
    rec = _Recorder()
    _dispatcher(store, _FakeQueue(), rec, budget_ok=False).dispatch(_message_event(), now=_NOW)
    _owner, _act, target, meta = rec.only(EVENT_TRIGGER_STORM_DROPPED)
    assert target == "trg-1"
    assert set(meta.keys()) == STORM_DROPPED_METADATA_KEYS


def test_task_missing_and_ungroundable_are_in_the_closed_vocabulary() -> None:
    # task_missing (door a, task vanished) + ungroundable (door b, a link event) are the sibling
    # forensic rows — emitted, and members of the closed set (A6 never meets a surprise action).
    store_a = _FakeStore([_trigger(FireTaskLeg(task_id="task-1"))], fired=True)
    rec_a = _Recorder()
    _dispatcher(store_a, _FakeQueue(), rec_a, task_store=_FakeTaskStore(missing=True)).dispatch(
        _message_event(), now=_NOW
    )
    _o, _a, target, meta = rec_a.only(EVENT_TRIGGER_TASK_MISSING)

    link_trigger = _trigger(EnqueueInitiativeCandidate())
    link_trigger = link_trigger.model_copy(
        update={
            "event_kind": EventKind.CONNECTOR_UNLINKED,
            "filter": LinkFilter(platform="email"),
        }
    )
    event: Event = ConnectorUnlinked(
        event_id="evt-2", owner_id=_OWNER, occurred_at=_NOW, platform="email"
    )
    rec_b = _Recorder()
    _dispatcher(_FakeStore([link_trigger], fired=True), _FakeQueue(), rec_b).dispatch(
        event, now=_NOW
    )
    rec_b.only(EVENT_TRIGGER_UNGROUNDABLE)

    for rec in (rec_a, rec_b):
        for _owner, action, _target, _meta in rec.audits:
            assert action in EVENT_TRIGGER_AUDIT_ACTIONS  # nothing outside the closed vocabulary


@pytest.mark.asyncio
async def test_candidate_wellbeing_dropped_row_matches_the_frozen_shape() -> None:
    captured: list[tuple[str, str, str, Mapping[str, str] | None]] = []

    class _Wellbeing:
        def is_gated_subject(self, *_a: object) -> bool:
            return True

    class _Producer:
        async def produce(self, *_a: object) -> object | None:
            return None

    def _audit(owner: str, action: str, target: str, meta: Mapping[str, str] | None = None) -> None:
        captured.append((owner, action, target, meta))

    handler = EventCandidateHandler(
        producer=_Producer(),  # type: ignore[arg-type]
        sink=SimpleNamespace(submit=None),  # type: ignore[arg-type]
        wellbeing=_Wellbeing(),  # type: ignore[arg-type]
        audit=_audit,
    )
    payload = EventCandidatePayload(
        persona_id=_PERSONA,
        event_kind="connector.message_received",
        event_id="evt-1",
        trigger_id="trg-1",
        human="a message arrived",
        causal_chain=("trg-1",),
        grounding_kind="conversation",
        grounding_ref="conv-9",
    )
    await handler.handle(payload, SimpleNamespace(owner_id=_OWNER))  # type: ignore[arg-type]
    assert len(captured) == 1
    _owner, action, target, meta = captured[0]
    assert action == EVENT_TRIGGER_CANDIDATE_WELLBEING_DROPPED
    assert action in EVENT_TRIGGER_AUDIT_ACTIONS
    assert target == "trg-1"
    assert meta is not None
    assert set(meta.keys()) == CANDIDATE_WELLBEING_DROPPED_METADATA_KEYS


# --- route (b): the leg-attached EventFire identity ---------------------------------------------


def test_event_fire_identity_fields_are_frozen() -> None:
    # The model's fields ARE the contract A6 renders; a field add/remove is caught here.
    assert set(EventFire.model_fields) - {"kind"} == EVENT_FIRE_IDENTITY_FIELDS


def test_fired_leg_carries_the_full_event_fire_identity_and_human() -> None:
    store = _FakeStore([_trigger(FireTaskLeg(task_id="task-1"))], fired=True)
    queue, rec = _FakeQueue(), _Recorder()
    _dispatcher(store, queue, rec).dispatch(_message_event(), now=_NOW)
    trigger_payload = queue.enqueued[0]["payload"]["trigger"]
    assert trigger_payload["kind"] == "event_fire"
    assert set(trigger_payload) - {"kind"} == EVENT_FIRE_IDENTITY_FIELDS
    # The SAME render string A6 shows on both surfaces: the leg identity + the audit row.
    _owner, _act, _target, meta = rec.only(EVENT_TRIGGER_FIRED)
    assert trigger_payload[PROVENANCE_RENDER_FIELD] == meta[PROVENANCE_RENDER_FIELD]
    assert trigger_payload[PROVENANCE_RENDER_FIELD]  # non-empty

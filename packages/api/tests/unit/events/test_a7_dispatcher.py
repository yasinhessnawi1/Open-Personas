"""Unit tests for the A7 event dispatcher — the two doors + the gate chain (Spec A7, T3).

Fakes stand in for the store/queue/task-store so the dispatcher's routing + gate logic is tested in
isolation: match → loop guard → pause → storm claim → R7 budget → the two doors (and NO third).
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from persona.errors import TaskNotFoundError
from persona.events import (
    TRIGGER_ACTION_KINDS,
    ConnectorMessageReceived,
    ConnectorUnlinked,
    EnqueueInitiativeCandidate,
    EventKind,
    FireTaskLeg,
    LinkFilter,
    MessageFilter,
)
from persona_api.events import (
    EVENT_CANDIDATE_JOB_TYPE,
    DispatchDisposition,
    EventDispatcher,
    EventDispatchError,
    EventTriggerRecord,
)
from persona_api.tasks.handler import TASK_LEG_JOB_TYPE

if TYPE_CHECKING:
    from collections.abc import Mapping

    from persona.events import Event

_NOW = datetime(2026, 7, 5, 9, 0, tzinfo=UTC)
_OWNER = "own-1"
_PERSONA = "pers-1"


# --- fakes ------------------------------------------------------------------


class _FakeStore:
    def __init__(self, triggers: list[EventTriggerRecord], claim: FireClaimStub) -> None:
        self._triggers = triggers
        self._claim = claim
        self.claim_calls: list[str] = []

    def list_active(self, owner_id: str, event_kind: EventKind) -> list[EventTriggerRecord]:
        return [t for t in self._triggers if t.owner_id == owner_id and t.event_kind == event_kind]

    def claim_fire(self, _owner_id: str, trigger_id: str, **_kwargs: object) -> FireClaimStub:
        self.claim_calls.append(trigger_id)
        return self._claim


class FireClaimStub:
    def __init__(self, *, fired: bool, coalesced_count: int = 0) -> None:
        self.fired = fired
        self.coalesced_count = coalesced_count


class _FakeQueue:
    def __init__(self) -> None:
        self.enqueued: list[dict[str, Any]] = []

    def enqueue(self, **kwargs: Any) -> None:  # noqa: ANN401
        self.enqueued.append(kwargs)


class _FakeTaskStore:
    def __init__(self, *, head: int | None = 3, missing: bool = False) -> None:
        self._head = head
        self._missing = missing

    def get(self, owner_id: str, task_id: str) -> object:  # noqa: ARG002
        if self._missing:
            raise TaskNotFoundError(task_id)
        return SimpleNamespace(head_checkpoint_seq=self._head)


class _Recorder:
    def __init__(self) -> None:
        self.audits: list[tuple[str, str, str, Mapping[str, str] | None]] = []
        self.surfaced: list[tuple[str, str, str]] = []

    def audit(self, owner: str, action: str, target: str, meta: Mapping[str, str] | None) -> None:
        self.audits.append((owner, action, target, meta))

    def surface(self, owner: str, trigger_id: str, human: str) -> None:
        self.surfaced.append((owner, trigger_id, human))


# --- helpers ----------------------------------------------------------------


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
    pause: bool = False,
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
        pause_check=lambda _o: pause,
        budget_check=lambda _o: budget_ok,
    )


# --- tests ------------------------------------------------------------------


def test_matching_message_fires_task_leg_with_event_fire_provenance() -> None:
    store = _FakeStore([_trigger(FireTaskLeg(task_id="task-1"))], FireClaimStub(fired=True))
    queue, rec = _FakeQueue(), _Recorder()
    outcomes = _dispatcher(store, queue, rec).dispatch(_message_event(), now=_NOW)

    assert [o.disposition for o in outcomes] == [DispatchDisposition.FIRED]
    assert len(queue.enqueued) == 1
    job = queue.enqueued[0]
    assert job["type"] == TASK_LEG_JOB_TYPE
    trigger_payload = job["payload"]["trigger"]
    assert trigger_payload["kind"] == "event_fire"
    assert trigger_payload["trigger_id"] == "trg-1"
    assert trigger_payload["causal_chain"] == ["trg-1"]  # the chain now carries this trigger
    assert any(a[1] == "event_trigger.fired" for a in rec.audits)


def test_matching_routes_to_candidate_door() -> None:
    store = _FakeStore([_trigger(EnqueueInitiativeCandidate())], FireClaimStub(fired=True))
    queue, rec = _FakeQueue(), _Recorder()
    outcomes = _dispatcher(store, queue, rec).dispatch(_message_event(), now=_NOW)

    assert outcomes[0].disposition is DispatchDisposition.FIRED
    assert outcomes[0].action_kind == "enqueue_candidate"
    job = queue.enqueued[0]
    assert job["type"] == EVENT_CANDIDATE_JOB_TYPE
    assert job["payload"]["grounding_kind"] == "conversation"
    assert job["payload"]["grounding_ref"] == "conv-9"


def test_non_matching_filter_produces_no_action() -> None:
    trigger = _trigger(FireTaskLeg(task_id="task-1"), filter_=MessageFilter(platform="slack"))
    store = _FakeStore([trigger], FireClaimStub(fired=True))
    queue, rec = _FakeQueue(), _Recorder()
    outcomes = _dispatcher(store, queue, rec).dispatch(_message_event(), now=_NOW)

    assert outcomes == []
    assert queue.enqueued == []
    assert store.claim_calls == []  # a non-match never even claims


def test_loop_refused_when_trigger_already_in_causal_chain() -> None:
    store = _FakeStore([_trigger(FireTaskLeg(task_id="task-1"))], FireClaimStub(fired=True))
    queue, rec = _FakeQueue(), _Recorder()
    event = _message_event(causal_chain=("trg-1",))  # this event was caused by trg-1 already
    outcomes = _dispatcher(store, queue, rec).dispatch(event, now=_NOW)

    assert outcomes[0].disposition is DispatchDisposition.LOOP_REFUSED
    assert queue.enqueued == []
    assert store.claim_calls == []  # a loop refusal never consumes the cooldown window
    assert any(a[1] == "event_trigger.loop_refused" for a in rec.audits)


def test_depth_cap_refuses_a_distinct_trigger_cycle() -> None:
    # A chain of DISTINCT triggers (each new, so the chain-contains guard never fires) is stopped by
    # the depth cap once the chain reaches max_chain_depth (=3 here) — the belt-and-braces backstop.
    store = _FakeStore([_trigger(FireTaskLeg(task_id="task-1"))], FireClaimStub(fired=True))
    queue, rec = _FakeQueue(), _Recorder()
    event = _message_event(causal_chain=("a", "b", "c"))  # depth 3, trigger "trg-1" is NOT in it
    outcomes = _dispatcher(store, queue, rec).dispatch(event, now=_NOW)

    assert outcomes[0].disposition is DispatchDisposition.DEPTH_EXCEEDED
    assert queue.enqueued == []
    assert store.claim_calls == []  # a depth refusal never consumes the cooldown window
    assert any(a[1] == "event_trigger.depth_exceeded" for a in rec.audits)


def test_paused_owner_does_not_fire() -> None:
    store = _FakeStore([_trigger(FireTaskLeg(task_id="task-1"))], FireClaimStub(fired=True))
    queue, rec = _FakeQueue(), _Recorder()
    outcomes = _dispatcher(store, queue, rec, pause=True).dispatch(_message_event(), now=_NOW)

    assert outcomes[0].disposition is DispatchDisposition.PAUSED
    assert queue.enqueued == []
    assert store.claim_calls == []  # a pause never consumes the cooldown window


def test_fired_outcome_reports_the_coalesced_count() -> None:
    # Regression: the FIRED outcome must carry the count the claim coalesced (not default 0) — the
    # "and N more" telemetry T5's real-burst proof asserts.
    store = _FakeStore(
        [_trigger(FireTaskLeg(task_id="task-1"))], FireClaimStub(fired=True, coalesced_count=7)
    )
    queue, rec = _FakeQueue(), _Recorder()
    outcomes = _dispatcher(store, queue, rec).dispatch(_message_event(), now=_NOW)
    assert outcomes[0].disposition is DispatchDisposition.FIRED
    assert outcomes[0].coalesced_count == 7


def test_coalesced_when_claim_does_not_win() -> None:
    store = _FakeStore(
        [_trigger(FireTaskLeg(task_id="task-1"))], FireClaimStub(fired=False, coalesced_count=4)
    )
    queue, rec = _FakeQueue(), _Recorder()
    outcomes = _dispatcher(store, queue, rec).dispatch(_message_event(), now=_NOW)

    assert outcomes[0].disposition is DispatchDisposition.COALESCED
    assert outcomes[0].coalesced_count == 4
    assert queue.enqueued == []


def test_over_budget_drops_with_audit_and_surface() -> None:
    store = _FakeStore([_trigger(FireTaskLeg(task_id="task-1"))], FireClaimStub(fired=True))
    queue, rec = _FakeQueue(), _Recorder()
    outcomes = _dispatcher(store, queue, rec, budget_ok=False).dispatch(_message_event(), now=_NOW)

    assert outcomes[0].disposition is DispatchDisposition.DROPPED_OVER_CAP
    assert queue.enqueued == []  # never enqueued over-cap
    assert any(a[1] == "event_trigger.storm_dropped" for a in rec.audits)
    assert len(rec.surfaced) == 1  # the drop is surfaced, never silent


def test_task_missing_is_a_benign_no_op() -> None:
    store = _FakeStore([_trigger(FireTaskLeg(task_id="task-1"))], FireClaimStub(fired=True))
    queue, rec = _FakeQueue(), _Recorder()
    dispatcher = _dispatcher(store, queue, rec, task_store=_FakeTaskStore(missing=True))
    outcomes = dispatcher.dispatch(_message_event(), now=_NOW)

    assert outcomes[0].disposition is DispatchDisposition.TASK_MISSING
    assert queue.enqueued == []  # no leg, no retry
    assert any(a[1] == "event_trigger.task_missing" for a in rec.audits)


def test_candidate_door_on_ungroundable_link_event() -> None:
    trigger = EventTriggerRecord(
        id="trg-1",
        owner_id=_OWNER,
        persona_id=_PERSONA,
        task_id=None,
        event_kind=EventKind.CONNECTOR_UNLINKED,
        platform="email",
        filter=LinkFilter(platform="email"),
        action=EnqueueInitiativeCandidate(),
        enabled=True,
        disabled_reason=None,
        last_fired_at=None,
        pending_coalesced_count=0,
        created_at=_NOW,
        updated_at=_NOW,
    )
    event: Event = ConnectorUnlinked(
        event_id="evt-2", owner_id=_OWNER, occurred_at=_NOW, platform="email"
    )
    store = _FakeStore([trigger], FireClaimStub(fired=True))
    queue, rec = _FakeQueue(), _Recorder()
    outcomes = _dispatcher(store, queue, rec).dispatch(event, now=_NOW)

    assert outcomes[0].disposition is DispatchDisposition.UNGROUNDABLE
    assert queue.enqueued == []
    assert any(a[1] == "event_trigger.ungroundable" for a in rec.audits)


def test_there_are_exactly_two_doors_and_a_third_raises() -> None:
    # The criterion-2 structural proof at the dispatcher: the routing handles exactly the two
    # closed action kinds, and a would-be third door reaches the loud backstop (never a silent act).
    assert set(TRIGGER_ACTION_KINDS) == {"fire_task_leg", "enqueue_candidate"}
    store = _FakeStore([], FireClaimStub(fired=True))
    dispatcher = _dispatcher(store, _FakeQueue(), _Recorder())
    fake_third_door_trigger = SimpleNamespace(
        id="trg-x",
        persona_id=_PERSONA,
        action=SimpleNamespace(kind="run_tool"),  # a door that does not exist
    )
    with pytest.raises(EventDispatchError):
        dispatcher._route(  # noqa: SLF001 — exercising the exhaustiveness backstop directly
            fake_third_door_trigger,  # type: ignore[arg-type]
            _message_event(),
            now=_NOW,
            coalesced_count=0,
        )

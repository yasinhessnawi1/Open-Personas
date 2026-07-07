"""Unit tests for the A7 connector emission hooks (Spec A7, T6)."""

from __future__ import annotations

from datetime import UTC, datetime

from persona.events import ConnectorLinked, ConnectorMessageReceived
from persona_api.events import make_connector_linked_emit, make_message_received_emit
from persona_api.events.connector_hooks import on_connector_unlinked

_NOW = datetime(2026, 7, 6, 9, 0, tzinfo=UTC)


class _FakeDispatcher:
    def __init__(self) -> None:
        self.dispatched: list[object] = []

    def dispatch(self, event: object, *, now: datetime) -> list[object]:  # noqa: ARG002
        self.dispatched.append(event)
        return []


def test_message_received_emit_builds_the_event() -> None:
    disp = _FakeDispatcher()
    emit = make_message_received_emit(disp)  # type: ignore[arg-type]
    emit(
        owner_id="own-1",
        persona_id="pers-1",
        conversation_id="conv-9",
        platform="email",
        sender_id="landlord@example.com",
        thread_id="thr-1",
        subject="Rent",
        body="pay up",
        message_id="msg-1",
        occurred_at=_NOW,
    )
    event = disp.dispatched[0]
    assert isinstance(event, ConnectorMessageReceived)
    assert event.event_id == "msg-1"  # the platform-stable dedup key
    assert event.conversation_id == "conv-9"  # the grounding ref (A7-D-7)
    assert event.sender_id == "landlord@example.com"
    assert event.subject == "Rent"


def test_connector_linked_emit_builds_the_event() -> None:
    disp = _FakeDispatcher()
    emit = make_connector_linked_emit(disp)  # type: ignore[arg-type]
    emit(owner_id="own-1", platform="telegram", occurred_at=_NOW)
    event = disp.dispatched[0]
    assert isinstance(event, ConnectorLinked)
    assert event.platform == "telegram"
    assert event.owner_id == "own-1"


def test_on_connector_unlinked_is_a_no_op_when_disabled() -> None:
    # Default gate OFF ⇒ returns 0 without touching the engine (guard precedes any DB access).
    assert (
        on_connector_unlinked(
            rls_engine=None,  # type: ignore[arg-type]  # unused on the disabled path
            config=None,  # type: ignore[arg-type]
            owner_id="own-1",
            platform="email",
            now=_NOW,
        )
        == 0
    )

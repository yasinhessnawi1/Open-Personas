"""Synthesis trigger producers (Spec K2, T8d-producer). Unit-proven with a fake queue."""

# ruff: noqa: ANN401 — the fake queue mirrors the real enqueue's loose kwargs.

from __future__ import annotations

from typing import Any

from persona_api.services.synthesis_trigger import (
    enqueue_conversation_synthesis,
    enqueue_run_synthesis,
)


class _FakeQueue:
    def __init__(self) -> None:
        self.enqueued: list[dict[str, Any]] = []

    def enqueue(
        self,
        *,
        type: str,  # noqa: A002 — mirrors the queue API
        owner_id: str,
        payload: dict[str, Any],
        idempotency_key: str,
        **_: Any,
    ) -> None:
        self.enqueued.append(
            {
                "type": type,
                "owner_id": owner_id,
                "payload": payload,
                "idempotency_key": idempotency_key,
            }
        )


def test_conversation_trigger_enqueues_a_conversation_synthesis() -> None:
    q = _FakeQueue()
    enqueue_conversation_synthesis(
        q, owner_id="u1", conversation_id="conv-1", persona_id="p1", message_count=4
    )
    # A conversation boundary enqueues the K2 synthesis job AND (K8) the episodic-consolidation
    # job — two durable A0 producers off the same interaction boundary. Select by type so the
    # assertion is order-independent.
    assert len(q.enqueued) == 2
    job = next(j for j in q.enqueued if j["type"] == "synthesis")
    assert job["owner_id"] == "u1"
    assert job["payload"]["interaction_kind"] == "conversation"
    assert job["payload"]["interaction_id"] == "conv-1"
    assert job["payload"]["high_water_mark"] == 4
    assert job["idempotency_key"] == "synthesis:conversation:conv-1:4"
    assert any(j["type"] == "episodic_consolidation" for j in q.enqueued)


def test_run_trigger_enqueues_an_agentic_run_synthesis() -> None:
    q = _FakeQueue()
    enqueue_run_synthesis(q, owner_id="u1", run_id="run-9", persona_id="p1")
    job = q.enqueued[0]
    assert job["payload"]["interaction_kind"] == "agentic_run"
    assert job["payload"]["interaction_id"] == "run-9"
    assert job["idempotency_key"] == "synthesis:agentic_run:run-9:1"


def test_no_queue_is_a_safe_noop() -> None:
    # The CLI / queue-not-configured path must not raise.
    enqueue_conversation_synthesis(
        None, owner_id="u1", conversation_id="c", persona_id="p", message_count=1
    )
    enqueue_run_synthesis(None, owner_id="u1", run_id="r", persona_id="p")

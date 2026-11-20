"""The ``conversation_memory_forget`` job and its producer gate (R9-311). No database.

The job forgets the chunk ids its row delete captured, and only for a conversation its owner
marked forgotten. The route enqueues it only when a worker in this process runs it. The SQL is
proved on Postgres in tests/integration/test_conversation_memory_forget_postgres.py.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import pytest
from persona.jobs import JobRegistry
from persona_api.jobs.handlers import conversation_memory_forget as handler_module
from persona_api.jobs.handlers.conversation_memory_forget import (
    ConversationMemoryForgetHandler,
    conversation_memory_forget_queue_available,
    register_conversation_memory_forget_handler,
)
from persona_api.services import conversation_memory_forget as service
from persona_api.services.conversation_memory_forget import ConversationMemoryForgetPayload

if TYPE_CHECKING:
    from collections.abc import Iterator

_PAYLOAD = ConversationMemoryForgetPayload(
    persona_id="p1", conversation_id="c1", chunk_ids=["s2", "s1"]
)


class _Context:
    owner_id = "u1"
    job_id = "job-1"

    @contextmanager
    def connection(self) -> Iterator[str]:
        yield "the owner's connection"


class _Store:
    instances: list[_Store] = []  # noqa: RUF012 - a test recorder

    def __init__(self, **kwargs: Any) -> None:  # noqa: ANN401
        self.kwargs = kwargs
        self.removed: list[tuple[str, list[str]]] = []
        _Store.instances.append(self)

    def remove_documents(self, persona_id: str, doc_ids: list[str]) -> None:
        self.removed.append((persona_id, doc_ids))


@pytest.fixture(autouse=True)
def _fake_store(monkeypatch: pytest.MonkeyPatch) -> None:
    _Store.instances = []
    monkeypatch.setattr(handler_module, "EpisodicStore", _Store)


def _run(monkeypatch: pytest.MonkeyPatch, *, forgotten: bool) -> list[dict[str, Any]]:
    asked: list[dict[str, Any]] = []

    def _is_forgotten(conn: object, **kwargs: Any) -> bool:  # noqa: ANN401
        asked.append({"conn": conn, **kwargs})
        return forgotten

    monkeypatch.setattr(handler_module, "is_forgotten", _is_forgotten)
    handler = ConversationMemoryForgetHandler(backend="backend", audit_logger="audit")  # type: ignore[arg-type]
    asyncio.run(handler.handle(_PAYLOAD, _Context()))  # type: ignore[arg-type]
    return asked


def test_the_job_forgets_the_captured_ids_of_a_forgotten_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked = _run(monkeypatch, forgotten=True)

    assert asked == [
        {
            "conn": "the owner's connection",
            "owner_id": "u1",
            "persona_id": "p1",
            "conversation_id": "c1",
        }
    ]
    (store,) = _Store.instances
    assert store.kwargs == {"backend": "backend", "audit_logger": "audit"}
    assert store.removed == [("p1", ["s1", "s2"])]


def test_the_job_does_nothing_unless_its_owner_forgot_the_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run(monkeypatch, forgotten=False)

    assert _Store.instances == []


def test_a_store_failure_fails_the_job_so_the_queue_retries_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _broken(self: _Store, persona_id: str, doc_ids: list[str]) -> None:  # noqa: ARG001
        msg = "the memory store is unavailable"
        raise RuntimeError(msg)

    monkeypatch.setattr(_Store, "remove_documents", _broken)

    with pytest.raises(RuntimeError):
        _run(monkeypatch, forgotten=True)


def test_the_payload_is_ids_only_and_round_trips_through_json() -> None:
    stored = json.loads(json.dumps(_PAYLOAD.model_dump()))

    assert stored == {"persona_id": "p1", "conversation_id": "c1", "chunk_ids": ["s2", "s1"]}
    assert ConversationMemoryForgetPayload.model_validate(stored) == _PAYLOAD
    assert service.idempotency_key(_PAYLOAD) == "conversation_memory_forget:c1"


def test_the_tenant_registers_with_a_retry_that_outlasts_an_outage() -> None:
    registry = JobRegistry()

    register_conversation_memory_forget_handler(registry, backend="b", audit_logger="a")  # type: ignore[arg-type]

    spec = registry.get(service.JOB_TYPE)
    assert spec.retry.max_attempts == service.JOB_MAX_ATTEMPTS
    assert spec.payload_model is ConversationMemoryForgetPayload


class _Worker:
    def __init__(self, *types: str) -> None:
        self.job_types = frozenset(types)


@pytest.mark.parametrize(
    ("queue", "worker", "expected"),
    [
        (object(), _Worker(service.JOB_TYPE), True),
        (object(), _Worker("title_refresh"), False),
        (None, _Worker(service.JOB_TYPE), False),
        (object(), None, False),
    ],
)
def test_the_route_enqueues_only_where_a_worker_here_runs_the_job(
    queue: object | None, worker: object | None, expected: bool
) -> None:
    assert (
        conversation_memory_forget_queue_available(job_queue=queue, in_process_worker=worker)
        is expected
    )


def test_off_postgres_the_record_step_records_nothing() -> None:
    class _Dialect:
        name = "sqlite"

    class _Conn:
        dialect = _Dialect()

        def execute(self, *_a: object) -> None:
            msg = "no statement may run off Postgres"
            raise AssertionError(msg)

    assert service.record(_Conn(), ["s1"], owner_id="u1", conversation_id="c1", durable=True) == []  # type: ignore[arg-type]

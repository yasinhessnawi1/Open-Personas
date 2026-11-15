"""The ``persona_erase`` job carries a ledger erasure and nothing else (R9-284).

The erasure itself is pinned in tests/unit/services/test_erase_persona.py. Here: the job only
runs an erasure its own owner claimed, a finished erasure is a no-op on any redelivery, a
refusal is permanent, and an erasure whose job died is resumed under a key of its own.
"""

from __future__ import annotations

import asyncio
from pathlib import Path  # noqa: TC003 - a fixture annotation
from typing import TYPE_CHECKING, Any

import pytest
from _erase_seed import OTHER_OWNER, OWNER, PERSONA, SIBLING, STRANGER, Seeded, seed
from persona.errors import PermanentJobError
from persona_api.jobs.handlers.persona_erase import (
    PERSONA_ERASE_JOB_TYPE,
    PersonaEraseHandler,
    PersonaErasePayload,
    enqueue_persona_erase,
    persona_erase_idempotency_key,
    persona_erase_queue_available,
    resume_persona_erasures,
    resume_token,
)
from persona_api.services.erasure_ledger import ErasureLedger, ErasureState, SubjectKind
from persona_api.services.erasure_service import PersonaEraseDeps, capture_persona
from pydantic import ValidationError

if TYPE_CHECKING:
    from persona_api.services.erasure_ledger import Erasure

_ERASURE_ID = "erasure_" + "a" * 32


class _Context:
    def __init__(self, owner_id: str) -> None:
        self.owner_id = owner_id


@pytest.fixture
def seeded(tmp_path: Path, embedder: object) -> Seeded:
    return seed(tmp_path, embedder)  # type: ignore[arg-type]


def _handler(seeded: Seeded) -> PersonaEraseHandler:
    return PersonaEraseHandler(
        deps=PersonaEraseDeps(
            engine=seeded.engine,
            memory_backend=seeded.memory,
            audit_logger=seeded.audit,
            file_storage=seeded.storage,
            workspace_root=seeded.workspace_root,
            audit_root=seeded.audit_root,
        )
    )


def _claim(seeded: Seeded, owner: str = OWNER, persona: str = PERSONA) -> Erasure:
    with seeded.engine.connect() as conn:
        captured = capture_persona(conn, owner_id=owner, persona_id=persona)
    return ErasureLedger(seeded.engine).claim(
        owner_id=owner, subject_kind=SubjectKind.PERSONA, subject_id=persona, captured=captured
    )


def _payload(erasure: Erasure) -> PersonaErasePayload:
    return PersonaErasePayload(erasure_id=erasure.id, persona_id=erasure.subject_id)


def _state(seeded: Seeded, persona: str = PERSONA) -> ErasureState | None:
    found = ErasureLedger(seeded.engine).get(SubjectKind.PERSONA, persona)
    return None if found is None else found.state


@pytest.mark.parametrize(
    "fields",
    [
        {"erasure_id": "erasure_1", "persona_id": PERSONA},
        {"erasure_id": _ERASURE_ID, "persona_id": ".."},
        {"erasure_id": _ERASURE_ID, "persona_id": PERSONA, "resume_token": "a b"},
        {"erasure_id": _ERASURE_ID, "persona_id": PERSONA, "verbatim": "text"},
    ],
)
def test_the_payload_holds_ids_and_nothing_else(fields: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        PersonaErasePayload(**fields)


def test_a_resume_has_its_own_key() -> None:
    first = PersonaErasePayload(erasure_id=_ERASURE_ID, persona_id=PERSONA)
    again = PersonaErasePayload(erasure_id=_ERASURE_ID, persona_id=PERSONA, resume_token="123")
    assert persona_erase_idempotency_key(first) == f"erase:{_ERASURE_ID}"
    assert persona_erase_idempotency_key(again) == f"erase:{_ERASURE_ID}:123"


def test_the_job_runs_the_erasure_to_completion(seeded: Seeded) -> None:
    erasure = _claim(seeded)

    asyncio.run(_handler(seeded).handle(_payload(erasure), _Context(OWNER)))  # type: ignore[arg-type]

    assert _state(seeded) is ErasureState.COMPLETE


def test_a_redelivered_job_for_a_finished_erasure_does_nothing(seeded: Seeded) -> None:
    erasure = _claim(seeded)
    handler = _handler(seeded)
    asyncio.run(handler.handle(_payload(erasure), _Context(OWNER)))  # type: ignore[arg-type]
    finished = ErasureLedger(seeded.engine).get(SubjectKind.PERSONA, PERSONA)

    asyncio.run(handler.handle(_payload(erasure), _Context(OWNER)))  # type: ignore[arg-type]

    assert ErasureLedger(seeded.engine).get(SubjectKind.PERSONA, PERSONA) == finished


def test_a_job_of_another_owner_is_refused_and_erases_nothing(seeded: Seeded) -> None:
    erasure = _claim(seeded)

    with pytest.raises(PermanentJobError):
        asyncio.run(
            _handler(seeded).handle(_payload(erasure), _Context(OTHER_OWNER))  # type: ignore[arg-type]
        )

    assert _state(seeded) is ErasureState.PENDING


def test_a_job_with_no_erasure_behind_it_is_refused(seeded: Seeded) -> None:
    payload = PersonaErasePayload(erasure_id=_ERASURE_ID, persona_id=SIBLING)

    with pytest.raises(PermanentJobError):
        asyncio.run(_handler(seeded).handle(payload, _Context(OWNER)))  # type: ignore[arg-type]


def test_a_refused_erasure_is_a_permanent_failure(seeded: Seeded) -> None:
    forged = ErasureLedger(seeded.engine).claim(
        owner_id=OWNER, subject_kind=SubjectKind.PERSONA, subject_id=STRANGER, captured={}
    )

    with pytest.raises(PermanentJobError):
        asyncio.run(_handler(seeded).handle(_payload(forged), _Context(OWNER)))  # type: ignore[arg-type]


class _Worker:
    def __init__(self, *types: str) -> None:
        self.job_types = frozenset(types)


@pytest.mark.parametrize(
    ("queue", "worker", "expected"),
    [
        (None, _Worker(PERSONA_ERASE_JOB_TYPE), False),
        (object(), None, False),
        (object(), _Worker("avatar_generation"), False),
        (object(), _Worker(PERSONA_ERASE_JOB_TYPE), True),
    ],
)
def test_the_job_is_enqueued_only_where_a_worker_consumes_it(
    queue: object | None, worker: object | None, expected: bool
) -> None:
    assert persona_erase_queue_available(job_queue=queue, in_process_worker=worker) is expected


class _Queue:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def enqueue(self, **kwargs: Any) -> None:  # noqa: ANN401
        self.calls.append(kwargs)


def test_enqueue_carries_the_owner_and_the_key(seeded: Seeded) -> None:
    erasure = _claim(seeded)
    queue = _Queue()

    enqueue_persona_erase(queue, erasure, token="42")  # type: ignore[arg-type]

    (call,) = queue.calls
    assert call["type"] == PERSONA_ERASE_JOB_TYPE
    assert call["owner_id"] == OWNER
    assert call["idempotency_key"] == f"erase:{erasure.id}:42"
    assert call["payload"] == {
        "erasure_id": erasure.id,
        "persona_id": PERSONA,
        "resume_token": "42",
    }


def test_every_unfinished_persona_erasure_is_resumed_and_nothing_else(seeded: Seeded) -> None:
    ledger = ErasureLedger(seeded.engine)
    pending = _claim(seeded)
    failed = _claim(seeded, persona=SIBLING)
    ledger.fail(failed.id, "memory_failed")
    finished = _claim(seeded, owner=OTHER_OWNER, persona=STRANGER)
    ledger.complete(finished.id)
    ledger.claim(owner_id=OWNER, subject_kind=SubjectKind.ACCOUNT, subject_id=OWNER, captured={})
    resumed: list[tuple[str, str]] = []

    count = resume_persona_erasures(
        engine=seeded.engine, run=lambda e, token: resumed.append((e.id, token))
    )

    assert count == 2
    latest = {e.id: e for e in ledger.unfinished()}
    assert sorted(resumed) == sorted(
        (eid, resume_token(latest[eid])) for eid in (pending.id, failed.id)
    )

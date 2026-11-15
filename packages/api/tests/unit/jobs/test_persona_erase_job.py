"""The ``persona_erase`` job carries a ledger erasure and nothing else (R9-284).

The erasure itself is pinned in tests/unit/services/test_erase_persona.py. Here: the job only
runs an erasure its own owner claimed, a finished erasure is a no-op on any redelivery, a
refusal is permanent, and an erasure whose job died is resumed under a key of its own.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator  # noqa: TC003 - a fixture annotation
from datetime import timedelta
from pathlib import Path  # noqa: TC003 - a fixture annotation
from typing import TYPE_CHECKING, Any

import pytest
from _erase_seed import OTHER_OWNER, OWNER, PERSONA, SIBLING, STRANGER, Seeded, legacy_edition
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
from persona_api.services import erasure_service
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
def seeded(tmp_path: Path, embedder: object) -> Iterator[Seeded]:
    with legacy_edition(tmp_path, embedder) as legacy:  # type: ignore[arg-type]
        yield legacy


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
        engine=seeded.engine,
        grace=timedelta(0),
        run=lambda e, token: resumed.append((e.id, token)),
    )

    assert count == 2
    latest = {e.id: e for e in ledger.unfinished()}
    assert sorted(resumed) == sorted(
        (eid, resume_token(latest[eid])) for eid in (pending.id, failed.id)
    )


@pytest.mark.parametrize(
    ("key_suffix", "state", "resumed"),
    [
        ("", "queued", 0),
        (":123", "running", 0),
        (":123", "claimed", 0),
        ("", "dead", 1),
        ("", "failed", 1),
        ("x", "queued", 1),  # another erasure's key that merely starts the same way
    ],
)
def test_an_erasure_with_a_live_job_is_left_to_it(
    seeded: Seeded, key_suffix: str, state: str, resumed: int
) -> None:
    """Review M1: the sweep does not start a second run beside a job that will run it."""
    from _erase_seed import row  # noqa: PLC0415 - one helper, one test

    erasure = _claim(seeded)
    with seeded.engine.begin() as conn:
        row(
            conn,
            "jobs",
            id="erase_job",
            type=PERSONA_ERASE_JOB_TYPE,
            owner_id=OWNER,
            payload={"erasure_id": erasure.id, "persona_id": PERSONA},
            idempotency_key=f"erase:{erasure.id}{key_suffix}",
            state=state,
        )
    ran: list[str] = []

    count = resume_persona_erasures(
        engine=seeded.engine, grace=timedelta(0), run=lambda e, _t: ran.append(e.id)
    )

    assert count == resumed
    assert ran == [erasure.id] * resumed


@pytest.mark.parametrize(("code", "resumed"), [("not_the_owners_persona", 0), ("row_failed", 1)])
def test_a_permanent_refusal_is_left_to_the_alert(seeded: Seeded, code: str, resumed: int) -> None:
    """Review L2: re-enqueueing a refusal every 30 seconds would run forever."""
    erasure = _claim(seeded)
    ErasureLedger(seeded.engine).fail(erasure.id, code)

    count = resume_persona_erasures(
        engine=seeded.engine, grace=timedelta(0), run=lambda _e, _t: None
    )

    assert count == resumed


def test_rows_that_keep_appearing_are_retried_not_refused(
    seeded: Seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review L2: the row lock race is one the next attempt can win."""
    from _erase_seed import row  # noqa: PLC0415 - one helper, one test

    erasure = _claim(seeded)
    real_add = ErasureLedger.add_captured
    started = iter(range(10))

    def _another_starts(self: ErasureLedger, erasure_id: str, name: str, ids: list[str]) -> None:
        real_add(self, erasure_id, name, ids)
        with seeded.engine.begin() as conn:  # a new conversation every time one is recorded
            row(
                conn,
                "conversations",
                id=f"convrace{next(started)}",
                owner_id=OWNER,
                persona_id=PERSONA,
                origin="chat",
            )

    _another_starts(ErasureLedger(seeded.engine), erasure.id, "conversations", [])
    monkeypatch.setattr(ErasureLedger, "add_captured", _another_starts)

    with pytest.raises(erasure_service.ErasureRaceError):
        asyncio.run(_handler(seeded).handle(_payload(erasure), _Context(OWNER)))  # type: ignore[arg-type]

    failed = ErasureLedger(seeded.engine).get(SubjectKind.PERSONA, PERSONA)
    assert failed is not None
    assert failed.error_code == "row_failed"
    assert failed.error_code not in erasure_service.PERMANENT_REFUSALS


def test_an_erasure_changed_within_the_grace_is_left_to_its_route(seeded: Seeded) -> None:
    """Review L4: the route that just claimed it may still be fencing or deleting the row."""
    _claim(seeded)
    ran: list[str] = []

    fresh = resume_persona_erasures(engine=seeded.engine, run=lambda e, _t: ran.append(e.id))
    settled = resume_persona_erasures(
        engine=seeded.engine, grace=timedelta(0), run=lambda e, _t: ran.append(e.id)
    )

    assert (fresh, settled) == (0, 1)


def test_a_route_finding_the_erasure_locked_leaves_the_row_to_the_holder(seeded: Seeded) -> None:
    """Review L4: the route fences and deletes only while it holds the erasure's lock."""
    from persona_api.services.erasure_lock import erasure_lock  # noqa: PLC0415
    from persona_api.services.erasure_start import begin_persona_erasure  # noqa: PLC0415

    with seeded.engine.connect() as conn:
        captured = capture_persona(conn, owner_id=OWNER, persona_id=PERSONA)
    held = ErasureLedger(seeded.engine).claim(
        owner_id=OWNER, subject_kind=SubjectKind.PERSONA, subject_id=PERSONA, captured=captured
    )
    with erasure_lock(seeded.engine, held.id) as mine:
        assert mine
        erasure = begin_persona_erasure(
            rls_engine=seeded.engine,
            platform_engine=seeded.engine,
            owner_id=OWNER,
            persona_id=PERSONA,
        )

    assert erasure.id == held.id
    again = ErasureLedger(seeded.engine).get(SubjectKind.PERSONA, PERSONA)
    assert again is not None
    assert again.counts == {}, "the route recorded nothing while another run held the lock"

"""Erasing a persona on Postgres: ``memory_chunks``, the cascade and the owner guard (R9-284).

The unit tests (tests/unit/services/test_erase_persona.py) pin every step on the community
engine with Chroma. This is the same erasure on a migrated Postgres, where memory is one table
with no foreign key, documents are keyed by conversation, and migration 062's guard refuses a
memory row whose owner is gone. Runs in CI.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import pytest
from _erase_seed import (
    OWNER,
    PERSONA,
    SIBLING,
    STRANGER,
    Seeded,
    chunk,
    conversation_of,
    ids_naming,
    seed_into,
)
from persona.errors import MemoryOwnerMissingError
from persona.stores.postgres import PostgresBackend
from persona_api.services import erasure_service
from persona_api.services.erasure_ledger import ErasureLedger, ErasureState, SubjectKind
from persona_api.services.erasure_service import PersonaEraseDeps, capture_persona, erase_persona
from sqlalchemy import text

if TYPE_CHECKING:
    from pathlib import Path

    from persona_api.services.erasure_ledger import Erasure
    from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

_TABLES = (
    "personas",
    "conversations",
    "messages",
    "tasks",
    "runs",
    "schedules",
    "jobs",
    "jobs_archive",
    "notifications",
    "initiative_notices",
    "tool_audit_events",
)


@pytest.fixture
def seeded(migrated_engine: Engine, embedder: object, tmp_path: Path) -> Seeded:
    memory = PostgresBackend(engine=migrated_engine, embedder=embedder)  # type: ignore[arg-type]
    return seed_into(migrated_engine, memory, tmp_path)


def _memory_of(engine: Engine, pid: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                text("SELECT count(*) FROM memory_chunks WHERE persona_id IN (:p, :c)"),
                {"p": pid, "c": conversation_of(pid)},
            ).scalar_one()
        )


def _rows_of(engine: Engine, pid: str) -> dict[str, list[str]]:
    found = {t: ids_naming(engine, t, pid[-6:]) for t in _TABLES}
    return {t: ids for t, ids in found.items() if ids}


def _erase(seeded: Seeded) -> None:
    with seeded.engine.connect() as conn:
        captured = capture_persona(conn, owner_id=OWNER, persona_id=PERSONA)
    erasure = ErasureLedger(seeded.engine).claim(
        owner_id=OWNER, subject_kind=SubjectKind.PERSONA, subject_id=PERSONA, captured=captured
    )
    erase_persona(
        PersonaEraseDeps(
            engine=seeded.engine,
            memory_backend=seeded.memory,
            audit_logger=seeded.audit,
            file_storage=seeded.storage,
            workspace_root=seeded.workspace_root,
            audit_root=seeded.audit_root,
        ),
        erasure,
    )


def test_nothing_of_the_persona_remains_on_postgres_and_its_neighbours_keep_all(
    seeded: Seeded,
) -> None:
    sibling = (_memory_of(seeded.engine, SIBLING), _rows_of(seeded.engine, SIBLING))
    stranger = (_memory_of(seeded.engine, STRANGER), _rows_of(seeded.engine, STRANGER))
    assert _memory_of(seeded.engine, PERSONA) == 7

    _erase(seeded)

    assert _memory_of(seeded.engine, PERSONA) == 0
    assert _rows_of(seeded.engine, PERSONA) == {}
    assert (_memory_of(seeded.engine, SIBLING), _rows_of(seeded.engine, SIBLING)) == sibling
    assert (_memory_of(seeded.engine, STRANGER), _rows_of(seeded.engine, STRANGER)) == stranger
    done = ErasureLedger(seeded.engine).get(SubjectKind.PERSONA, PERSONA)
    assert done is not None
    assert done.state is ErasureState.COMPLETE
    assert sum(done.counts["memory"].values()) == 7


def test_after_the_erasure_the_guard_refuses_new_memory_for_the_persona(seeded: Seeded) -> None:
    _erase(seeded)

    with pytest.raises(MemoryOwnerMissingError):
        seeded.memory.upsert(persona_id=PERSONA, store_kind="episodic", chunks=[chunk("late")])
    assert _memory_of(seeded.engine, PERSONA) == 0


def test_two_runs_at_once_hold_one_advisory_lock_and_count_once(
    seeded: Seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review M1 on Postgres: two sessions, one session advisory lock, one run does the work."""
    with seeded.engine.connect() as conn:
        captured = capture_persona(conn, owner_id=OWNER, persona_id=PERSONA)
    erasure = ErasureLedger(seeded.engine).claim(
        owner_id=OWNER, subject_kind=SubjectKind.PERSONA, subject_id=PERSONA, captured=captured
    )
    deps = PersonaEraseDeps(
        engine=seeded.engine,
        memory_backend=seeded.memory,
        audit_logger=seeded.audit,
        file_storage=seeded.storage,
        workspace_root=seeded.workspace_root,
        audit_root=seeded.audit_root,
    )
    first_inside, second_done = threading.Event(), threading.Event()
    steps = dict(erasure_service.STEPS)
    fence = steps["fence"]

    def held_open(d: PersonaEraseDeps, e: Erasure) -> dict[str, int]:
        if not first_inside.is_set():
            first_inside.set()
            second_done.wait(timeout=60)
        return fence(d, e)

    steps["fence"] = held_open
    monkeypatch.setattr(erasure_service, "STEPS", tuple(steps.items()))
    errors: list[BaseException] = []

    def first() -> None:
        try:
            erase_persona(deps, erasure)
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            errors.append(exc)

    thread = threading.Thread(target=first)
    thread.start()
    assert first_inside.wait(timeout=60)
    try:
        erase_persona(deps, erasure)
    finally:
        second_done.set()
        thread.join(timeout=120)

    assert errors == []
    done = ErasureLedger(seeded.engine).get(SubjectKind.PERSONA, PERSONA)
    assert done is not None
    assert done.state is ErasureState.COMPLETE
    assert sum(done.counts["memory"].values()) == 7
    with seeded.engine.connect() as conn:
        audited = conn.execute(
            text("SELECT count(*) FROM audit_log WHERE action = 'persona.erase' AND target = :p"),
            {"p": PERSONA},
        ).scalar_one()
    assert audited == 1

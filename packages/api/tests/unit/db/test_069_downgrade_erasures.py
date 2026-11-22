"""Migration 069's downgrade: unfinished creates go, each with a persona erasure (R9-322).

Runs the downgrade's own steps on the community engine (the same SQL as Postgres for what they
do; the full round trip is pinned on Postgres in test_migration_069_persona_create_complete.py).
The erasure holds what ``capture_persona`` would have captured (PR #56 review LOW-1), and the
delete takes only the creates the downgrade read (review LOW-2).
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from _erase_seed import SeedTemplates
from persona_api.db.models import (
    conversations,
    event_triggers,
    personas,
    runs,
    schedules,
    tasks,
    users,
)
from persona_api.services.erasure_ledger import ErasureLedger, ErasureState, SubjectKind
from persona_api.services.erasure_service import capture_persona
from sqlalchemy import insert, select, update
from sqlalchemy.engine import Connection, Engine

_MIGRATION = (
    Path(__file__).resolve().parents[3] / "alembic" / "versions" / "069_persona_create_complete.py"
)
_OWNER = "user_069"
_HALF = "persona_" + "1" * 32
_DONE = "persona_" + "2" * 32
_ALREADY = "persona_" + "3" * 32
_NOT_MINTED = "p_half"
_LATE = "persona_" + "5" * 32
_FINISHED_MEANWHILE = "persona_" + "6" * 32


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("persona_create_complete_069", _MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def engine(tmp_path: Path, erase_templates: SeedTemplates) -> Iterator[Engine]:
    eng = erase_templates.community_engine(tmp_path)
    with eng.begin() as conn:
        conn.execute(insert(users).values(id=_OWNER, email="u069@x.test"))
    yield eng
    eng.dispose()


def _persona(conn: Connection, persona_id: str, *, complete: bool) -> None:
    conn.execute(
        insert(personas).values(id=persona_id, owner_id=_OWNER, yaml="y", create_complete=complete)
    )


def _schedule(conn: Connection, schedule_id: str, template: object) -> None:
    conn.execute(
        insert(schedules).values(
            id=schedule_id,
            owner_id=_OWNER,
            timezone="UTC",
            target_job_type="chat_turn",
            payload_template=template,
            one_time_at=datetime(2030, 1, 1, tzinfo=UTC),
        )
    )


def _everything_a_capture_reads(conn: Connection, persona_id: str) -> None:
    """One of each thing ``capture_persona`` captures, and an owner's schedule it must not."""
    conn.execute(
        insert(conversations).values(id="conv_half", owner_id=_OWNER, persona_id=persona_id)
    )
    _schedule(conn, "sched_task", {})
    _schedule(conn, f"initsched:{persona_id}", {})
    _schedule(conn, "sched_names_conv", {"args": {"conversation_id": "conv_half"}})
    _schedule(conn, "sched_unrelated", {"conversation_id": "conv_elsewhere"})
    conn.execute(
        insert(tasks).values(
            id="task_half",
            owner_id=_OWNER,
            persona_id=persona_id,
            contract_json={},
            schedule_id="sched_task",
            run_ids=["run_named_by_task"],
        )
    )
    conn.execute(
        insert(runs).values(id="run_half", owner_id=_OWNER, persona_id=persona_id, task="t")
    )
    conn.execute(
        insert(event_triggers).values(
            id="trigger_half",
            owner_id=_OWNER,
            persona_id=persona_id,
            event_kind="email",
            filter={},
            action={},
        )
    )


def test_the_capture_is_what_capture_persona_captures(engine: Engine) -> None:
    """Review LOW-1: all five keys, task schedules, the initiative scan, schedules whose template
    names the persona's ids, and runs a task names."""
    with engine.begin() as conn:
        _persona(conn, _HALF, complete=False)
        _everything_a_capture_reads(conn, _HALF)

    with engine.connect() as conn:
        mirrored = _migration().capture(conn, _OWNER, _HALF)
        service = capture_persona(conn, owner_id=_OWNER, persona_id=_HALF)

    assert mirrored == service
    assert mirrored == {
        "conversations": ["conv_half"],
        "tasks": ["task_half"],
        "runs": ["run_half", "run_named_by_task"],
        "schedules": sorted([f"initsched:{_HALF}", "sched_names_conv", "sched_task"]),
        "event_triggers": ["trigger_half"],
    }


def test_069_downgrade_opens_one_erasure_per_unfinished_create(engine: Engine) -> None:
    """Each unfinished, minted persona is deleted and gets an open erasure holding its ids; a
    complete one is kept, one with an erasure already open keeps it, and an id the erasure
    would refuse is deleted with none."""
    ledger = ErasureLedger(engine)
    with engine.begin() as conn:
        for pid, complete in (
            (_HALF, False),
            (_DONE, True),
            (_ALREADY, False),
            (_NOT_MINTED, False),
        ):
            _persona(conn, pid, complete=complete)
        _everything_a_capture_reads(conn, _HALF)
    with engine.connect() as conn:
        expected = capture_persona(conn, owner_id=_OWNER, persona_id=_HALF)
    already = ledger.claim(
        owner_id=_OWNER, subject_kind=SubjectKind.PERSONA, subject_id=_ALREADY, captured={}
    )

    with engine.begin() as conn:
        deleted = _migration().erase_unfinished_creates(conn)

    assert deleted == sorted([_HALF, _ALREADY, _NOT_MINTED])
    with engine.connect() as conn:
        left = set(conn.execute(select(personas.c.id)).scalars())
    assert left == {_DONE}
    half = ledger.get(SubjectKind.PERSONA, _HALF)
    assert half is not None
    assert half.owner_id == _OWNER
    assert half.state is ErasureState.PENDING, "the sweep resumes it"
    assert half.captured == expected, "read before the cascade took the rows"
    assert ledger.get(SubjectKind.PERSONA, _DONE) is None, "a complete persona stays"
    assert ledger.get(SubjectKind.PERSONA, _ALREADY) == already, "its open erasure is kept"
    assert ledger.get(SubjectKind.PERSONA, _NOT_MINTED) is None


def test_the_delete_takes_only_the_creates_it_read(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review LOW-2: between the read and the delete a new create starts and another finishes.
    The new one is not deleted (its own completion or undo settles it), the finished one is
    kept, and neither gets an erasure."""
    migration = _migration()
    with engine.begin() as conn:
        _persona(conn, _HALF, complete=False)
        _persona(conn, _FINISHED_MEANWHILE, complete=False)
    read = migration.unfinished_creates

    def _meanwhile(conn: Connection) -> dict[str, Any]:
        creates = read(conn)
        with engine.begin() as other:  # other sessions, after the read
            _persona(other, _LATE, complete=False)
            other.execute(
                update(personas)
                .where(personas.c.id == _FINISHED_MEANWHILE)
                .values(create_complete=True)
            )
        return creates

    monkeypatch.setattr(migration, "unfinished_creates", _meanwhile)

    with engine.begin() as conn:
        deleted = migration.erase_unfinished_creates(conn)

    assert deleted == [_HALF]
    with engine.connect() as conn:
        left = {
            str(r.id): bool(r.create_complete)
            for r in conn.execute(select(personas.c.id, personas.c.create_complete))
        }
    assert left == {_LATE: False, _FINISHED_MEANWHILE: True}
    ledger = ErasureLedger(engine)
    assert ledger.get(SubjectKind.PERSONA, _HALF) is not None
    assert ledger.get(SubjectKind.PERSONA, _LATE) is None, "still being created"
    assert ledger.get(SubjectKind.PERSONA, _FINISHED_MEANWHILE) is None, "a persona now"


def test_the_delete_names_the_ids_it_was_given(engine: Engine) -> None:
    """Review LOW-2: the delete is by id, not by the unfinished predicate alone."""
    with engine.begin() as conn:
        for pid in (_HALF, _LATE):
            _persona(conn, pid, complete=False)

    with engine.begin() as conn:
        deleted = _migration().delete_creates(conn, [_HALF])
        nothing = _migration().delete_creates(conn, [])

    assert deleted == [_HALF]
    assert nothing == []
    with engine.connect() as conn:
        assert set(conn.execute(select(personas.c.id)).scalars()) == {_LATE}

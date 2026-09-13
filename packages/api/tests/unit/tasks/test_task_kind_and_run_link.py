"""A task knows its kind and a run names its task, on the community engine (Spec W1, T1).

Drives the REAL ``TaskStore`` + ``services.run_record`` against a real database (the
community SQLite engine: real FK, CHECK and default enforcement) and asserts the durable
shape, not a mocked mapping:

- ``kind`` round-trips through the store (``ad_hoc`` in, ``ad_hoc`` out) and defaults to
  ``standing`` for a task that never says (every pre-W1 row reads standing).
- ``runs.task_id`` links a run to its task, and deleting the task leaves the run row in
  place with ``task_id`` cleared (``ON DELETE SET NULL``), so a run stays viewable.
- A run inserted without a task keeps ``task_id NULL`` (the legacy archive shape,
  D-W1-15).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona.tasks import Contract, Task, TaskKind
from persona_api.db.community import (
    create_community_schema,
    ensure_owner,
    make_community_engine,
)
from persona_api.db.models import personas as personas_t
from persona_api.db.models import runs as runs_t
from persona_api.db.models import tasks as tasks_t
from persona_api.services import run_record
from persona_api.tasks import TaskStore
from sqlalchemy import delete, insert, select

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy import Engine

_NOW = datetime(2026, 9, 6, 9, 0, tzinfo=UTC)
_OWNER = "user_w1"
_PERSONA = "persona_w1"


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = make_community_engine(tmp_path / "w1.db")
    create_community_schema(eng)
    ensure_owner(eng, owner_id=_OWNER, email="w1@example.com")
    with eng.begin() as conn:
        conn.execute(insert(personas_t).values(id=_PERSONA, owner_id=_OWNER, yaml="name: x"))
    yield eng
    eng.dispose()


def _task(task_id: str, **overrides: object) -> Task:
    base: dict[str, object] = {
        "id": task_id,
        "owner_id": _OWNER,
        "persona_id": _PERSONA,
        "contract": Contract(goal="list the three newest AI persona repos"),
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    base.update(overrides)
    return Task(**base)  # type: ignore[arg-type]


# --- kind ----------------------------------------------------------------------


def test_ad_hoc_kind_round_trips_through_the_store(engine: Engine) -> None:
    store = TaskStore(engine)
    store.create(_task("t-adhoc", kind=TaskKind.AD_HOC))
    assert store.get(_OWNER, "t-adhoc").kind is TaskKind.AD_HOC


def test_a_task_that_never_says_is_standing(engine: Engine) -> None:
    store = TaskStore(engine)
    store.create(_task("t-standing"))
    assert store.get(_OWNER, "t-standing").kind is TaskKind.STANDING
    # The durable default agrees with the entity default (a pre-W1 row reads standing).
    with engine.begin() as conn:
        stored = conn.execute(
            select(tasks_t.c.kind).where(tasks_t.c.id == "t-standing")
        ).scalar_one()
    assert stored == "standing"


def test_the_list_carries_both_kinds(engine: Engine) -> None:
    store = TaskStore(engine)
    store.create(_task("t-1", kind=TaskKind.AD_HOC))
    store.create(_task("t-2"))
    kinds = {t.id: t.kind for t in store.list_for_owner(_OWNER)}
    assert kinds == {"t-1": TaskKind.AD_HOC, "t-2": TaskKind.STANDING}


# --- runs.task_id ------------------------------------------------------------------


def _run_row(engine: Engine, run_id: str) -> dict[str, object]:
    with engine.begin() as conn:
        row = conn.execute(select(runs_t).where(runs_t.c.id == run_id)).mappings().one()
    return dict(row)


def test_a_run_names_its_task(engine: Engine) -> None:
    TaskStore(engine).create(_task("t-linked"))
    run_record.insert_run(
        engine,
        run_id="run-1",
        owner_id=_OWNER,
        persona_id=_PERSONA,
        task="list the three newest AI persona repos",
        started_at=_NOW,
        task_id="t-linked",
    )
    assert _run_row(engine, "run-1")["task_id"] == "t-linked"


def test_deleting_the_task_keeps_the_run_viewable_with_task_id_cleared(engine: Engine) -> None:
    TaskStore(engine).create(_task("t-gone"))
    run_record.insert_run(
        engine,
        run_id="run-2",
        owner_id=_OWNER,
        persona_id=_PERSONA,
        task="goal",
        started_at=_NOW,
        task_id="t-gone",
    )
    with engine.begin() as conn:
        conn.execute(delete(tasks_t).where(tasks_t.c.id == "t-gone"))
    row = _run_row(engine, "run-2")
    assert row["task_id"] is None  # SET NULL, not CASCADE: the run row survives
    assert row["status"] == "running"


def test_a_run_without_a_task_is_the_legacy_archive_shape(engine: Engine) -> None:
    run_record.insert_run(
        engine,
        run_id="run-legacy",
        owner_id=_OWNER,
        persona_id=_PERSONA,
        task="an old bare run",
        started_at=_NOW,
    )
    assert _run_row(engine, "run-legacy")["task_id"] is None


def test_a_run_cannot_name_a_task_that_does_not_exist(engine: Engine) -> None:
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            insert(runs_t).values(
                id="run-orphan",
                owner_id=_OWNER,
                persona_id=_PERSONA,
                task="goal",
                task_id="no-such-task",
                started_at=_NOW,
            )
        )


# --- the task's run history (Spec W1, T3; D-W1-3) ---------------------------------


def test_a_tasks_runs_are_listed_newest_first_with_their_task_id(engine: Engine) -> None:
    from datetime import timedelta

    from persona_api.services import run_service

    TaskStore(engine).create(_task("t-hist"))
    TaskStore(engine).create(_task("t-other"))
    for run_id, task_id, offset in (
        ("run-a", "t-hist", 0),
        ("run-b", "t-hist", 60),
        ("run-c", "t-other", 30),
    ):
        run_record.insert_run(
            engine,
            run_id=run_id,
            owner_id=_OWNER,
            persona_id=_PERSONA,
            task="goal",
            started_at=_NOW + timedelta(seconds=offset),
            task_id=task_id,
        )
    rows = run_service.list_runs_for_task(rls_engine=engine, task_id="t-hist")
    assert [r["id"] for r in rows] == ["run-b", "run-a"]  # newest first, the other task excluded
    summaries = [run_service.summarise_run(r) for r in rows]
    assert {s.task_id for s in summaries} == {"t-hist"}
    assert summaries[0].status == "running"
    assert "steps" not in rows[0]  # the light projection: the viewer loads the steps


def test_a_legacy_run_summarises_with_no_task(engine: Engine) -> None:
    from persona_api.services import run_service

    run_record.insert_run(
        engine, run_id="run-old", owner_id=_OWNER, persona_id=_PERSONA, task="old", started_at=_NOW
    )
    rows = run_service.list_runs(rls_engine=engine)
    assert run_service.summarise_run(rows[0]).task_id is None

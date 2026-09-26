"""The budget's two writing decisions really serialise on the task row (R9-158 re-review).

The extension and the pause at the cap each decide from a read of the cap, then write. On
the community engine SQLite renders no ``FOR UPDATE`` and its driver begins a transaction
only at the first write, so without ``BEGIN IMMEDIATE`` the read was unlocked and the
at-most-once property was incidental. These hold the database's write lock in one
connection while the decision runs in another thread, commit a competing extension, and
assert the durable outcome: exactly one extension row, and no task left paused below its
cap. (The Postgres version, with ``SELECT ... FOR UPDATE``, is
``tests/integration/test_budget_row_lock.py``.)
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona.tasks import Contract, ContractBounds, CostLedger, Task, TaskState
from persona_api.approvals.budget import BudgetEnforcer, ExtensionOutcome
from persona_api.db.community import create_community_schema, ensure_owner, make_community_engine
from persona_api.db.models import audit_log as audit_log_t
from persona_api.db.models import personas as personas_t
from persona_api.jobs.queue import JobQueue
from persona_api.tasks.store import TaskStore
from sqlalchemy import func, insert, select

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from sqlalchemy import Engine

_NOW = datetime(2026, 9, 26, 9, 0, tzinfo=UTC)
_OWNER = "user_row_lock"
_PERSONA = "persona_row_lock"
_CAP = 50_000
#: Long enough for the decision thread to reach its read before the holder commits. The
#: assertion is on the durable outcome, never on this wait: a slow thread only makes the
#: test pass trivially, it cannot make it fail.
_HOLD_SECONDS = 0.5


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "row_lock.db"
    engine = make_community_engine(path)
    create_community_schema(engine)
    ensure_owner(engine, owner_id=_OWNER, email="l@example.com")
    with engine.begin() as conn:
        conn.execute(insert(personas_t).values(id=_PERSONA, owner_id=_OWNER, yaml="name: x"))
    TaskStore(engine).create(
        Task(
            id="t1",
            owner_id=_OWNER,
            persona_id=_PERSONA,
            contract=Contract(goal="g", bounds=ContractBounds(total_budget_micros=_CAP)),
            state=TaskState.ACTIVE,
            ledger=CostLedger(model_micros=_CAP),  # exactly at the cap
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    engine.dispose()
    return path


def _enforcer(engine: Engine) -> BudgetEnforcer:
    return BudgetEnforcer(engine=engine, tasks=TaskStore(engine), queue=JobQueue(engine))


def _extensions(engine: Engine) -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                select(func.count())
                .select_from(audit_log_t)
                .where(audit_log_t.c.action == "budget.extended")
            ).scalar_one()
        )


def _race(db_path: Path, decision: Callable[[Engine], object]) -> object:
    """Run ``decision`` in a thread while another connection holds the write lock and
    commits a competing extension; return what the decision returned."""
    holder_engine = make_community_engine(db_path)
    started = threading.Event()
    result: dict[str, object] = {}

    def _run() -> None:
        engine = make_community_engine(db_path)  # its own connections, in its own thread
        try:
            started.set()
            result["value"] = decision(engine)
        except Exception as exc:  # noqa: BLE001 - surfaced by the assertion below
            result["error"] = exc
        finally:
            engine.dispose()

    with holder_engine.connect() as holder:
        holder.exec_driver_sql("BEGIN IMMEDIATE")
        holder.execute(
            insert(audit_log_t).values(
                id=f"audit_{uuid.uuid4().hex}",
                user_id=_OWNER,
                action="budget.extended",
                target="t1",
                metadata={"amount_micros": "10000", "cap_micros": str(_CAP)},
            )
        )
        thread = threading.Thread(target=_run)
        thread.start()
        started.wait(5)
        time.sleep(_HOLD_SECONDS)
        holder.commit()
    thread.join(30)
    holder_engine.dispose()
    assert "error" not in result, result.get("error")
    return result["value"]


def test_an_extension_racing_another_waits_for_it_and_extends_once(db_path: Path) -> None:
    outcome = _race(
        db_path, lambda engine: _enforcer(engine).extend(_OWNER, "t1", 10_000, now=_NOW).outcome
    )
    engine = make_community_engine(db_path)
    try:
        assert (outcome, _extensions(engine)) == (ExtensionOutcome.NOT_AT_CAP, 1)
    finally:
        engine.dispose()


def test_a_pause_racing_an_extension_does_not_leave_the_task_paused_below_its_cap(
    db_path: Path,
) -> None:
    def _pause(engine: Engine) -> bool:
        stale = TaskStore(engine).get(_OWNER, "t1")  # read before the extension committed
        return _enforcer(engine).enforce(_OWNER, stale, now=_NOW)

    halted = _race(db_path, _pause)
    engine = make_community_engine(db_path)
    try:
        task = TaskStore(engine).get(_OWNER, "t1")
        assert (halted, task.paused, _extensions(engine)) == (False, False, 1)
    finally:
        engine.dispose()


def test_another_owner_cannot_take_the_lock_on_a_task_that_is_not_theirs(db_path: Path) -> None:
    engine = make_community_engine(db_path)
    try:
        ensure_owner(engine, owner_id="someone_else", email="s@example.com")
        extension = _enforcer(engine).extend("someone_else", "t1", 10_000, now=_NOW)
        assert (extension.outcome, _extensions(engine)) == (ExtensionOutcome.NOT_AT_CAP, 0)
    finally:
        engine.dispose()

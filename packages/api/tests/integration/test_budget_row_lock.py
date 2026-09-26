"""The budget's two writing decisions serialise on the task row, on Postgres (R9-158).

The extension and the pause at the cap each read the cap, then write. Both run inside
``SELECT ... FOR UPDATE`` on the task row (``BudgetEnforcer._row_lock``). These hold that
row lock in a second connection while a decision runs in a thread, commit a competing
extension, and assert the durable outcome: exactly one extension row, and no task left
paused below its cap. The SQLite version (``BEGIN IMMEDIATE``) is
``tests/unit/approvals/test_budget_gate_under_the_row_lock.py``.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime

import pytest
from persona.tasks import Contract, ContractBounds, Task
from persona_api.approvals.budget import BudgetEnforcer, ExtensionOutcome
from persona_api.tasks.store import TaskStore
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
_OWNER = "user_row_lock"
_CAP = 1000
#: Long enough for the decision thread to reach its lock before the holder commits. The
#: assertion is on the durable outcome: a slow thread makes the test pass trivially, never
#: fail.
_HOLD_SECONDS = 0.5


class _FakeQueue:
    def __init__(self) -> None:
        self.enqueued: list[dict[str, object]] = []

    def enqueue(self, **kwargs: object) -> None:
        self.enqueued.append(kwargs)

    def count_spent_attempts(self, *, owner_id: str, idempotency_key: str) -> int:  # noqa: ARG002
        return 0


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (non-superuser role) not set; skipping row-lock test")
    engine = create_engine(app_url.replace("+asyncpg", "+psycopg"))
    with migrated_engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES (:u, 'l@x')"), {"u": _OWNER})
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES ('p', :u, 'name: x')"),
            {"u": _OWNER},
        )
    tasks = TaskStore(engine)
    tasks.create(
        Task(
            id="t1",
            owner_id=_OWNER,
            persona_id="p",
            contract=Contract(goal="g", bounds=ContractBounds(total_budget_micros=_CAP)),
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    tasks.start(_OWNER, "t1", now=_NOW)
    with migrated_engine.begin() as conn:
        conn.execute(text("UPDATE tasks SET ledger_model_micros = :s WHERE id = 't1'"), {"s": _CAP})
    yield engine
    engine.dispose()


def _extensions(engine: Engine) -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                text("SELECT count(*) FROM audit_log WHERE action = 'budget.extended'")
            ).scalar_one()
        )


def _race(su: Engine, decision: Callable[[], object]) -> object:
    """Hold the task row lock and commit a competing extension while ``decision`` runs."""
    started = threading.Event()
    result: dict[str, object] = {}

    def _run() -> None:
        started.set()
        try:
            result["value"] = decision()
        except Exception as exc:  # noqa: BLE001 - surfaced by the assertion below
            result["error"] = exc

    with su.connect() as holder:
        holder.execute(text("SELECT id FROM tasks WHERE id = 't1' FOR UPDATE"))
        holder.execute(
            text(
                "INSERT INTO audit_log (id, user_id, action, target, metadata) "
                "VALUES ('audit_race', :u, 'budget.extended', 't1', "
                '\'{"amount_micros": "500"}\'::jsonb)'
            ),
            {"u": _OWNER},
        )
        thread = threading.Thread(target=_run)
        thread.start()
        started.wait(5)
        time.sleep(_HOLD_SECONDS)
        holder.commit()
    thread.join(30)
    assert "error" not in result, result.get("error")
    return result["value"]


def test_an_extension_racing_another_extends_once(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    enforcer = BudgetEnforcer(engine=app_engine, tasks=TaskStore(app_engine), queue=_FakeQueue())  # type: ignore[arg-type]
    outcome = _race(migrated_engine, lambda: enforcer.extend(_OWNER, "t1", 500, now=_NOW).outcome)
    assert (outcome, _extensions(migrated_engine)) == (ExtensionOutcome.NOT_AT_CAP, 1)


def test_a_pause_racing_an_extension_leaves_the_task_unpaused(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    tasks = TaskStore(app_engine)
    enforcer = BudgetEnforcer(engine=app_engine, tasks=tasks, queue=_FakeQueue())  # type: ignore[arg-type]
    stale = tasks.get(_OWNER, "t1")  # read before the extension commits: at the cap
    halted = _race(migrated_engine, lambda: enforcer.enforce(_OWNER, stale, now=_NOW))
    assert (halted, tasks.get(_OWNER, "t1").paused, _extensions(migrated_engine)) == (
        False,
        False,
        1,
    )

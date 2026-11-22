"""T3 (spec P1) — the startup restart sweep (D-P1-restart-sweep).

Single-worker (D-08-5): in-flight ``asyncio`` tasks die on a process restart, but
their DB rows stay ``running``/``streaming`` forever — a reattach then 404s and
the UI spins. On API startup, BEFORE serving, we reconcile every orphaned
in-flight row to a terminal state (``interrupted`` for chat turns; ``error`` for
runs) so "viewable, not resumable" (S08-2) is honest. Idempotent: a second pass
finds nothing. Runs on the RLS-bypassing engine (admin for cloud / the community
engine for sqlite) so it sees every tenant's rows.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona import logging as plog
from persona.concurrency import PROCESS_BOOT_ID
from persona.config import PersonaCoreConfig
from persona_api.background.restart_sweep import reconcile_in_flight_on_startup
from persona_api.db.community import (
    create_community_schema,
    ensure_owner,
    make_community_engine,
)
from persona_api.db.models import conversations as conversations_t
from persona_api.db.models import inflight_ops as inflight_ops_t
from persona_api.db.models import messages as messages_t
from persona_api.db.models import personas as personas_t
from persona_api.db.models import runs as runs_t
from persona_api.db.models import tasks as tasks_t
from sqlalchemy import insert, select

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine

_OWNER = "user_alice"
_PERSONA = "astrid"
_CONV = "conv_1"


@pytest.fixture
def engine(tmp_path: object) -> Iterator[Engine]:
    eng = make_community_engine(tmp_path / "t.db")  # type: ignore[operator]
    create_community_schema(eng)
    ensure_owner(eng, owner_id=_OWNER, email="a@example.com")
    with eng.begin() as conn:
        conn.execute(insert(personas_t).values(id=_PERSONA, owner_id=_OWNER, yaml="name: Astrid"))
        conn.execute(insert(conversations_t).values(id=_CONV, owner_id=_OWNER, persona_id=_PERSONA))
    return eng


def _msg(conn: object, mid: str, status: str | None) -> None:
    conn.execute(  # type: ignore[attr-defined]
        insert(messages_t).values(
            id=mid, conversation_id=_CONV, role="assistant", content="x", streaming_status=status
        )
    )


def _run(conn: object, rid: str, status: str, *, task_id: str | None = None) -> None:
    conn.execute(  # type: ignore[attr-defined]
        insert(runs_t).values(
            id=rid,
            owner_id=_OWNER,
            persona_id=_PERSONA,
            task="t",
            task_id=task_id,
            status=status,
            started_at=datetime.now(UTC),
        )
    )


def _task(conn: object, tid: str) -> None:
    """A task for a run to belong to (Spec W1): the reason a parked run is answerable."""
    conn.execute(  # type: ignore[attr-defined]
        insert(tasks_t).values(
            id=tid,
            owner_id=_OWNER,
            persona_id=_PERSONA,
            contract_json={"goal": "book the dentist"},
            state="waiting",
            wait_kind="on_user",
        )
    )


def test_sweep_marks_orphaned_streaming_message_interrupted(engine: Engine) -> None:
    with engine.begin() as conn:
        _msg(conn, "m_running", "running")
        _msg(conn, "m_complete", "complete")
        _msg(conn, "m_legacy", None)

    counts = reconcile_in_flight_on_startup(engine=engine)

    with engine.begin() as conn:
        rows = {
            r["id"]: r["streaming_status"]
            for r in conn.execute(select(messages_t)).mappings().all()
        }
    assert rows["m_running"] == "interrupted"  # orphaned in-flight → interrupted
    assert rows["m_complete"] == "complete"  # terminal untouched
    assert rows["m_legacy"] is None  # legacy/non-streamed untouched
    assert counts["messages"] == 1


def test_sweep_marks_orphaned_runs_error_with_finished_at(engine: Engine) -> None:
    with engine.begin() as conn:
        _run(conn, "r_running", "running")
        _run(conn, "r_awaiting", "awaiting_user")
        _run(conn, "r_done", "completed")
        _run(conn, "r_error", "error")

    counts = reconcile_in_flight_on_startup(engine=engine)

    with engine.begin() as conn:
        rows = {r["id"]: dict(r) for r in conn.execute(select(runs_t)).mappings().all()}
    assert rows["r_running"]["status"] == "error"
    assert rows["r_running"]["finished_at"] is not None
    assert rows["r_running"]["error"]  # carries a reason
    # An awaiting-user run with NO task behind it is the old in-process shape: its response
    # queue died with the process, so it can never be answered and is still an orphan.
    assert rows["r_awaiting"]["status"] == "error"
    assert rows["r_done"]["status"] == "completed"  # terminal untouched
    assert rows["r_error"]["status"] == "error"  # already-terminal untouched
    assert counts["runs"] == 2


def test_sweep_is_idempotent(engine: Engine) -> None:
    with engine.begin() as conn:
        _msg(conn, "m_running", "running")
        _run(conn, "r_running", "running")

    first = reconcile_in_flight_on_startup(engine=engine)
    second = reconcile_in_flight_on_startup(engine=engine)

    assert first == {"messages": 1, "runs": 1, "long_ops": 0}
    assert second == {"messages": 0, "runs": 0, "long_ops": 0}  # a second pass finds nothing


def test_sweep_noop_on_clean_database(engine: Engine) -> None:
    assert reconcile_in_flight_on_startup(engine=engine) == {
        "messages": 0,
        "runs": 0,
        "long_ops": 0,
    }


def test_sweep_logs_a_warning_with_the_healed_count(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """R9-022: healing an orphan must never be silent — the sweep logs at
    WARNING (not INFO) with the per-table count, so an operator sees evidence
    of an unclean prior shutdown even though the rows themselves are now
    safely terminal."""
    plog.reset_for_testing()  # fresh sinks bound to THIS test's (capsys-redirected) stderr
    plog.get_logger("test.trigger", config=PersonaCoreConfig(log_format="pretty"))
    with engine.begin() as conn:
        _msg(conn, "m_running", "running")
        _run(conn, "r_running", "running")

    reconcile_in_flight_on_startup(engine=engine)

    plog.reset_for_testing()  # flush before reading (mirrors persona.logging's own tests)
    output = capsys.readouterr().err
    assert "WARNING" in output
    assert "turns_interrupted=1" in output
    assert "runs_errored=1" in output


def test_sweep_logs_nothing_on_a_clean_database(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """The converse: a clean pass (nothing to heal) stays silent — WARNING is
    reserved for evidence of an actual unclean shutdown, not routine boots."""
    plog.reset_for_testing()
    plog.get_logger("test.trigger", config=PersonaCoreConfig(log_format="pretty"))

    reconcile_in_flight_on_startup(engine=engine)

    plog.reset_for_testing()
    output = capsys.readouterr().err
    assert "restart sweep reconciled" not in output


def test_a_parked_run_that_belongs_to_a_task_survives_the_sweep(engine: Engine) -> None:
    """Spec W1 (D-W1-34): the sweep's premise is that an in-flight run's answer died with the
    process. A run parked on a question inside a TASK breaks that premise: the task sits
    waiting on the user and the answer comes back through the durable reply route, which no
    restart can lose. Reaping it would error a question the user is about to answer, and the
    task would be left waiting on a run that says it failed.
    """
    with engine.begin() as conn:
        _task(conn, "task_1")
        _run(conn, "r_parked_in_a_task", "awaiting_user", task_id="task_1")
        _run(conn, "r_parked_alone", "awaiting_user")
        _run(conn, "r_running_in_a_task", "running", task_id="task_1")

    counts = reconcile_in_flight_on_startup(engine=engine)

    with engine.begin() as conn:
        rows = {r["id"]: dict(r) for r in conn.execute(select(runs_t)).mappings().all()}
    assert rows["r_parked_in_a_task"]["status"] == "awaiting_user"  # spared: still answerable
    assert rows["r_parked_in_a_task"]["finished_at"] is None
    assert rows["r_parked_alone"]["status"] == "error"  # no task behind it: still an orphan
    # A task-owned run that was RUNNING is still an orphan: nothing is waiting for a person,
    # its loop simply died. Only the parked shape is answerable.
    assert rows["r_running_in_a_task"]["status"] == "error"
    assert counts["runs"] == 2


def test_the_boot_sweep_lifts_the_statement_timeout_before_its_scans() -> None:
    """R9-313: both UPDATEs can scan their whole table at boot. A cancel here would fail the
    lifespan and crash-loop the api, so the sweep's own transaction has no statement timeout."""
    from contextlib import contextmanager  # noqa: PLC0415

    sql: list[str] = []

    class _Result:
        rowcount = 0

    class _Conn:
        dialect = type("D", (), {"name": "postgresql"})()

        def execute(self, statement: object, _params: object = None) -> _Result:
            sql.append(str(statement))
            return _Result()

    class _Engine:
        @contextmanager
        def begin(self):  # type: ignore[no-untyped-def]  # noqa: ANN202
            yield _Conn()

    counts = reconcile_in_flight_on_startup(engine=_Engine())  # type: ignore[arg-type]
    assert counts == {"messages": 0, "runs": 0, "long_ops": 0}
    assert sql[0] == "SELECT set_config('statement_timeout', '0', true)"
    assert sql[1].startswith("UPDATE messages")
    assert sql[2].startswith("UPDATE runs")
    # R9-318: the long-op slot sweep shares the lifted transaction.
    assert sql[3].startswith("DELETE FROM inflight_ops")


# --- R9-318: long-op slots left by an earlier boot ------------------------------------------

_PREVIOUS_BOOT = "boot_of_the_previous_process"


def _slot(conn: object, sid: str, *, instance_id: str | None, boot_id: str | None) -> None:
    conn.execute(  # type: ignore[attr-defined]
        insert(inflight_ops_t).values(
            id=sid, user_id=_OWNER, op_class="chat", instance_id=instance_id, boot_id=boot_id
        )
    )


def _slot_ids(engine: Engine) -> set[str]:
    with engine.begin() as conn:
        return set(conn.execute(select(inflight_ops_t.c.id)).scalars().all())


def test_a_slot_left_by_a_previous_boot_is_released(engine: Engine) -> None:
    """R9-318: a turn abandoned at the shutdown deadline never reaches its ``finally``, so its
    slot outlives the process. Left alone it holds the user's cap for the hour-long TTL; the
    boot sweep frees it before the new process serves anyone."""
    with engine.begin() as conn:
        _slot(conn, "slot_previous", instance_id="api", boot_id=_PREVIOUS_BOOT)

    counts = reconcile_in_flight_on_startup(engine=engine, long_op_instance="api")

    assert _slot_ids(engine) == set()
    assert counts["long_ops"] == 1


def test_a_slot_written_before_boot_ids_existed_is_released(engine: Engine) -> None:
    """A row from the code before migration 070 carries no instance or boot id. It can only
    come from an earlier process, so the sweep frees it too."""
    with engine.begin() as conn:
        _slot(conn, "slot_legacy", instance_id=None, boot_id=None)

    counts = reconcile_in_flight_on_startup(engine=engine, long_op_instance="api")

    assert _slot_ids(engine) == set()
    assert counts["long_ops"] == 1


def test_a_slot_of_the_current_process_is_kept(engine: Engine) -> None:
    """A slot this very process took is a live op: the sweep must not free it, or the cap
    would admit one op too many."""
    with engine.begin() as conn:
        _slot(conn, "slot_live", instance_id="api", boot_id=PROCESS_BOOT_ID)

    counts = reconcile_in_flight_on_startup(engine=engine, long_op_instance="api")

    assert _slot_ids(engine) == {"slot_live"}
    assert counts["long_ops"] == 0


def test_a_slot_of_another_instance_is_kept(engine: Engine) -> None:
    """With a second api instance, a booting instance frees only its own earlier boots'
    slots. Another instance's slot may be a live op on that instance."""
    with engine.begin() as conn:
        _slot(conn, "slot_other_instance", instance_id="api-2", boot_id=_PREVIOUS_BOOT)
        _slot(conn, "slot_mine", instance_id="api-1", boot_id=_PREVIOUS_BOOT)

    counts = reconcile_in_flight_on_startup(engine=engine, long_op_instance="api-1")

    assert _slot_ids(engine) == {"slot_other_instance"}
    assert counts["long_ops"] == 1


def test_the_long_op_sweep_is_idempotent(engine: Engine) -> None:
    with engine.begin() as conn:
        _slot(conn, "slot_previous", instance_id="api", boot_id=_PREVIOUS_BOOT)
        _slot(conn, "slot_live", instance_id="api", boot_id=PROCESS_BOOT_ID)

    first = reconcile_in_flight_on_startup(engine=engine, long_op_instance="api")
    second = reconcile_in_flight_on_startup(engine=engine, long_op_instance="api")

    assert first["long_ops"] == 1
    assert second["long_ops"] == 0
    assert _slot_ids(engine) == {"slot_live"}


def test_the_sweep_logs_the_released_slots(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """Never silent: a released slot is evidence of an unclean shutdown, like an orphan turn."""
    plog.reset_for_testing()
    plog.get_logger("test.trigger", config=PersonaCoreConfig(log_format="pretty"))
    with engine.begin() as conn:
        _slot(conn, "slot_previous", instance_id="api", boot_id=_PREVIOUS_BOOT)

    reconcile_in_flight_on_startup(engine=engine, long_op_instance="api")

    plog.reset_for_testing()
    output = capsys.readouterr().err
    assert "WARNING" in output
    assert "long_ops_released=1" in output

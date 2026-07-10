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
from persona.config import PersonaCoreConfig
from persona_api.background.restart_sweep import reconcile_in_flight_on_startup
from persona_api.db.community import (
    create_community_schema,
    ensure_owner,
    make_community_engine,
)
from persona_api.db.models import conversations as conversations_t
from persona_api.db.models import messages as messages_t
from persona_api.db.models import personas as personas_t
from persona_api.db.models import runs as runs_t
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


def _run(conn: object, rid: str, status: str) -> None:
    conn.execute(  # type: ignore[attr-defined]
        insert(runs_t).values(
            id=rid,
            owner_id=_OWNER,
            persona_id=_PERSONA,
            task="t",
            status=status,
            started_at=datetime.now(UTC),
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
    assert rows["r_awaiting"]["status"] == "error"  # an awaiting-user run is also orphaned
    assert rows["r_done"]["status"] == "completed"  # terminal untouched
    assert rows["r_error"]["status"] == "error"  # already-terminal untouched
    assert counts["runs"] == 2


def test_sweep_is_idempotent(engine: Engine) -> None:
    with engine.begin() as conn:
        _msg(conn, "m_running", "running")
        _run(conn, "r_running", "running")

    first = reconcile_in_flight_on_startup(engine=engine)
    second = reconcile_in_flight_on_startup(engine=engine)

    assert first == {"messages": 1, "runs": 1}
    assert second == {"messages": 0, "runs": 0}  # a second pass finds nothing


def test_sweep_noop_on_clean_database(engine: Engine) -> None:
    assert reconcile_in_flight_on_startup(engine=engine) == {"messages": 0, "runs": 0}


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

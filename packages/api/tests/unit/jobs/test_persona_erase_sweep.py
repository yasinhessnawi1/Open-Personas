"""The periodic erasure sweep and the worker's maintenance hooks (R9-284, review M2).

A failed or stranded erasure must not wait for the next process start: the worker's
maintenance cadence resumes it, and one that has failed or run for 15 minutes is reported to
the operator once.
"""

from __future__ import annotations

from collections.abc import Iterator  # noqa: TC003 - a fixture annotation
from datetime import UTC, datetime, timedelta
from pathlib import Path  # noqa: TC003 - a fixture annotation
from typing import Any

import pytest
from persona.jobs import JobRegistry
from persona_api.db.community import create_community_schema, make_community_engine
from persona_api.db.models import erasures as erasures_t
from persona_api.jobs import worker as worker_module
from persona_api.jobs.handlers import persona_erase
from persona_api.jobs.handlers.persona_erase import PersonaEraseSweep, stalled_persona_erasures
from persona_api.services.erasure_ledger import ErasureLedger, SubjectKind
from sqlalchemy import update
from sqlalchemy.engine import Engine  # noqa: TC002 - a fixture annotation

_OWNER = "user_alice"
_NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = make_community_engine(tmp_path / "community.db")
    create_community_schema(eng)
    yield eng
    eng.dispose()


def _claim(engine: Engine, persona: str, *, age: timedelta = timedelta(0)) -> str:
    erasure = ErasureLedger(engine).claim(
        owner_id=_OWNER, subject_kind=SubjectKind.PERSONA, subject_id=persona, captured={}
    )
    with engine.begin() as conn:
        conn.execute(
            update(erasures_t)
            .where(erasures_t.c.id == erasure.id)
            .values(requested_at=_NOW - age, updated_at=_NOW - age)
        )
    return erasure.id


def _persona(letter: str) -> str:
    return "persona_" + letter * 32


def test_failed_and_long_running_erasures_are_stalled_and_a_fresh_one_is_not(
    engine: Engine,
) -> None:
    fresh = _claim(engine, _persona("a"), age=timedelta(minutes=14))
    old = _claim(engine, _persona("b"), age=timedelta(minutes=15))
    failed = _claim(engine, _persona("c"))
    ErasureLedger(engine).fail(failed, "memory_failed")
    done = _claim(engine, _persona("d"), age=timedelta(hours=2))
    ErasureLedger(engine).complete(done)

    stalled = {e.id for e in stalled_persona_erasures(engine, now=_NOW)}

    assert stalled == {old, failed}
    assert fresh not in stalled


def test_the_sweep_resumes_and_reports_each_stall_once(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    stuck = _claim(engine, _persona("a"), age=timedelta(hours=1))
    alerts: list[str] = []
    monkeypatch.setattr(persona_erase._log, "error", lambda _msg, **kw: alerts.append(kw["eid"]))  # noqa: SLF001
    monkeypatch.setattr(persona_erase, "datetime", _FrozenClock)
    resumed: list[str] = []
    sweep = PersonaEraseSweep(
        engine=engine, grace=timedelta(0), run=lambda e, _t: resumed.append(e.id)
    )

    assert sweep() == 1
    assert sweep() == 1

    assert resumed == [stuck, stuck]
    assert alerts == [stuck]


class _FrozenClock(datetime):
    @classmethod
    def now(cls, tz: Any = None) -> datetime:  # noqa: ANN401
        return _NOW if tz is None else _NOW.astimezone(tz)


class _Queue:
    def reclaim_expired(self, **_kw: Any) -> int:  # noqa: ANN401
        return 0

    def archive_terminal(self, **_kw: Any) -> int:  # noqa: ANN401
        return 0

    def purge_archive(self, **_kw: Any) -> int:  # noqa: ANN401
        return 0


def test_every_maintenance_hook_runs_and_one_failing_does_not_stop_the_rest(
    engine: Engine,
) -> None:
    ran: list[str] = []

    def broken() -> None:
        ran.append("broken")
        raise RuntimeError("a sweep failed")

    worker = worker_module.Worker(
        dispatch_engine=engine,
        rls_engine=engine,
        registry=JobRegistry(),
        maintenance_hooks=(broken, lambda: ran.append("erasures")),
    )
    worker._queue = _Queue()  # type: ignore[assignment]  # noqa: SLF001

    worker.run_maintenance()

    assert ran == ["broken", "erasures"]


def test_the_graph_backstop_runs_every_sweep_and_marks_nothing(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review H-2: whatever the graph still names is erased on every sweep until it is gone."""
    left = {(_OWNER, _persona("a")), (_OWNER, _persona("b"))}
    erased: list[str] = []

    def _erase(_engine: Engine, *, owner_id: str, persona_id: str, **_kw: bool) -> dict[str, int]:
        erased.append(persona_id)
        if persona_id == _persona("a"):
            raise RuntimeError("a lock timed out")
        left.discard((owner_id, persona_id))
        return {"nodes_deleted": 1}

    monkeypatch.setattr(persona_erase, "erase_persona_graph", _erase)
    sweep = PersonaEraseSweep(
        engine=engine,
        grace=timedelta(0),
        run=lambda _e, _t: None,
        find_leftovers=lambda _engine, **_kw: sorted(left),
    )

    sweep()
    sweep()

    # The failing persona is tried on every sweep; the one that succeeded is not found again.
    assert erased == [_persona("a"), _persona("b"), _persona("a")]


def test_a_backstop_failure_does_not_skip_the_stall_alerts(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    stuck = _claim(engine, _persona("c"), age=timedelta(hours=1))
    alerts: list[str] = []
    monkeypatch.setattr(
        persona_erase._log,  # noqa: SLF001
        "error",
        lambda _msg, **kw: alerts.append(kw.get("eid", "backstop")),
    )
    monkeypatch.setattr(persona_erase, "datetime", _FrozenClock)

    def _cannot_look(_engine: Engine, **_kw: object) -> list[tuple[str, str]]:
        raise RuntimeError("the graph query failed")

    sweep = PersonaEraseSweep(
        engine=engine, grace=timedelta(0), run=lambda _e, _t: None, find_leftovers=_cannot_look
    )

    sweep()

    assert alerts == ["backstop", stuck]


def test_the_backstop_looks_at_nothing_without_a_graph(engine: Engine) -> None:
    """The legacy edition (SQLite) has no graph tables to ask."""
    assert persona_erase.erased_personas_in_graph(engine) == []


@pytest.mark.parametrize("sqlstate", ["40P01", "40001"])
def test_a_backstop_aborted_by_postgres_is_tried_again_next_sweep(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, sqlstate: str
) -> None:
    """Review LOW-2: a deadlock (40P01) or serialization failure (40001) is not final."""
    import psycopg.errors  # noqa: PLC0415 - only this test builds driver errors
    from sqlalchemy.exc import OperationalError  # noqa: PLC0415

    left = {(_OWNER, _persona("a"))}
    tries: list[str] = []

    def _erase(_engine: Engine, *, owner_id: str, persona_id: str, **_kw: bool) -> dict[str, int]:
        tries.append(persona_id)
        if len(tries) == 1:
            raise OperationalError("DELETE", {}, psycopg.errors.lookup(sqlstate)("aborted"))
        left.discard((owner_id, persona_id))
        return {"nodes_deleted": 1}

    monkeypatch.setattr(persona_erase, "erase_persona_graph", _erase)
    sweep = PersonaEraseSweep(
        engine=engine,
        grace=timedelta(0),
        run=lambda _e, _t: None,
        find_leftovers=lambda _engine, **_kw: sorted(left),
    )

    sweep()
    sweep()

    assert tries == [_persona("a"), _persona("a")]
    assert left == set()


class _Clock:
    def __init__(self) -> None:
        self.now = _NOW

    def __call__(self) -> datetime:
        return self.now


def test_the_backstop_looks_at_the_last_day_each_sweep_and_at_everything_once_a_day(
    engine: Engine,
) -> None:
    """Review MEDIUM-3: the full scan ran every 30 seconds."""
    looks: list[datetime | None] = []

    def _find(_engine: Engine, *, since: datetime | None) -> list[tuple[str, str]]:
        looks.append(since)
        return []

    clock = _Clock()
    sweep = PersonaEraseSweep(
        engine=engine,
        grace=timedelta(0),
        run=lambda _e, _t: None,
        find_leftovers=_find,
        clock=clock,
    )

    sweep()  # a process's first sweep: everything
    clock.now = _NOW + timedelta(minutes=1)
    sweep()
    clock.now = _NOW + timedelta(hours=23, minutes=59)
    sweep()
    clock.now = _NOW + timedelta(hours=24)
    sweep()  # a day on: everything again
    clock.now = _NOW + timedelta(hours=24, minutes=1)
    sweep()

    day = timedelta(hours=24)
    assert looks == [
        None,
        _NOW + timedelta(minutes=1) - day,
        _NOW + timedelta(hours=23, minutes=59) - day,
        None,
        _NOW + timedelta(hours=24, minutes=1) - day,
    ]


def test_a_failed_full_pass_look_is_tried_again_next_sweep(engine: Engine) -> None:
    looks: list[datetime | None] = []

    def _find(_engine: Engine, *, since: datetime | None) -> list[tuple[str, str]]:
        looks.append(since)
        if len(looks) == 1:
            raise RuntimeError("the graph query failed")
        return []

    clock = _Clock()
    sweep = PersonaEraseSweep(
        engine=engine,
        grace=timedelta(0),
        run=lambda _e, _t: None,
        find_leftovers=_find,
        clock=clock,
    )

    sweep()
    clock.now = _NOW + timedelta(minutes=1)
    sweep()

    assert looks == [None, None], "the day's full pass has not happened yet"


def test_a_persona_only_the_full_pass_found_is_retried_on_the_next_sweep(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure is not left for tomorrow's full pass: the windowed look does not see it."""
    old = (_OWNER, _persona("a"))
    tries: list[str] = []

    def _find(_engine: Engine, *, since: datetime | None) -> list[tuple[str, str]]:
        return [old] if since is None else []

    def _erase(_engine: Engine, *, owner_id: str, persona_id: str, **_kw: bool) -> dict[str, int]:  # noqa: ARG001
        tries.append(persona_id)
        if len(tries) == 1:
            raise RuntimeError("a lock timed out")
        return {"nodes_deleted": 1}

    monkeypatch.setattr(persona_erase, "erase_persona_graph", _erase)
    clock = _Clock()
    sweep = PersonaEraseSweep(
        engine=engine,
        grace=timedelta(0),
        run=lambda _e, _t: None,
        find_leftovers=_find,
        clock=clock,
    )

    for minute in range(3):
        clock.now = _NOW + timedelta(minutes=minute)
        sweep()

    assert tries == [_persona("a"), _persona("a")], "retried once, then done"


def test_a_busy_graph_lock_defers_the_persona_and_nothing_waits(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 6 M-3: the sweep runs on the worker's event loop, so it never waits for the lock."""
    from persona_api.services.erasure_graph import GraphMaintenanceBusyError  # noqa: PLC0415

    left = {(_OWNER, _persona("a"))}
    calls: list[bool] = []
    errors: list[str] = []
    infos: list[str] = []

    def _erase(_engine: Engine, *, owner_id: str, persona_id: str, wait: bool) -> dict[str, int]:
        calls.append(wait)
        if len(calls) == 1:
            raise GraphMaintenanceBusyError(owner_id)
        left.discard((owner_id, persona_id))
        return {"nodes_deleted": 1}

    monkeypatch.setattr(persona_erase, "erase_persona_graph", _erase)
    monkeypatch.setattr(persona_erase._log, "error", lambda msg, **_k: errors.append(msg))  # noqa: SLF001
    monkeypatch.setattr(persona_erase._log, "info", lambda msg, **_k: infos.append(msg))  # noqa: SLF001
    clock = _Clock()
    sweep = PersonaEraseSweep(
        engine=engine,
        grace=timedelta(0),
        run=lambda _e, _t: None,
        find_leftovers=lambda _engine, *, since: sorted(left) if since is None else [],  # noqa: ARG005
        clock=clock,
    )

    sweep()
    clock.now = _NOW + timedelta(minutes=1)
    sweep()  # a windowed look, which does not see it: the retry does

    assert calls == [False, False], "never asked to wait"
    assert left == set()
    assert errors == []
    assert any("deferred" in m for m in infos)


def _always_busy_then(outcome: object) -> tuple[list[dict[str, object]], object]:
    """A graph step that is always busy without waiting, and does ``outcome`` when it waits."""
    from persona_api.services.erasure_graph import GraphMaintenanceBusyError  # noqa: PLC0415

    calls: list[dict[str, object]] = []

    def _erase(_engine: Engine, *, owner_id: str, persona_id: str, **kw: object) -> dict[str, int]:
        calls.append({"persona": persona_id, **kw})
        if kw.get("wait", True):
            if isinstance(outcome, Exception):
                raise outcome
            return {"nodes_deleted": 1}
        raise GraphMaintenanceBusyError(owner_id)

    return calls, _erase


@pytest.mark.parametrize("outcome", ["success", "timeout"])
def test_ten_deferrals_in_a_row_make_the_sweep_wait_for_the_lock(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    """Round 7 LOW-4: the waiting run is bounded, and a timeout is an operator alert."""
    from sqlalchemy.exc import OperationalError  # noqa: PLC0415

    failure = OperationalError("SELECT pg_advisory_xact_lock", {}, Exception("lock timeout"))
    calls, erase = _always_busy_then(failure if outcome == "timeout" else None)
    monkeypatch.setattr(persona_erase, "erase_persona_graph", erase)
    errors: list[str] = []
    monkeypatch.setattr(persona_erase._log, "error", lambda msg, **_k: errors.append(msg))  # noqa: SLF001
    key = (_OWNER, _persona("a"))
    sweep = PersonaEraseSweep(
        engine=engine,
        grace=timedelta(0),
        run=lambda _e, _t: None,
        find_leftovers=lambda _engine, **_kw: [key],
    )

    for _ in range(persona_erase.DEFERRALS_BEFORE_WAIT - 1):
        sweep()
    assert all(c["wait"] is False for c in calls), "nine deferrals: still never waits"
    sweep()  # the tenth: no event loop here, so the bounded wait runs inline

    waited = [c for c in calls if c["wait"] is True]
    assert waited == [
        {"persona": key[1], "wait": True, "lock_timeout_ms": persona_erase.WAIT_LOCK_TIMEOUT_MS}
    ]
    if outcome == "success":
        assert sweep._deferrals == {}, "reset on success"  # noqa: SLF001
        assert key not in sweep._retry  # noqa: SLF001
        assert errors == []
    else:
        assert any(m.startswith("OPERATOR ALERT: graph backstop blocked") for m in errors)
        assert key in sweep._retry, "tried again next sweep"  # noqa: SLF001


def test_on_the_event_loop_the_bounded_wait_runs_in_a_thread(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep returns at once; the wait happens off the loop."""
    import asyncio  # noqa: PLC0415
    import threading  # noqa: PLC0415

    calls, erase = _always_busy_then(None)
    threads: list[int] = []

    def _erase(*args: object, **kwargs: object) -> object:
        if kwargs.get("wait") is True:
            threads.append(threading.get_ident())
        return erase(*args, **kwargs)  # type: ignore[operator]

    monkeypatch.setattr(persona_erase, "erase_persona_graph", _erase)
    sweep = PersonaEraseSweep(
        engine=engine,
        grace=timedelta(0),
        run=lambda _e, _t: None,
        find_leftovers=lambda _engine, **_kw: [(_OWNER, _persona("a"))],
    )
    sweep._deferrals[(_OWNER, _persona("a"))] = persona_erase.DEFERRALS_BEFORE_WAIT - 1  # noqa: SLF001

    async def _on_the_loop() -> int:
        sweep()
        loop_thread = threading.get_ident()
        await asyncio.gather(*sweep._tasks)  # noqa: SLF001
        return loop_thread

    loop_thread = asyncio.run(_on_the_loop())

    assert len(threads) == 1
    assert threads[0] != loop_thread, "the wait ran in a worker thread"
    assert sweep._deferrals == {}  # noqa: SLF001

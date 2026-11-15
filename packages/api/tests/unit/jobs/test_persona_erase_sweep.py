"""The periodic erasure sweep and the worker's maintenance hooks (R9-284, review M2).

A failed or stranded erasure must not wait for the next process start: the worker's
maintenance cadence resumes it, and one that has failed or run for 15 minutes is reported to
the operator once.
"""

from __future__ import annotations

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
def engine(tmp_path: Path) -> Engine:
    eng = make_community_engine(tmp_path / "community.db")
    create_community_schema(eng)
    return eng


def _claim(engine: Engine, persona: str, *, age: timedelta = timedelta(0)) -> str:
    erasure = ErasureLedger(engine).claim(
        owner_id=_OWNER, subject_kind=SubjectKind.PERSONA, subject_id=persona, captured={}
    )
    with engine.begin() as conn:
        conn.execute(
            update(erasures_t).where(erasures_t.c.id == erasure.id).values(requested_at=_NOW - age)
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
    sweep = PersonaEraseSweep(engine=engine, run=lambda e, _t: resumed.append(e.id))

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


def test_a_finished_erasure_has_its_graph_step_run_once_more_after_a_lease(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review H2's backstop: a job past every check when the erasure finished can still write."""
    lease = persona_erase.GRAPH_RECHECK_AFTER
    ages = {
        "too_fresh": lease / 2,
        "settled": lease + timedelta(seconds=1),
        "long_ago": 4 * lease,
    }
    ids = {}
    for letter, (name, age) in zip("abc", ages.items(), strict=True):
        ids[name] = _claim(engine, _persona(letter))
        ErasureLedger(engine).complete(ids[name])
        with engine.begin() as conn:
            conn.execute(
                update(erasures_t)
                .where(erasures_t.c.id == ids[name])
                .values(completed_at=_NOW - age)
            )
    rechecked: list[str] = []
    monkeypatch.setattr(
        persona_erase,
        "erase_persona_graph",
        lambda _engine, *, persona_id, **_kw: rechecked.append(persona_id) or {},
    )
    monkeypatch.setattr(persona_erase, "datetime", _FrozenClock)
    sweep = PersonaEraseSweep(engine=engine, run=lambda _e, _t: None)

    sweep()
    sweep()

    assert rechecked == [_persona("b")]

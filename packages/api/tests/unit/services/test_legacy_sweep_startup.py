"""When the legacy audit sweep runs at start, and that it never stops the resume (R9-290).

One pass of the startup task: a worker is said to consume the erase job, so the task runs its
resume once and ends. The sweep and the resume are stand-ins that record what ran.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from persona_api.services import erasure_start


def _state(**extra: Any) -> SimpleNamespace:  # noqa: ANN401
    return SimpleNamespace(
        platform_engine=SimpleNamespace(dialect=SimpleNamespace(name="sqlite")),
        memory_backend=object(),
        audit_logger=object(),
        file_storage=None,
        workspace_root=None,
        audit_root=Path("audit"),
        job_queue=object(),
        in_process_worker=object(),
        **extra,
    )


@pytest.fixture
def ran(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    order: list[str] = []

    class _Sweep:
        def __init__(self, **_kwargs: Any) -> None:  # noqa: ANN401
            pass

        def __call__(self) -> int:
            order.append("resume")
            return 0

    monkeypatch.setattr(erasure_start, "PersonaEraseSweep", _Sweep)
    monkeypatch.setattr(erasure_start, "persona_erase_queue_available", lambda **_k: True)
    monkeypatch.setattr(
        erasure_start, "run_legacy_sweep_logged", lambda *_a: order.append("legacy sweep")
    )
    return order


def _one_pass(state: SimpleNamespace) -> None:
    async def _run() -> None:
        task = erasure_start.resume_persona_erasures_on_startup(state, interval_seconds=0.01)
        assert task is not None
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(_run())


def test_the_sweep_runs_where_the_ledger_is_known_writable(ran: list[str]) -> None:
    _one_pass(_state(erasure_ledger_writable=True))

    assert "legacy sweep" in ran


def test_the_sweep_does_not_run_where_the_ledger_cannot_be_written(ran: list[str]) -> None:
    """Review H1 a: a boot on a row-level-security engine would mistake live personas."""
    _one_pass(_state(erasure_ledger_writable=False))

    assert ran == ["resume"]


def _passes(state: SimpleNamespace, *, then: Any = None) -> bool:  # noqa: ANN401
    """Run the startup task for a few passes, calling ``then`` midway; whether it ended."""

    async def _run() -> bool:
        task = erasure_start.resume_persona_erasures_on_startup(state, interval_seconds=0.01)
        assert task is not None
        await asyncio.sleep(0.1)
        if then is not None:
            then()
        try:
            await asyncio.wait_for(task, timeout=1)
        except TimeoutError:
            return False
        return True

    return asyncio.run(_run())


def test_an_unknown_probe_runs_no_sweep_and_keeps_asking(ran: list[str]) -> None:
    """Round 2 LOW-6: no answer at boot (None) is not a yes; the task waits for one."""
    ended = _passes(_state(erasure_ledger_writable=None))

    assert not ended, "it keeps asking"
    assert ran == ["resume"], "one resume, no sweep"


def test_the_sweep_runs_once_the_probe_turns_writable(ran: list[str]) -> None:
    """Round 2 LOW-6: /healthz re-probes; once the ledger is writable the sweep runs, once."""
    state = _state(erasure_ledger_writable=None)

    ended = _passes(state, then=lambda: setattr(state, "erasure_ledger_writable", True))

    assert ended
    assert ran == ["resume", "legacy sweep"], "the worker resumes on its own after the first"


def test_the_sweep_runs_before_the_first_resume(ran: list[str]) -> None:
    """Review M2: an erasure the sweep claims is picked up by that same first pass."""
    _one_pass(_state(erasure_ledger_writable=True))

    assert ran == ["legacy sweep", "resume"]


def test_a_sweep_that_raises_never_stops_an_open_erasure(
    tmp_path: Path,
    embedder: Any,  # noqa: ANN401
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review M2: the sweep's failure is logged; the resume still finishes the erasure."""
    from _erase_seed import OWNER, PERSONA, legacy_edition  # noqa: PLC0415
    from persona_api.services import legacy_audit_sweep  # noqa: PLC0415
    from persona_api.services.erasure_ledger import (  # noqa: PLC0415
        ErasureLedger,
        ErasureState,
        SubjectKind,
    )
    from persona_api.services.erasure_service import capture_persona  # noqa: PLC0415

    def _broken(*_a: Any, **_k: Any) -> None:  # noqa: ANN401
        msg = "the sweep fell over"
        raise RuntimeError(msg)

    monkeypatch.setattr(legacy_audit_sweep, "sweep_legacy_audit_files", _broken)
    with legacy_edition(tmp_path, embedder) as seeded:  # type: ignore[arg-type]
        with seeded.engine.connect() as conn:
            captured = capture_persona(conn, owner_id=OWNER, persona_id=PERSONA)
        ledger = ErasureLedger(seeded.engine)
        ledger.claim(
            owner_id=OWNER, subject_kind=SubjectKind.PERSONA, subject_id=PERSONA, captured=captured
        )
        state = SimpleNamespace(
            platform_engine=seeded.engine,
            memory_backend=seeded.memory,
            audit_logger=seeded.audit,
            file_storage=seeded.storage,
            workspace_root=seeded.workspace_root,
            audit_root=seeded.audit_root,
            job_queue=None,
            in_process_worker=None,
            erasure_ledger_writable=True,
        )

        def _state_of() -> ErasureState | None:
            erasure = ledger.get(SubjectKind.PERSONA, PERSONA)
            return None if erasure is None else erasure.state

        async def _boot() -> None:
            task = erasure_start.resume_persona_erasures_on_startup(
                state, interval_seconds=0.05, grace=timedelta(0)
            )
            assert task is not None
            for _ in range(600):
                if _state_of() is ErasureState.COMPLETE:
                    break
                await asyncio.sleep(0.05)
            task.cancel()

        asyncio.run(_boot())

        assert _state_of() is ErasureState.COMPLETE

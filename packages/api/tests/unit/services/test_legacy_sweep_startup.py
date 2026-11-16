"""When the legacy audit sweep runs at start, and that it never stops the resume (R9-290).

One pass of the startup task: a worker is said to consume the erase job, so the task runs its
resume once and ends. The sweep and the resume are stand-ins that record what ran.
"""

from __future__ import annotations

import asyncio
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


@pytest.mark.parametrize("writable", [False, None])
def test_the_sweep_does_not_run_where_the_ledger_is_not_known_writable(
    ran: list[str],
    writable: bool | None,  # noqa: FBT001
) -> None:
    """Review H1 a: a boot on a row-level-security engine would mistake live personas."""
    _one_pass(_state(erasure_ledger_writable=writable))

    assert ran == ["resume"]

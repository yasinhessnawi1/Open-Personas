"""The production leg runner is built WITH the proposal recorder (Spec W1, T2; D-W1-1).

Spec A3's ``PolicyGatedToolbox`` was documented as "injected only at the leg runner" and
never constructed anywhere in production, so every leg ran ungated. The runner builder now
gates whenever it holds a recorder; this pins that the worker composition root hands it one.
A mechanical source guard (the A10-D-9 one-write-path pattern), because building the whole
worker registry needs a live Postgres and a dozen collaborators, while the regression it
prevents is a one-line omission: ``RuntimeFactoryLegRunnerBuilder(runtime_factory)`` with
no recorder would quietly return every leg to the ungated bare loop.
"""

from __future__ import annotations

import re
from pathlib import Path

_WORKER_ROOT = (
    Path(__file__).resolve().parents[3] / "src" / "persona_api" / "background" / "worker_root.py"
)


def test_the_worker_root_builds_the_leg_runner_with_the_approval_store_as_recorder() -> None:
    source = _WORKER_ROOT.read_text(encoding="utf-8")
    calls = re.findall(r"RuntimeFactoryLegRunnerBuilder\((.*?)\)", source, re.S)
    assert calls, "the worker root must build the leg runner"
    for args in calls:
        assert "recorder=ApprovalStore(" in args, (
            "a leg runner built without a recorder runs every leg UNGATED (the pre-W1 gap)"
        )

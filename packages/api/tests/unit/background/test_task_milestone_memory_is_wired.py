"""The worker root gives the task tenant a milestone recorder (Spec A2, T10; D-A2-4).

``MilestoneRecorder`` shipped complete and tested and no production module ever built one, so
a persona kept no memory of its own tasks. The two emitters (the leg handler at the leg
boundary, the continuation at the wait transition) both need one, and both take it optionally
because the plain A2 shape wires none, which is exactly how the omission stayed invisible.

A mechanical source guard, the same shape as the leg-runner gate above it: building the whole
worker registry needs a live Postgres and a dozen collaborators, while the regression it
prevents is a dropped keyword.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from persona_api.services.runtime_factory import RuntimeFactory

_WORKER_ROOT = (
    Path(__file__).resolve().parents[3] / "src" / "persona_api" / "background" / "worker_root.py"
)


def _task_leg_tenant_source() -> str:
    """The body of ``_register_task_leg_tenant`` (where the task tenant is composed)."""
    source = _WORKER_ROOT.read_text(encoding="utf-8")
    start = source.index("def _register_task_leg_tenant(")
    end = source.index("\ndef ", start + 1)
    return source[start:end]


def test_the_worker_root_builds_the_recorder_over_the_personas_own_episodic_store() -> None:
    """Over the SAME store the persona reads from, or the memory lands where nobody looks."""
    assert "MilestoneRecorder(runtime_factory.build_task_episodic_store())" in (
        _task_leg_tenant_source()
    ), "the task tenant must compose the milestone recorder on the persona's episodic store"


def test_the_runtime_factory_really_exposes_that_store() -> None:
    """The string guard above is only worth something if the method it names exists."""
    assert callable(RuntimeFactory.build_task_episodic_store)
    assert inspect.signature(RuntimeFactory.build_task_episodic_store).parameters.keys() == {"self"}


def test_both_emitters_are_handed_the_recorder() -> None:
    """The leg's milestones AND the wait transition's, off one recorder."""
    body = _task_leg_tenant_source()
    for call in ("TaskContinuation(", "register_task_leg_handler("):
        start = body.index(call)
        args = body[start : body.index("\n    )", start)]
        assert "milestones=milestones" in args, (
            f"{call} without a recorder leaves that half of a task's memory unwritten"
        )

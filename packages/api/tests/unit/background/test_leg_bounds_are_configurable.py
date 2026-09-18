"""An operator can bound a leg from the environment (completion sweep part 2, Findings F+K).

``.env.example`` had documented four per-leg knobs since Spec A2 and said the worker
composition injected them. Nothing read any of the four. ``LegBox`` carried the fields and
``TaskLegHandler`` took a ``box``, but both production construction sites built ``LegBox()``
with no arguments, and ``CheckpointStore``'s ``token_budget`` had no caller either. The
documentation had even drifted away from the code it described: it claimed a step bound of
10 long after R9-102 raised the enforced default to 20.

These pin the wiring, not the mechanism. ``LegBox.exhausted_by`` was tested all along and was
always correct; what could never happen was an operator's number reaching it. So each test
crosses the bound in its real unit: it sets the environment variable, builds the config the
process builds, runs the SAME composition the worker runs, and reads the number back off the
registered handler.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from persona.tasks import (
    DEFAULT_CHECKPOINT_TOKEN_BUDGET,
    DEFAULT_LEG_MAX_STEPS,
    DEFAULT_LEG_WALL_CLOCK_SECONDS,
    Contract,
    ContractBounds,
    LegBox,
    SpendKind,
    Task,
)
from persona_api.background.worker_root import _register_task_leg_tenant, build_leg_box
from persona_api.config import APIConfig
from persona_api.tasks.handler import TASK_LEG_JOB_TYPE, TaskLegHandler
from sqlalchemy import create_engine

_NOW = datetime(2026, 9, 18, 7, 0, tzinfo=UTC)

#: Every knob this module touches, cleared before each case so a stray value in the
#: developer's own environment cannot silently pass (or fail) a bound.
_KNOBS = (
    "PERSONA_TASK_LEG_MAX_STEPS",
    "PERSONA_TASK_LEG_WALLCLOCK_SECONDS",
    "PERSONA_TASK_LEG_BUDGET_MICROS",
    "PERSONA_TASK_CHECKPOINT_TOKEN_BUDGET",
)


class _StubRuntimeFactory:
    """Only what the leg composition reads off the factory while it is being assembled."""

    metadata_resolver = None

    def build_task_recall(self, *_args: object, **_kwargs: object) -> None:  # pragma: no cover
        return None


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for knob in _KNOBS:
        monkeypatch.delenv(knob, raising=False)


def _registered_handler(monkeypatch: pytest.MonkeyPatch, **env: str) -> TaskLegHandler:
    """Compose the leg tenant exactly as the worker does and hand back its handler.

    The composition runs for real. No Postgres is touched, because every store here only
    holds its engine until a job arrives. That is the point: a test that built its own
    ``LegBox`` would have passed for the whole life of the defect.
    """
    from persona.jobs import JobRegistry

    for name, value in env.items():
        monkeypatch.setenv(name, value)
    config = APIConfig()

    registry = JobRegistry()
    _register_task_leg_tenant(
        registry,
        rls_engine=create_engine("sqlite://"),
        runtime_factory=_StubRuntimeFactory(),  # type: ignore[arg-type]
        memory_backend=None,
        edition=None,
        audit_root=Path("/tmp/persona-leg-bounds-test"),
        config=config,
    )
    handler = registry.get(TASK_LEG_JOB_TYPE).handler
    assert isinstance(handler, TaskLegHandler)
    return handler


def test_a_configured_step_bound_reaches_the_registered_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``PERSONA_TASK_LEG_MAX_STEPS=3`` and the leg gets three steps, not twenty."""
    handler = _registered_handler(monkeypatch, PERSONA_TASK_LEG_MAX_STEPS="3")

    assert handler._box.max_steps == 3  # noqa: SLF001


def test_a_configured_wall_clock_bound_reaches_the_registered_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The drain-critical bound is the one an operator most needs to tune per deployment."""
    handler = _registered_handler(monkeypatch, PERSONA_TASK_LEG_WALLCLOCK_SECONDS="42.5")

    assert handler._box.wall_clock_seconds == 42.5  # noqa: SLF001


def test_a_configured_spend_cap_reaches_the_registered_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-leg spend ceiling, which until now had no way in at all."""
    handler = _registered_handler(monkeypatch, PERSONA_TASK_LEG_BUDGET_MICROS="5000")

    assert handler._box.budget_micros == 5_000  # noqa: SLF001


def test_a_configured_checkpoint_token_budget_reaches_the_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finding K: the checkpoint budget is enforced by the store the composition builds."""
    handler = _registered_handler(monkeypatch, PERSONA_TASK_CHECKPOINT_TOKEN_BUDGET="777")

    assert handler._checkpoints._budget == 777  # noqa: SLF001


def test_the_unset_defaults_are_the_core_constants(monkeypatch: pytest.MonkeyPatch) -> None:
    """No environment, no behaviour change: the shipped bounds are exactly the core ones.

    Imported rather than retyped, so the documented number and the enforced one cannot drift
    apart again the way they did when R9-102 raised the step bound and the docs kept saying 10.
    """
    handler = _registered_handler(monkeypatch)

    assert handler._box == LegBox()  # noqa: SLF001
    assert handler._box.max_steps == DEFAULT_LEG_MAX_STEPS  # noqa: SLF001
    assert handler._box.wall_clock_seconds == DEFAULT_LEG_WALL_CLOCK_SECONDS  # noqa: SLF001
    assert handler._box.budget_micros is None  # noqa: SLF001
    assert handler._checkpoints._budget == DEFAULT_CHECKPOINT_TOKEN_BUDGET  # noqa: SLF001


def test_an_empty_spend_cap_reads_as_no_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """``PERSONA_TASK_LEG_BUDGET_MICROS=`` is the documented way to say "no cap".

    It is set-but-blank in a great many .env files, copied straight from the example, and a
    worker that refused to boot on it would be punishing an operator for reading the docs.
    """
    monkeypatch.setenv("PERSONA_TASK_LEG_BUDGET_MICROS", "   ")

    assert APIConfig().task_leg_budget_micros is None


# --- the minimum rule, in both directions -------------------------------------


def _task(*, cap_micros: int, already_spent: int) -> Task:
    task = Task(
        id="t1",
        owner_id="user_a",
        persona_id="persona_a",
        contract=Contract(
            goal="watch the fare", bounds=ContractBounds(total_budget_micros=cap_micros)
        ),
        state="active",
        created_at=_NOW,
        updated_at=_NOW,
    )
    return task.record_spend(SpendKind.MODEL, already_spent, now=_NOW) if already_spent else task


def _handler_with(*, configured: int | None, remaining: int) -> TaskLegHandler:
    """A handler carrying only the two numbers ``_leg_box`` reconciles."""
    handler = TaskLegHandler.__new__(TaskLegHandler)
    handler._box = LegBox(max_steps=7, wall_clock_seconds=42.0, budget_micros=configured)  # noqa: SLF001
    handler._leg_budget_micros = lambda _owner, _task: remaining  # noqa: SLF001
    return handler


def test_the_configured_cap_wins_when_it_is_the_smaller_of_the_two() -> None:
    """An operator asking for tighter legs than the task's purse gets tighter legs."""
    box = _handler_with(configured=1_000, remaining=5_000)._leg_box(  # noqa: SLF001
        "user_a", _task(cap_micros=5_000, already_spent=0)
    )

    assert box.budget_micros == 1_000


def test_the_remaining_task_budget_wins_when_it_is_the_smaller_of_the_two() -> None:
    """A generous per-leg ceiling can never let a leg outspend what its task has left.

    This is the direction that matters for money: the configured cap is a convenience, the
    remaining budget is the promise made to the user, and a convenience may not override it.
    """
    box = _handler_with(configured=5_000, remaining=1_000)._leg_box(  # noqa: SLF001
        "user_a", _task(cap_micros=6_000, already_spent=5_000)
    )

    assert box.budget_micros == 1_000


def test_with_no_configured_cap_the_remaining_budget_is_the_whole_rule() -> None:
    """R9-176's behaviour is unchanged when the knob is left alone."""
    box = _handler_with(configured=None, remaining=1_234)._leg_box(  # noqa: SLF001
        "user_a", _task(cap_micros=9_000, already_spent=0)
    )

    assert box.budget_micros == 1_234


# --- the helper itself --------------------------------------------------------


def test_build_leg_box_reads_all_three_bounds_off_the_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One function builds the box, so no second construction site can drift from it."""
    monkeypatch.setenv("PERSONA_TASK_LEG_MAX_STEPS", "4")
    monkeypatch.setenv("PERSONA_TASK_LEG_WALLCLOCK_SECONDS", "99")
    monkeypatch.setenv("PERSONA_TASK_LEG_BUDGET_MICROS", "12345")

    box = build_leg_box(APIConfig())

    assert box == LegBox(max_steps=4, wall_clock_seconds=99.0, budget_micros=12_345)

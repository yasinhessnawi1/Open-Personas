"""Which writer a leg gets, and what a leg records about itself (Spec W1, T14).

Two composition facts that are invisible from inside either package:

- the flag decides the writer, ONCE, at the worker root (D-W1-18). Off is the deterministic
  writer production has always used; on is the distiller with that same writer as its
  fallback, so turning the flag on can only add an attempt to think, never remove the floor;
- the leg's measured shape rides the audit detail the handler already writes (research V-4),
  so the close-out can argue about the bounds from what legs do rather than from one
  datapoint that arrived by accident.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from persona.backends import BackendConfig
from persona.schema.tools import ToolCall, ToolResult
from persona.tasks import Contract, SpendKind, Task, micros_from_cents
from persona_api.background.worker_root import _checkpoint_writer
from persona_api.config import APIConfig
from persona_api.services.llm_usage_collector import collect_llm_usage
from persona_api.tasks.handler import _LegCost
from persona_api.tasks.leg_profile import leg_profile
from persona_runtime.agentic.call_ledger import CACHED_RESULT_NOTE, REPEAT_ERROR_HINT
from persona_runtime.agentic.run import Run, RunStatus
from persona_runtime.agentic.step import Step, StepType
from persona_runtime.legs import CompactingCheckpointWriter, LegDisposition, LegOutcome
from persona_runtime.legs.semantic_distiller import (
    DEFAULT_DISTILL_TIMEOUT_S,
    SemanticCheckpointWriter,
)
from persona_runtime.tier import TierConfig, TierRegistry

_NOW = datetime(2026, 9, 12, 9, 0, tzinfo=UTC)
_DUMMY_CFG = BackendConfig(provider="anthropic", model="m", api_key=None)  # type: ignore[arg-type]


def _registry() -> TierRegistry:
    return TierRegistry(
        {
            "small": TierConfig(name="small", backend_config=_DUMMY_CFG),
            "mid": TierConfig(name="mid", backend_config=_DUMMY_CFG),
        }
    )


def _writer(**config_overrides: object):  # noqa: ANN202 - the writer union is internal
    return _checkpoint_writer(
        APIConfig(**config_overrides),  # type: ignore[arg-type]
        rls_engine=None,  # type: ignore[arg-type]  # only reached when a write resolves a backend
        tier_registry=_registry(),
        free_tier_registry=None,
    )


def test_the_flag_on_is_the_default_now() -> None:
    """After the T13 eval record, a worker built from the default config distils."""
    assert isinstance(_writer(), SemanticCheckpointWriter)


def test_the_flag_defaults_on_now_that_the_eval_is_recorded() -> None:
    """D-W1-18 shipped it OFF and flips it ON in the same spec once the gate is met. It was,
    and the record names its parts: the continuation eval with the distiller in the loop, on
    the small tier production runs, judged by a different model family, 3/3 coherent with
    zero amnesia and zero ossification. Fail-soft protects against a crash; this is the
    evidence about the thing fail-soft cannot see, a plan that ossifies."""
    assert APIConfig().task_semantic_distiller_enabled is True


def test_the_kill_switch_still_works() -> None:
    """One env var back to false and every leg gets the writer production had before."""
    assert isinstance(_writer(task_semantic_distiller_enabled=False), CompactingCheckpointWriter)


def test_the_flag_on_is_the_distiller_over_the_same_floor() -> None:
    writer = _writer(task_semantic_distiller_enabled=True)

    assert isinstance(writer, SemanticCheckpointWriter)
    assert isinstance(writer._fallback, CompactingCheckpointWriter)  # noqa: SLF001 (the floor IS the point)


def test_the_backend_is_resolved_per_write_not_at_composition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R9-096: the worker binds the owner scope per job, so a backend resolved once here
    would serve every owner from whoever's plan happened to be active at boot."""
    calls: list[dict[str, object]] = []

    def _spy(**kwargs: object) -> object:
        calls.append(kwargs)
        return object()

    monkeypatch.setattr("persona_api.background.worker_root.plan_scoped_background_backend", _spy)
    writer = _writer(task_semantic_distiller_enabled=True, task_semantic_distiller_tier="mid")

    assert calls == []  # nothing resolved while composing
    writer._backend_provider()  # noqa: SLF001 (the provider is the seam)
    assert len(calls) == 1
    assert calls[0]["tier"] == "mid"  # D-W1-17: the configured tier, not a hardcoded one
    # D-W1-44: metered. The call happens because this leg ran, so it is billed with the leg
    # (one deduct, the leg's own billing key), not absorbed by the platform.
    assert calls[0]["metered"] is True


def test_the_distillation_timeout_is_configured_not_hardcoded() -> None:
    """The W1 operator pass watched a real scheduled leg time out at the old 20s default and
    lose its plan while the floor wrote the checkpoint. The number depends on the tier (a
    reasoning model thinks before it answers), so it is a knob with a measured default."""
    assert APIConfig().task_semantic_distiller_timeout_seconds == DEFAULT_DISTILL_TIMEOUT_S
    writer = _writer(
        task_semantic_distiller_enabled=True, task_semantic_distiller_timeout_seconds=7.5
    )

    assert writer._timeout_s == 7.5  # noqa: SLF001 (the wiring IS the assertion)


# --- the leg profile (research V-4) -----------------------------------------


def _run(steps: list[Step] | None = None) -> Run:
    return Run(
        persona_id="p",
        task="x",
        status=RunStatus.COMPLETED,
        steps=steps if steps is not None else [],
        output="done",
        started_at=_NOW,
        finished_at=_NOW.replace(second=12),
    )


def _outcome(*, steps: list[Step], box_limit: str | None = None) -> LegOutcome:
    run = _run(steps)
    task = Task(
        id="t1",
        owner_id="u",
        persona_id="p",
        contract=Contract(goal="g"),
        created_at=_NOW,
        updated_at=_NOW,
    )
    return LegOutcome(
        task=task,
        checkpoint=None,
        run=run,
        disposition=LegDisposition.COMPLETED,
        box_limit=box_limit,
        spend={},
    )


def _search(query: str, *, refused: bool = False) -> Step:
    call = ToolCall(name="web_search", args={"query": query}, call_id="c1")
    content = f"three results\n\n{REPEAT_ERROR_HINT}" if refused else "three results"
    result = ToolResult(tool_name="web_search", call_id="c1", content=content, is_error=refused)
    return Step(type=StepType.TOOL_CALL, tool_calls=[call], results=[result], tokens=1200)


def test_the_profile_measures_the_shape_of_the_leg() -> None:
    profile = leg_profile(_outcome(steps=[_search("a"), _search("b")]))

    assert profile["steps"] == "2"
    assert profile["tool_calls"] == "2"
    assert profile["tool_calls_per_step"] == "1.00"
    assert profile["distinct_queries"] == "2"
    assert profile["tokens_total"] == "2400"
    assert profile["tokens_per_step_max"] == "1200"
    assert profile["wall_clock_ms"] == "12000"
    assert profile["box_limit"] == "none"


def test_the_same_query_twice_is_one_distinct_question() -> None:
    """What the bounds question cares about is how much a leg ASKS the world, and a repeat
    the guard answered from the ledger asked it nothing."""
    profile = leg_profile(_outcome(steps=[_search("a"), _search("a")]))

    assert profile["tool_calls"] == "2"
    assert profile["distinct_queries"] == "1"


def test_reads_served_from_the_ledger_are_counted() -> None:
    """The half the operator pass actually saw. A model that repeats a SEARCH gets the stored
    answer, not a refusal, so counting only refusals made a leg that visibly saved a search
    report zero: the measurement understated exactly what it exists to measure."""
    cached = _search("a")
    cached.results[0] = ToolResult(
        tool_name="web_search",
        call_id="c1",
        content=f"three results\n\n{CACHED_RESULT_NOTE}",
    )
    profile = leg_profile(_outcome(steps=[_search("a"), cached]))

    assert profile["reads_served_from_ledger"] == "1"
    assert profile["repeats_refused"] == "0"  # a cached read is not a refusal


def test_refused_repeats_are_counted() -> None:
    """The T10 guards' visible effect on a real leg: calls the model asked for and did not
    pay for."""
    profile = leg_profile(_outcome(steps=[_search("a"), _search("a", refused=True)]))

    assert profile["repeats_refused"] == "1"


def test_a_box_trip_is_named() -> None:
    profile = leg_profile(_outcome(steps=[_search("a")], box_limit="wall_clock"))

    assert profile["box_limit"] == "wall_clock"


def test_a_leg_that_never_ran_measures_nothing_rather_than_zeroes() -> None:
    """An approval gate ends the leg before a run exists. Recording zeroes would put a leg
    that never ran into the averages the close-out reads."""
    task = Task(
        id="t1",
        owner_id="u",
        persona_id="p",
        contract=Contract(goal="g"),
        created_at=_NOW,
        updated_at=_NOW,
    )
    gated = LegOutcome(
        task=task,
        checkpoint=None,
        run=None,
        disposition=LegDisposition.WAITING_APPROVAL,
        box_limit=None,
        spend={},
    )

    assert leg_profile(gated) == {"box_limit": "none"}


# --- who pays for the distillation (Spec W1, D-W1-44) -----------------------


def test_the_distillers_usage_reaches_the_legs_accumulator() -> None:
    """D-W1-44: the distiller's model call is billed WITH the leg, not absorbed.

    The path is the one M3 already built for background calls: a usage-collecting backend
    records into the per-op sink, and the leg's cost reads that sink's totals, so it rides
    the SAME per-leg deduct (one billing key, one charge) rather than becoming a second row
    against the owner.
    """
    with collect_llm_usage() as sink:
        cost = _LegCost(None, sink)
        before, _ = cost.result()
        sink.record(
            provider="nvidia",
            model="nvidia/nemotron-3-super-120b-a12b",
            prompt_tokens=1800,
            completion_tokens=320,
            cost_usd=0.0004,
        )
        after, basis = cost.result()

    assert before == 0.0
    assert after > 0.0, "the distillation's real cost has to reach the leg's charge"
    assert basis is not None
    # R9-161: and the same cost reaches the task ledger, in the ledger's own unit.
    assert cost.ledger_spend(_run())[SpendKind.MODEL] == micros_from_cents(after)


def test_a_leg_that_did_not_distil_bills_exactly_what_it_billed_before() -> None:
    """The deterministic writer, an unmetered install, and a distillation that never
    reached a model all record nothing, so turning this on cannot move a bill that has no
    distillation behind it."""
    with collect_llm_usage() as sink:
        cost = _LegCost(None, sink)

    assert cost.result() == (0.0, None)
    assert cost.ledger_spend(_run())[SpendKind.MODEL] == 0

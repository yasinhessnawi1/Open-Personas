"""What a leg actually cost, measured rather than assumed (Spec W1, T14).

The spec's §2.4 asks whether the leg bounds are the right ones: ten steps, 180 seconds, the
per-leg budget. Nobody can answer that from the outside, because nothing recorded what a leg
does with them. The one production datapoint anyone had was a leg climbing from 8.7k to 24.5k
tokens per step, and that arrived by accident.

So every leg now records its own shape beside the spend it already meters: how many steps it
took, how many tools it called, how many distinct questions it asked the world, how many
calls the T10 guards refused, what a step cost, and whether the box stopped it.

**No bound moves in W1.** This is the measurement the close-out argues from, not a change of
policy. A profile that shows every leg stopping at the wall clock says something; so does one
showing legs finishing in four steps. Both are better than the current position, which is a
number chosen once and never looked at again.

Home: the audit `job.spend` detail dict the handler already writes (research V-4). It costs
no schema and no second writer, and the digest already reads audit metadata per task. The run
record's `steps` JSON keeps the raw events for anyone who wants the detail behind a number.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona_runtime.agentic.call_ledger import CACHED_RESULT_NOTE, canonical_key
from persona_runtime.legs.ledger import SEARCH_TOOLS

if TYPE_CHECKING:
    from persona_runtime.agentic.run import Run
    from persona_runtime.legs import LegOutcome

__all__ = ["leg_profile"]

#: The note the T10 ledger appends when it refuses a repeat of a call that FAILED. A result
#: carrying it is a call the run asked for and did not pay for.
_REPEAT_MARKER = "You already tried this exact call in this run"


def leg_profile(outcome: LegOutcome) -> dict[str, str]:
    """The leg's measured shape, as strings for the audit detail dict.

    Strings because the detail dict is stored as JSON alongside other string fields and read
    by eye as often as by code; a consistent shape beats a mixed one. Every value is derived
    from the durable run, so a re-delivery that re-runs the leg records its own numbers
    rather than inheriting the first attempt's.
    """
    run = outcome.run
    if run is None:
        # A gate ended the leg before a run existed (the approval park). There is nothing to
        # measure, and inventing zeroes would put a leg that never ran into the averages.
        return {"box_limit": outcome.box_limit or "none"}
    steps = len(run.steps)
    calls = [call for step in run.steps for call in step.tool_calls]
    results = [result for step in run.steps for result in step.results]
    tokens = [step.tokens for step in run.steps]
    return {
        "steps": str(steps),
        "tool_calls": str(len(calls)),
        "tool_calls_per_step": _ratio(len(calls), steps),
        "distinct_queries": str(len({canonical_key(c) for c in calls if c.name in SEARCH_TOOLS})),
        "repeats_refused": str(sum(1 for r in results if _REPEAT_MARKER in r.content)),
        # The other half of the T10 ledger, and the half the operator pass actually saw: a
        # repeated READ served from the run's own ledger. Counting only refused failures made
        # a leg that visibly saved a search report "repeats_refused: 0", which is a
        # measurement that understates the thing it exists to measure.
        "reads_served_from_ledger": str(sum(1 for r in results if CACHED_RESULT_NOTE in r.content)),
        "tokens_total": str(sum(tokens)),
        "tokens_per_step_max": str(max(tokens, default=0)),
        "wall_clock_ms": str(int(_wall_clock_ms(run))),
        "box_limit": outcome.box_limit or "none",
    }


def _ratio(numerator: int, denominator: int) -> str:
    """Two decimals, or ``0.00`` for a leg with no steps to divide by."""
    return f"{(numerator / denominator) if denominator else 0.0:.2f}"


def _wall_clock_ms(run: Run) -> float:
    """How long the leg ran, from the run's own timestamps.

    The step latencies would miss everything between them (dispatch, tool time, the
    compaction call), which is most of what a wall-clock bound actually bounds.
    """
    if run.finished_at is None:
        return 0.0
    return (run.finished_at - run.started_at).total_seconds() * 1000.0

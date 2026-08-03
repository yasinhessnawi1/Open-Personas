"""The leg box — steps + wall-clock + spend bounds per leg (Spec A2, T3; D-A2-2).

Every leg is bounded so it can end cleanly soon: deploys (A0 drain), waits, cancellation,
and budget pauses all degrade to one operation — *finish the box, write the checkpoint,
stop*. The bounds are **enforced, not advisory** (criterion 6); "this leg needs longer" is
always answered by *another leg*.

This module is pure: the box config + the predicate that says whether (and by what) a
running leg is exhausted. The runtime trips the loop's ``CancelToken`` at the next step
boundary when :meth:`LegBox.exhausted_by` returns non-``None`` (no loop modification — the
loop already checks the token at ``loop.py:233``). The wall-clock default sits inside A0's
270s drain with a ≥30s margin for the checkpoint commit (D-A0-5 co-decided).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "DEFAULT_LEG_MAX_STEPS",
    "DEFAULT_LEG_WALL_CLOCK_SECONDS",
    "LegBox",
    "LegBoxLimit",
]

#: Default per-leg step bound. Passed to ``AgenticLoop(max_steps=...)`` (the loop enforces
#: it natively; the box mirrors it for the cooperative check).
#:
#: Raised 10 → 20 (R9-102), matching the loop's own default. The original 10 was set
#: "below the chat loop's 20 so a step-heavy leg can't blow the wall-clock (D-A2-2)", and
#: that reasoning is backwards for background work: a chat turn has a person waiting, so
#: wall-clock is expensive there and it got 20; a leg has NOBODY waiting, so wall-clock is
#: nearly free, and it got half. The surface that could most afford to think was the one
#: rationed.
#:
#: It also could not finish. Production: an hourly "brief Hacker News" leg spent all 10
#: steps fetching one story URL each (8-12k tokens apiece), never reached synthesis, and
#: was cancelled with ZERO output after ~100k tokens — charged in full. A task needing
#: N fetches plus a synthesis step can never complete on a budget of N.
#:
#: Raising this is cost-POSITIVE, not a spend increase: a leg cancelled at the ceiling
#: bills the full retrieval and delivers nothing, while a leg that finishes bills slightly
#: more and delivers the answer. The waste was the truncation, not the steps.
#:
#: This does NOT fix the underlying inefficiency — the model fetches serially, one URL per
#: step, when the loop already supports several tool calls in a single step. Batched
#: retrieval is the real fix and stays open; this stops a solvable task being guillotined
#: while that is built.
DEFAULT_LEG_MAX_STEPS = 20

#: Default per-leg wall-clock bound (seconds) — a ≥30s margin below A0's 270s drain so the
#: checkpoint commit lands inside the drain window (D-A2-2 / D-A0-5).
DEFAULT_LEG_WALL_CLOCK_SECONDS = 180.0


class LegBoxLimit(StrEnum):
    """Which bound a leg hit (so the checkpoint can record *why* it stopped)."""

    STEPS = "steps"
    WALL_CLOCK = "wall_clock"
    BUDGET = "budget"


class LegBox(BaseModel):
    """The per-leg bounds (D-A2-2). All config-driven; the api injects the tuned values.

    Attributes:
        max_steps: The step bound (also passed to the loop's ``max_steps``).
        wall_clock_seconds: The wall-clock bound — must sit inside A0's drain.
        budget_micros: The per-leg spend cap (``amount_micros``); ``None`` = no spend cap
            (the box still bounds via steps + wall-clock). Tuned from A2-R-3 evidence.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_steps: int = Field(default=DEFAULT_LEG_MAX_STEPS, gt=0)
    wall_clock_seconds: float = Field(default=DEFAULT_LEG_WALL_CLOCK_SECONDS, gt=0)
    budget_micros: int | None = Field(default=None, ge=0)

    def exhausted_by(
        self,
        *,
        steps_taken: int,
        elapsed_seconds: float,
        spent_micros: int,
    ) -> LegBoxLimit | None:
        """Return the first bound a running leg has hit, or ``None`` if within all bounds.

        Priority is wall-clock → budget → steps: time is the drain-critical bound (a leg
        that overruns its wall-clock threatens the drain window), so it is reported first
        when several are breached at once.

        Args:
            steps_taken: Steps the leg has completed.
            elapsed_seconds: Wall-clock seconds the leg has run.
            spent_micros: Spend the leg has metered so far.

        Returns:
            The :class:`LegBoxLimit` that is breached (the highest-priority one), or
            ``None`` if the leg is still within its box.
        """
        if elapsed_seconds >= self.wall_clock_seconds:
            return LegBoxLimit.WALL_CLOCK
        if self.budget_micros is not None and spent_micros >= self.budget_micros:
            return LegBoxLimit.BUDGET
        if steps_taken >= self.max_steps:
            return LegBoxLimit.STEPS
        return None

"""The restraint discipline's pure policy (Spec A5, T2; A5-D-3/D-4 pins).

Four pure pieces the pipeline (T7) composes in the pinned order:

- **thresholds** — the value gate and the acceptance floor as SEPARATE
  predicates, because their composition order is load-bearing: acceptance is a
  suppressor read only AFTER the anti-engagement checks (A5-D-X-paccept-
  suppressor); keeping the predicates apart lets T7 pin the order and test it.
- **cadence** — the 1/3/2 caps model over delivery counts (A5-D-3). Over-cap
  is a verdict, never a drop: the pipeline batches the survivor.
- **delivery resolution** — interrupt-vs-batch with quiet hours ABSOLUTE (the
  Phase-3 polarity pin): an interrupt claim is honoured only for a hard
  deadline inside the horizon, and even then a fire inside the user's quiet
  window holds to the window edge — the user's boundary outranks the notice,
  the possibly-missed deadline is the accepted, audited cost.
- **staleness** — the why-now-lapsed half of held-candidate expiry (the
  second Phase-3 pin). The time half lives HERE (:func:`why_now_lapsed`); the
  citations-still-resolve half is T7's re-grounding at flush — the named
  split, nothing implicit.

Quiet-hours and timezone semantics are A8's shared definitions ONLY
(:mod:`persona.schedules.quiet_hours`, :mod:`persona.timezone`) — this module
adds no window or zone semantics of its own; :func:`local_minute_of_day` is a
plain ``zoneinfo`` wall-clock projection (DST handled by ``astimezone``), the
conversion the quiet-hours module's contract asks its caller to perform.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from persona.initiative.config import InitiativeSettings  # noqa: TC001 — runtime use

if TYPE_CHECKING:
    from persona.schedules.quiet_hours import QuietHours

__all__ = [
    "CadenceCounts",
    "DeliveryKind",
    "DeliveryResolution",
    "cadence_exhausted",
    "local_minute_of_day",
    "meets_acceptance_floor",
    "meets_value_threshold",
    "resolve_delivery",
    "why_now_lapsed",
]


def meets_value_threshold(value: float, settings: InitiativeSettings) -> bool:
    """The value gate (A5-D-3): below the threshold, the candidate is simply not raised."""
    return value >= settings.value_threshold


def meets_acceptance_floor(acceptance: float, settings: InitiativeSettings) -> bool:
    """The acceptance floor (A5-D-X-paccept-suppressor) — a SUPPRESSOR only.

    The pipeline reads this predicate only after the anti-engagement checks
    have passed; it can only gate a candidate DOWN. It is deliberately a
    separate function from :func:`meets_value_threshold` so the composition
    order stays T7's pinned, tested property — never an implicit blend.
    """
    return acceptance >= settings.acceptance_floor


class CadenceCounts(BaseModel):
    """Delivered-initiative counts the caps evaluate against (A5-D-3).

    Counted at *delivery* (the ledger's delivered dispositions), never at
    candidate creation: a suppressed candidate spends no budget.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    persona_day: int = Field(ge=0)
    persona_week: int = Field(ge=0)
    user_day: int = Field(ge=0)


def cadence_exhausted(counts: CadenceCounts, settings: InitiativeSettings) -> bool:
    """Whether ANY of the three caps is spent (persona/day, persona/week, user/day).

    The user-day cap is the pile-on guard above the per-persona caps: however
    many personas share the graph, the user hears at most
    ``daily_cap_per_user`` initiatives a day. Over-cap means BATCH, never
    drop — the verdict is the pipeline's signal to hold the candidate for the
    next flush.
    """
    return (
        counts.persona_day >= settings.daily_cap_per_persona
        or counts.persona_week >= settings.weekly_cap_per_persona
        or counts.user_day >= settings.daily_cap_per_user
    )


class DeliveryKind(StrEnum):
    """How a surviving candidate reaches the user (A5-D-3's urgency line)."""

    DELIVER_NOW = "deliver_now"
    HOLD_TO_QUIET_EDGE = "hold_to_quiet_edge"
    BATCH = "batch"


class DeliveryResolution(BaseModel):
    """The delivery decision, with the quiet-window edge when holding.

    ``hold_edge_minute`` is set only for ``HOLD_TO_QUIET_EDGE`` — the local
    minute-of-day at which the hold releases. It is always the quiet window's
    END minute (the first non-quiet moment — A8's own field, no new
    semantics): a hold releases *forward*, so the nearest-boundary advisory
    (:func:`persona.schedules.quiet_hours.quiet_hours_edge`, built for
    scheduling offers) would be wrong here — it can point *backwards* at the
    window's start.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: DeliveryKind
    hold_edge_minute: int | None = None


def local_minute_of_day(instant: datetime, timezone_name: str) -> int:
    """Project a tz-aware instant onto the user's local minute-of-day (0–1439).

    The conversion :mod:`persona.schedules.quiet_hours` asks its caller to
    perform — ``zoneinfo`` owns the DST arithmetic; no new time semantics here.
    """
    local = instant.astimezone(ZoneInfo(timezone_name))
    return local.hour * 60 + local.minute


def resolve_delivery(
    *,
    interrupt_claimed: bool,
    deadline: datetime | None,
    now: datetime,
    timezone_name: str,
    quiet: QuietHours | None,
    settings: InitiativeSettings,
) -> DeliveryResolution:
    """Resolve interrupt-vs-batch with quiet hours ABSOLUTE (A5-D-3 + the polarity pin).

    An interrupt claim is honoured only when a hard ``deadline`` exists and
    falls within ``(now, now + interrupt_horizon]`` — the scan's urgency
    estimate is validated, never trusted (a claim without a qualifying
    deadline batches). And even a valid interrupt inside the user's quiet
    window HOLDS to the nearest window edge, **even if the deadline may pass
    before the window ends**: the user's boundary outranks the notice (the
    Phase-3 polarity pin — the missed notice is the accepted cost, and the
    pipeline audits the hold).

    Args:
        interrupt_claimed: The candidate's ``urgency == INTERRUPT`` claim.
        deadline: The hard commitment instant from the candidate's grounding
            (T7 extracts it from the anchor), or ``None`` when the grounding
            carries no date.
        now: The current tz-aware instant.
        timezone_name: The user's resolved IANA zone (A8's
            :func:`~persona.timezone.resolve_timezone` output).
        quiet: The user's quiet window, or ``None`` when unset (off-until-set).
        settings: The restraint knobs (horizon).

    Returns:
        ``DELIVER_NOW``, ``HOLD_TO_QUIET_EDGE`` (with the release minute), or
        ``BATCH``.
    """
    if not interrupt_claimed:
        return DeliveryResolution(kind=DeliveryKind.BATCH)
    horizon = timedelta(hours=settings.interrupt_horizon_hours)
    if deadline is None or deadline <= now or deadline - now > horizon:
        return DeliveryResolution(kind=DeliveryKind.BATCH)
    if quiet is not None and quiet.contains(local_minute_of_day(now, timezone_name)):
        return DeliveryResolution(
            kind=DeliveryKind.HOLD_TO_QUIET_EDGE, hold_edge_minute=quiet.end_minute
        )
    return DeliveryResolution(kind=DeliveryKind.DELIVER_NOW)


def why_now_lapsed(*, anchor_time: datetime | None, now: datetime) -> bool:
    """The time half of held-candidate expiry (the second Phase-3 polarity pin).

    ``True`` when the candidate's dated anchor has passed — the hold is a
    buffer, not a queue that serves rotten items: at flush, a lapsed candidate
    is dropped with an audited disposition, never delivered stale. A candidate
    with no dated anchor (``None``) never lapses by TIME — its staleness check
    is T7's re-grounding at flush (citations must still resolve), the other
    half of the named split.
    """
    return anchor_time is not None and anchor_time <= now

"""Per-user quiet-hours — the warn-plus-nearest-edge advisory (Spec A8, A8-D-6).

A quiet-hours window is a per-user local-time span (stored with the user's timezone) during
which scheduling is discouraged. **Off until the user sets it** (A8-D-6): absent columns mean
no window, so nothing is ever flagged. When a window IS set, scheduling *into* it **warns +
offers the nearest edge** — it never silently shifts the time (the no-surprise property). A5's
cadence discipline reads the SAME window definition (one source of truth).

Pure, minutes-of-day arithmetic (no timezone math here — the caller converts the proposed fire
to the user's local wall-clock first, then asks this module). Supports a window that wraps
midnight (e.g. 22:00–07:00). Zero infrastructure — unit-tested directly.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = ["QuietHours", "quiet_hours_edge"]

_MINUTES_PER_DAY = 24 * 60


class QuietHours(BaseModel):
    """A per-user quiet window as local minutes-of-day (A8-D-6).

    ``[start_minute, end_minute)`` half-open; wraps midnight when ``start_minute >
    end_minute`` (e.g. 22:00 → 07:00 is ``start=1320, end=420``). ``start == end`` is an empty
    window (nothing is quiet) — the off state is a *null* window, not a zero-width one, but an
    accidental zero-width one is treated as off for safety.

    Attributes:
        start_minute: Inclusive start of the quiet span (0–1439 = 00:00–23:59).
        end_minute: Exclusive end of the quiet span (0–1439).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    start_minute: int = Field(ge=0, le=_MINUTES_PER_DAY - 1)
    end_minute: int = Field(ge=0, le=_MINUTES_PER_DAY - 1)

    @model_validator(mode="after")
    def _not_empty(self) -> QuietHours:
        if self.start_minute == self.end_minute:
            msg = "quiet-hours window is empty (start == end); leave it unset to disable"
            raise ValueError(msg)
        return self

    def contains(self, minute_of_day: int) -> bool:
        """Whether ``minute_of_day`` (0–1439) falls inside the quiet window."""
        if self.start_minute < self.end_minute:
            return self.start_minute <= minute_of_day < self.end_minute
        return minute_of_day >= self.start_minute or minute_of_day < self.end_minute  # wraps


def _circular_distance(a: int, b: int) -> int:
    """The shortest minutes-of-day distance between two clock minutes (wrapping midnight)."""
    diff = abs(a - b) % _MINUTES_PER_DAY
    return min(diff, _MINUTES_PER_DAY - diff)


def quiet_hours_edge(minute_of_day: int, quiet: QuietHours) -> int | None:
    """The nearest quiet-window edge if ``minute_of_day`` is inside it, else ``None`` (A8-D-6).

    Returns ``None`` when the proposed local minute is NOT in quiet hours (schedule freely). When
    it IS, returns the nearest boundary minute (the window's start or end, whichever is closer by
    circular distance; a tie prefers the ``end`` — the first moment quiet hours are over, the
    natural forward suggestion). The caller renders this as "that's in your quiet hours — 07:00
    instead?" and never applies it without the user's yes (warn-plus-offer, never a silent shift).
    """
    if not quiet.contains(minute_of_day):
        return None
    to_start = _circular_distance(minute_of_day, quiet.start_minute)
    to_end = _circular_distance(minute_of_day, quiet.end_minute)
    return quiet.start_minute if to_start < to_end else quiet.end_minute

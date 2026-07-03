"""The calendar-initiated reschedule — the twin of the chat verb (Spec A8, T9).

One mechanism, two twin interfaces (A8-D-4): the calendar edit routes through the SAME CAS-guarded
door as chat (:func:`persona_api.schedules.reschedule.reschedule`, ``actor=user_via_ui``) — there is
no second write path from the web. ``preview`` computes the next-fire + the full human-terms clause
+ the quiet-hours warn **server-side, from the engine** (bar 3: the picker never fabricates a time
the DST gap/fold policy would shift; bar 5: quiet-hours parity with chat). ``apply`` builds the new
schedule from the current row + the picker-mapped cadence and applies it. The client sends
picker-state, never a raw RRULE — :func:`pattern_to_rule` is server-side (bar 1).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from persona.schedules import (
    QuietHours,
    RecurrenceRule,
    Schedule,
    next_fire_after,
    pattern_to_rule,
    quiet_hours_edge,
    render_human_terms,
)
from pydantic import BaseModel, ConfigDict

from persona_api.schedules.reschedule import RescheduleActor, reschedule
from persona_api.services import user_service

if TYPE_CHECKING:
    from persona.schedules import RecurrencePattern
    from sqlalchemy import Engine

    from persona_api.schedules.store import ScheduleStore

__all__ = ["ReschedulePreview", "apply_calendar_reschedule", "preview_calendar_reschedule"]


class ReschedulePreview(BaseModel):
    """The engine's preview of a proposed calendar reschedule (no write)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    human_terms: str  # the full new clause, human terms (no raw RRULE)
    timezone: str
    next_fire: datetime | None  # the engine's next fire (None if the cadence never fires)
    quiet_hours_offer: str | None  # HH:MM nearest edge if the next fire is inside quiet hours


def _resolve_cadence(
    pattern: RecurrencePattern | None, one_time_at: datetime | None
) -> tuple[RecurrenceRule | None, datetime | None]:
    """Map picker-state → (recurrence, one_time) — the RRULE mapping is server-side (bar 1)."""
    if pattern is not None:
        return pattern_to_rule(pattern), None
    return None, one_time_at


def preview_calendar_reschedule(
    engine: Engine,
    *,
    owner_id: str,
    pattern: RecurrencePattern | None,
    one_time_at: datetime | None,
    timezone: str,
    now: datetime,
) -> ReschedulePreview:
    """Compute the next-fire + clause + quiet-hours warn for a proposed reschedule (no write)."""
    recurrence, one_time = _resolve_cadence(pattern, one_time_at)
    preview = Schedule(
        id="preview",
        owner_id=owner_id,
        timezone=timezone,
        recurrence=recurrence,
        one_time_at=one_time,
        target_job_type="preview",
        created_at=now,
        updated_at=now,
    )
    next_fire = next_fire_after(preview, after=now)
    offer = _quiet_offer(engine, owner_id, next_fire, timezone)
    return ReschedulePreview(
        human_terms=render_human_terms(
            recurrence=recurrence, one_time_at=one_time, timezone=timezone
        ),
        timezone=timezone,
        next_fire=next_fire,
        quiet_hours_offer=offer,
    )


def apply_calendar_reschedule(
    store: ScheduleStore,
    engine: Engine,
    *,
    owner_id: str,
    schedule_id: str,
    pattern: RecurrencePattern | None,
    one_time_at: datetime | None,
    timezone: str,
    now: datetime,
) -> Schedule:
    """Apply the reschedule through the SAME CAS door as chat (``actor=user_via_ui``, bar 4)."""
    recurrence, one_time = _resolve_cadence(pattern, one_time_at)
    current = store.get(owner_id, schedule_id)
    new_schedule = current.model_copy(
        update={
            "recurrence": recurrence,
            "one_time_at": one_time,
            "timezone": timezone,
            "updated_at": now,
        }
    )
    return reschedule(
        store,
        engine,
        owner_id=owner_id,
        schedule_id=schedule_id,
        new_schedule=new_schedule,
        actor=RescheduleActor.USER_VIA_UI,
        provenance="calendar time-picker edit",
        now=now,
    )


def _quiet_offer(
    engine: Engine, owner_id: str, next_fire: datetime | None, timezone: str
) -> str | None:
    """The nearest quiet-window edge (HH:MM) if the next fire is inside the user's quiet hours."""
    if next_fire is None:
        return None
    profile = user_service.get_user_profile(engine, user_id=owner_id)
    if not profile:
        return None
    start, end = profile.get("quiet_hours_start"), profile.get("quiet_hours_end")
    if not isinstance(start, int) or not isinstance(end, int):
        return None
    try:
        quiet = QuietHours(start_minute=start, end_minute=end)
    except ValueError:
        return None
    local = next_fire.astimezone(ZoneInfo(timezone))
    edge = quiet_hours_edge(local.hour * 60 + local.minute, quiet)
    return None if edge is None else f"{edge // 60:02d}:{edge % 60:02d}"

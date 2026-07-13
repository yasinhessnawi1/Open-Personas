"""The occurrences read model — the calendar's + A6-Review's shared data source (A8, A8-D-11).

Computes upcoming fires across an owner's schedules from the SAME engine path the tick fires
(:func:`persona.schedules.occurrences_between` — no client-side recurrence math anywhere,
criterion 5), joined to the owning task (id + persona, for the calendar's deep-link + colour),
plus recent fire history from the audit trail. **Server-capped** (A8-D-11): the window is
clamped to a max horizon and the result to a max count, and the response carries an honest
``truncated`` marker — a wide ``?from&to`` can never become unbounded compute or a silent cap.

Owner-scoped throughout (RLS ``current_user_id`` GUC): owner B's occurrences are B's schedules,
never A's — the standing cross-tenant guarantee, tested non-vacuously.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta  # noqa: TC003 — datetime is a runtime Pydantic field type
from typing import TYPE_CHECKING

from persona.schedules import occurrences_between, render_human_terms
from pydantic import BaseModel, ConfigDict
from sqlalchemy import bindparam, text

from persona_api.db.engine import rls_connection
from persona_api.schedules.store import ScheduleStore
from persona_api.services.schedule_create_service import REMINDER_GOAL_PREFIX
from persona_api.textline import one_line

if TYPE_CHECKING:
    from persona.schedules import Schedule
    from sqlalchemy import Engine

    from persona_api.config import APIConfig

__all__ = ["FireEvent", "Occurrence", "OccurrencesResult", "list_occurrences"]

# Schedule-audit actions that describe a past fire/miss (the honest history markers).
_HISTORY_ACTIONS = ("schedule.fire", "schedule.fire_late", "schedule.miss")
_HISTORY_STATUS = {
    "schedule.fire": "ran",
    "schedule.fire_late": "ran_late",
    "schedule.miss": "missed",
}


class Occurrence(BaseModel):
    """One computed future fire (tz-aware UTC instant) + its schedule/task context."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schedule_id: str
    task_id: str | None
    persona_id: str | None
    fire_at: datetime  # the absolute UTC instant (the client formats it in the display tz)
    timezone: str  # the schedule's captured IANA zone (the human_terms frame, D-A1-4)
    human_terms: str  # the cadence in human terms (no raw RRULE, ever)
    #: R11-B3: WHAT fires — the A10 subject when the user named one, else the backing
    #: task's goal (one calm line) — so the calendar reads "Resume the battery survey",
    #: never the cadence clause twice. ``None`` when neither exists (client falls back
    #: to ``human_terms``).
    subject: str | None = None


class FireEvent(BaseModel):
    """One past fire/miss from the audit trail (the calendar's ran/missed markers)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schedule_id: str
    at: datetime  # the fired / missed scheduled instant (UTC)
    status: str  # ran | ran_late | missed


class OccurrencesResult(BaseModel):
    """The windowed occurrences + fire history + the honest truncation marker (A8-D-11)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    occurrences: tuple[Occurrence, ...]
    history: tuple[FireEvent, ...]
    window_from: datetime
    window_to: datetime  # the EFFECTIVE end after the horizon clamp
    truncated: (
        bool  # True iff the horizon or count cap bound the result (ask for a narrower window)
    )


def list_occurrences(
    engine: Engine,
    *,
    owner_id: str,
    from_: datetime,
    to: datetime,
    config: APIConfig,
    persona_id: str | None = None,
) -> OccurrencesResult:
    """Compute the owner's occurrences in ``[from_, to]`` + recent history, server-capped.

    The window is clamped to ``schedule_occurrences_max_horizon_days`` and the merged result to
    ``schedule_occurrences_max_count``; ``truncated`` is set when either binds. Occurrences come
    from :func:`occurrences_between` (the tick's own engine path); history from the ``audit_log``
    fire/miss notes. Everything is owner-scoped (RLS).

    ``persona_id`` (R9-024, additive-optional) restricts the result to schedules that resolve to
    that persona — the chat right-panel calendar's server-side filter. Resolution is INDIRECT
    (schedules carry no ``persona_id`` column, :func:`_resolved_persona_id`): a task-backed
    schedule (``task_scheduled_fire``) resolves through the SAME ``_task_by_schedule`` join
    already computed for every occurrence's ``task_id``/``persona_id`` fields (no extra query); a
    schedule with no backing task (``initiative_scan``) resolves from its own
    ``payload_template.persona_id``. ``None`` (the default) is byte-identical to pre-R9-024
    behaviour — no schedule is ever filtered out.
    """
    max_count = config.schedule_occurrences_max_count
    horizon = timedelta(days=config.schedule_occurrences_max_horizon_days)
    effective_to = min(to, from_ + horizon)
    truncated = effective_to < to

    store = ScheduleStore(engine)
    schedules = store.list_for_owner(owner_id)
    task_by_schedule = _task_by_schedule(engine, owner_id)

    collected: list[Occurrence] = []
    for schedule in schedules:
        if persona_id is not None:
            resolved = _resolved_persona_id(schedule, task_by_schedule)
            if resolved != persona_id:
                continue
        # Cap each schedule at the global budget; the merged list is truncated below.
        for fire_at in occurrences_between(schedule, from_, effective_to, cap=max_count):
            task = task_by_schedule.get(schedule.id)
            collected.append(
                Occurrence(
                    schedule_id=schedule.id,
                    task_id=task[0] if task else None,
                    # R11-B3: the DISPLAY persona uses the same two-step resolution the
                    # R9-024 filter uses (task join, else the initiative payload) — the
                    # identity spine needs a persona wherever one truthfully exists.
                    persona_id=_resolved_persona_id(schedule, task_by_schedule),
                    fire_at=fire_at,
                    timezone=schedule.timezone,
                    human_terms=render_human_terms(
                        recurrence=schedule.recurrence,
                        one_time_at=schedule.one_time_at,
                        timezone=schedule.timezone,
                    ),
                    subject=_subject(schedule, task),
                )
            )

    collected.sort(key=lambda o: o.fire_at)
    if len(collected) > max_count:
        collected = collected[:max_count]
        truncated = True

    history = _fire_history(engine, owner_id, from_=from_, to=effective_to, cap=max_count)
    return OccurrencesResult(
        occurrences=tuple(collected),
        history=tuple(history),
        window_from=from_,
        window_to=effective_to,
        truncated=truncated,
    )


def _resolved_persona_id(
    schedule: Schedule, task_by_schedule: dict[str, tuple[str, str, str | None]]
) -> str | None:
    """The schedule's owning persona — the R9-024 filter signal AND (R11-B3) the
    occurrence's display persona (the calendar's identity spine).

    Two linkage kinds, resolved in priority order: a task-backed schedule
    (``task_scheduled_fire``) is owned by its task's ``persona_id`` (the ``_task_by_schedule``
    join); a schedule with no backing task (``initiative_scan``) carries ``persona_id`` directly
    in its ``payload_template``. Neither present → ``None`` (never matches a filter; the
    client renders a neutral row).
    """
    task = task_by_schedule.get(schedule.id)
    if task is not None:
        return task[1]
    raw = schedule.payload_template.get("persona_id")
    return raw if isinstance(raw, str) else None


def _subject(schedule: Schedule, task: tuple[str, str, str | None] | None) -> str | None:
    """WHAT the occurrence does, one calm line (R11-B3): the A10 user subject when
    present, else the backing task's goal — never the cadence clause again. A
    reminder-template goal sheds its frame ("Remind and update the user about: X"
    → "X"): the calendar shows the thing, not the plumbing."""
    raw = schedule.payload_template.get("subject")
    if isinstance(raw, str) and raw.strip():
        return one_line(raw)
    if task is not None and task[2]:
        goal = task[2]
        if goal.startswith(REMINDER_GOAL_PREFIX):
            goal = goal[len(REMINDER_GOAL_PREFIX) :]
        return one_line(goal)
    return None


def _task_by_schedule(engine: Engine, owner_id: str) -> dict[str, tuple[str, str, str | None]]:
    """Map ``schedule_id → (task_id, persona_id, goal)`` for the owner's schedule-backed
    tasks (RLS). The goal feeds the occurrence subject fallback (R11-B3)."""
    stmt = text(
        "SELECT id, persona_id, schedule_id, contract_json->>'goal' AS goal "
        "FROM tasks WHERE schedule_id IS NOT NULL"
    )
    with rls_connection(engine, owner_id) as conn:
        rows = conn.execute(stmt).mappings().all()
    return {r["schedule_id"]: (r["id"], r["persona_id"], r["goal"]) for r in rows}


def _fire_history(
    engine: Engine, owner_id: str, *, from_: datetime, to: datetime, cap: int
) -> list[FireEvent]:
    """Recent fire/miss events from the audit trail, within the window (RLS-scoped)."""
    # ``IN :actions`` with an expanding bindparam, NOT Postgres ``= ANY(:list)``:
    # the community edition runs this on SQLite (D-33-X-community-engine), where
    # ANY does not exist; expanding IN compiles portably on both dialects.
    stmt = text(
        "SELECT action, target, metadata FROM audit_log "
        "WHERE user_id = :owner AND action IN :actions "
        "ORDER BY created_at DESC LIMIT :cap"
    ).bindparams(bindparam("actions", expanding=True))
    with rls_connection(engine, owner_id) as conn:
        rows = (
            conn.execute(
                stmt,
                {"owner": owner_id, "actions": list(_HISTORY_ACTIONS), "cap": cap},
            )
            .mappings()
            .all()
        )
    events: list[FireEvent] = []
    for row in rows:
        # Raw text() carries no column type info: Postgres/psycopg gives JSONB
        # back as a dict, SQLite gives the stored TEXT — decode the latter.
        meta_raw = row["metadata"]
        if isinstance(meta_raw, str):
            try:
                meta = json.loads(meta_raw)
            except ValueError:
                meta = {}
        else:
            meta = meta_raw or {}
        raw = meta.get("fire_time") or meta.get("missed_fire_time")
        if not isinstance(raw, str):
            continue
        at = _parse_iso(raw)
        if at is None or not (from_ <= at <= to):
            continue
        events.append(
            FireEvent(schedule_id=row["target"], at=at, status=_HISTORY_STATUS[row["action"]])
        )
    return events


def _parse_iso(value: str) -> datetime | None:
    from datetime import datetime as _dt

    try:
        return _dt.fromisoformat(value)
    except ValueError:
        return None

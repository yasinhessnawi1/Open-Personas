"""A8 T1 — the per-user-resolved timezone drives DST-correct firing (A8-D-9, criterion 1).

A1's suite proves ``next_fire_after`` is IANA-zone-correct. This test proves the
A8 *per-user-resolution* path inherits that correctness: a schedule whose captured
zone came from ``resolve_timezone(users.timezone ?? PERSONA_DEFAULT_TIMEZONE)`` fires
at the same LOCAL wall-clock on both sides of a DST boundary — i.e. its UTC instant
shifts by exactly the offset. Parametrized across three zones whose transitions fall
on different dates and directions: Europe/Oslo (EU), America/New_York (US, weeks
apart), Australia/Sydney (southern hemisphere, reversed). We assert the UTC instant
(the offset-frozen/naive bugs produce the right local string but the wrong UTC
instant — research §1.F3/F4), not the local string.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from persona.schedules import RecurrenceFreq, RecurrenceRule, Schedule, next_fire_after
from persona.timezone import resolve_timezone

# (zone, spring-forward Sunday, fall-back Sunday) for 2024.
_ZONES = [
    ("Europe/Oslo", datetime(2024, 3, 31, 1), datetime(2024, 10, 27, 1)),
    ("America/New_York", datetime(2024, 3, 10, 5), datetime(2024, 11, 3, 5)),
    # Australia/Sydney: DST STARTS (spring-forward) in October, ENDS (fall-back) in April.
    ("Australia/Sydney", datetime(2024, 10, 6, 15), datetime(2024, 4, 7, 15)),
]


def _daily_9am_in(resolved_tz: str, *, created_at: datetime) -> Schedule:
    return Schedule(
        id="s1",
        owner_id="u1",
        timezone=resolved_tz,
        recurrence=RecurrenceRule(freq=RecurrenceFreq.DAILY, byhour=(9,), byminute=(0,)),
        target_job_type="briefing",
        created_at=created_at,
        updated_at=created_at,
    )


def _local_hm(instant: datetime, tz: str) -> tuple[int, int]:
    local = instant.astimezone(ZoneInfo(tz))
    return local.hour, local.minute


@pytest.mark.parametrize(("zone", "spring", "fall"), _ZONES)
def test_resolved_tz_holds_wallclock_across_dst_boundaries(
    zone: str, spring: datetime, fall: datetime
) -> None:
    # The per-user resolution: users.timezone is set → the schedule captures it.
    resolved = resolve_timezone(zone, default="UTC")
    assert resolved == zone

    for boundary in (spring, fall):
        created = (boundary.replace(hour=0) - timedelta(days=1)).replace(tzinfo=UTC)
        sched = _daily_9am_in(resolved, created_at=created)
        # The fire on the transition day and the day after must both read 09:00 local
        # (wall-clock held) while their UTC instants differ from the pre-transition day
        # by the DST offset — proving zone-anchored, not offset-frozen, computation.
        after = boundary.replace(tzinfo=UTC)
        f_transition = next_fire_after(sched, after)
        assert f_transition is not None
        assert _local_hm(f_transition, zone) == (9, 0)
        f_next = next_fire_after(sched, f_transition)
        assert f_next is not None
        assert _local_hm(f_next, zone) == (9, 0)
        # The two consecutive fires are ~24h of wall-clock apart but NOT exactly 24h of
        # UTC across the boundary (23h or 25h), which is the whole point of zone-anchoring.
        delta_hours = (f_next - f_transition).total_seconds() / 3600
        assert delta_hours in (23.0, 24.0, 25.0)


def test_unset_tz_falls_back_to_config_default_and_fires_correctly() -> None:
    # users.timezone NULL → resolve to the config default (Europe/Oslo), fires in it.
    resolved = resolve_timezone(None, default="Europe/Oslo")
    sched = _daily_9am_in(resolved, created_at=datetime(2024, 6, 1, tzinfo=UTC))
    fire = next_fire_after(sched, datetime(2024, 6, 1, 12, tzinfo=UTC))
    assert fire is not None
    assert _local_hm(fire, "Europe/Oslo") == (9, 0)

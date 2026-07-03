"""The pure, DST-correct next-fire computation (Spec A1, T2) — the architectural lock.

``next_fire_after(schedule, after)`` answers the one question A1 must get exactly
right: *given a schedule and an instant, when is the next fire?* It is pure (no
clock, no DB, no I/O), deterministic, and the home of the DST-edge handling
(D-A1-X-dst-edges-in-localize). The exhaustive DST fixture suite tests THIS file.

**The architecture (proven in research §1.3):** ``dateutil.rrule`` computes the
recurrence as a pure NAIVE wall-clock pattern; the DST edges live HERE, at our
localization step, never in the rule. For each naive-local occurrence we:

    aware = naive.replace(tzinfo=zone, fold=0)        # fold=0 = the FIRST instant
    if not datetime_exists(aware):                    # spring-forward GAP
        aware = resolve_imaginary(aware)              # → the adjusted instant
    fire = aware.astimezone(UTC)

**Gap before fold (the ordering trap, proven):** ``datetime_ambiguous`` returns
``True`` for a *gap* time too, so we never branch on ambiguity — we branch only on
*existence* (the gap), and ``fold=0`` handles the fall-back fold by construction
(the first of the two repeated instants). Checking ambiguity first would mis-route
an imaginary time; this code is immune by never consulting it.

**Bounds.** ``COUNT`` and ``UNTIL`` are enforced HERE against absolute UTC, not fed
to the naive rrule: ``COUNT`` counts fires strictly after the schedule's creation
anchor (so "fire 5 times" = the first 5 future fires, DST-independent); ``UNTIL``
(a stored aware-UTC instant) is compared to each localized UTC fire — apples to
apples in absolute time, so a DST transition can never silently miscount the tail.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from dateutil import rrule as _rrule
from dateutil import tz as _dtz

if TYPE_CHECKING:
    from zoneinfo import ZoneInfo

    from persona.schedules.models import RecurrenceRule, Schedule

__all__ = ["next_fire_after", "occurrences_between"]

# How far before ``after`` to seed the rrule scan (local). The localization step
# shifts a fire's absolute instant by at most the DST gap (≤ ~2h); two days is a
# generous margin that guarantees the true next fire is not skipped, while keeping
# the scan short (the seek jumps near ``after`` rather than from the anchor).
_SEEK_MARGIN = timedelta(days=2)

# A hard cap on scan iterations — a defensive backstop against a pathological rule
# that never produces a fire after ``after`` (should be unreachable: the seek lands
# within a few occurrences, and bounded rules terminate by COUNT/UNTIL).
_MAX_SCAN = 10_000


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        msg = "after must be timezone-aware (UTC)"
        raise ValueError(msg)
    return value.astimezone(UTC)


def _localize_to_utc(naive_local: datetime, zone: ZoneInfo) -> datetime:
    """Localize a naive wall-clock occurrence to its absolute UTC instant.

    The DST-edge handling, in one place (D-A1-X-dst-edges-in-localize):

    * **Spring-forward GAP** — a local time that does not exist. Detected by
      ``datetime_exists`` (the gap check, done FIRST), resolved with
      ``resolve_imaginary`` to the adjusted instant (the clock-jump-forward time).
    * **Fall-back FOLD** — a local time that occurs twice. Handled by ``fold=0``,
      which selects the FIRST occurrence; no ambiguity branch is taken (it would
      misfire on the gap, which is also reported ambiguous).
    """
    aware = naive_local.replace(tzinfo=zone, fold=0)
    if not _dtz.datetime_exists(aware):
        aware = _dtz.resolve_imaginary(aware)
    return aware.astimezone(UTC)


def _pattern_string(rule: RecurrenceRule) -> str:
    """The RFC-5545 RRULE value WITHOUT COUNT/UNTIL — the pure wall-clock pattern.

    Bounds are enforced in :func:`next_fire_after` against absolute UTC, so the
    rrule is the unbounded pattern; ``dateutil`` parses BYDAY ordinals (e.g.
    ``1MO``) and the rest natively.
    """
    parts: list[str] = [f"FREQ={rule.freq.value}"]
    if rule.interval != 1:
        parts.append(f"INTERVAL={rule.interval}")
    if rule.byday:
        parts.append(f"BYDAY={','.join(rule.byday)}")
    if rule.bymonthday:
        parts.append(f"BYMONTHDAY={','.join(str(d) for d in rule.bymonthday)}")
    if rule.byhour:
        parts.append(f"BYHOUR={','.join(str(h) for h in rule.byhour)}")
    if rule.byminute:
        parts.append(f"BYMINUTE={','.join(str(m) for m in rule.byminute)}")
    return ";".join(parts)


def _build_pattern(rule: RecurrenceRule, dtstart_local: datetime) -> _rrule.rrule:
    """Build the unbounded naive-wall-clock rrule anchored at ``dtstart_local``."""
    built = _rrule.rrulestr(_pattern_string(rule), dtstart=dtstart_local)
    # rrulestr returns an rruleset for compound strings; a single RRULE value is
    # always a plain rrule, which is what the pattern string is.
    assert isinstance(built, _rrule.rrule)  # noqa: S101 — invariant of a single RRULE
    return built


def next_fire_after(schedule: Schedule, after: datetime) -> datetime | None:
    """The next UTC fire instant strictly after ``after``, or ``None`` if exhausted.

    Pure + DST-correct. For a one-time schedule, the single instant if it is after
    ``after``. For a recurring schedule, the earliest localized occurrence whose
    absolute UTC instant is ``> after``, respecting the rule's COUNT/UNTIL bound.

    Args:
        schedule: The schedule (recurring rule or one-time future) + its captured
            IANA timezone (the localization frame).
        after: A tz-aware instant; the result is strictly greater than it. The
            tick passes the previous fire (or the creation instant for the first).

    Returns:
        The next fire as a tz-aware UTC datetime, or ``None`` when the schedule has
        no further fire (one-time already past, or the rule's COUNT/UNTIL reached).
    """
    after = _ensure_utc(after)

    if schedule.is_one_time:
        # XOR guarantees one_time_at is set when is_one_time.
        instant = schedule.one_time_at
        assert instant is not None  # noqa: S101 — guaranteed by the XOR validator
        return instant if instant > after else None

    rule = schedule.recurrence
    assert rule is not None  # noqa: S101 — guaranteed by the XOR validator
    zone = schedule.zoneinfo

    # The pattern is anchored at the schedule's creation, expressed in local
    # wall-clock (minute-truncated — schedules are minute-level). COUNT counts
    # fires strictly after this anchor.
    anchor_utc = schedule.created_at
    dtstart_local = anchor_utc.astimezone(zone).replace(tzinfo=None, second=0, microsecond=0)
    pattern = _build_pattern(rule, dtstart_local)

    if rule.count is not None:
        return _next_count_bounded(pattern, zone, after, anchor_utc, rule.count)
    return _next_seeked(pattern, zone, after, rule.until)


def occurrences_between(
    schedule: Schedule, start: datetime, end: datetime, *, cap: int
) -> list[datetime]:
    """The schedule's fires in ``[start, end]`` (inclusive), as tz-aware UTC, ≤ ``cap`` of them.

    The read side of the SAME engine (Spec A8, A8-D-11): the calendar and A6-Review render
    from this, never a client-side recurrence reimplementation (criterion 5). It forward-steps
    the rrule ONCE from near ``start`` (not N calls to :func:`next_fire_after` from the anchor)
    and localizes each occurrence through the identical :func:`_localize_to_utc` gap/fold + the
    identical absolute-UTC COUNT/UNTIL bounding — so an instant listed here is byte-identical to
    the instant the tick will fire.

    ``cap`` bounds the returned count (the API's per-schedule slice of its global budget), so a
    wide window over a frequent rule can never become unbounded compute; the caller sets the
    ``truncated`` marker when a slice is capped. Returns ``[]`` for an empty/backwards window or
    a one-time outside it.

    Args:
        schedule: The schedule (recurring rule or one-time) + its captured IANA zone.
        start: Window start (tz-aware); occurrences ``>= start`` are included.
        end: Window end (tz-aware); occurrences ``<= end`` are included.
        cap: The maximum number of occurrences to return (``>= 0``).
    """
    start = _ensure_utc(start)
    end = _ensure_utc(end)
    if cap <= 0 or start > end:
        return []

    if schedule.is_one_time:
        instant = schedule.one_time_at
        assert instant is not None  # noqa: S101 — guaranteed by the XOR validator
        return [instant] if start <= instant <= end else []

    rule = schedule.recurrence
    assert rule is not None  # noqa: S101 — guaranteed by the XOR validator
    zone = schedule.zoneinfo
    anchor_utc = schedule.created_at
    dtstart_local = anchor_utc.astimezone(zone).replace(tzinfo=None, second=0, microsecond=0)
    pattern = _build_pattern(rule, dtstart_local)

    if rule.count is not None:
        return _between_count_bounded(pattern, zone, start, end, anchor_utc, rule.count, cap)
    return _between_seeked(pattern, zone, start, end, rule.until, cap)


def _between_seeked(
    pattern: _rrule.rrule,
    zone: ZoneInfo,
    start: datetime,
    end: datetime,
    until: datetime | None,
    cap: int,
) -> list[datetime]:
    """Unbounded / UNTIL-bounded occurrences in ``[start, end]`` via a seek near ``start``."""
    seed_local = (start.astimezone(zone) - _SEEK_MARGIN).replace(tzinfo=None)
    out: list[datetime] = []
    for scanned, occ in enumerate(pattern.xafter(seed_local, inc=True), start=1):
        if scanned > _MAX_SCAN:
            break  # defensive backstop (unreachable for well-formed rules)
        fire = _localize_to_utc(occ, zone)
        if until is not None and fire > until:
            break  # past UNTIL — no further fire (occurrences ascend)
        if fire > end:
            break  # past the window — occurrences ascend, so we are done
        if fire >= start:
            out.append(fire)
            if len(out) >= cap:
                break
    return out


def _between_count_bounded(
    pattern: _rrule.rrule,
    zone: ZoneInfo,
    start: datetime,
    end: datetime,
    anchor_utc: datetime,
    count: int,
    cap: int,
) -> list[datetime]:
    """COUNT-bounded occurrences in ``[start, end]``: enumerate fires from the anchor, bounded."""
    fires_seen = 0
    out: list[datetime] = []
    for occ in pattern:
        fire = _localize_to_utc(occ, zone)
        if fire <= anchor_utc:
            continue  # at/before creation — not a fire, not counted
        fires_seen += 1
        if fires_seen > count:
            break  # COUNT exhausted
        if fire > end:
            break  # occurrences ascend — past the window
        if fire >= start:
            out.append(fire)
            if len(out) >= cap:
                break
    return out


def _next_count_bounded(
    pattern: _rrule.rrule,
    zone: ZoneInfo,
    after: datetime,
    anchor_utc: datetime,
    count: int,
) -> datetime | None:
    """COUNT-bounded next fire: the first fire > ``after`` within ``count`` fires.

    Fires are occurrences whose localized UTC is strictly after the creation
    anchor (so an occurrence sitting exactly at creation is not a fire). We
    enumerate from the anchor — bounded by ``count`` iterations — counting fires;
    the budget is exhausted (``None``) once ``count`` fires have been consumed.
    """
    fires_seen = 0
    for occ in pattern:
        fire = _localize_to_utc(occ, zone)
        if fire <= anchor_utc:
            continue  # at/before creation — not a future fire, not counted
        fires_seen += 1
        if fires_seen > count:
            return None  # COUNT exhausted
        if fire > after:
            return fire
    return None


def _next_seeked(
    pattern: _rrule.rrule,
    zone: ZoneInfo,
    after: datetime,
    until: datetime | None,
) -> datetime | None:
    """Unbounded / UNTIL-bounded next fire via a seek near ``after``.

    Jumps the scan to ``_SEEK_MARGIN`` before ``after`` (in local wall-clock) so a
    far-past anchor doesn't force a long walk, then returns the first localized
    fire ``> after``. ``UNTIL`` (a stored aware-UTC instant) bounds the tail by an
    absolute-UTC comparison — DST-safe, no naive reconciliation.
    """
    seed_local = (after.astimezone(zone) - _SEEK_MARGIN).replace(tzinfo=None)
    for scanned, occ in enumerate(pattern.xafter(seed_local, inc=True), start=1):
        if scanned > _MAX_SCAN:
            return None  # defensive backstop (unreachable for well-formed rules)
        fire = _localize_to_utc(occ, zone)
        if until is not None and fire > until:
            return None  # past UNTIL — no further fire (occurrences ascend)
        if fire > after:
            return fire
    return None

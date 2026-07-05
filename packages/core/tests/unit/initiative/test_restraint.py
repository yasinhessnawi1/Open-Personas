"""Unit tests — restraint policy (Spec A5, T2; A5-D-3 + the two Phase-3 polarity pins)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from persona.initiative.config import InitiativeSettings
from persona.initiative.restraint import (
    CadenceCounts,
    DeliveryKind,
    cadence_exhausted,
    local_minute_of_day,
    meets_acceptance_floor,
    meets_value_threshold,
    resolve_delivery,
    why_now_lapsed,
)
from persona.schedules.quiet_hours import QuietHours

_SETTINGS = InitiativeSettings()
# 2026-07-04 21:30 UTC = 23:30 in Oslo (CEST, UTC+2) — inside a 22:00–07:00 quiet window.
_NOW = datetime(2026, 7, 4, 21, 30, tzinfo=UTC)
_OSLO = "Europe/Oslo"
# The wrapping window 22:00 → 07:00 local (start=1320, end=420) — the midnight-wrap case.
_NIGHT_QUIET = QuietHours(start_minute=22 * 60, end_minute=7 * 60)


def _counts(persona_day: int = 0, persona_week: int = 0, user_day: int = 0) -> CadenceCounts:
    return CadenceCounts(persona_day=persona_day, persona_week=persona_week, user_day=user_day)


class TestThresholds:
    def test_value_threshold_gates_below_and_admits_at(self) -> None:
        assert meets_value_threshold(_SETTINGS.value_threshold, _SETTINGS) is True
        assert meets_value_threshold(_SETTINGS.value_threshold - 0.01, _SETTINGS) is False

    def test_acceptance_floor_gates_below_and_admits_at(self) -> None:
        assert meets_acceptance_floor(_SETTINGS.acceptance_floor, _SETTINGS) is True
        assert meets_acceptance_floor(_SETTINGS.acceptance_floor - 0.01, _SETTINGS) is False

    def test_acceptance_is_a_separate_predicate_from_value(self) -> None:
        """The suppressor stays composable in T7's pinned order — never one blended score."""
        assert meets_value_threshold is not meets_acceptance_floor


class TestCadenceCaps:
    def test_all_below_caps_is_not_exhausted(self) -> None:
        assert cadence_exhausted(_counts(), _SETTINGS) is False

    def test_persona_day_cap_exhausts_at_the_ratified_one(self) -> None:
        assert cadence_exhausted(_counts(persona_day=1), _SETTINGS) is True

    def test_persona_week_cap_exhausts_at_the_ratified_three(self) -> None:
        assert cadence_exhausted(_counts(persona_week=3), _SETTINGS) is True

    def test_user_day_cap_is_the_pile_on_guard(self) -> None:
        """Two other personas already spoke today ⇒ this persona's candidate batches."""
        assert cadence_exhausted(_counts(user_day=2), _SETTINGS) is True

    def test_any_single_cap_suffices(self) -> None:
        below = _counts(persona_day=0, persona_week=2, user_day=1)
        at_week_cap = _counts(persona_day=0, persona_week=3, user_day=0)
        assert cadence_exhausted(below, _SETTINGS) is False
        assert cadence_exhausted(at_week_cap, _SETTINGS) is True


class TestResolveDelivery:
    def _resolve(self, **overrides: object) -> object:
        kwargs: dict[str, object] = {
            "interrupt_claimed": True,
            "deadline": _NOW + timedelta(hours=12),
            "now": _NOW,
            "timezone_name": _OSLO,
            "quiet": None,
            "settings": _SETTINGS,
        }
        kwargs.update(overrides)
        return resolve_delivery(**kwargs)  # type: ignore[arg-type]

    def test_batch_claim_batches(self) -> None:
        assert self._resolve(interrupt_claimed=False).kind is DeliveryKind.BATCH  # type: ignore[attr-defined]

    def test_interrupt_claim_without_deadline_is_rejected_to_batch(self) -> None:
        """The scan's urgency estimate is validated, never trusted."""
        assert self._resolve(deadline=None).kind is DeliveryKind.BATCH  # type: ignore[attr-defined]

    def test_deadline_beyond_horizon_batches(self) -> None:
        far = _NOW + timedelta(hours=_SETTINGS.interrupt_horizon_hours + 1)
        assert self._resolve(deadline=far).kind is DeliveryKind.BATCH  # type: ignore[attr-defined]

    def test_passed_deadline_batches(self) -> None:
        assert self._resolve(deadline=_NOW - timedelta(minutes=5)).kind is DeliveryKind.BATCH  # type: ignore[attr-defined]

    def test_valid_interrupt_outside_quiet_delivers_now(self) -> None:
        # 21:30 UTC = 23:30 Oslo; with NO quiet window set, a <48h deadline interrupts.
        resolution = self._resolve()
        assert resolution.kind is DeliveryKind.DELIVER_NOW  # type: ignore[attr-defined]

    def test_quiet_hours_are_absolute_even_when_the_deadline_will_pass(self) -> None:
        """THE Phase-3 polarity pin: the user's boundary outranks the notice.

        23:30 Oslo, quiet 22:00–07:00, deadline 02:30 Oslo (3h away — inside the
        48h horizon AND before the quiet window ends). The hold still wins; the
        release is the window's END (07:00 = minute 420), and the missed notice
        is the accepted, audited cost. Nobody "fixes" this into a 3am wake-up.
        """
        deadline_inside_window = _NOW + timedelta(hours=3)
        resolution = self._resolve(quiet=_NIGHT_QUIET, deadline=deadline_inside_window)
        assert resolution.kind is DeliveryKind.HOLD_TO_QUIET_EDGE  # type: ignore[attr-defined]
        assert resolution.hold_edge_minute == 7 * 60  # type: ignore[attr-defined]

    def test_hold_releases_at_the_window_end_never_the_nearest_edge(self) -> None:
        """At 23:30 the NEAREST boundary is 22:00 (90 min back); the hold must point
        FORWARD to 07:00 — the first non-quiet minute — not backwards at the start."""
        resolution = self._resolve(quiet=_NIGHT_QUIET)
        assert resolution.hold_edge_minute == _NIGHT_QUIET.end_minute  # type: ignore[attr-defined]

    def test_wrapping_window_pre_midnight_and_post_midnight_both_hold(self) -> None:
        """The midnight wrap exercised through A8's shared contains() semantics."""
        # 04:30 UTC = 06:30 Oslo — still inside 22:00–07:00 after the wrap.
        early = datetime(2026, 7, 5, 4, 30, tzinfo=UTC)
        resolution = resolve_delivery(
            interrupt_claimed=True,
            deadline=early + timedelta(hours=4),
            now=early,
            timezone_name=_OSLO,
            quiet=_NIGHT_QUIET,
            settings=_SETTINGS,
        )
        assert resolution.kind is DeliveryKind.HOLD_TO_QUIET_EDGE

    def test_just_after_window_end_delivers(self) -> None:
        # 05:30 UTC = 07:30 Oslo — one half-hour past the window end.
        after = datetime(2026, 7, 5, 5, 30, tzinfo=UTC)
        resolution = resolve_delivery(
            interrupt_claimed=True,
            deadline=after + timedelta(hours=4),
            now=after,
            timezone_name=_OSLO,
            quiet=_NIGHT_QUIET,
            settings=_SETTINGS,
        )
        assert resolution.kind is DeliveryKind.DELIVER_NOW


class TestLocalMinuteOfDay:
    def test_summer_offset_applies(self) -> None:
        # July: Oslo is UTC+2 → 06:00 UTC = 08:00 local.
        assert local_minute_of_day(datetime(2026, 7, 4, 6, 0, tzinfo=UTC), _OSLO) == 8 * 60

    def test_winter_offset_applies(self) -> None:
        # January: Oslo is UTC+1 → 06:00 UTC = 07:00 local (zoneinfo owns the DST arithmetic).
        assert local_minute_of_day(datetime(2026, 1, 15, 6, 0, tzinfo=UTC), _OSLO) == 7 * 60


class TestWhyNowLapsed:
    def test_passed_anchor_lapses(self) -> None:
        assert why_now_lapsed(anchor_time=_NOW - timedelta(minutes=1), now=_NOW) is True

    def test_future_anchor_holds(self) -> None:
        assert why_now_lapsed(anchor_time=_NOW + timedelta(hours=1), now=_NOW) is False

    def test_undated_anchor_never_lapses_by_time(self) -> None:
        """The named split: time-lapse is here; citations-still-resolve is T7's re-grounding."""
        assert why_now_lapsed(anchor_time=None, now=_NOW) is False


@pytest.mark.parametrize("bad_pair", [(10, 10)])
def test_quiet_hours_shared_definition_rejects_empty_window(bad_pair: tuple[int, int]) -> None:
    """A8's own validator holds — A5 adds zero window semantics (T2 bar 3)."""
    with pytest.raises(ValueError, match="empty"):
        QuietHours(start_minute=bad_pair[0], end_minute=bad_pair[1])

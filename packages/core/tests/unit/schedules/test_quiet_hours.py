"""A8 T5 — quiet-hours warn-plus-nearest-edge, off-until-set (A8-D-6).

Pure minutes-of-day arithmetic: containment (incl. midnight-wrapping windows) + the nearest-edge
advisory. Off-until-set is a property of the CALLER (a null window → no ``QuietHours`` → the
helper is never called); here we prove the window logic itself.
"""

from __future__ import annotations

import pytest
from persona.schedules import QuietHours, quiet_hours_edge


def _hm(h: int, m: int = 0) -> int:
    return h * 60 + m


def test_non_wrapping_window_contains() -> None:
    q = QuietHours(start_minute=_hm(22), end_minute=_hm(23))
    assert q.contains(_hm(22, 30)) is True
    assert q.contains(_hm(21, 59)) is False
    assert q.contains(_hm(23)) is False  # exclusive end


def test_wrapping_window_contains_across_midnight() -> None:
    q = QuietHours(start_minute=_hm(22), end_minute=_hm(7))  # 22:00 → 07:00
    assert q.contains(_hm(23)) is True
    assert q.contains(_hm(2)) is True  # after midnight, still quiet
    assert q.contains(_hm(6, 59)) is True
    assert q.contains(_hm(7)) is False  # exclusive end
    assert q.contains(_hm(12)) is False


def test_edge_returns_none_outside_quiet_hours() -> None:
    q = QuietHours(start_minute=_hm(22), end_minute=_hm(7))
    assert quiet_hours_edge(_hm(12), q) is None  # noon — schedule freely


def test_edge_offers_nearest_boundary_inside_quiet_hours() -> None:
    q = QuietHours(start_minute=_hm(22), end_minute=_hm(7))  # wraps
    # 06:00 is close to the 07:00 end (60 min) and far from the 22:00 start → offer 07:00.
    assert quiet_hours_edge(_hm(6), q) == _hm(7)
    # 23:00 is close to the 22:00 start (60 min) and far from 07:00 → offer 22:00.
    assert quiet_hours_edge(_hm(23), q) == _hm(22)


def test_edge_tie_prefers_the_end() -> None:
    q = QuietHours(start_minute=_hm(22), end_minute=_hm(0))  # 22:00 → 00:00 (2h)
    # 23:00 is 60 min from both 22:00 and 00:00 → tie → prefer the END (00:00) going forward.
    assert quiet_hours_edge(_hm(23), q) == _hm(0)


def test_empty_window_is_rejected() -> None:
    # An empty (start == end) window is invalid — "off" is a null window, not a zero-width one.
    with pytest.raises(ValueError, match="empty"):
        QuietHours(start_minute=_hm(8), end_minute=_hm(8))

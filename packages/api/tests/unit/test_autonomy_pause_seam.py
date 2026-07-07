"""The per-owner pause injectable seam (A6-D-8, frozen): the default no-op + its shape.

Pure logic — no DB. The DB-backed real predicate + its self-scoping teeth live in the
integration suite (``test_kill_switch.py``). This pins the *seam*: A7/A10 wire their origination
call site against ``never_paused`` (green today), and A6 swaps in the real check at composition.
"""

from __future__ import annotations

from persona_api.approvals import AutonomyPauseCheck, never_paused


def test_never_paused_default_is_a_noop() -> None:
    assert never_paused("any_owner") is False
    assert never_paused("") is False


def test_never_paused_satisfies_the_autonomy_pause_check_type() -> None:
    # The seam is an owner_id -> bool callable; a handler defaulting to it must accept the real
    # check (same shape) without any change at the call site.
    check: AutonomyPauseCheck = never_paused
    assert check("owner_x") is False

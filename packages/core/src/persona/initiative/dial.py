"""The per-persona initiative dial (Spec A5, T1; A5-D-5).

Three settings, monotone in autonomy: ``OFF`` silences the scan entirely (the
handler exits — the schedule stays, one source of truth), ``PROPOSE_ONLY``
converts every would-be-act into a proposal at the envelope step, and
``ACT_WITHIN_ENVELOPE`` lets all-safe plans execute as act-then-report.

The default is **PROPOSE_ONLY** (ratified at the Phase-1 gate): a new persona
acting unprompted before trust exists is the fastest way to lose the
capability; act-within-envelope is the earned setting, one toggle away. The
dial moves by the user's hand only — never by success statistics (spec §2
out-of-scope; A3-D-X-defer-trust-automation's posture).
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["DEFAULT_INITIATIVE_DIAL", "InitiativeDial"]


class InitiativeDial(StrEnum):
    """Per-persona initiative setting (A5-D-5). Stored on the persona row."""

    OFF = "off"
    PROPOSE_ONLY = "propose_only"
    ACT_WITHIN_ENVELOPE = "act_within_envelope"


#: The ratified default (Phase-1 gate): propose-only earns the upgrade.
DEFAULT_INITIATIVE_DIAL = InitiativeDial.PROPOSE_ONLY

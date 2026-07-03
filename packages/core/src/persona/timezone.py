"""Per-user timezone resolution + IANA validation (Spec A8, A8-D-9).

A8 realises the K6 seam A4 named: a per-user timezone (``users.timezone``, an
IANA name) that falls back to the config default (``PERSONA_DEFAULT_TIMEZONE``)
when unset. This module is the single home for the IANA-zone validity check plus
the resolve-with-fallback rule, shared by:

- the profile request schema — **validate at the write boundary** (a bad zone is
  a fail-fast 422, never stored to mis-fire later); and
- the origination timezone provider — **resolve at read time** (an unset/blank
  value falls back to the config default).

A8-D-9: the resolved zone governs the DEFAULT captured zone for NEW schedules and
the RENDERING default for echoes/calendar — it NEVER silently re-anchors an
existing schedule's captured zone (that stays stable until an explicit reschedule,
per D-A1-4). This module only computes the effective zone; it does not touch any
existing schedule row.

Pure + dependency-free (stdlib ``zoneinfo`` only), so it unit-tests with zero
infrastructure exactly like ``next_fire_after``. Consolidates the inline IANA
check the config default validator and the ``Schedule`` captured-zone validator
each carried.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from persona.errors import InvalidTimezoneError

__all__ = ["is_valid_timezone", "resolve_timezone", "validate_timezone"]


def is_valid_timezone(name: str) -> bool:
    """Whether ``name`` is a resolvable IANA zone (e.g. ``Europe/Oslo``)."""
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def validate_timezone(name: str) -> str:
    """Return ``name`` if it is a valid IANA zone, else raise :class:`InvalidTimezoneError`.

    The fail-fast write-boundary check (the profile PATCH, a reschedule tz): a bad
    zone is rejected (→ 422 at the API edge) rather than stored to mis-fire later —
    the same posture as the config default's startup validator and the ``Schedule``
    model's captured-zone validator, consolidated here.
    """
    if not is_valid_timezone(name):
        raise InvalidTimezoneError(
            "invalid timezone (must be an IANA zone)", context={"timezone": name}
        )
    return name


def resolve_timezone(stored: str | None, *, default: str) -> str:
    """Resolve the effective timezone: the user's stored zone, else the config default.

    The read-time rule (A8-D-9): a user with ``users.timezone`` set gets it; an
    unset (``None``) or blank value falls back to ``default``
    (``PERSONA_DEFAULT_TIMEZONE``). Defensively, a stored-but-invalid value (a
    hand-edited row, or a zone dropped by a tzdata update) also falls back to
    ``default`` rather than crashing the origination path — write-time validation
    makes this rare, and a fail-soft read is safer than a raised error mid-turn.
    """
    if stored and is_valid_timezone(stored):
        return stored
    return default

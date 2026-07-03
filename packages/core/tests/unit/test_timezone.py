"""Unit tests for per-user timezone resolution + IANA validation (Spec A8, A8-D-9).

Pure, zero-infrastructure — the resolve-with-fallback rule and the write-boundary
validity check that the profile PATCH and the origination provider both depend on.
"""

from __future__ import annotations

import pytest
from persona.errors import InvalidTimezoneError
from persona.timezone import is_valid_timezone, resolve_timezone, validate_timezone


@pytest.mark.parametrize("name", ["Europe/Oslo", "America/New_York", "Australia/Sydney", "UTC"])
def test_is_valid_timezone_accepts_iana_zones(name: str) -> None:
    assert is_valid_timezone(name) is True


@pytest.mark.parametrize("name", ["", "Mars/Phobos", "not a zone", "GMT+9", "Europe/Nowhere"])
def test_is_valid_timezone_rejects_non_iana(name: str) -> None:
    assert is_valid_timezone(name) is False


def test_validate_timezone_returns_the_zone_when_valid() -> None:
    assert validate_timezone("Europe/Oslo") == "Europe/Oslo"


def test_validate_timezone_raises_invalid_timezone_error_with_context() -> None:
    with pytest.raises(InvalidTimezoneError) as excinfo:
        validate_timezone("Mars/Phobos")
    assert excinfo.value.context["timezone"] == "Mars/Phobos"


def test_resolve_timezone_returns_stored_when_set_and_valid() -> None:
    assert resolve_timezone("Europe/Oslo", default="UTC") == "Europe/Oslo"


def test_resolve_timezone_falls_back_to_default_when_unset() -> None:
    # NULL users.timezone → the config default (the primary A8-D-9 fallback path).
    assert resolve_timezone(None, default="Europe/Oslo") == "Europe/Oslo"


def test_resolve_timezone_falls_back_to_default_when_blank() -> None:
    assert resolve_timezone("", default="Europe/Oslo") == "Europe/Oslo"


def test_resolve_timezone_falls_back_soft_when_stored_value_is_invalid() -> None:
    # A hand-edited row / a zone dropped by a tzdata update → fail-soft to the
    # default rather than crashing the origination path (validation is at write time).
    assert resolve_timezone("Mars/Phobos", default="Europe/Oslo") == "Europe/Oslo"

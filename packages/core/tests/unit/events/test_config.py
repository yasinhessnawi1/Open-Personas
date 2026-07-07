"""Unit tests for EventTriggerSettings (Spec A7, A7-D-4)."""

from __future__ import annotations

import pytest
from persona.events import EventTriggerSettings
from pydantic import ValidationError


def test_defaults_are_the_ratified_operating_point() -> None:
    settings = EventTriggerSettings()
    assert settings.enabled is False  # the criterion-9 feature gate
    assert settings.cooldown_seconds == 300
    assert settings.max_chain_depth == 3
    assert settings.per_owner_max_fires_per_hour == 60
    assert settings.fire_cost_estimate == 1


def test_env_overrides_are_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSONA_EVENT_TRIGGERS_ENABLED", "true")
    monkeypatch.setenv("PERSONA_EVENT_TRIGGERS_COOLDOWN_SECONDS", "60")
    monkeypatch.setenv("PERSONA_EVENT_TRIGGERS_MAX_CHAIN_DEPTH", "5")
    settings = EventTriggerSettings()
    assert settings.enabled is True
    assert settings.cooldown_seconds == 60
    assert settings.max_chain_depth == 5


def test_bounds_are_enforced() -> None:
    with pytest.raises(ValidationError):
        EventTriggerSettings(cooldown_seconds=0)
    with pytest.raises(ValidationError):
        EventTriggerSettings(max_chain_depth=0)
    with pytest.raises(ValidationError):
        EventTriggerSettings(per_owner_max_fires_per_hour=0)
    with pytest.raises(ValidationError):
        EventTriggerSettings(fire_cost_estimate=-1)


def test_fire_cost_estimate_may_be_zero_to_disable_day_cap_precheck() -> None:
    assert EventTriggerSettings(fire_cost_estimate=0).fire_cost_estimate == 0

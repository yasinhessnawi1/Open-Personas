"""Unit tests — initiative settings + dial (Spec A5, T1; A5-D-5, A5-D-X-thresholds-config)."""

from __future__ import annotations

import pytest
from persona.initiative import DEFAULT_INITIATIVE_DIAL, InitiativeDial, InitiativeSettings
from pydantic import ValidationError


class TestInitiativeSettings:
    def test_disabled_by_default(self) -> None:
        """The criterion-9 gate: initiative does not enable by default."""
        assert InitiativeSettings().enabled is False

    def test_defaults_are_the_ratified_numbers(self) -> None:
        s = InitiativeSettings()
        assert s.scan_hour == 7
        assert s.scan_tier == "small"
        assert s.scan_candidate_cap == 3
        assert s.recent_nodes_limit == 30
        assert s.daily_cap_per_persona == 1
        assert s.weekly_cap_per_persona == 3
        assert s.daily_cap_per_user == 2
        assert s.interrupt_horizon_hours == 48

    def test_env_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PERSONA_INITIATIVE_ENABLED", "true")
        monkeypatch.setenv("PERSONA_INITIATIVE_VALUE_THRESHOLD", "0.75")
        monkeypatch.setenv("PERSONA_INITIATIVE_DAILY_CAP_PER_PERSONA", "2")
        s = InitiativeSettings()
        assert s.enabled is True
        assert s.value_threshold == 0.75
        assert s.daily_cap_per_persona == 2

    def test_scan_hour_bounded(self) -> None:
        with pytest.raises(ValidationError):
            InitiativeSettings(scan_hour=24)

    def test_weekly_cap_below_daily_cap_fails_fast(self) -> None:
        with pytest.raises(ValidationError):
            InitiativeSettings(daily_cap_per_persona=3, weekly_cap_per_persona=2)

    @pytest.mark.parametrize("field", ["value_threshold", "acceptance_floor"])
    def test_thresholds_bounded_to_unit_interval(self, field: str) -> None:
        with pytest.raises(ValidationError):
            InitiativeSettings(**{field: 1.5})


class TestInitiativeDial:
    def test_default_is_propose_only(self) -> None:
        """A5-D-5, ratified: the conservative lean is the default."""
        assert DEFAULT_INITIATIVE_DIAL is InitiativeDial.PROPOSE_ONLY

    def test_the_three_settings(self) -> None:
        assert {d.value for d in InitiativeDial} == {"off", "propose_only", "act_within_envelope"}

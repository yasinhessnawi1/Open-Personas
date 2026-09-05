"""The per-turn first-audio bound is configured, defaulted and validated (R9-124)."""

from __future__ import annotations

import pytest
from persona_voice.config import VoiceConfig
from pydantic import ValidationError


def test_the_default_is_twenty_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PERSONA_VOICE_TURN_FIRST_AUDIO_TIMEOUT_S", raising=False)
    assert VoiceConfig().turn_first_audio_timeout_s == 20.0


def test_the_env_var_overrides_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSONA_VOICE_TURN_FIRST_AUDIO_TIMEOUT_S", "7.5")
    assert VoiceConfig().turn_first_audio_timeout_s == 7.5


def test_a_non_positive_bound_is_refused_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero would cut every turn before it starts; the config fails fast instead."""
    monkeypatch.setenv("PERSONA_VOICE_TURN_FIRST_AUDIO_TIMEOUT_S", "0")
    with pytest.raises(ValidationError):
        VoiceConfig()

"""The voice graph pool's worker count is configured, defaulted and validated (R9-323)."""

from __future__ import annotations

import pytest
from persona_voice.config import VoiceConfig
from pydantic import ValidationError

_ENV = "PERSONA_VOICE_GRAPH_QUERY_WORKERS"


def test_the_default_is_two_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_ENV, raising=False)
    assert VoiceConfig().graph_query_workers == 2


def test_the_env_var_overrides_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV, "4")
    assert VoiceConfig().graph_query_workers == 4


@pytest.mark.parametrize("value", ["0", "-1", "65"])
def test_an_out_of_range_count_is_refused_at_startup(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Zero workers would never run a query; a huge pool defeats the bound."""
    monkeypatch.setenv(_ENV, value)
    with pytest.raises(ValidationError):
        VoiceConfig()

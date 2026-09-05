"""Retired models are dropped from a chain at startup, loudly (R9-124).

NVIDIA's ``meta/llama-3.3-70b-instruct`` reached end of life on 2026-08-26 and sat
second in the deployed mid chain of all three apps for ten days. The classifier fix
walks past such a slot at call time; this filter removes it before any call is made, so
a stale env var costs one WARN line instead of a doomed round trip per turn.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.backends import retired
from persona.backends.retired import RETIRED_MODEL_IDS, filter_retired_models

if TYPE_CHECKING:
    import pytest

_DEAD = ("nvidia", "meta/llama-3.3-70b-instruct")
_LIVE_A = ("groq", "openai/gpt-oss-120b")
_LIVE_B = ("anthropic", "claude-sonnet-4-6")


def test_the_known_retirements_are_recorded() -> None:
    """The record is the point: an operator reading an old env var can tell a typo
    from a retirement."""
    assert "nvidia/meta/llama-3.3-70b-instruct" in RETIRED_MODEL_IDS
    assert "groq/llama-3.3-70b-versatile" in RETIRED_MODEL_IDS


def test_a_retired_slot_is_dropped_and_the_rest_keep_their_order() -> None:
    assert filter_retired_models([_LIVE_A, _DEAD, _LIVE_B], tier_name="mid") == [
        _LIVE_A,
        _LIVE_B,
    ]


def test_a_chain_without_retirements_passes_through_untouched() -> None:
    chain = [_LIVE_A, _LIVE_B]
    assert filter_retired_models(chain, tier_name="mid") == chain


def test_a_chain_made_only_of_retired_models_comes_back_empty() -> None:
    """The caller (the tier registry) decides what an empty chain means; the filter
    never invents a replacement."""
    assert filter_retired_models([_DEAD], tier_name="mid") == []


def test_the_drop_is_announced(monkeypatch: pytest.MonkeyPatch) -> None:
    """Silent removal would be the same failure pointing the other way: the operator
    must be able to see from the startup log why a configured slot is not serving."""
    lines: list[str] = []

    class _Recorder:
        def warning(self, template: str, **kw: object) -> None:
            lines.append(template + " " + " ".join(f"{k}={v}" for k, v in kw.items()))

        def __getattr__(self, _name: str) -> object:
            return lambda *_a, **_kw: None

    monkeypatch.setattr(retired, "_LOG", _Recorder())
    filter_retired_models([_DEAD], tier_name="mid")
    joined = " ".join(lines)
    assert "meta/llama-3.3-70b-instruct" in joined
    assert "tier=mid" in joined

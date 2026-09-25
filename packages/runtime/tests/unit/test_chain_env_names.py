"""``CHAIN_ENV_NAMES`` is exactly the set of model-chain settings the builders read (R9-213).

Each hosted process reads its own copy of every model list, so a list of "the chain
settings" is only worth anything if it cannot drift from what the code actually reads.
These tests prove it both ways: the constant equals the six names, and building the paid
and free registries from an environment holding ONLY those six names (plus one provider
key) serves each tier from its own chain, while removing any one of them removes exactly
its tier.

"Only those six" is enforced, not assumed: the fixture clears every other chain-shaped
setting (the per-tier triplets, the ``PERSONA_MODEL`` default and the dev cheap-tier
switch), because the paid builder falls back to that default for every tier when no
MODELS list is read, and a test that registered tiers through the fallback would pass on
a broken read.
"""

from __future__ import annotations

import os
import re

import pytest
from persona_runtime.tier import (
    CHAIN_ENV_NAMES,
    free_tier_registry_from_env,
    tier_registry_from_env,
)

_SIX = (
    "PERSONA_FRONTIER_MODELS",
    "PERSONA_MID_MODELS",
    "PERSONA_SMALL_MODELS",
    "PERSONA_FREE_FRONTIER_MODELS",
    "PERSONA_FREE_MID_MODELS",
    "PERSONA_FREE_SMALL_MODELS",
)

_PAID = {
    "PERSONA_FRONTIER_MODELS": "frontier",
    "PERSONA_MID_MODELS": "mid",
    "PERSONA_SMALL_MODELS": "small",
}
_FREE = {
    "PERSONA_FREE_FRONTIER_MODELS": "frontier",
    "PERSONA_FREE_MID_MODELS": "mid",
    "PERSONA_FREE_SMALL_MODELS": "small",
}

#: Every setting that can put a model behind a tier other than the six chains themselves.
_CHAIN_SHAPED = re.compile(
    r"^PERSONA_((FREE_)?(FRONTIER|MID|SMALL)_\w+|MODEL|PROVIDER|API_KEY|BASE_URL"
    r"|DEV_CHEAP_TIERS|OPENROUTER_SUBSCRIPTION_MODE)$"
)


@pytest.fixture(autouse=True)
def _only_the_chain_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No inherited chain-shaped env at all, then one dummy key (no network at construction)."""
    for name in list(os.environ):
        if _CHAIN_SHAPED.match(name):
            monkeypatch.delenv(name)
    monkeypatch.setenv("PERSONA_OPENROUTER_API_KEY", "dummy-openrouter-key")


def _model_for(name: str) -> str:
    """The model id the chain named ``name`` carries in these tests (after ``openrouter/``)."""
    return f"vendor/{name.lower()}:free"


def _set_all(monkeypatch: pytest.MonkeyPatch, *, except_name: str | None = None) -> None:
    for name in CHAIN_ENV_NAMES:
        if name != except_name:
            monkeypatch.setenv(name, f"openrouter/{_model_for(name)}")


def test_chain_env_names_is_exactly_the_six_chain_settings() -> None:
    assert CHAIN_ENV_NAMES == _SIX


def test_each_tier_is_served_from_its_own_chain_when_only_the_six_are_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_all(monkeypatch)
    paid = tier_registry_from_env()
    free = free_tier_registry_from_env()
    assert paid.configured_tier_names == ("frontier", "mid", "small")
    assert free.configured_tier_names == ("frontier", "mid", "small")
    assert {tier: paid.model_name_for(tier) for tier in _PAID.values()} == {
        tier: _model_for(name) for name, tier in _PAID.items()
    }
    assert {tier: free.model_name_for(tier) for tier in _FREE.values()} == {
        tier: _model_for(name) for name, tier in _FREE.items()
    }


@pytest.mark.parametrize("name", list(_PAID))
def test_removing_one_paid_name_removes_exactly_its_paid_tier(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    _set_all(monkeypatch, except_name=name)
    expected = tuple(tier for n, tier in _PAID.items() if n != name)
    assert tier_registry_from_env().configured_tier_names == expected
    assert free_tier_registry_from_env().configured_tier_names == ("frontier", "mid", "small")


@pytest.mark.parametrize("name", list(_FREE))
def test_removing_one_free_name_removes_exactly_its_free_tier(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    _set_all(monkeypatch, except_name=name)
    expected = tuple(tier for n, tier in _FREE.items() if n != name)
    assert free_tier_registry_from_env().configured_tier_names == expected
    assert tier_registry_from_env().configured_tier_names == ("frontier", "mid", "small")

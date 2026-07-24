"""The free-tier no-paid-fallback registry (Spec M4, T5a) — the load-bearing safety.

``free_tier_registry_from_env`` builds a SEPARATE, free-only :class:`TierRegistry` from
``PERSONA_FREE_<TIER>_MODELS``. A free user's whole tier chain comes from here, so the
fallback walk can NEVER reach a paid model. Real construction (no mocks): the backends
are built with dummy keys (no network at construction), and the assertions inspect the
CONCRETE resolved ``(provider, model)`` chain — **these tests FAIL if any paid provider
appears in a free tier's chain**. Fail-closed: an unconfigured free tier is ABSENT with
NO paid default, so ``get`` raises rather than serving a paid model.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from persona.backends.multi_model import MultiModelChatBackend
from persona_runtime.errors import TierNotConfiguredError
from persona_runtime.tier import free_tier_registry_from_env

if TYPE_CHECKING:
    from persona_runtime.tier import TierRegistry

# Providers that MUST NOT appear in any free tier's chain (the paid direct providers).
_PAID_PROVIDERS = {"anthropic", "openai"}


def _chain(registry: TierRegistry, tier: str) -> list[tuple[str, str]]:
    """The CONCRETE resolved ``(provider, model)`` chain for a tier (the real backends)."""
    backend = registry.get(tier)
    if isinstance(backend, MultiModelChatBackend):
        return [(b.provider_name, b.model_name) for b in backend.backends]
    return [(backend.provider_name, backend.model_name)]


@pytest.fixture(autouse=True)
def _dummy_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dummy provider keys so backends construct (no network fires at construction)."""
    monkeypatch.setenv("PERSONA_OPENROUTER_API_KEY", "sk-or-dummy")
    monkeypatch.setenv("PERSONA_ANTHROPIC_API_KEY", "sk-ant-dummy")
    monkeypatch.setenv("PERSONA_OPENAI_API_KEY", "sk-oai-dummy")
    # Clean any inherited tier env so each test controls the config exactly.
    for var in (
        "PERSONA_FREE_FRONTIER_MODELS",
        "PERSONA_FREE_MID_MODELS",
        "PERSONA_FREE_SMALL_MODELS",
        "PERSONA_FRONTIER_MODELS",
        "PERSONA_MID_MODELS",
        "PERSONA_SMALL_MODELS",
    ):
        monkeypatch.delenv(var, raising=False)


def test_free_registry_chain_is_free_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "PERSONA_FREE_FRONTIER_MODELS",
        "openrouter/deepseek/deepseek-chat:free,openrouter/meta-llama/llama-3-8b:free",
    )
    registry = free_tier_registry_from_env()
    chain = _chain(registry, "frontier")

    providers = {p for p, _ in chain}
    assert providers == {"openrouter"}  # ONLY the free provider in the whole chain
    assert providers.isdisjoint(_PAID_PROVIDERS)  # zero paid providers reachable
    assert [m for _, m in chain] == [
        "deepseek/deepseek-chat:free",
        "meta-llama/llama-3-8b:free",
    ]


def test_free_registry_ignores_the_paid_tier_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The paid ``PERSONA_FRONTIER_MODELS`` (anthropic/openai) NEVER leaks into the free chain."""
    monkeypatch.setenv("PERSONA_FRONTIER_MODELS", "anthropic/claude-sonnet-4-6,openai/gpt-4o")
    monkeypatch.setenv(
        "PERSONA_FREE_FRONTIER_MODELS",
        "openrouter/a:free,openrouter/b:free",
    )
    registry = free_tier_registry_from_env()
    chain = _chain(registry, "frontier")

    assert {p for p, _ in chain} == {"openrouter"}
    assert not any(p in _PAID_PROVIDERS for p, _ in chain)  # anthropic/openai absent


def test_unconfigured_free_registry_is_empty_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``PERSONA_FREE_*_MODELS`` → EMPTY registry → get raises (NO paid default)."""
    # Even a configured PAID tier env must NOT be used as a fallback for the free registry.
    monkeypatch.setenv("PERSONA_FRONTIER_MODELS", "anthropic/claude-sonnet-4-6")
    registry = free_tier_registry_from_env()

    assert registry.configured_tier_names == ()
    with pytest.raises(TierNotConfiguredError):
        registry.get("frontier")  # a free user's turn fails closed → graceful T5b, never paid


def test_free_small_falls_back_within_free_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only free mid configured → small + frontier fall back to the free mid (all free)."""
    monkeypatch.setenv(
        "PERSONA_FREE_MID_MODELS",
        "openrouter/mid-a:free,openrouter/mid-b:free",
    )
    registry = free_tier_registry_from_env()

    for tier in ("small", "frontier", "mid"):
        providers = {p for p, _ in _chain(registry, tier)}
        assert providers == {"openrouter"}  # every tier resolves to the free mid chain
        assert providers.isdisjoint(_PAID_PROVIDERS)


def test_malformed_free_tier_is_absent_not_a_boot_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed free tier is skipped (fail-closed), not a hard boot crash."""
    monkeypatch.setenv("PERSONA_FREE_FRONTIER_MODELS", "not-a-valid-slot")  # no provider/model
    monkeypatch.setenv("PERSONA_FREE_MID_MODELS", "openrouter/ok-a:free,openrouter/ok-b:free")
    registry = free_tier_registry_from_env()

    # The bad frontier is absent; mid built fine → frontier falls back to the (free) mid.
    assert "frontier" not in registry.configured_tier_names
    assert {p for p, _ in _chain(registry, "frontier")} == {"openrouter"}

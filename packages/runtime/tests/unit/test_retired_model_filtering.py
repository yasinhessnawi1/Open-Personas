"""A retired model configured in a chain is dropped when the registry is built (R9-124).

The exact mid chain live on all three Fly apps on 2026-09-05 carried NVIDIA's
``meta/llama-3.3-70b-instruct`` ten days after its end of life. Every turn that reached
that slot paid a doomed round trip. The registry now drops such a slot at startup with a
WARN, so the chain the process runs is the chain that can answer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from persona.backends.multi_model import MultiModelChatBackend
from persona_runtime.tier import free_tier_registry_from_env, tier_registry_from_env

if TYPE_CHECKING:
    from persona_runtime.tier import TierRegistry

_ALL_TIER_VARS = (
    "PERSONA_FREE_FRONTIER_MODELS",
    "PERSONA_FREE_MID_MODELS",
    "PERSONA_FREE_SMALL_MODELS",
    "PERSONA_FRONTIER_MODELS",
    "PERSONA_MID_MODELS",
    "PERSONA_SMALL_MODELS",
)


def _chain(registry: TierRegistry, tier: str) -> list[tuple[str, str]]:
    """The CONCRETE resolved ``(provider, model)`` chain for a tier (the real backends)."""
    backend = registry.get(tier)
    if isinstance(backend, MultiModelChatBackend):
        return [(b.provider_name, b.model_name) for b in backend.backends]
    return [(backend.provider_name, backend.model_name)]


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dummy provider keys so backends construct (nothing connects at construction)."""
    for var, value in (
        ("PERSONA_NVIDIA_API_KEY", "nvapi-dummy"),
        ("PERSONA_ANTHROPIC_API_KEY", "sk-ant-dummy"),
        ("PERSONA_GROQ_API_KEY", "gsk-dummy"),
        ("PERSONA_OPENROUTER_API_KEY", "sk-or-dummy"),
    ):
        monkeypatch.setenv(var, value)
    for var in _ALL_TIER_VARS:
        monkeypatch.delenv(var, raising=False)


def test_the_deployed_chain_loses_only_its_dead_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "PERSONA_MID_MODELS",
        "groq/openai/gpt-oss-120b,nvidia/meta/llama-3.3-70b-instruct,anthropic/claude-sonnet-4-6",
    )
    assert _chain(tier_registry_from_env(), "mid") == [
        ("groq", "openai/gpt-oss-120b"),
        ("anthropic", "claude-sonnet-4-6"),
    ]


def test_the_free_registry_drops_a_retired_slot_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "PERSONA_FREE_MID_MODELS",
        "openrouter/openai/gpt-oss-20b:free,groq/llama-3.3-70b-versatile",
    )
    assert _chain(free_tier_registry_from_env(), "mid") == [
        ("openrouter", "openai/gpt-oss-20b:free")
    ]

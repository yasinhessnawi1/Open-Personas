"""Composition-root proof for R9-020 — titles off small (the recognition-tier shape).

``RuntimeFactory.build_title`` resolves the TITLE tier — ``tier_for("title")``
(mid by default) with the ``PERSONA_API_TITLE_TIER`` config override — never the
background/small tier that echoed the titling instruction (the R4 first-words-
fallback root). Proven at the composition root: the registry records which tier
was asked for, so "never small" is a recorded fact, not an inference. Plus the
config field itself: env-read, default ``mid``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from persona_api.config import APIConfig
from persona_api.services.runtime_factory import RuntimeFactory


class _TitleBackend:
    """Records the prompt; returns a canned title (duck-typed ChatBackend)."""

    def __init__(self, tier: str) -> None:
        self.tier = tier
        self.calls: list[list[Any]] = []

    async def chat(self, messages: list[Any], **kwargs: Any) -> Any:  # noqa: ANN401, ARG002
        self.calls.append(messages)
        return SimpleNamespace(content=f"Title from {self.tier}")


class _RecordingRegistry:
    """Serves per-tier backends and records every tier asked for."""

    def __init__(self, *tiers: str) -> None:
        self.backends = {t: _TitleBackend(t) for t in tiers}
        self.requested: list[str] = []

    def get(self, name: str) -> _TitleBackend:
        self.requested.append(name)
        return self.backends[name]


def _factory(registry: object, *, api_config: object | None = None) -> RuntimeFactory:
    return RuntimeFactory(
        rls_engine=object(),  # type: ignore[arg-type] — never touched at composition time
        embedder=SimpleNamespace(model_name="fake", dimension=384),  # type: ignore[arg-type]
        tier_registry=registry,  # type: ignore[arg-type]
        turn_log_writer=object(),  # type: ignore[arg-type]
        audit_root=Path("/tmp/persona-audit-test"),
        api_config=api_config,  # type: ignore[arg-type]
    )


class TestTitleTierComposition:
    def test_build_title_asks_for_mid_by_default(self) -> None:
        registry = _RecordingRegistry("frontier", "mid", "small")
        title = asyncio.run(_factory(registry).build_title("help with my lease"))
        assert registry.requested == ["mid"]  # never small — the R9-020 contract
        assert title == "Title from mid"

    def test_env_override_composes_the_frontier_backend(self) -> None:
        registry = _RecordingRegistry("frontier", "mid", "small")
        api_config = SimpleNamespace(title_tier="frontier")
        title = asyncio.run(
            _factory(registry, api_config=api_config).build_title("help with my lease")
        )
        assert registry.requested == ["frontier"]
        assert title == "Title from frontier"

    def test_config_default_mid_is_also_an_explicit_override(self) -> None:
        # With a real APIConfig the override is ALWAYS passed (default mid) —
        # exactly the recognition posture; the policy row is the no-config path.
        registry = _RecordingRegistry("frontier", "mid", "small")
        asyncio.run(
            _factory(registry, api_config=SimpleNamespace(title_tier="mid")).build_title("x")
        )
        assert registry.requested == ["mid"]

    def test_first_message_reaches_the_prompt(self) -> None:
        registry = _RecordingRegistry("mid")
        asyncio.run(_factory(registry).build_title("my exact first message"))
        (prompt,) = registry.backends["mid"].calls
        assert prompt[-1].content == "my exact first message"


class TestTitleTierConfigField:
    def test_default_is_mid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PERSONA_API_TITLE_TIER", raising=False)
        assert APIConfig().title_tier == "mid"

    def test_env_var_is_respected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PERSONA_API_TITLE_TIER", "frontier")
        assert APIConfig().title_tier == "frontier"

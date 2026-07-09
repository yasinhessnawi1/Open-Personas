"""Tests for ``persona.backends.openrouter_passthrough`` (Spec M1 T2).

Covers the OpenRouter passthrough backend builder: fail-open when no key is
configured, process-caching per model id, and fail-open on a construction
error — never raising, since a bad ``preferred_model`` choice must not break
a turn (T4 injects this into the agentic loop's model-selection hook).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.backends.openrouter_passthrough import (
    OPENROUTER_KEY_ENV,
    build_openrouter_passthrough,
)

if TYPE_CHECKING:
    import pytest


def test_passthrough_returns_none_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(OPENROUTER_KEY_ENV, raising=False)
    build_openrouter_passthrough.cache_clear()
    assert build_openrouter_passthrough("z-ai/glm-4.6") is None


def test_passthrough_builds_and_caches_per_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(OPENROUTER_KEY_ENV, "test-key-not-real")
    build_openrouter_passthrough.cache_clear()
    b1 = build_openrouter_passthrough("z-ai/glm-4.6")
    b2 = build_openrouter_passthrough("z-ai/glm-4.6")
    b3 = build_openrouter_passthrough("deepseek/deepseek-chat")
    assert b1 is not None  # cached
    assert b1 is b2  # cached
    assert b3 is not None  # per-id
    assert b3 is not b1  # per-id
    assert b1.provider_name == "openrouter"
    assert b1.model_name == "z-ai/glm-4.6"


def test_passthrough_construction_failure_is_none_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(OPENROUTER_KEY_ENV, "test-key-not-real")
    build_openrouter_passthrough.cache_clear()
    # A blank id (whitespace-only after strip) is rejected before construction
    # is even attempted → None, logged; never raises.
    assert build_openrouter_passthrough(" ") is None


def test_passthrough_factory_exception_is_none_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The construction try/except itself: a factory raise → None (fail-open), never propagates."""
    monkeypatch.setenv(OPENROUTER_KEY_ENV, "test-key-not-real")

    def _boom(config):  # noqa: ANN001, ANN202, ARG001 — signature mirrors load_backend
        msg = "provider exploded at construction"
        raise RuntimeError(msg)

    import persona.backends._factory

    monkeypatch.setattr(persona.backends._factory, "load_backend", _boom, raising=True)
    build_openrouter_passthrough.cache_clear()
    assert build_openrouter_passthrough("z-ai/glm-4.6") is None

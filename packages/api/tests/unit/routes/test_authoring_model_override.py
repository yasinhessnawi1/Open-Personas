"""The PERSONA_AUTHORING_MODEL override (Spec M3, T2c — D-M3-9).

When set + resolvable, authoring (draft) runs on that specific model (Sonnet 5)
WITHOUT repointing the frontier tier that chat routing shares. Unset, malformed,
unknown-provider, or keyless → fail-soft to the frontier-tier backend; authoring
never 500s. Billing is unaffected (it prices the actual served model).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from persona_api.routes.personas import (
    _authoring_backend,
    _build_authoring_override_backend,
)


class _TierBackend:
    """A stand-in frontier-tier backend the fallback path returns."""

    provider_name = "frontier-stub"
    model_name = "frontier-stub-model"


def _fake_request(*, authoring_model: str | None, tier_backend: object | None = None) -> object:
    """A minimal request exposing the app.state surface ``_authoring_backend`` reads."""

    class _Registry:
        def get(self, _tier: str) -> object:
            if tier_backend is None:
                msg = "no tier backend wired"
                raise AssertionError(msg)
            return tier_backend

    state = SimpleNamespace(
        config=SimpleNamespace(authoring_model=authoring_model),
        tier_registry=_Registry() if tier_backend is not None else None,
        authoring_tier="frontier",
    )
    return SimpleNamespace(app=SimpleNamespace(state=state))


# -- _build_authoring_override_backend --------------------------------------


def test_override_builds_the_pinned_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSONA_ANTHROPIC_API_KEY", "sk-test")
    backend = _build_authoring_override_backend("anthropic/claude-sonnet-5")
    assert backend is not None
    assert backend.provider_name == "anthropic"
    assert backend.model_name == "claude-sonnet-5"


def test_override_openrouter_slug_keeps_embedded_slashes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSONA_OPENROUTER_API_KEY", "sk-or-test")
    backend = _build_authoring_override_backend("openrouter/z-ai/glm-4.6")
    assert backend is not None
    assert backend.provider_name == "openrouter"
    assert backend.model_name == "z-ai/glm-4.6"  # first-slash split preserves the slug


def test_unknown_provider_fails_soft_to_none() -> None:
    assert _build_authoring_override_backend("bogus/model") is None


@pytest.mark.parametrize("bad", ["noslash", "", "anthropic/", "/model"])
def test_malformed_string_fails_soft_to_none(bad: str) -> None:
    assert _build_authoring_override_backend(bad) is None


def test_missing_api_key_fails_soft_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PERSONA_ANTHROPIC_API_KEY", raising=False)
    assert _build_authoring_override_backend("anthropic/claude-sonnet-5") is None


# -- _authoring_backend (override vs tier) ----------------------------------


def test_authoring_backend_uses_the_override_when_set_and_resolvable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PERSONA_ANTHROPIC_API_KEY", "sk-test")
    request = _fake_request(
        authoring_model="anthropic/claude-sonnet-5", tier_backend=_TierBackend()
    )
    backend = _authoring_backend(request)  # type: ignore[arg-type]
    assert backend.model_name == "claude-sonnet-5"  # the override, NOT the tier stub


def test_authoring_backend_falls_back_to_the_tier_when_unset() -> None:
    request = _fake_request(authoring_model=None, tier_backend=_TierBackend())
    backend = _authoring_backend(request)  # type: ignore[arg-type]
    assert backend.model_name == "frontier-stub-model"  # today's frontier-tier behaviour


def test_authoring_backend_falls_back_to_the_tier_when_override_is_keyless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PERSONA_ANTHROPIC_API_KEY", raising=False)
    request = _fake_request(
        authoring_model="anthropic/claude-sonnet-5", tier_backend=_TierBackend()
    )
    backend = _authoring_backend(request)  # type: ignore[arg-type]
    assert backend.model_name == "frontier-stub-model"  # fail-soft to the tier, never 500

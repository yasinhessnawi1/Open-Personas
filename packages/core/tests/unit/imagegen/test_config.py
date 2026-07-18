"""Tests for ``persona.imagegen.config`` — :class:`ImageBackendConfig` (Spec 15 T04).

Mirrors ``tests/unit/backends/test_backends_config.py`` (Spec 02) per the
Spec 15 decisions gate paragraph #1. Asserts env-var round-trip,
``SecretStr`` repr-safety, prefix override via :meth:`from_env`,
field-constraint enforcement (D-15-X-provider-moderation-default range,
D-15-3 cap symmetry), and the "missing key returns None at config-time;
backend constructor fails at construction" invariant.
"""

from __future__ import annotations

from typing import get_args

import pytest
from persona.imagegen.config import DEFAULT_BASE_URLS, ImageBackendConfig, ImageProvider
from pydantic import SecretStr, ValidationError


class TestDefaults:
    def test_default_provider_is_openrouter(self) -> None:
        # Spec M3 (D-M3-10, T3a): the wired default is GPT Image 2 via OpenRouter.
        config = ImageBackendConfig()
        assert config.provider == "openrouter"

    def test_default_model_is_gpt_5_4_image_2(self) -> None:
        # Spec M3 (D-M3-10): confirmed OpenRouter slug (env-overridable).
        config = ImageBackendConfig()
        assert config.model == "openai/gpt-5.4-image-2"

    def test_default_api_key_is_none(self) -> None:
        # Missing-key behaviour at config-time: api_key returns None.
        # The concrete backend constructor raises ImageGenUnavailableError
        # at __init__; this test only asserts the config-layer contract.
        config = ImageBackendConfig()
        assert config.api_key is None

    def test_default_base_url_is_none(self) -> None:
        config = ImageBackendConfig()
        assert config.base_url is None

    def test_default_request_timeout_s(self) -> None:
        config = ImageBackendConfig()
        assert config.request_timeout_s == 120.0

    def test_default_fal_safety_tolerance(self) -> None:
        # D-15-X-provider-moderation-default: fal default tolerance is 2.
        config = ImageBackendConfig()
        assert config.fal_safety_tolerance == 2


class TestEnvRoundTrip:
    def test_provider_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PERSONA_IMAGEGEN_PROVIDER", "fal")
        config = ImageBackendConfig()
        assert config.provider == "fal"

    def test_model_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PERSONA_IMAGEGEN_MODEL", "fal-ai/flux-pro/v1.1")
        config = ImageBackendConfig()
        assert config.model == "fal-ai/flux-pro/v1.1"

    def test_api_key_from_env_is_secret_str(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PERSONA_IMAGEGEN_API_KEY", "sk-secret")
        config = ImageBackendConfig()
        assert isinstance(config.api_key, SecretStr)
        assert config.api_key.get_secret_value() == "sk-secret"

    def test_base_url_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PERSONA_IMAGEGEN_BASE_URL", "https://proxy.example/v1/")
        config = ImageBackendConfig()
        assert config.base_url == "https://proxy.example/v1/"

    def test_request_timeout_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PERSONA_IMAGEGEN_REQUEST_TIMEOUT_S", "30.5")
        config = ImageBackendConfig()
        assert config.request_timeout_s == 30.5

    def test_fal_safety_tolerance_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PERSONA_IMAGEGEN_FAL_SAFETY_TOLERANCE", "5")
        config = ImageBackendConfig()
        assert config.fal_safety_tolerance == 5


class TestSecretStrSafety:
    def test_repr_does_not_leak_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PERSONA_IMAGEGEN_API_KEY", "sk-super-secret-token")
        config = ImageBackendConfig()
        rendered = repr(config)
        assert "sk-super-secret-token" not in rendered

    def test_str_does_not_leak_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PERSONA_IMAGEGEN_API_KEY", "sk-super-secret-token")
        config = ImageBackendConfig()
        rendered = str(config)
        assert "sk-super-secret-token" not in rendered

    def test_secret_value_accessible_when_explicitly_unwrapped(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("PERSONA_IMAGEGEN_API_KEY", "sk-secret")
        config = ImageBackendConfig()
        assert config.api_key is not None
        assert config.api_key.get_secret_value() == "sk-secret"


class TestFieldConstraints:
    @pytest.mark.parametrize("good", [1, 2, 3, 4, 5, 6])
    def test_fal_safety_tolerance_in_range(self, good: int) -> None:
        config = ImageBackendConfig(fal_safety_tolerance=good)
        assert config.fal_safety_tolerance == good

    @pytest.mark.parametrize("bad", [0, -1, 7, 8, 100])
    def test_fal_safety_tolerance_out_of_range_rejected(self, bad: int) -> None:
        with pytest.raises(ValidationError):
            ImageBackendConfig(fal_safety_tolerance=bad)

    @pytest.mark.parametrize("good", [0.001, 1.0, 60.0, 120.0, 3600.0])
    def test_request_timeout_positive(self, good: float) -> None:
        config = ImageBackendConfig(request_timeout_s=good)
        assert config.request_timeout_s == good

    @pytest.mark.parametrize("bad", [0.0, -1.0, -120.0])
    def test_request_timeout_must_be_positive(self, bad: float) -> None:
        with pytest.raises(ValidationError):
            ImageBackendConfig(request_timeout_s=bad)

    def test_unknown_provider_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ImageBackendConfig(provider="stability")  # type: ignore[arg-type]


class TestOpenRouterProvider:
    """Spec 22 T09b — OpenRouter image-gen provider surface."""

    def test_openrouter_in_image_provider_literal(self) -> None:
        assert "openrouter" in get_args(ImageProvider)

    def test_openrouter_accepted_by_config(self) -> None:
        config = ImageBackendConfig(
            provider="openrouter",
            model="google/gemini-2.5-flash-image",
            api_key=SecretStr("sk-or-v1-test"),
        )
        assert config.provider == "openrouter"
        assert config.model == "google/gemini-2.5-flash-image"

    def test_openrouter_default_base_url(self) -> None:
        assert DEFAULT_BASE_URLS["openrouter"] == "https://openrouter.ai/api/v1/"


class TestFromEnv:
    def test_default_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PERSONA_IMAGEGEN_PROVIDER", "fal")
        monkeypatch.setenv("PERSONA_IMAGEGEN_API_KEY", "fal-key")
        config = ImageBackendConfig.from_env()
        assert config.provider == "fal"
        assert config.api_key is not None
        assert config.api_key.get_secret_value() == "fal-key"

    def test_custom_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PERSONA_IMAGEGEN_PRIMARY_PROVIDER", "openai")
        monkeypatch.setenv("PERSONA_IMAGEGEN_PRIMARY_API_KEY", "openai-primary-key")
        monkeypatch.setenv("PERSONA_IMAGEGEN_ALTERNATE_PROVIDER", "fal")
        monkeypatch.setenv("PERSONA_IMAGEGEN_ALTERNATE_API_KEY", "fal-alternate-key")
        primary = ImageBackendConfig.from_env(prefix="PERSONA_IMAGEGEN_PRIMARY_")
        alternate = ImageBackendConfig.from_env(prefix="PERSONA_IMAGEGEN_ALTERNATE_")
        assert primary.provider == "openai"
        assert alternate.provider == "fal"
        assert primary.api_key is not None
        assert alternate.api_key is not None
        assert primary.api_key.get_secret_value() == "openai-primary-key"
        assert alternate.api_key.get_secret_value() == "fal-alternate-key"

    def test_from_env_isolation_between_prefixes(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The default-prefix env vars do NOT bleed into a custom-prefix
        # config (and vice versa).
        monkeypatch.setenv("PERSONA_IMAGEGEN_PROVIDER", "openai")
        monkeypatch.setenv("PERSONA_IMAGEGEN_X_PROVIDER", "fal")
        default_cfg = ImageBackendConfig.from_env()
        x_cfg = ImageBackendConfig.from_env(prefix="PERSONA_IMAGEGEN_X_")
        assert default_cfg.provider == "openai"
        assert x_cfg.provider == "fal"

    def test_from_env_returns_image_backend_config(self) -> None:
        config = ImageBackendConfig.from_env()
        # Subclass under the hood; still an ImageBackendConfig.
        assert isinstance(config, ImageBackendConfig)


class TestImageProviderLiteral:
    def test_image_provider_literal_values(self) -> None:
        # D-15-1 (v0.1: openai + fal) + Spec 20 D-20-1 adds nvidia + Spec 22
        # D-22-8 adds openrouter + Spec 25 D-25-11 adds cloudflare. Adding a
        # provider requires a decisions-doc update + a new factory entry.
        assert set(get_args(ImageProvider)) == {
            "openai",
            "fal",
            "nvidia",
            "openrouter",
            "cloudflare",
        }

    def test_default_base_urls_cover_all_providers(self) -> None:
        for provider in get_args(ImageProvider):
            assert provider in DEFAULT_BASE_URLS, f"DEFAULT_BASE_URLS missing {provider}"


class TestMissingKeyBehaviour:
    """The "missing key returns None at config-time" half of the contract.

    Construction-time fail-fast lives in the concrete backend constructors
    (T06 OpenAI / T07 fal) — this layer only asserts the config-time
    contract: an unset env var means ``api_key is None``, which the
    backend constructor then maps to
    :class:`persona.imagegen.errors.ImageGenUnavailableError`.
    """

    def test_unset_api_key_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PERSONA_IMAGEGEN_API_KEY", raising=False)
        config = ImageBackendConfig()
        assert config.api_key is None

    def test_empty_string_api_key_becomes_secret_str(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # An empty-string env var produces a SecretStr wrapping "" — NOT
        # None. The backend constructor treats both as "missing"; we
        # assert the config layer leaves this discrimination to the
        # backend.
        monkeypatch.setenv("PERSONA_IMAGEGEN_API_KEY", "")
        config = ImageBackendConfig()
        assert config.api_key is not None
        assert config.api_key.get_secret_value() == ""


class TestImmutabilityVsBaseSettings:
    """``BaseSettings`` is not frozen — instances are still mutable.

    This is deliberate (matches Spec 02's :class:`BackendConfig`); a
    frozen settings model would prevent tier-override composition.
    Boundary-crossing types (``ImageGenOptions`` etc.) are frozen
    (D-15-X-pydantic-boundary-types); the config object stays mutable
    because it never crosses a public surface — only the factory reads it.
    """

    def test_config_is_mutable(self) -> None:
        config = ImageBackendConfig()
        # Should NOT raise — assignment after construction is permitted.
        config.fal_safety_tolerance = 4
        assert config.fal_safety_tolerance == 4

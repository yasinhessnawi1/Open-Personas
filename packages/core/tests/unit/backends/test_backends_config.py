"""Tests for ``persona.backends.config`` — env-driven BackendConfig + from_env."""

from __future__ import annotations

import pytest
from persona.backends.config import DEFAULT_BASE_URLS, BackendConfig
from pydantic import SecretStr, ValidationError


class TestDefaults:
    def test_default_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Clear any contaminating env vars from the developer's shell.
        for k in (
            "PERSONA_PROVIDER",
            "PERSONA_MODEL",
            "PERSONA_API_KEY",
            "PERSONA_BASE_URL",
            "PERSONA_MAX_TOKENS",
            "PERSONA_TEMPERATURE",
            "PERSONA_REQUEST_TIMEOUT_S",
            "PERSONA_LOCAL_MODEL_ID",
            "PERSONA_LOCAL_QUANTIZATION",
            "PERSONA_LOCAL_DEVICE",
        ):
            monkeypatch.delenv(k, raising=False)
        config = BackendConfig()
        assert config.provider == "anthropic"
        assert config.model == "claude-sonnet-4-6"
        assert config.api_key is None
        assert config.base_url is None
        assert config.max_tokens == 4096
        assert config.temperature == 0.0
        assert config.request_timeout_s == 60.0
        assert config.local_quantization == "4bit"
        assert config.local_device == "auto"


class TestEnvLoading:
    def test_reads_provider_and_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PERSONA_PROVIDER", "openai")
        monkeypatch.setenv("PERSONA_MODEL", "gpt-4o-mini")
        config = BackendConfig()
        assert config.provider == "openai"
        assert config.model == "gpt-4o-mini"

    def test_reads_api_key_as_secret(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PERSONA_API_KEY", "sk-secret-123")
        config = BackendConfig()
        assert config.api_key is not None
        assert config.api_key.get_secret_value() == "sk-secret-123"

    def test_api_key_not_in_repr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PERSONA_API_KEY", "sk-secret-123")
        config = BackendConfig()
        rendered = repr(config)
        assert "sk-secret-123" not in rendered

    def test_reads_request_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PERSONA_REQUEST_TIMEOUT_S", "120.0")
        config = BackendConfig()
        assert config.request_timeout_s == 120.0

    def test_reads_base_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PERSONA_BASE_URL", "https://my-proxy.example/v1/")
        config = BackendConfig()
        assert config.base_url == "https://my-proxy.example/v1/"


class TestValidation:
    def test_invalid_provider_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PERSONA_PROVIDER", "bogus")
        with pytest.raises(ValidationError):
            BackendConfig()

    def test_local_provider_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PERSONA_PROVIDER", "local")
        monkeypatch.setenv("PERSONA_LOCAL_MODEL_ID", "google/gemma-2-9b-it")
        config = BackendConfig()
        assert config.provider == "local"
        assert config.local_model_id == "google/gemma-2-9b-it"

    def test_ollama_provider_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PERSONA_PROVIDER", "ollama")
        config = BackendConfig()
        assert config.provider == "ollama"

    def test_negative_max_tokens_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PERSONA_MAX_TOKENS", "-1")
        with pytest.raises(ValidationError):
            BackendConfig()

    def test_negative_timeout_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PERSONA_REQUEST_TIMEOUT_S", "-1.0")
        with pytest.raises(ValidationError):
            BackendConfig()

    def test_invalid_local_quantization_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PERSONA_LOCAL_QUANTIZATION", "16bit")
        with pytest.raises(ValidationError):
            BackendConfig()


class TestFromEnv:
    def test_from_env_with_default_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PERSONA_PROVIDER", "groq")
        config = BackendConfig.from_env()
        assert config.provider == "groq"

    def test_from_env_with_tier_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PERSONA_TIER_FRONTIER_PROVIDER", "anthropic")
        monkeypatch.setenv("PERSONA_TIER_FRONTIER_MODEL", "claude-opus-4-7")
        monkeypatch.setenv("PERSONA_TIER_FRONTIER_API_KEY", "sk-frontier")
        # Default-prefix vars should NOT bleed in for the tier load.
        monkeypatch.setenv("PERSONA_PROVIDER", "openai")
        config = BackendConfig.from_env(prefix="PERSONA_TIER_FRONTIER_")
        assert config.provider == "anthropic"
        assert config.model == "claude-opus-4-7"
        assert config.api_key is not None
        assert config.api_key.get_secret_value() == "sk-frontier"

    def test_from_env_falls_back_to_field_defaults_when_no_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Make absolutely sure no env vars match the test prefix.
        prefix = "PERSONA_TEST_EMPTY_"
        for suffix in (
            "PROVIDER",
            "MODEL",
            "API_KEY",
            "BASE_URL",
            "MAX_TOKENS",
            "TEMPERATURE",
            "REQUEST_TIMEOUT_S",
            "LOCAL_MODEL_ID",
            "LOCAL_QUANTIZATION",
            "LOCAL_DEVICE",
        ):
            monkeypatch.delenv(f"{prefix}{suffix}", raising=False)
        config = BackendConfig.from_env(prefix=prefix)
        assert config.provider == "anthropic"
        assert config.model == "claude-sonnet-4-6"


class TestDefaultBaseUrls:
    @pytest.mark.parametrize(
        "provider",
        ["anthropic", "openai", "deepseek", "groq", "together", "nvidia", "ollama"],
    )
    def test_known_provider_has_base_url(self, provider: str) -> None:
        assert provider in DEFAULT_BASE_URLS
        assert DEFAULT_BASE_URLS[provider].startswith(("https://", "http://"))

    def test_local_not_in_base_urls(self) -> None:
        # 'local' is the HF backend; no HTTP endpoint.
        assert "local" not in DEFAULT_BASE_URLS


# -----------------------------------------------------------------------------
# Spec 20 — NVIDIA provider wiring
# -----------------------------------------------------------------------------


class TestNvidiaProvider:
    """Spec 20 T09 — NVIDIA chat-basic provider wiring.

    Verifies that NVIDIA is a first-class :data:`Provider` member with a
    registered default base URL pointing at the NIM hosted catalog. The
    trailing ``/v1/`` mirrors the OpenAI-compat convention (the openai
    SDK does NOT append /v1/ like the anthropic SDK does).
    """

    def test_nvidia_provider_constructs(self) -> None:
        config = BackendConfig(
            provider="nvidia",
            model="nvidia/llama-3.3-nemotron-super-49b-v1.5",
            api_key=SecretStr("test"),
        )
        assert config.provider == "nvidia"
        assert config.model == "nvidia/llama-3.3-nemotron-super-49b-v1.5"

    def test_nvidia_base_url_registered(self) -> None:
        assert DEFAULT_BASE_URLS["nvidia"] == "https://integrate.api.nvidia.com/v1/"

    def test_nvidia_base_url_has_trailing_v1(self) -> None:
        # The openai SDK (used for nvidia dispatch) does NOT append /v1/
        # like the anthropic SDK does — so the registered base URL must
        # carry the explicit /v1/ suffix. See the comment block in
        # persona.backends.config.DEFAULT_BASE_URLS.
        assert DEFAULT_BASE_URLS["nvidia"].endswith("/v1/")

    def test_nvidia_provider_via_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PERSONA_PROVIDER", "nvidia")
        monkeypatch.setenv("PERSONA_MODEL", "nvidia/llama-3.3-nemotron-super-49b-v1.5")
        monkeypatch.setenv("PERSONA_API_KEY", "nvapi-test")
        config = BackendConfig()
        assert config.provider == "nvidia"
        assert config.api_key is not None
        assert config.api_key.get_secret_value() == "nvapi-test"


# -----------------------------------------------------------------------------
# Spec 20 T12 — D-20-3 extra_body opaque pass-through
# -----------------------------------------------------------------------------


class TestExtraBody:
    def test_defaults_to_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PERSONA_EXTRA_BODY", raising=False)
        config = BackendConfig()
        assert config.extra_body is None

    def test_accepts_dict(self) -> None:
        config = BackendConfig(
            api_key=SecretStr("k"),
            extra_body={"chat_template_kwargs": {"thinking": True}},
        )
        assert config.extra_body == {"chat_template_kwargs": {"thinking": True}}

    def test_rejects_non_dict(self) -> None:
        with pytest.raises(ValidationError):
            BackendConfig(api_key=SecretStr("k"), extra_body="not-a-dict")  # type: ignore[arg-type]


class TestTierPrefixReplacesRatherThanMerges:
    """A configured tier reads ONLY its own names — the globals do not reach it.

    `from_env(prefix=...)` builds a subclass whose settings config is replaced, not merged,
    so `PERSONA_MAX_TOKENS` never arrives at the mid tier. It is silent: no warning, and the
    tier simply uses the code default, which looks identical to "the value was applied and
    happened to equal the default".

    Pinned rather than changed. Making the globals inherit would be a real semantic change to
    a config system, and it is the owner's to make; what was wrong was that nobody could find
    this out except by reading `config.py`. It is now in `.env.example` too.
    """

    _GLOBALS = ("PERSONA_MAX_TOKENS", "PERSONA_TEMPERATURE")
    _TIERED = ("PERSONA_MID_MAX_TOKENS", "PERSONA_MID_TEMPERATURE")

    def _clear(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for k in (*self._GLOBALS, *self._TIERED):
            monkeypatch.delenv(k, raising=False)

    def test_a_global_does_not_reach_a_tier(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clear(monkeypatch)
        monkeypatch.setenv("PERSONA_MAX_TOKENS", "32000")

        assert BackendConfig.from_env().max_tokens == 32000
        assert BackendConfig.from_env(prefix="PERSONA_MID_").max_tokens == 4096, (
            "the tier must fall back to the DEFAULT, not inherit the global — if this "
            "changes, .env.example's 'Model backend defaults' note is now wrong"
        )

    def test_the_per_tier_name_is_what_works(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clear(monkeypatch)
        monkeypatch.setenv("PERSONA_MAX_TOKENS", "32000")
        monkeypatch.setenv("PERSONA_MID_MAX_TOKENS", "9000")

        assert BackendConfig.from_env(prefix="PERSONA_MID_").max_tokens == 9000

    def test_the_same_holds_for_temperature(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Not a quirk of one field: the whole settings config is replaced."""
        self._clear(monkeypatch)
        monkeypatch.setenv("PERSONA_TEMPERATURE", "0.7")

        assert BackendConfig.from_env().temperature == 0.7
        assert BackendConfig.from_env(prefix="PERSONA_MID_").temperature == 0.0

    def test_one_tier_does_not_leak_into_another(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The isolation cuts both ways, which is the argument FOR the current semantics."""
        self._clear(monkeypatch)
        monkeypatch.setenv("PERSONA_MID_MAX_TOKENS", "9000")

        assert BackendConfig.from_env(prefix="PERSONA_MID_").max_tokens == 9000
        assert BackendConfig.from_env(prefix="PERSONA_SMALL_").max_tokens == 4096

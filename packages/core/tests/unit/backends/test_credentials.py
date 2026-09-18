"""Tests for ``persona.backends.credentials``.

Covers the Spec 20 T11 deliverables:

* :class:`ProviderCredentialResolver` — D-20-15 three-tier disposition.
* :func:`parse_models_list` — D-20-17 case (d) malformed reasons +
  D-20-18 EXPLICIT REJECT of ``local`` / ``ollama``.
* :func:`resolve_tier_config` — D-20-17 four-case precedence between
  ``PERSONA_<TIER>_MODELS`` and the ``PROVIDER+MODEL+API_KEY`` triplet,
  plus the partial-triplet :class:`IncompleteTierConfigError` branch.
"""

from __future__ import annotations

import logging

import pytest
from persona.backends.config import DEFAULT_BASE_URLS
from persona.backends.credentials import (
    FREE_BY_DEFINITION_OPENROUTER_SLUGS,
    ProviderCredentialResolver,
    ProviderCredentials,
    TierResolution,
    filter_openrouter_free_mode,
    is_free_openrouter_slot,
    parse_models_list,
    resolve_tier_config,
)
from persona.backends.errors import (
    AuthenticationError,
    IncompleteTierConfigError,
    LocalProviderInModelsListError,
    MalformedTierModelsError,
    ProviderCredentialMissingError,
)
from pydantic import SecretStr

# --------------------------------------------------------------------------- #
# ProviderCredentialResolver — D-20-15 three-tier disposition
# --------------------------------------------------------------------------- #


class TestProviderCredentialResolver:
    """D-20-15 three-tier disposition tests."""

    def test_keyless_ollama_returns_none_api_key_and_default_base_url(self) -> None:
        resolver = ProviderCredentialResolver(env={})
        creds = resolver.resolve("ollama")
        assert isinstance(creds, ProviderCredentials)
        assert creds.provider == "ollama"
        assert creds.api_key is None
        assert creds.base_url == DEFAULT_BASE_URLS["ollama"]

    def test_keyless_local_returns_none_api_key_and_empty_base_url(self) -> None:
        # D-20-18 finding: `local` has no entry in DEFAULT_BASE_URLS — runs
        # in-process via HuggingFace; no HTTP transport.
        resolver = ProviderCredentialResolver(env={})
        creds = resolver.resolve("local")
        assert creds.api_key is None
        assert creds.base_url == ""

    def test_keyless_ollama_honours_base_url_override(self) -> None:
        resolver = ProviderCredentialResolver(
            env={"PERSONA_OLLAMA_BASE_URL": "http://gpu-box:11434"}
        )
        creds = resolver.resolve("ollama")
        assert creds.base_url == "http://gpu-box:11434"

    def test_api_keyed_provider_returns_secretstr_and_default_base_url(self) -> None:
        resolver = ProviderCredentialResolver(env={"PERSONA_ANTHROPIC_API_KEY": "sk-ant-test"})
        creds = resolver.resolve("anthropic")
        assert isinstance(creds.api_key, SecretStr)
        assert creds.api_key.get_secret_value() == "sk-ant-test"
        assert creds.base_url == DEFAULT_BASE_URLS["anthropic"]

    def test_api_keyed_provider_honours_base_url_override(self) -> None:
        resolver = ProviderCredentialResolver(
            env={
                "PERSONA_OPENAI_API_KEY": "sk-test",
                "PERSONA_OPENAI_BASE_URL": "https://my-proxy.example/v1/",
            }
        )
        creds = resolver.resolve("openai")
        assert creds.base_url == "https://my-proxy.example/v1/"

    def test_missing_env_var_raises_provider_credential_missing(self) -> None:
        resolver = ProviderCredentialResolver(env={})
        with pytest.raises(ProviderCredentialMissingError) as exc_info:
            resolver.resolve("anthropic")
        assert exc_info.value.context == {
            "provider": "anthropic",
            "env_var": "PERSONA_ANTHROPIC_API_KEY",
        }

    @pytest.mark.parametrize("blank", ["", "   ", "\t", "\n"])
    def test_empty_or_whitespace_env_var_raises_authentication_error(self, blank: str) -> None:
        resolver = ProviderCredentialResolver(env={"PERSONA_GROQ_API_KEY": blank})
        with pytest.raises(AuthenticationError) as exc_info:
            resolver.resolve("groq")
        assert exc_info.value.context == {
            "provider": "groq",
            "env_var": "PERSONA_GROQ_API_KEY",
        }

    def test_repr_does_not_leak_api_key(self) -> None:
        resolver = ProviderCredentialResolver(
            env={"PERSONA_ANTHROPIC_API_KEY": "super-secret-value"}
        )
        creds = resolver.resolve("anthropic")
        assert "super-secret-value" not in repr(creds)
        assert "<redacted>" in repr(creds)

    def test_env_snapshot_is_immutable(self) -> None:
        # D-20-15 — snapshot at construction so per-test env doesn't leak.
        env = {"PERSONA_ANTHROPIC_API_KEY": "first"}
        resolver = ProviderCredentialResolver(env=env)
        env["PERSONA_ANTHROPIC_API_KEY"] = "second"
        creds = resolver.resolve("anthropic")
        assert creds.api_key is not None
        assert creds.api_key.get_secret_value() == "first"


# --------------------------------------------------------------------------- #
# parse_models_list — D-20-17 case (d) malformed reasons
# --------------------------------------------------------------------------- #


class TestParseModelsListMalformed:
    """D-20-17 case (d) — every malformed reason has its own raise site."""

    def test_empty_string_raises_empty_after_strip(self) -> None:
        with pytest.raises(MalformedTierModelsError) as exc_info:
            parse_models_list("frontier", "")
        assert exc_info.value.context["reason"] == "empty_after_strip"
        assert exc_info.value.context["tier"] == "frontier"

    def test_whitespace_only_raises_empty_after_strip(self) -> None:
        with pytest.raises(MalformedTierModelsError) as exc_info:
            parse_models_list("mid", "   \t  ")
        assert exc_info.value.context["reason"] == "empty_after_strip"

    def test_trailing_comma_raises_empty_csv_entry(self) -> None:
        with pytest.raises(MalformedTierModelsError) as exc_info:
            parse_models_list("frontier", "anthropic/claude-sonnet-4-6,,")
        assert exc_info.value.context["reason"] == "empty_csv_entry"
        assert exc_info.value.context["position"] == "1"

    def test_leading_comma_raises_empty_csv_entry(self) -> None:
        with pytest.raises(MalformedTierModelsError) as exc_info:
            parse_models_list("frontier", ",openai/gpt-4o")
        assert exc_info.value.context["reason"] == "empty_csv_entry"
        assert exc_info.value.context["position"] == "0"

    def test_missing_slash_raises_missing_slash(self) -> None:
        with pytest.raises(MalformedTierModelsError) as exc_info:
            parse_models_list("frontier", "claude-sonnet-4-6")
        assert exc_info.value.context["reason"] == "missing_slash"
        assert exc_info.value.context["position"] == "0"

    def test_unknown_provider_raises_unknown_provider(self) -> None:
        with pytest.raises(MalformedTierModelsError) as exc_info:
            parse_models_list("mid", "bogusvendor/x")
        assert exc_info.value.context["reason"] == "unknown_provider"
        assert exc_info.value.context["position"] == "0"

    def test_empty_model_raises_empty_model(self) -> None:
        with pytest.raises(MalformedTierModelsError) as exc_info:
            parse_models_list("frontier", "anthropic/")
        assert exc_info.value.context["reason"] == "empty_model"
        assert exc_info.value.context["position"] == "0"

    def test_value_is_preserved_in_context(self) -> None:
        raw = "anthropic/claude-sonnet-4-6,bogusvendor/x"
        with pytest.raises(MalformedTierModelsError) as exc_info:
            parse_models_list("frontier", raw)
        assert exc_info.value.context["value"] == raw


# --------------------------------------------------------------------------- #
# parse_models_list — D-20-18 EXPLICIT REJECT
# --------------------------------------------------------------------------- #


class TestParseModelsListLocalReject:
    """D-20-18 — `local` and `ollama` rejected with hint context."""

    def test_local_provider_rejected_with_local_hint(self) -> None:
        with pytest.raises(LocalProviderInModelsListError) as exc_info:
            parse_models_list("frontier", "local/llama-2-7b")
        ctx = exc_info.value.context
        assert ctx["tier"] == "frontier"
        assert ctx["position"] == "0"
        assert "PERSONA_LOCAL_MODEL_ID" in ctx["hint"]

    def test_ollama_provider_rejected_with_ollama_hint(self) -> None:
        with pytest.raises(LocalProviderInModelsListError) as exc_info:
            parse_models_list("mid", "ollama/llama2")
        ctx = exc_info.value.context
        assert ctx["tier"] == "mid"
        assert ctx["position"] == "0"
        assert "PERSONA_<TIER>_PROVIDER=ollama" in ctx["hint"]
        assert "cross-provider fallback peer" in ctx["hint"]

    def test_local_rejected_mid_list_carries_position(self) -> None:
        with pytest.raises(LocalProviderInModelsListError) as exc_info:
            parse_models_list(
                "frontier", "anthropic/claude-sonnet-4-6,local/llama-2-7b,openai/gpt-4o"
            )
        assert exc_info.value.context["position"] == "1"

    def test_ollama_rejected_mid_list_carries_position(self) -> None:
        with pytest.raises(LocalProviderInModelsListError) as exc_info:
            parse_models_list("mid", "deepseek/deepseek-chat,ollama/llama2")
        assert exc_info.value.context["position"] == "1"


# --------------------------------------------------------------------------- #
# parse_models_list — happy paths
# --------------------------------------------------------------------------- #


class TestParseModelsListHappy:
    """D-20-13 SLASH convention round-trips faithfully."""

    def test_single_entry_returns_one_tuple(self) -> None:
        result = parse_models_list("frontier", "anthropic/claude-sonnet-4-6")
        assert result == [("anthropic", "claude-sonnet-4-6")]

    def test_multi_entry_preserves_order(self) -> None:
        # D-20-4 latency-similarity order matters; parser MUST NOT reorder.
        result = parse_models_list(
            "frontier", "anthropic/claude-sonnet-4-6,openai/gpt-4o,deepseek/deepseek-chat"
        )
        assert result == [
            ("anthropic", "claude-sonnet-4-6"),
            ("openai", "gpt-4o"),
            ("deepseek", "deepseek-chat"),
        ]

    def test_whitespace_around_entries_is_stripped(self) -> None:
        result = parse_models_list("mid", "  anthropic/claude-sonnet-4-6 , openai/gpt-4o  ")
        assert result == [
            ("anthropic", "claude-sonnet-4-6"),
            ("openai", "gpt-4o"),
        ]

    def test_model_with_internal_slash_keeps_remainder(self) -> None:
        # HuggingFace-style IDs use slash — first-slash split per D-20-13.
        # Note: only Provider-valid tokens for the prefix.
        result = parse_models_list("frontier", "together/meta-llama/Llama-3-70b")
        assert result == [("together", "meta-llama/Llama-3-70b")]


# --------------------------------------------------------------------------- #
# resolve_tier_config — D-20-17 four-case precedence
# --------------------------------------------------------------------------- #


class TestResolveTierConfigPrecedence:
    """D-20-17 four cases + partial-triplet branch."""

    def test_case_a_models_set_triplet_unset_returns_parsed_list(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # MODELS set + triplet UNSET → MODELS wins, NO log.
        env = {
            "PERSONA_FRONTIER_MODELS": "anthropic/claude-sonnet-4-6,openai/gpt-4o",
        }
        with caplog.at_level(logging.INFO, logger="persona.backends.credentials"):
            resolution = resolve_tier_config("frontier", env=env)
        assert isinstance(resolution, TierResolution)
        assert resolution.models == [
            ("anthropic", "claude-sonnet-4-6"),
            ("openai", "gpt-4o"),
        ]
        assert resolution.triplet is None
        assert resolution.triplet_ignored is False
        # No precedence log.
        assert not any("case (c)" in r.getMessage() for r in caplog.records)

    def test_case_b_triplet_set_models_unset_returns_triplet(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        env = {
            "PERSONA_MID_PROVIDER": "deepseek",
            "PERSONA_MID_MODEL": "deepseek-chat",
            "PERSONA_MID_API_KEY": "sk-test",
        }
        with caplog.at_level(logging.INFO, logger="persona.backends.credentials"):
            resolution = resolve_tier_config("mid", env=env)
        assert resolution.models is None
        assert resolution.triplet is not None
        provider, model, api_key = resolution.triplet
        assert provider == "deepseek"
        assert model == "deepseek-chat"
        assert api_key.get_secret_value() == "sk-test"
        assert resolution.triplet_ignored is False

    def test_case_c_models_set_triplet_partial_models_wins_with_info_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        env = {
            "PERSONA_FRONTIER_MODELS": "anthropic/claude-sonnet-4-6",
            "PERSONA_FRONTIER_PROVIDER": "openai",
            "PERSONA_FRONTIER_API_KEY": "sk-test",
        }
        with caplog.at_level(logging.INFO):
            resolution = resolve_tier_config("frontier", env=env)
        assert resolution.models == [("anthropic", "claude-sonnet-4-6")]
        assert resolution.triplet is None
        assert resolution.triplet_ignored is True

    def test_case_d_malformed_raises_via_parser(self) -> None:
        env = {"PERSONA_FRONTIER_MODELS": "anthropic/claude-sonnet-4-6,,"}
        with pytest.raises(MalformedTierModelsError) as exc_info:
            resolve_tier_config("frontier", env=env)
        assert exc_info.value.context["reason"] == "empty_csv_entry"

    def test_partial_triplet_one_var_set_raises_incomplete_tier_config(self) -> None:
        env = {"PERSONA_SMALL_PROVIDER": "groq"}
        with pytest.raises(IncompleteTierConfigError) as exc_info:
            resolve_tier_config("small", env=env)
        missing = exc_info.value.context["missing_vars"]
        assert "PERSONA_SMALL_MODEL" in missing
        assert "PERSONA_SMALL_API_KEY" in missing

    def test_partial_triplet_two_vars_set_raises_incomplete_tier_config(self) -> None:
        env = {
            "PERSONA_SMALL_PROVIDER": "groq",
            "PERSONA_SMALL_MODEL": "llama-3.1-8b-instant",
        }
        with pytest.raises(IncompleteTierConfigError) as exc_info:
            resolve_tier_config("small", env=env)
        assert "PERSONA_SMALL_API_KEY" in exc_info.value.context["missing_vars"]

    def test_empty_env_returns_unresolved_tier_resolution(self) -> None:
        # Neither MODELS nor triplet set: not an error — T17 falls back to
        # the global default backend per existing Spec 05 behaviour.
        resolution = resolve_tier_config("imagegen", env={})
        assert resolution.models is None
        assert resolution.triplet is None
        assert resolution.triplet_ignored is False

    def test_case_c_local_in_models_still_rejected(self) -> None:
        # The D-20-18 reject path fires even when a triplet would otherwise
        # be ignored — fail-loud at the parser before precedence resolves.
        env = {
            "PERSONA_FRONTIER_MODELS": "local/llama-2-7b",
            "PERSONA_FRONTIER_PROVIDER": "openai",
            "PERSONA_FRONTIER_MODEL": "gpt-4o",
            "PERSONA_FRONTIER_API_KEY": "sk-test",
        }
        with pytest.raises(LocalProviderInModelsListError):
            resolve_tier_config("frontier", env=env)


class TestCloudflareChatProvider:
    """Cloudflare Workers AI as a first-class chat provider.

    The account id is not static, so the chat base URL is assembled at
    resolution time from ``PERSONA_CLOUDFLARE_ACCOUNT_ID`` (mirroring the
    imagegen path, which keeps the account id as a separate field and builds
    the URL at backend-construction time). ``@cf/<vendor>/<model>`` ids carry
    internal slashes that must pass through the tier parser intact.
    """

    def test_models_list_parses_cloudflare_at_cf_model_no_error(self) -> None:
        # The exact value that crash-looped prod with MalformedTierModelsError.
        result = parse_models_list(
            "frontier", "cloudflare/@cf/zai-org/glm-5.2,anthropic/claude-sonnet-4-6"
        )
        assert result == [
            ("cloudflare", "@cf/zai-org/glm-5.2"),
            ("anthropic", "claude-sonnet-4-6"),
        ]

    def test_at_cf_model_id_passes_through_intact(self) -> None:
        # First-slash split: provider=cloudflare, model keeps all its slashes.
        result = parse_models_list("mid", "cloudflare/@cf/meta/llama-4-scout-17b-16e-instruct")
        assert result == [("cloudflare", "@cf/meta/llama-4-scout-17b-16e-instruct")]

    def test_base_url_built_from_account_id(self) -> None:
        resolver = ProviderCredentialResolver(
            env={
                "PERSONA_CLOUDFLARE_API_KEY": "cf-test",
                "PERSONA_CLOUDFLARE_ACCOUNT_ID": "acct-123",
            }
        )
        creds = resolver.resolve("cloudflare")
        assert creds.api_key is not None
        assert creds.api_key.get_secret_value() == "cf-test"
        assert creds.base_url == "https://api.cloudflare.com/client/v4/accounts/acct-123/ai/v1/"

    def test_missing_account_id_fails_fast(self) -> None:
        resolver = ProviderCredentialResolver(env={"PERSONA_CLOUDFLARE_API_KEY": "cf-test"})
        with pytest.raises(ProviderCredentialMissingError) as exc_info:
            resolver.resolve("cloudflare")
        assert exc_info.value.context == {
            "provider": "cloudflare",
            "env_var": "PERSONA_CLOUDFLARE_ACCOUNT_ID",
        }

    def test_blank_account_id_fails_fast(self) -> None:
        resolver = ProviderCredentialResolver(
            env={
                "PERSONA_CLOUDFLARE_API_KEY": "cf-test",
                "PERSONA_CLOUDFLARE_ACCOUNT_ID": "   ",
            }
        )
        with pytest.raises(ProviderCredentialMissingError) as exc_info:
            resolver.resolve("cloudflare")
        assert exc_info.value.context["env_var"] == "PERSONA_CLOUDFLARE_ACCOUNT_ID"

    def test_missing_api_key_raises_credential_missing(self) -> None:
        # Account id present but API key absent → standard credential-missing.
        resolver = ProviderCredentialResolver(env={"PERSONA_CLOUDFLARE_ACCOUNT_ID": "acct-123"})
        with pytest.raises(ProviderCredentialMissingError) as exc_info:
            resolver.resolve("cloudflare")
        assert exc_info.value.context["env_var"] == "PERSONA_CLOUDFLARE_API_KEY"

    def test_explicit_base_url_override_skips_account_assembly(self) -> None:
        # A full PERSONA_CLOUDFLARE_BASE_URL override wins; account id not needed.
        resolver = ProviderCredentialResolver(
            env={
                "PERSONA_CLOUDFLARE_API_KEY": "cf-test",
                "PERSONA_CLOUDFLARE_BASE_URL": "https://proxy.example/ai/v1/",
            }
        )
        creds = resolver.resolve("cloudflare")
        assert creds.base_url == "https://proxy.example/ai/v1/"


class TestFilterOpenRouterFreeMode:
    """Spec 22 D-22-2 (chat) + D-22-20 (image) free-mode MODELS filter."""

    _MIXED: list[tuple[str, str]] = [
        ("openrouter", "anthropic/claude-3.5-sonnet"),
        ("openrouter", "meta-llama/llama-3.3-70b-instruct:free"),
        ("nvidia", "nvidia/llama-3.3-nemotron-super-49b-v1.5"),
    ]

    def test_paid_mode_is_noop(self) -> None:
        out = filter_openrouter_free_mode(
            self._MIXED, mode="paid", tier_name="frontier", keep_free_suffix=True
        )
        assert out == self._MIXED

    def test_none_mode_is_noop(self) -> None:
        out = filter_openrouter_free_mode(
            self._MIXED, mode=None, tier_name="frontier", keep_free_suffix=True
        )
        assert out == self._MIXED

    def test_chat_free_mode_keeps_free_suffix_drops_others(self) -> None:
        # D-22-2: drop non-:free openrouter, keep :free + non-openrouter.
        out = filter_openrouter_free_mode(
            self._MIXED, mode="free", tier_name="frontier", keep_free_suffix=True
        )
        assert out == [
            ("openrouter", "meta-llama/llama-3.3-70b-instruct:free"),
            ("nvidia", "nvidia/llama-3.3-nemotron-super-49b-v1.5"),
        ]

    def test_image_free_mode_drops_all_openrouter(self) -> None:
        # D-22-20: keep_free_suffix=False drops EVERY openrouter entry
        # (no :free image models exist), keeps non-openrouter.
        out = filter_openrouter_free_mode(
            self._MIXED, mode="free", tier_name="imagegen", keep_free_suffix=False
        )
        assert out == [("nvidia", "nvidia/llama-3.3-nemotron-super-49b-v1.5")]

    def test_non_openrouter_never_touched(self) -> None:
        models = [("nvidia", "nvidia/x"), ("openai", "gpt-4o")]
        out = filter_openrouter_free_mode(
            models, mode="free", tier_name="frontier", keep_free_suffix=True
        )
        assert out == models

    def test_empty_result_when_all_dropped(self) -> None:
        models = [("openrouter", "anthropic/claude-3.5-sonnet")]
        out = filter_openrouter_free_mode(
            models, mode="free", tier_name="frontier", keep_free_suffix=True
        )
        assert out == []


class TestFreeByDefinitionAllowlist:
    """R9-179 item 1, ``openrouter/free`` is free without the ``:free`` suffix."""

    def test_allowlisted_slug_is_kept_in_free_mode(self) -> None:
        # The free-only auto-router is the LAST fallback of both free chains; the
        # suffix rule alone dropped it, so free-mode silently lost its safety net.
        models = [("openrouter", "free")]
        out = filter_openrouter_free_mode(
            models, mode="free", tier_name="frontier", keep_free_suffix=True
        )
        assert out == models

    def test_paid_slug_is_still_dropped_in_free_mode(self) -> None:
        # The allowlist is an exception, not an amnesty: a paid slug still goes.
        out = filter_openrouter_free_mode(
            [("openrouter", "anthropic/claude-3.5-sonnet")],
            mode="free",
            tier_name="frontier",
            keep_free_suffix=True,
        )
        assert out == []

    def test_free_suffix_is_still_kept_in_free_mode(self) -> None:
        models = [("openrouter", "meta-llama/llama-3.3-70b-instruct:free")]
        out = filter_openrouter_free_mode(
            models, mode="free", tier_name="frontier", keep_free_suffix=True
        )
        assert out == models

    def test_allowlisted_slug_is_dropped_on_the_image_posture(self) -> None:
        # keep_free_suffix=False drops every OpenRouter entry: the auto-router is a
        # CHAT router, so no image tier should be reprieved by the allowlist.
        out = filter_openrouter_free_mode(
            [("openrouter", "free")], mode="free", tier_name="imagegen", keep_free_suffix=False
        )
        assert out == []

    def test_predicate_agrees_with_the_filter(self) -> None:
        assert is_free_openrouter_slot("openrouter", "free")
        assert is_free_openrouter_slot("openrouter", "meta-llama/llama-3.3-70b-instruct:free")
        assert not is_free_openrouter_slot("openrouter", "anthropic/claude-3.5-sonnet")
        # Only OpenRouter bills per free-account request; a same-named slug elsewhere is not it.
        assert not is_free_openrouter_slot("nvidia", "free")

    def test_allowlist_names_the_auto_router(self) -> None:
        # Pins the constant's CONTENT: emptying it is the mutation this test catches.
        assert "openrouter/free" in FREE_BY_DEFINITION_OPENROUTER_SLUGS

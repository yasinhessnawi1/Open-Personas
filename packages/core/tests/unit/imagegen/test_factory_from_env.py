"""Tests for :func:`persona.imagegen.load_image_backend_from_env` — Spec 20 T17.

Covers D-20-17 four cases (a/b/c/d) × image-gen tier + D-20-15 per-slot
disposition (≥1 resolves → wrapper; ALL fail → :class:`TierNotConfiguredError`)
for the ImageBackend factory mirror.

Concrete backend imports are stubbed via ``sys.modules`` so this test file
does not depend on T06/T07/T10 being present at runtime — mirrors the
existing ``test_factory.py`` lazy-import discipline.
"""

# ruff: noqa: N801, ARG002 — TestImageCaseA_..D_.. classes named after D-20-17
# cases, and ``stub_image_backends`` is accepted solely for its monkey-patch
# side-effect on ``sys.modules``.

from __future__ import annotations

import sys
import types
from typing import TYPE_CHECKING

import pytest
from loguru import logger as _loguru_logger
from persona.backends.errors import (
    LocalProviderInModelsListError,
    MalformedTierModelsError,
    TierNotConfiguredError,
)
from persona.imagegen import (
    ImageBackendConfig,
    load_image_backend_from_env,
)
from persona.imagegen.multi_model_image import MultiModelImageBackend

if TYPE_CHECKING:
    from collections.abc import Iterator


def _make_fake_backend_module(provider_name: str, class_name: str) -> types.ModuleType:
    """Build a stub backend module that records the config it was constructed from."""
    module = types.ModuleType(f"persona.imagegen.{provider_name}_image")

    class _Stub:
        last_config: ImageBackendConfig | None = None

        def __init__(self, config: ImageBackendConfig) -> None:
            type(self).last_config = config
            self._provider = provider_name
            self._model = config.model

        @property
        def provider_name(self) -> str:
            return self._provider

        @property
        def model_name(self) -> str:
            return self._model

        async def generate(
            self,
            prompt: str,  # noqa: ARG002
            *,
            options: object = None,  # noqa: ARG002
        ) -> object:
            raise NotImplementedError

        async def edit(
            self,
            input_image: object,  # noqa: ARG002
            instructions: str,  # noqa: ARG002
            *,
            options: object = None,  # noqa: ARG002
        ) -> object:
            raise NotImplementedError("edit not supported in v1")

    _Stub.__name__ = class_name
    _Stub.__qualname__ = class_name
    setattr(module, class_name, _Stub)
    return module


@pytest.fixture
def stub_image_backends(monkeypatch: pytest.MonkeyPatch) -> dict[str, types.ModuleType]:
    """Install stub OpenAI / fal / nvidia / openrouter image-backend modules."""
    openai_mod = _make_fake_backend_module("openai", "OpenAIImageBackend")
    fal_mod = _make_fake_backend_module("fal", "FalImageBackend")
    nvidia_mod = _make_fake_backend_module("nvidia", "NvidiaImageBackend")
    openrouter_mod = _make_fake_backend_module("openrouter", "OpenRouterImageBackend")
    monkeypatch.setitem(sys.modules, "persona.imagegen.openai_image", openai_mod)
    monkeypatch.setitem(sys.modules, "persona.imagegen.fal_image", fal_mod)
    monkeypatch.setitem(sys.modules, "persona.imagegen.nvidia_image", nvidia_mod)
    monkeypatch.setitem(sys.modules, "persona.imagegen.openrouter_image", openrouter_mod)
    return {
        "openai": openai_mod,
        "fal": fal_mod,
        "nvidia": nvidia_mod,
        "openrouter": openrouter_mod,
    }


@pytest.fixture
def loguru_capture() -> Iterator[list[str]]:
    """Loguru sink capturing emitted INFO+ messages."""
    captured: list[str] = []
    sink_id = _loguru_logger.add(
        lambda msg: captured.append(str(msg)),
        level="INFO",
    )
    try:
        yield captured
    finally:
        _loguru_logger.remove(sink_id)


def _clear_imagegen_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip PERSONA_IMAGEGEN_* + per-provider API key env vars."""
    for suffix in ("MODELS", "PROVIDER", "MODEL", "API_KEY", "BASE_URL"):
        monkeypatch.delenv(f"PERSONA_IMAGEGEN_{suffix}", raising=False)
    for provider in ("OPENAI", "FAL", "NVIDIA", "ANTHROPIC", "DEEPSEEK", "GROQ", "TOGETHER"):
        monkeypatch.delenv(f"PERSONA_{provider}_API_KEY", raising=False)
        monkeypatch.delenv(f"PERSONA_{provider}_BASE_URL", raising=False)


# --------------------------------------------------------------------------- #
# D-20-17 case (a) — MODELS-only
# --------------------------------------------------------------------------- #


class TestImageCaseA_ModelsOnly:
    def test_two_providers_builds_multi_model_image_backend(
        self,
        monkeypatch: pytest.MonkeyPatch,
        stub_image_backends: dict[str, types.ModuleType],
    ) -> None:
        _clear_imagegen_env(monkeypatch)
        monkeypatch.setenv(
            "PERSONA_IMAGEGEN_MODELS",
            "nvidia/flux.2-klein-4b,fal/flux-1.1-pro",
        )
        monkeypatch.setenv("PERSONA_NVIDIA_API_KEY", "nvapi-test")
        monkeypatch.setenv("PERSONA_FAL_API_KEY", "fal-test")

        backend = load_image_backend_from_env()
        assert isinstance(backend, MultiModelImageBackend)
        assert backend.tier_name == "imagegen"
        # Chain order matches D-20-4 (CSV order preserved).
        assert [b.provider_name for b in backend.backends] == ["nvidia", "fal"]
        assert [b.model_name for b in backend.backends] == ["flux.2-klein-4b", "flux-1.1-pro"]

    def test_length_one_bypasses_wrapper(
        self,
        monkeypatch: pytest.MonkeyPatch,
        stub_image_backends: dict[str, types.ModuleType],
    ) -> None:
        _clear_imagegen_env(monkeypatch)
        monkeypatch.setenv("PERSONA_IMAGEGEN_MODELS", "nvidia/flux.2-klein-4b")
        monkeypatch.setenv("PERSONA_NVIDIA_API_KEY", "nvapi-test")

        backend = load_image_backend_from_env()
        assert not isinstance(backend, MultiModelImageBackend)
        assert backend.provider_name == "nvidia"
        assert backend.model_name == "flux.2-klein-4b"


# --------------------------------------------------------------------------- #
# D-20-17 case (b) — Triplet-only (backward-compat)
# --------------------------------------------------------------------------- #


class TestImageCaseB_TripletOnly:
    def test_triplet_only_builds_bare_backend(
        self,
        monkeypatch: pytest.MonkeyPatch,
        stub_image_backends: dict[str, types.ModuleType],
    ) -> None:
        _clear_imagegen_env(monkeypatch)
        monkeypatch.setenv("PERSONA_IMAGEGEN_PROVIDER", "openai")
        monkeypatch.setenv("PERSONA_IMAGEGEN_MODEL", "gpt-image-1")
        monkeypatch.setenv("PERSONA_IMAGEGEN_API_KEY", "sk-test")

        backend = load_image_backend_from_env()
        assert not isinstance(backend, MultiModelImageBackend)
        assert backend.provider_name == "openai"
        assert backend.model_name == "gpt-image-1"


# --------------------------------------------------------------------------- #
# D-20-17 case (c) — BOTH set: MODELS wins + INFO log
# --------------------------------------------------------------------------- #


class TestImageCaseC_BothSet:
    def test_both_set_models_wins_and_info_log_emitted(
        self,
        monkeypatch: pytest.MonkeyPatch,
        stub_image_backends: dict[str, types.ModuleType],
        loguru_capture: list[str],
    ) -> None:
        _clear_imagegen_env(monkeypatch)
        monkeypatch.setenv(
            "PERSONA_IMAGEGEN_MODELS",
            "nvidia/flux.2-klein-4b,openai/gpt-image-1",
        )
        # Triplet vars set too — should be ignored.
        monkeypatch.setenv("PERSONA_IMAGEGEN_PROVIDER", "fal")
        monkeypatch.setenv("PERSONA_IMAGEGEN_MODEL", "fal-ai/flux-pro/v1.1")
        monkeypatch.setenv("PERSONA_IMAGEGEN_API_KEY", "fal-triplet-key")
        monkeypatch.setenv("PERSONA_NVIDIA_API_KEY", "nvapi-test")
        monkeypatch.setenv("PERSONA_OPENAI_API_KEY", "sk-openai-test")

        backend = load_image_backend_from_env()
        assert isinstance(backend, MultiModelImageBackend)
        assert [b.provider_name for b in backend.backends] == ["nvidia", "openai"]

        case_c_msgs = [msg for msg in loguru_capture if "case (c)" in msg]
        assert len(case_c_msgs) == 1
        msg = case_c_msgs[0]
        assert "PERSONA_IMAGEGEN_PROVIDER" in msg
        assert "PERSONA_IMAGEGEN_MODEL" in msg
        assert "PERSONA_IMAGEGEN_API_KEY" in msg


# --------------------------------------------------------------------------- #
# D-20-17 case (d) — Malformed
# --------------------------------------------------------------------------- #


class TestImageCaseD_Malformed:
    def test_missing_slash_raises(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _clear_imagegen_env(monkeypatch)
        monkeypatch.setenv("PERSONA_IMAGEGEN_MODELS", "no-slash-here")
        with pytest.raises(MalformedTierModelsError) as exc_info:
            load_image_backend_from_env()
        assert exc_info.value.context["reason"] == "missing_slash"
        assert exc_info.value.context["tier"] == "imagegen"

    def test_unknown_provider_raises(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _clear_imagegen_env(monkeypatch)
        monkeypatch.setenv("PERSONA_IMAGEGEN_MODELS", "fake_provider/whatever")
        with pytest.raises(MalformedTierModelsError) as exc_info:
            load_image_backend_from_env()
        assert exc_info.value.context["reason"] == "unknown_provider"

    def test_local_in_models_rejected_with_hint(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """D-20-18 — ``local`` token raises with operator-actionable hint."""
        _clear_imagegen_env(monkeypatch)
        monkeypatch.setenv("PERSONA_IMAGEGEN_MODELS", "local/some-hf-model")
        with pytest.raises(LocalProviderInModelsListError) as exc_info:
            load_image_backend_from_env()
        ctx = exc_info.value.context
        assert ctx["tier"] == "imagegen"
        assert "single-backend fast path" in ctx["hint"]

    def test_ollama_in_models_rejected_with_hint(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """D-20-18 — ``ollama`` token raises with operator-actionable hint."""
        _clear_imagegen_env(monkeypatch)
        monkeypatch.setenv("PERSONA_IMAGEGEN_MODELS", "ollama/llama-3.1")
        with pytest.raises(LocalProviderInModelsListError):
            load_image_backend_from_env()


# --------------------------------------------------------------------------- #
# D-20-15 per-slot disposition
# --------------------------------------------------------------------------- #


class TestImageD20_15Disposition:
    def test_all_providers_missing_credentials_raises_tier_not_configured(
        self,
        monkeypatch: pytest.MonkeyPatch,
        stub_image_backends: dict[str, types.ModuleType],
    ) -> None:
        _clear_imagegen_env(monkeypatch)
        monkeypatch.setenv(
            "PERSONA_IMAGEGEN_MODELS",
            "nvidia/flux.2-klein-4b,openai/gpt-image-1",
        )
        # No keys set → all slots fail.
        with pytest.raises(TierNotConfiguredError) as exc_info:
            load_image_backend_from_env()
        ctx = exc_info.value.context
        assert ctx["tier"] == "imagegen"
        assert "nvidia/flux.2-klein-4b" in ctx["configured_models"]
        assert "openai/gpt-image-1" in ctx["configured_models"]
        assert "PERSONA_NVIDIA_API_KEY" in ctx["consulted_env_vars"]
        assert "PERSONA_OPENAI_API_KEY" in ctx["consulted_env_vars"]

    def test_chat_only_provider_in_models_list_raises_malformed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        stub_image_backends: dict[str, types.ModuleType],
    ) -> None:
        """Chat-only provider (no :class:`ImageBackend` impl) fails at parse layer.

        The image-gen MODELS-list parser narrows the chat-side Provider
        Literal to ``_IMAGE_PROVIDERS`` (``openai`` / ``fal`` / ``nvidia``),
        so ``anthropic`` is ``unknown_provider`` at parse time rather than
        a downstream ``ImageProviderError`` at dispatch time. Mirrors T11's
        D-20-17 case (d) shape.
        """
        _clear_imagegen_env(monkeypatch)
        monkeypatch.setenv(
            "PERSONA_IMAGEGEN_MODELS",
            "anthropic/claude-sonnet-4-6,openai/gpt-image-1",
        )
        monkeypatch.setenv("PERSONA_ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setenv("PERSONA_OPENAI_API_KEY", "sk-openai-test")
        with pytest.raises(MalformedTierModelsError) as exc_info:
            load_image_backend_from_env()
        assert exc_info.value.context["reason"] == "unknown_provider"
        assert "openai" in exc_info.value.context["supported"]


# --------------------------------------------------------------------------- #
# Unset path — neither MODELS nor triplet
# --------------------------------------------------------------------------- #


class TestImageUnset:
    def test_unset_falls_back_to_default_image_backend_config(
        self,
        monkeypatch: pytest.MonkeyPatch,
        stub_image_backends: dict[str, types.ModuleType],
    ) -> None:
        """When neither form is set, the factory builds a default
        :class:`ImageBackendConfig` — provider defaults to ``openrouter``
        (Spec M3 D-M3-10) and the concrete backend's fail-fast surfaces missing
        api_key."""
        _clear_imagegen_env(monkeypatch)
        # No env vars set → ImageBackendConfig.from_env() yields the default
        # provider=openrouter with api_key=None. The stub doesn't fail on missing
        # key, so we just verify the bare-single path is taken.
        backend = load_image_backend_from_env()
        assert not isinstance(backend, MultiModelImageBackend)
        assert backend.provider_name == "openrouter"


class TestImageOpenRouterFreeModeDrop:
    """Spec 22 D-22-20 — in free-mode, ALL openrouter image entries drop
    (no :free image-output models exist on OpenRouter)."""

    def test_free_mode_drops_all_openrouter_keeps_other(
        self,
        monkeypatch: pytest.MonkeyPatch,
        stub_image_backends: dict[str, types.ModuleType],
    ) -> None:
        _clear_imagegen_env(monkeypatch)
        monkeypatch.setenv(
            "PERSONA_IMAGEGEN_MODELS",
            "openrouter/google/gemini-2.5-flash-image,nvidia/flux.2-klein-4b",
        )
        monkeypatch.setenv("PERSONA_OPENROUTER_API_KEY", "sk-or-v1-test")
        monkeypatch.setenv("PERSONA_NVIDIA_API_KEY", "nvapi-test")

        backend = load_image_backend_from_env(openrouter_subscription_mode="free")
        # openrouter dropped → only nvidia survives → length-1 bare backend.
        assert not isinstance(backend, MultiModelImageBackend)
        assert backend.provider_name == "nvidia"

    def test_paid_mode_keeps_openrouter(
        self,
        monkeypatch: pytest.MonkeyPatch,
        stub_image_backends: dict[str, types.ModuleType],
    ) -> None:
        _clear_imagegen_env(monkeypatch)
        monkeypatch.setenv("PERSONA_IMAGEGEN_MODELS", "openrouter/google/gemini-2.5-flash-image")
        monkeypatch.setenv("PERSONA_OPENROUTER_API_KEY", "sk-or-v1-test")

        backend = load_image_backend_from_env(openrouter_subscription_mode="paid")
        assert backend.provider_name == "openrouter"
        assert backend.model_name == "google/gemini-2.5-flash-image"

    def test_none_mode_keeps_openrouter(
        self,
        monkeypatch: pytest.MonkeyPatch,
        stub_image_backends: dict[str, types.ModuleType],
    ) -> None:
        _clear_imagegen_env(monkeypatch)
        monkeypatch.setenv("PERSONA_IMAGEGEN_MODELS", "openrouter/google/gemini-2.5-flash-image")
        monkeypatch.setenv("PERSONA_OPENROUTER_API_KEY", "sk-or-v1-test")

        backend = load_image_backend_from_env()
        assert backend.provider_name == "openrouter"

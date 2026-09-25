"""Cross-spec integration test for OpenRouter (Spec 22 T15).

Exercises the OpenRouter feature end-to-end across the composition root
(:mod:`persona_api.app`), the chat :class:`TierRegistry`
(:func:`persona_runtime.tier.tier_registry_from_env`), the image-gen factory
(:func:`persona.imagegen.load_image_backend_from_env`), and Spec 20's
:class:`MultiModelChatBackend` — the dimensions Spec 22's acceptance criteria
turn on:

* **AC4 / AC5 / D-22-2** — subscription mode resolved once at the composition
  root is threaded into both factories; free-mode drops non-``:free``
  ``openrouter`` chat entries.
* **AC8** — cross-provider fallback: ``openrouter/...,nvidia/...`` in one MODELS
  list builds a :class:`MultiModelChatBackend` with both backends, in order,
  through Spec 20's wrapper **unchanged**.
* **AC9** — capability inference: an ``openrouter`` backend reports
  tools/vision inferred from the underlying model (tier-3, no catalog).
* **D-22-20** — free-mode drops ALL ``openrouter`` image entries.
* The composition-root mode resolution itself (D-22-3 / D-22-9) is unit tested
  beside the resolver, in ``persona_runtime`` (R9-224).

No network: backends construct their SDK clients without calling out, and the
subscription probe is replaced by an in-memory fake. The live smoke matrix is
an ``external`` concern (operator-run, not CI).
"""

from __future__ import annotations

import pytest
from persona.backends.multi_model import MultiModelChatBackend
from persona.imagegen import load_image_backend_from_env
from persona.imagegen.multi_model_image import MultiModelImageBackend
from persona_api.app import _compose_image_backend
from persona_runtime.openrouter_subscription import resolve_openrouter_subscription_mode
from persona_runtime.tier import tier_registry_from_env

pytestmark = pytest.mark.integration


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for tier in ("FRONTIER", "MID", "SMALL", "IMAGEGEN"):
        for suffix in ("MODELS", "PROVIDER", "MODEL", "API_KEY", "BASE_URL"):
            monkeypatch.delenv(f"PERSONA_{tier}_{suffix}", raising=False)
    for provider in ("OPENROUTER", "NVIDIA", "ANTHROPIC", "OPENAI", "FAL"):
        monkeypatch.delenv(f"PERSONA_{provider}_API_KEY", raising=False)
        monkeypatch.delenv(f"PERSONA_{provider}_BASE_URL", raising=False)
    monkeypatch.delenv("PERSONA_OPENROUTER_SUBSCRIPTION_MODE", raising=False)


# ---------------------------------------------------------------------------
# AC8 — cross-provider fallback through the Spec 20 wrapper (unchanged)
# ---------------------------------------------------------------------------


class TestCrossProviderFallback:
    def test_openrouter_plus_nvidia_builds_ordered_wrapper(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_env(monkeypatch)
        monkeypatch.setenv(
            "PERSONA_FRONTIER_MODELS",
            "openrouter/anthropic/claude-3.5-sonnet,nvidia/llama-3.3-nemotron-super-49b-v1.5",
        )
        monkeypatch.setenv("PERSONA_OPENROUTER_API_KEY", "sk-or-v1-test")
        monkeypatch.setenv("PERSONA_NVIDIA_API_KEY", "nvapi-test")

        reg = tier_registry_from_env()
        backend = reg.get("frontier")
        assert isinstance(backend, MultiModelChatBackend)
        # OpenRouter primary, NVIDIA fallback — CSV order preserved (D-20-4),
        # the Spec 20 wrapper handles both as ordinary providers (D-22 §5).
        assert [b.provider_name for b in backend.backends] == ["openrouter", "nvidia"]
        assert backend.backends[0].model_name == "anthropic/claude-3.5-sonnet"


# ---------------------------------------------------------------------------
# AC9 — capability inference end-to-end (tier-1 + tier-3, no catalog)
# ---------------------------------------------------------------------------


class TestCapabilityInferenceEndToEnd:
    def test_openrouter_backend_infers_tools_and_vision(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_env(monkeypatch)
        monkeypatch.setenv("PERSONA_MID_MODELS", "openrouter/anthropic/claude-3.5-sonnet")
        monkeypatch.setenv("PERSONA_OPENROUTER_API_KEY", "sk-or-v1-test")

        backend = tier_registry_from_env().get("mid")
        # Inferred from the underlying Anthropic "all"/"all" matrix rows.
        assert backend.supports_native_tools is True
        assert backend.supports_vision is True

    def test_openrouter_free_slug_tools_inferred_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_env(monkeypatch)
        monkeypatch.setenv("PERSONA_MID_MODELS", "openrouter/anthropic/claude-3.5-sonnet:free")
        monkeypatch.setenv("PERSONA_OPENROUTER_API_KEY", "sk-or-v1-test")

        backend = tier_registry_from_env().get("mid")
        # D-22-10c asymmetric conservatism: :free → tools False, vision via base.
        assert backend.supports_native_tools is False
        assert backend.supports_vision is True


# ---------------------------------------------------------------------------
# D-22-2 — chat free-mode filter threaded from the composition root
# ---------------------------------------------------------------------------


class TestChatFreeModeFilterEndToEnd:
    def test_free_mode_drops_non_free_keeps_free_and_nvidia(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_env(monkeypatch)
        monkeypatch.setenv(
            "PERSONA_FRONTIER_MODELS",
            "openrouter/anthropic/claude-3.5-sonnet,"  # non-:free → dropped
            "openrouter/meta-llama/llama-3.3-70b-instruct:free,"  # :free → kept
            "nvidia/llama-3.3-nemotron-super-49b-v1.5",  # non-openrouter → kept
        )
        monkeypatch.setenv("PERSONA_OPENROUTER_API_KEY", "sk-or-v1-test")
        monkeypatch.setenv("PERSONA_NVIDIA_API_KEY", "nvapi-test")

        # mode resolved as free by the env override → threaded into the registry.
        monkeypatch.setenv("PERSONA_OPENROUTER_SUBSCRIPTION_MODE", "free")
        mode = resolve_openrouter_subscription_mode()
        reg = tier_registry_from_env(openrouter_subscription_mode=mode)
        backend = reg.get("frontier")
        assert isinstance(backend, MultiModelChatBackend)
        assert [b.provider_name for b in backend.backends] == ["openrouter", "nvidia"]
        assert backend.backends[0].model_name == "meta-llama/llama-3.3-70b-instruct:free"


# ---------------------------------------------------------------------------
# D-22-20 — image free-mode drop, threaded through _compose_image_backend
# ---------------------------------------------------------------------------


class TestImageFreeModeDropEndToEnd:
    def test_paid_mode_keeps_openrouter_image(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_env(monkeypatch)
        monkeypatch.setenv("PERSONA_IMAGEGEN_MODELS", "openrouter/google/gemini-2.5-flash-image")
        monkeypatch.setenv("PERSONA_OPENROUTER_API_KEY", "sk-or-v1-test")

        backend = load_image_backend_from_env(openrouter_subscription_mode="paid")
        assert backend.provider_name == "openrouter"

    def test_free_mode_drops_openrouter_keeps_other(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_env(monkeypatch)
        # Pair with openai/gpt-image-1 — a real backend that constructs cleanly
        # from just an api key (the survivor after the openrouter drop).
        monkeypatch.setenv(
            "PERSONA_IMAGEGEN_MODELS",
            "openrouter/google/gemini-2.5-flash-image,openai/gpt-image-1",
        )
        monkeypatch.setenv("PERSONA_OPENROUTER_API_KEY", "sk-or-v1-test")
        monkeypatch.setenv("PERSONA_OPENAI_API_KEY", "sk-openai-test")

        # _compose_image_backend threads the mode into load_image_backend_from_env.
        backend = _compose_image_backend("free")
        assert backend is not None
        # openrouter dropped (D-22-20) → only openai survives → bare backend.
        assert not isinstance(backend, MultiModelImageBackend)
        assert backend.provider_name == "openai"

    def test_compose_image_backend_none_mode_keeps_openrouter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_env(monkeypatch)
        monkeypatch.setenv("PERSONA_IMAGEGEN_MODELS", "openrouter/google/gemini-2.5-flash-image")
        monkeypatch.setenv("PERSONA_OPENROUTER_API_KEY", "sk-or-v1-test")

        backend = _compose_image_backend(None)
        assert backend is not None
        assert backend.provider_name == "openrouter"

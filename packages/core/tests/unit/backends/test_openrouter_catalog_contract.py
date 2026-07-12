"""Public-shape stability contract for the OpenRouter catalog client (T12).

Spec 22 ships :mod:`persona.backends.openrouter_catalog` as the **metadata
source Spec 23 consumes** for intelligent routing. This module pins that
read surface — method names, parameter names, and the capability/metadata
properties Spec 23 reads — so an accidental signature change trips here
before it breaks the downstream consumer. These are deliberately rigid
assertions; updating them is a conscious API-evolution decision, not a
mechanical edit.

Scope guard, not behaviour: the behavioural tests live in
``test_openrouter_catalog.py``.
"""

from __future__ import annotations

import inspect

from persona.backends import openrouter_catalog
from persona.backends.openrouter_catalog import (
    OpenRouterCatalogClient,
    OpenRouterKeyInfo,
    OpenRouterModelEntry,
    OpenRouterSubscriptionState,
    free_mode_fallback,
    subscription_state_from_key_info,
)

# The exact set of names Spec 23 (and any other consumer) may import.
_STABLE_PUBLIC_SURFACE: frozenset[str] = frozenset(
    {
        "OpenRouterArchitecture",
        "OpenRouterCatalogClient",
        "OpenRouterKeyInfo",
        "OpenRouterModelEntry",
        "OpenRouterPricing",
        "OpenRouterSubscriptionMode",
        "OpenRouterSubscriptionState",
        # Spec M2 (D-M2-6) — a conscious contract extension, not leakage: the
        # catalog TTL default + its env reader join the stable surface (the
        # composition roots that build priced-data clients import them).
        "DEFAULT_CATALOG_TTL_S",
        "catalog_ttl_from_env",
        "free_mode_fallback",
        "subscription_state_from_key_info",
    }
)


class TestModuleExportSurface:
    def test_dunder_all_is_exactly_the_stable_surface(self) -> None:
        # Adding to __all__ is allowed only by updating this contract.
        assert set(openrouter_catalog.__all__) >= _STABLE_PUBLIC_SURFACE
        # strip_dynamic_variant is a documented helper; everything else in
        # __all__ must be a known stable name (no accidental leakage).
        allowed = _STABLE_PUBLIC_SURFACE | {"strip_dynamic_variant"}
        assert set(openrouter_catalog.__all__) <= allowed


class TestCatalogClientSurface:
    def test_list_models_signature(self) -> None:
        sig = inspect.signature(OpenRouterCatalogClient.list_models)
        assert list(sig.parameters) == ["self", "force_refresh"]
        assert sig.parameters["force_refresh"].kind is inspect.Parameter.KEYWORD_ONLY
        assert sig.parameters["force_refresh"].default is False

    def test_get_key_info_signature(self) -> None:
        sig = inspect.signature(OpenRouterCatalogClient.get_key_info)
        assert list(sig.parameters) == ["self"]

    def test_close_exists(self) -> None:
        assert callable(OpenRouterCatalogClient.close)

    def test_constructor_signature(self) -> None:
        sig = inspect.signature(OpenRouterCatalogClient.__init__)
        params = sig.parameters
        assert "api_key" in params
        # ``ttl_s`` joined at Spec M2 (D-M2-6) — keyword-only like its peers,
        # defaulted so every pre-M2 construction site is untouched.
        for kw in ("base_url", "timeout_s", "transport", "ttl_s"):
            assert params[kw].kind is inspect.Parameter.KEYWORD_ONLY

    def test_ttl_surface_present(self) -> None:
        # Spec M2 (D-M2-6): the freshness surface the /v1/models path and the
        # api lifespan task consume. ``list_models`` semantics are UNCHANGED
        # (staleness is advisory; only refresh_if_stale fetches).
        assert isinstance(OpenRouterCatalogClient.is_stale, property)
        assert isinstance(OpenRouterCatalogClient.ttl_s, property)
        sig = inspect.signature(OpenRouterCatalogClient.refresh_if_stale)
        assert list(sig.parameters) == ["self"]


class TestModelEntryMetadataSurface:
    """The metadata fields + capability properties Spec 23 reads."""

    def test_metadata_fields_present(self) -> None:
        fields = OpenRouterModelEntry.model_fields
        for name in ("id", "context_length", "pricing", "architecture", "supported_parameters"):
            assert name in fields, f"OpenRouterModelEntry must expose '{name}' for Spec 23"

    def test_capability_properties_present(self) -> None:
        for prop in ("is_free", "supports_tools", "supports_vision"):
            assert isinstance(getattr(OpenRouterModelEntry, prop), property), (
                f"OpenRouterModelEntry.{prop} must remain a property for Spec 23"
            )

    def test_key_info_fields_present(self) -> None:
        for name in ("is_free_tier", "limit", "limit_remaining", "usage"):
            assert name in OpenRouterKeyInfo.model_fields


class TestSubscriptionStateSurface:
    def test_state_fields_present(self) -> None:
        for name in ("mode", "is_free_tier", "limit_remaining", "last_checked_at", "probe_failed"):
            assert name in OpenRouterSubscriptionState.model_fields

    def test_state_is_frozen(self) -> None:
        assert OpenRouterSubscriptionState.model_config.get("frozen") is True

    def test_mapper_signatures(self) -> None:
        from_info = inspect.signature(subscription_state_from_key_info)
        assert list(from_info.parameters) == ["key_info", "checked_at"]
        assert from_info.parameters["checked_at"].kind is inspect.Parameter.KEYWORD_ONLY

        fallback = inspect.signature(free_mode_fallback)
        assert list(fallback.parameters) == ["checked_at", "reason"]
        for name in ("checked_at", "reason"):
            assert fallback.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY

"""Tests for ``persona.backends.openrouter_catalog`` (Spec 22 T10 + T11).

The HTTP boundary is driven by :class:`httpx.MockTransport` — no real
network. Covers catalog parsing + caching + alias filtering + error
translation (T10) and the pure subscription-state mappers (T11).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from persona.backends.errors import (
    AuthenticationError,
    OpenRouterBalanceProbeError,
    OpenRouterCatalogError,
)
from persona.backends.openrouter_catalog import (
    OpenRouterCatalogClient,
    OpenRouterKeyInfo,
    OpenRouterModelEntry,
    free_mode_fallback,
    strip_dynamic_variant,
    subscription_state_from_key_info,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_CHECKED_AT = datetime(2026, 6, 11, tzinfo=UTC)


def _model_item(
    model_id: str,
    *,
    tools: bool = False,
    vision: bool = False,
    prompt: str = "0.000003",
    expiration_date: str | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": model_id,
        "canonical_slug": model_id.split(":")[0],
        "name": model_id,
        "context_length": 128000,
        "pricing": {"prompt": prompt, "completion": "0.000015", "image": "0.01"},
        "architecture": {
            "input_modalities": (["text", "image"] if vision else ["text"]),
            "output_modalities": ["text"],
            "tokenizer": "Claude",
        },
        "supported_parameters": (["tools", "tool_choice"] if tools else ["temperature"]),
        "extra_future_field": "ignored",  # exercises extra="ignore" (D-22-12)
    }
    if expiration_date is not None:
        item["expiration_date"] = expiration_date
    return item


def _client(
    handler: Callable[[httpx.Request], httpx.Response], *, calls: list[int] | None = None
) -> OpenRouterCatalogClient:
    def _wrapped(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(1)
        return handler(request)

    return OpenRouterCatalogClient("sk-or-v1-test", transport=httpx.MockTransport(_wrapped))


# ---------------------------------------------------------------------------
# T10 — list_models
# ---------------------------------------------------------------------------


class TestListModels:
    def test_parses_catalog_entries(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "data": [
                        _model_item("anthropic/claude-3.5-sonnet", tools=True, vision=True),
                        _model_item("deepseek/deepseek-chat:free", tools=True),
                    ]
                },
            )

        client = _client(handler)
        models = client.list_models()
        assert len(models) == 2
        first = models[0]
        assert isinstance(first, OpenRouterModelEntry)
        assert first.id == "anthropic/claude-3.5-sonnet"
        assert first.supports_tools is True
        assert first.supports_vision is True
        assert first.is_free is False
        assert first.pricing.prompt == Decimal("0.000003")
        assert models[1].is_free is True
        assert models[1].supports_vision is False
        client.close()

    def test_parses_expiration_date_when_present(self) -> None:
        """``expiration_date`` (the M1-T5 announced-EOL signal) round-trips as-is."""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "data": [
                        _model_item("arcee-ai/trinity-mini", expiration_date="2026-07-10"),
                    ]
                },
            )

        models = _client(handler).list_models()
        assert models[0].expiration_date == "2026-07-10"

    def test_expiration_date_defaults_to_none_when_absent(self) -> None:
        """Entries without ``expiration_date`` in the payload parse to ``None`` (not live-EOL)."""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": [_model_item("openai/gpt-4o")]})

        models = _client(handler).list_models()
        assert models[0].expiration_date is None

    def test_caches_until_force_refresh(self) -> None:
        calls: list[int] = []

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": [_model_item("openai/gpt-4o")]})

        client = _client(handler, calls=calls)
        client.list_models()
        client.list_models()
        assert len(calls) == 1  # second call served from cache (D-22-5)
        client.list_models(force_refresh=True)
        assert len(calls) == 2
        client.close()

    def test_filters_tilde_prefixed_aliases(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "data": [
                        _model_item("~anthropic/claude-sonnet-latest"),
                        _model_item("anthropic/claude-3.5-sonnet"),
                    ]
                },
            )

        models = _client(handler).list_models()
        assert [m.id for m in models] == ["anthropic/claude-3.5-sonnet"]  # D-22-14

    def test_skips_unparseable_entry_but_keeps_valid(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"data": [{"no_id": "broken"}, _model_item("openai/gpt-4o")]},
            )

        models = _client(handler).list_models()
        assert [m.id for m in models] == ["openai/gpt-4o"]

    def test_missing_data_array_raises_malformed(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"not_data": []})

        with pytest.raises(OpenRouterCatalogError) as info:
            _client(handler).list_models()
        assert info.value.context["reason"] == "malformed_response"

    def test_http_5xx_raises_http_error(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"error": {"message": "down", "code": 503}})

        with pytest.raises(OpenRouterCatalogError) as info:
            _client(handler).list_models()
        assert info.value.context["reason"] == "http_error"
        assert info.value.context["status_code"] == "503"

    def test_timeout_raises_timeout_reason(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("slow", request=request)

        with pytest.raises(OpenRouterCatalogError) as info:
            _client(handler).list_models()
        assert info.value.context["reason"] == "timeout"

    def test_non_object_body_raises_malformed(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=["not", "an", "object"])

        with pytest.raises(OpenRouterCatalogError) as info:
            _client(handler).list_models()
        assert info.value.context["reason"] == "malformed_response"


# ---------------------------------------------------------------------------
# T10 — get_key_info
# ---------------------------------------------------------------------------


class TestGetKeyInfo:
    def test_parses_paid_key(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "is_free_tier": False,
                        "limit": "10.0",
                        "limit_remaining": "7.5",
                        "usage": "2.5",
                    }
                },
            )

        info = _client(handler).get_key_info()
        assert isinstance(info, OpenRouterKeyInfo)
        assert info.is_free_tier is False
        assert info.limit_remaining == Decimal("7.5")

    def test_401_raises_authentication_error_loud(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": {"message": "User not found.", "code": 401}})

        with pytest.raises(AuthenticationError) as info:
            _client(handler).get_key_info()
        assert info.value.context["provider"] == "openrouter"
        assert info.value.context["status_code"] == "401"

    def test_5xx_raises_balance_probe_error(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": {"message": "boom", "code": 500}})

        with pytest.raises(OpenRouterBalanceProbeError) as info:
            _client(handler).get_key_info()
        assert info.value.context["reason"] == "http_error"

    def test_timeout_raises_balance_probe_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("slow", request=request)

        with pytest.raises(OpenRouterBalanceProbeError) as info:
            _client(handler).get_key_info()
        assert info.value.context["reason"] == "timeout"

    def test_missing_data_object_raises_malformed(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"no_data": True})

        with pytest.raises(OpenRouterBalanceProbeError) as info:
            _client(handler).get_key_info()
        assert info.value.context["reason"] == "malformed_response"


# ---------------------------------------------------------------------------
# T11 — subscription state mappers
# ---------------------------------------------------------------------------


class TestSubscriptionStateMappers:
    def test_paid_key_maps_to_paid_mode(self) -> None:
        info = OpenRouterKeyInfo(is_free_tier=False, limit_remaining=Decimal("5"))
        state = subscription_state_from_key_info(info, checked_at=_CHECKED_AT)
        assert state.mode == "paid"
        assert state.is_free_tier is False
        assert state.limit_remaining == Decimal("5")
        assert state.last_checked_at == _CHECKED_AT
        assert state.probe_failed is False

    def test_free_key_maps_to_free_mode(self) -> None:
        info = OpenRouterKeyInfo(is_free_tier=True)
        state = subscription_state_from_key_info(info, checked_at=_CHECKED_AT)
        assert state.mode == "free"
        assert state.is_free_tier is True

    def test_free_mode_fallback_flags_probe_failure(self) -> None:
        state = free_mode_fallback(checked_at=_CHECKED_AT, reason="timeout")
        assert state.mode == "free"
        assert state.is_free_tier is True
        assert state.probe_failed is True
        assert state.last_checked_at == _CHECKED_AT

    def test_subscription_state_is_frozen(self) -> None:
        state = free_mode_fallback(checked_at=_CHECKED_AT, reason="timeout")
        with pytest.raises(Exception, match="frozen|Instance is frozen|immutable"):
            state.mode = "paid"  # type: ignore[misc]


class TestStripDynamicVariant:
    @pytest.mark.parametrize(
        ("slug", "expected"),
        [
            ("anthropic/claude-3.5-sonnet:nitro", "anthropic/claude-3.5-sonnet"),
            ("openai/gpt-4o:floor", "openai/gpt-4o"),
            ("x/y:exacto", "x/y"),
            ("x/y:online", "x/y"),
            # static variants are separate catalog entries — left intact
            ("deepseek/deepseek-chat:free", "deepseek/deepseek-chat:free"),
            ("anthropic/claude-3.5-sonnet:thinking", "anthropic/claude-3.5-sonnet:thinking"),
            # no variant — unchanged
            ("anthropic/claude-3.5-sonnet", "anthropic/claude-3.5-sonnet"),
        ],
    )
    def test_strip(self, slug: str, expected: str) -> None:
        assert strip_dynamic_variant(slug) == expected

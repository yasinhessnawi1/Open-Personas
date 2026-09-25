"""The one OpenRouter subscription-mode resolver every process shares (R9-224).

:func:`persona_runtime.openrouter_subscription.resolve_openrouter_subscription_mode`
used to live in the api, where voice could not import it, so voice built its tier
registries with no mode at all. These tests moved here with it from the api's
integration suite (Spec 22 T15), which only CI ran; here they run on every machine.

Each probe is a REAL request through the real catalog client, answered by an
``httpx.MockTransport``, so the client's status mapping and the resolver's D-22-3 /
D-22-9 policy are what is under test. Nothing is monkeypatched below the client.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest
from loguru import logger as _loguru_logger
from persona.backends.openrouter_catalog import OpenRouterCatalogClient
from persona_runtime import openrouter_subscription
from persona_runtime.errors import InvalidSubscriptionModeError
from persona_runtime.openrouter_subscription import resolve_openrouter_subscription_mode

if TYPE_CHECKING:
    from collections.abc import Iterator

_MODE_ENV = "PERSONA_OPENROUTER_SUBSCRIPTION_MODE"


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("PERSONA_OPENROUTER_API_KEY", "PERSONA_OPENROUTER_BASE_URL", _MODE_ENV):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def resolver_errors() -> Iterator[list[str]]:
    """Every ERROR line the resolver module logs, rendered with all its extra fields."""
    captured: list[str] = []
    sink_id = _loguru_logger.add(
        lambda message: captured.append(str(message)),
        level="ERROR",
        format="{message} | {extra}",
        filter=lambda record: record["extra"].get("component") == "runtime.openrouter_subscription",
    )
    yield captured
    _loguru_logger.remove(sink_id)


def _openrouter_answers(
    monkeypatch: pytest.MonkeyPatch,
    *,
    status: int,
    body: dict[str, object] | None = None,
    text: str | None = None,
) -> list[str]:
    """Route the resolver's probe to a transport that answers ``status``.

    Returns the list of request paths the probe sent, so a test can prove the probe
    ran, or that it did not.
    """
    seen: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if text is not None:
            return httpx.Response(status, text=text)
        return httpx.Response(status, json=body if body is not None else {})

    class _Client(OpenRouterCatalogClient):
        def __init__(self, api_key: str, *, base_url: str | None = None) -> None:
            super().__init__(api_key, base_url=base_url, transport=httpx.MockTransport(_handler))

    monkeypatch.setattr(openrouter_subscription, "OpenRouterCatalogClient", _Client)
    return seen


def test_no_key_resolves_no_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    probes = _openrouter_answers(monkeypatch, status=200)

    assert resolve_openrouter_subscription_mode() is None
    assert probes == []


def test_the_override_is_honoured_without_a_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("PERSONA_OPENROUTER_API_KEY", "sk-or-v1-test")
    monkeypatch.setenv(_MODE_ENV, "paid")
    probes = _openrouter_answers(monkeypatch, status=500)

    assert resolve_openrouter_subscription_mode() == "paid"
    assert probes == []


def test_a_rejected_key_degrades_to_no_mode_instead_of_stopping_boot(
    monkeypatch: pytest.MonkeyPatch, resolver_errors: list[str]
) -> None:
    # D-22-9 is fail-loud at the resolver; at the composition root it is logged and
    # swallowed so one optional provider's bad key does not block startup.
    _clear_env(monkeypatch)
    monkeypatch.setenv("PERSONA_OPENROUTER_API_KEY", "sk-or-v1-test")
    probes = _openrouter_answers(monkeypatch, status=401)

    assert resolve_openrouter_subscription_mode() is None
    assert probes == ["/api/v1/key"]
    # Swallowed, but never silently: the operator is told the filter is off.
    disabled = [line for line in resolver_errors if "free-mode filtering disabled" in line]
    assert len(disabled) == 1
    assert disabled[0].startswith("OpenRouter API key rejected at startup")


def test_a_paid_account_resolves_paid(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("PERSONA_OPENROUTER_API_KEY", "sk-or-v1-test")
    probes = _openrouter_answers(monkeypatch, status=200, body={"data": {"is_free_tier": False}})

    assert resolve_openrouter_subscription_mode() == "paid"
    assert probes == ["/api/v1/key"]


def test_a_probe_that_fails_degrades_to_free_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    # D-22-3: a 5xx cannot confirm paid credits, so the mode degrades to free. This is
    # the case that makes two processes disagree when each probes on its own.
    _clear_env(monkeypatch)
    monkeypatch.setenv("PERSONA_OPENROUTER_API_KEY", "sk-or-v1-test")
    probes = _openrouter_answers(monkeypatch, status=503)

    assert resolve_openrouter_subscription_mode() == "free"
    assert probes == ["/api/v1/key"]


def test_a_probe_answered_with_html_degrades_to_free_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # R9-224 review: a 200 that is not JSON (a base URL missing /api/v1, a proxy page)
    # used to raise a raw decode error through this resolver and stop the process at
    # boot. It is now an unusable probe answer like any other, so D-22-3 applies.
    _clear_env(monkeypatch)
    monkeypatch.setenv("PERSONA_OPENROUTER_API_KEY", "sk-or-v1-test")
    probes = _openrouter_answers(monkeypatch, status=200, text="<html>Not the API</html>")

    assert resolve_openrouter_subscription_mode() == "free"
    assert probes == ["/api/v1/key"]


def test_an_invalid_override_fails_loud_for_the_api(monkeypatch: pytest.MonkeyPatch) -> None:
    # The api lets this propagate and refuses to boot; that behaviour is unchanged by
    # R9-224. It is still a ValueError, with the same message, and now also a named
    # error so that voice alone can catch it.
    _clear_env(monkeypatch)
    monkeypatch.setenv("PERSONA_OPENROUTER_API_KEY", "sk-or-v1-test")
    monkeypatch.setenv(_MODE_ENV, "premium")
    probes = _openrouter_answers(monkeypatch, status=200)

    with pytest.raises(InvalidSubscriptionModeError) as caught:
        resolve_openrouter_subscription_mode()

    assert isinstance(caught.value, ValueError)
    assert str(caught.value) == (
        "PERSONA_OPENROUTER_SUBSCRIPTION_MODE must be 'free' or 'paid' "
        "(case-insensitive); got 'premium'"
    )
    assert probes == []

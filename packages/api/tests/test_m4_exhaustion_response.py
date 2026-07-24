"""Graceful free-quota-exhaustion response (Spec M4, T5b).

A tiny app whose routes raise the exhaustion + fail-closed-tier errors; assert the
handlers return a graceful UPGRADE PROMPT (never a silent paid completion, never a 500),
with the copy in the API layer (Tension-5 — not the core "contact support" message). No DB.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from persona.errors import CreditsExhaustedError
from persona_api.billing.messages import (
    CREDITS_EXHAUSTED_DETAIL,
    FREE_CAPACITY_UNAVAILABLE_DETAIL,
    UPGRADE_ACTION,
)
from persona_api.errors import register_exception_handlers
from persona_runtime.errors import TierNotConfiguredError


def _app() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/credits")
    async def _credits() -> None:
        # M3's exhaustion cutoff raises this (with the internal message + balance context).
        raise CreditsExhaustedError(
            "Your free credits are used up. Top-up coming soon — contact support.",
            context={"balance": "0"},
        )

    @app.get("/tier")
    async def _tier() -> None:
        # The fail-closed free-tier path: an empty free registry raises this.
        raise TierNotConfiguredError(
            "no model tier is configured", context={"requested": "frontier"}
        )

    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(_app())


def test_credits_exhausted_returns_an_upgrade_prompt_not_a_completion(client: TestClient) -> None:
    resp = client.get("/credits")

    assert resp.status_code == 402  # a payment-required prompt, NOT a 200 paid completion
    body = resp.json()
    assert body["error"] == "credits_exhausted"
    assert body["action"] == UPGRADE_ACTION  # the structured CTA hint for the web
    assert body["detail"] == CREDITS_EXHAUSTED_DETAIL  # the api-layer upgrade copy
    assert body["context"] == {"balance": "0"}  # M3 context preserved


def test_exhaustion_copy_is_the_api_upgrade_prompt_not_core_contact_support(
    client: TestClient,
) -> None:
    """Tension-5: the user-facing copy is the API's upgrade prompt, never core's
    'Top-up coming soon — contact support.'"""
    detail = client.get("/credits").json()["detail"]
    assert "contact support" not in detail.lower()
    assert "upgrade" in detail.lower()


def test_unconfigured_free_tier_is_graceful_503_not_500(client: TestClient) -> None:
    resp = client.get("/tier")

    assert resp.status_code == 503  # graceful "capacity unavailable", NOT a leaked 500
    body = resp.json()
    assert body["error"] == "model_capacity_unavailable"
    assert body["action"] == UPGRADE_ACTION
    assert body["detail"] == FREE_CAPACITY_UNAVAILABLE_DETAIL
    assert resp.headers.get("Retry-After") == "30"


def test_tier_error_body_never_leaks_the_internal_context(client: TestClient) -> None:
    """The 503 body carries NO internal detail (no requested-tier context leak)."""
    body = client.get("/tier").json()
    assert "context" not in body  # generic graceful body only
    assert "frontier" not in str(body)

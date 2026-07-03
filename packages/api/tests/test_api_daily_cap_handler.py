"""Unit tests for the daily-spend-cap → HTTP handler (Spec R7, T5 / R7-D-5).

:class:`DailySpendCapExceededError` → **429 + ``Retry-After`` = seconds to the
next UTC midnight** + a structured body carrying ``cap`` / ``spent`` /
``reset_epoch``. Distinct from :class:`CreditsExhaustedError` (→ 402, "out of
credits forever"): the day-cap is "capped for *today*, back tomorrow" — 429 is the
correct "come back later", 402 would wrongly imply a permanent state.

Minimal FastAPI app + TestClient; no DB — purely the handler mapping.
"""

from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from persona.errors import DailySpendCapExceededError
from persona_api.errors import register_exception_handlers

_RESET = int(time.time()) + 3600  # an hour out


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/_test/daily_cap")
    async def _raise_daily_cap() -> None:
        raise DailySpendCapExceededError(
            "Daily spend cap reached — this resets at UTC midnight.",
            context={
                "cap": "10000",
                "spent": "9950",
                "requested_cost": "100",
                "reset_epoch": str(_RESET),
            },
        )

    return TestClient(app)


def test_daily_cap_maps_to_429(client: TestClient) -> None:
    assert client.get("/_test/daily_cap").status_code == 429


def test_daily_cap_retry_after_counts_down_to_reset(client: TestClient) -> None:
    resp = client.get("/_test/daily_cap")
    retry_after = int(resp.headers["Retry-After"])
    # Seconds-to-reset (± a second of test wall-clock), positive, never past the reset.
    assert 3500 <= retry_after <= 3600


def test_daily_cap_body_carries_cap_spent_reset(client: TestClient) -> None:
    body = client.get("/_test/daily_cap").json()
    assert body["error"] == "daily_spend_cap_exceeded"
    assert body["context"]["cap"] == "10000"
    assert body["context"]["spent"] == "9950"
    assert body["context"]["reset_epoch"] == str(_RESET)
    assert body["detail"]


def test_daily_cap_distinct_from_credits_exhausted(client: TestClient) -> None:
    """The code is ``daily_spend_cap_exceeded`` (429), NOT ``credits_exhausted`` (402)."""
    resp = client.get("/_test/daily_cap")
    assert resp.status_code != 402
    assert resp.json()["error"] == "daily_spend_cap_exceeded"


def test_daily_cap_missing_reset_epoch_falls_back_to_60s() -> None:
    """A refusal with no ``reset_epoch`` in context still returns a sane Retry-After."""
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/_test/no_reset")
    async def _raise() -> None:
        raise DailySpendCapExceededError("capped", context={"cap": "1", "spent": "1"})

    resp = TestClient(app).get("/_test/no_reset")
    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == "60"

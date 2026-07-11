"""Unit tests for the exception handlers (spec 08, T02).

Mount a tiny app whose routes raise each domain exception, then assert the
handler maps it to the right status + structured body. No DB.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from persona.errors import PersonaNotFoundError, SchemaVersionMismatchError, ToolNotAllowedError
from persona_api.errors import (
    AuthenticationError,
    ConversationNotFoundError,
    CreditsExhaustedError,
    RateLimitExceededError,
    RunNotFoundError,
    ScheduleStateError,
    register_exception_handlers,
)
from pydantic import BaseModel


def _app() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/auth")
    async def _auth() -> None:
        raise AuthenticationError("no token")

    @app.get("/credits")
    async def _credits() -> None:
        raise CreditsExhaustedError("broke", context={"balance": "0"})

    @app.get("/tool")
    async def _tool() -> None:
        raise ToolNotAllowedError("nope", context={"allowed": "web_search"})

    @app.get("/persona")
    async def _persona() -> None:
        raise PersonaNotFoundError("missing", context={"id": "p1"})

    @app.get("/conv")
    async def _conv() -> None:
        raise ConversationNotFoundError()

    @app.get("/run")
    async def _run() -> None:
        raise RunNotFoundError()

    @app.get("/rate")
    async def _rate() -> None:
        raise RateLimitExceededError(
            "slow down",
            context={"limit": "20", "remaining": "0", "reset": "1700000000"},
        )

    @app.get("/schema")
    async def _schema() -> None:
        raise SchemaVersionMismatchError("bad version")

    @app.get("/schedule-state")
    async def _schedule_state() -> None:
        # R9-023: a one-time re-arm attempt on an already-fired schedule. Pre-fix this
        # exception had no specific handler and fell through the catch-all _domain_500.
        raise ScheduleStateError(
            "one-time schedule already fired — create a new schedule",
            context={"schedule_id": "s1"},
        )

    @app.get("/provider-auth")
    async def _provider_auth() -> None:
        # Spec R2 F-07: the PROVIDER-key auth error (a rotated/invalid cloud model
        # key mid-request), distinct from persona.errors.AuthenticationError. Pre-fix
        # this fell through the catch-all _domain_500 → 500; it must be 401.
        from persona.backends.errors import AuthenticationError as BackendAuthError

        raise BackendAuthError("provider rejected the API key", context={"secret": "sk-LEAKED123"})

    class Body(BaseModel):
        n: int

    @app.post("/validate")
    async def _validate(body: Body) -> dict[str, int]:
        return {"n": body.n}

    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(_app())


@pytest.mark.parametrize(
    ("path", "code", "error"),
    [
        ("/auth", 401, "authentication_error"),
        ("/credits", 402, "credits_exhausted"),
        ("/tool", 403, "tool_not_allowed"),
        ("/persona", 404, "persona_not_found"),
        ("/conv", 404, "conversation_not_found"),
        ("/run", 404, "run_not_found"),
        ("/rate", 429, "rate_limit_exceeded"),
        ("/schema", 422, "schema_version_mismatch"),
        ("/schedule-state", 409, "schedule_state_conflict"),
    ],
)
def test_exception_maps_to_status_and_body(
    client: TestClient, path: str, code: int, error: str
) -> None:
    resp = client.get(path)
    assert resp.status_code == code
    assert resp.json()["error"] == error


def test_provider_authentication_error_maps_to_401_not_500(client: TestClient) -> None:
    """Spec R2 F-07 (R2-D-8): a backend/provider ``AuthenticationError`` (rotated or
    invalid model key mid-request) returns 401, not the catch-all 500. FastAPI MRO
    dispatch selects the specific handler over the ``PersonaError`` ``_domain_500``."""
    resp = client.get("/provider-auth")
    assert resp.status_code == 401
    assert resp.json()["error"] == "provider_auth_failed"


def test_provider_authentication_error_body_does_not_leak_the_key(client: TestClient) -> None:
    """The 401 body must be generic — no provider message, no key/context leak."""
    resp = client.get("/provider-auth")
    body = resp.text
    assert "sk-LEAKED123" not in body, "the provider secret must never reach the client"
    assert "provider rejected the API key" not in body, "no internal provider detail in the body"


def test_auth_sets_www_authenticate_header(client: TestClient) -> None:
    resp = client.get("/auth")
    assert resp.headers["WWW-Authenticate"] == "Bearer"


def test_rate_limit_sets_headers(client: TestClient) -> None:
    resp = client.get("/rate")
    assert resp.headers["X-RateLimit-Limit"] == "20"
    assert resp.headers["X-RateLimit-Remaining"] == "0"
    assert resp.headers["X-RateLimit-Reset"] == "1700000000"
    assert "Retry-After" in resp.headers


def test_context_appears_in_body(client: TestClient) -> None:
    resp = client.get("/tool")
    assert resp.json()["context"] == {"allowed": "web_search"}


def test_schedule_state_conflict_carries_the_message_and_schedule_id(client: TestClient) -> None:
    """R9-023: the 409 body carries the clear, user-facing message + the schedule id context."""
    resp = client.get("/schedule-state")
    body = resp.json()
    assert body["detail"] == "one-time schedule already fired — create a new schedule"
    assert body["context"] == {"schedule_id": "s1"}


def test_invalid_body_returns_structured_422(client: TestClient) -> None:
    resp = client.post("/validate", json={"n": "not-an-int"})
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "validation_error"
    assert isinstance(body["detail"], list)

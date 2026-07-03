"""The connector ASGI app (Spec C2 T7) — webhook security + issue-route authz.

Driven through FastAPI's TestClient with injected fakes. Asserts the four things
gated for this task: constant-time secret check, validate-BEFORE-parse (a bad
secret never reaches the parser), unset-secret-fails-closed, and the issue route
deriving the owner from the verified JWT (never from the request body).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from persona.auth.jwt_verifier import AuthenticatedUser
from persona.errors import AuthenticationError
from persona_connectors.telegram.app import build_telegram_app
from persona_connectors.telegram.webhook import TELEGRAM_SECRET_HEADER
from pydantic import SecretStr

_SECRET = "hook-secret"  # noqa: S105 — test literal
_SECRET_OBJ = SecretStr(_SECRET)  # module-level singleton (avoids a call in arg defaults)
_WEBHOOK = "/telegram/webhook"
_ISSUE = "/v1/connectors/telegram/link"


def _app(
    *,
    secret: SecretStr | None = _SECRET_OBJ,
    received: list[dict[str, object]] | None = None,
    issued_for: list[str] | None = None,
    jwt_owner: str | None = "user_from_token",
) -> TestClient:
    received_list = received if received is not None else []
    issued_list = issued_for if issued_for is not None else []

    async def on_update(update: dict[str, object]) -> None:
        received_list.append(update)

    async def issue_deep_link(owner_id: str) -> str:
        issued_list.append(owner_id)
        return f"https://t.me/bot?start=token_for_{owner_id}"

    async def verify_jwt(token: str) -> AuthenticatedUser:
        if jwt_owner is None or token == "bad":
            raise AuthenticationError("invalid token")
        return AuthenticatedUser(id=jwt_owner, email=None)

    app = build_telegram_app(
        webhook_secret=secret,
        on_update=on_update,
        issue_deep_link=issue_deep_link,
        verify_jwt=verify_jwt,
    )
    return TestClient(app)


# --- webhook security ---


def test_webhook_accepts_a_valid_secret_and_dispatches() -> None:
    received: list[dict[str, object]] = []
    client = _app(received=received)
    resp = client.post(
        _WEBHOOK,
        json={"update_id": 1, "message": {"text": "hi"}},
        headers={TELEGRAM_SECRET_HEADER: _SECRET},
    )
    assert resp.status_code == 200
    assert received == [{"update_id": 1, "message": {"text": "hi"}}]


def test_webhook_rejects_a_bad_secret_before_parsing() -> None:
    """A wrong secret → 403, and the body is NEVER parsed/dispatched (validate-before-parse)."""
    received: list[dict[str, object]] = []
    client = _app(received=received)
    # Send deliberately invalid JSON: if the secret were checked after parsing, this
    # would 400 (a parse error). A 403 proves the secret gate runs FIRST.
    resp = client.post(
        _WEBHOOK,
        content=b"this is not json",
        headers={TELEGRAM_SECRET_HEADER: "wrong", "Content-Type": "application/json"},
    )
    assert resp.status_code == 403
    assert received == []  # never dispatched


def test_webhook_unset_secret_rejects_everything() -> None:
    """Fail-closed: with no secret configured, even a 'correct-looking' request is 403."""
    received: list[dict[str, object]] = []
    client = _app(secret=None, received=received)
    resp = client.post(
        _WEBHOOK, json={"update_id": 1}, headers={TELEGRAM_SECRET_HEADER: "anything"}
    )
    assert resp.status_code == 403
    assert received == []


def test_webhook_missing_header_is_rejected() -> None:
    client = _app()
    resp = client.post(_WEBHOOK, json={"update_id": 1})
    assert resp.status_code == 403


# --- issue route authorization ---


def test_issue_derives_owner_from_verified_jwt_not_body() -> None:
    """The deep link binds to the JWT's owner — a body 'owner_id' is ignored."""
    issued: list[str] = []
    client = _app(issued_for=issued, jwt_owner="real_owner")
    resp = client.post(
        _ISSUE,
        json={"owner_id": "attacker_chosen_owner"},  # MUST be ignored
        headers={"Authorization": "Bearer good"},
    )
    assert resp.status_code == 200
    assert resp.json()["deep_link"] == "https://t.me/bot?start=token_for_real_owner"
    assert issued == ["real_owner"]  # owner came from the token, not the body


def test_issue_requires_a_bearer_token() -> None:
    client = _app()
    resp = client.post(_ISSUE, json={})
    assert resp.status_code == 401


def test_issue_rejects_an_invalid_jwt() -> None:
    issued: list[str] = []
    client = _app(issued_for=issued)
    resp = client.post(_ISSUE, json={}, headers={"Authorization": "Bearer bad"})
    assert resp.status_code == 401
    assert issued == []  # no link minted on a failed verify


@pytest.mark.parametrize("auth", ["", "Token good", "good"])
def test_issue_rejects_malformed_authorization(auth: str) -> None:
    client = _app()
    resp = client.post(_ISSUE, json={}, headers={"Authorization": auth})
    assert resp.status_code == 401


def test_issue_carries_server_authoritative_expiry() -> None:
    """C6-D-8: the deep-link response carries expires_at = issue_time + ttl (the web's
    countdown keys off the server value, never a client constant that could drift)."""
    fixed = datetime(2026, 7, 3, 12, 0, 0, tzinfo=UTC)

    async def on_update(_update: dict[str, object]) -> None: ...

    async def issue_deep_link(owner_id: str) -> str:
        return f"https://t.me/bot?start=token_for_{owner_id}"

    async def verify_jwt(_token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id="real_owner", email=None)

    app = build_telegram_app(
        webhook_secret=_SECRET_OBJ,
        on_update=on_update,
        issue_deep_link=issue_deep_link,
        verify_jwt=verify_jwt,
        link_ttl=timedelta(minutes=15),
        now=lambda: fixed,
    )
    body = TestClient(app).post(_ISSUE, headers={"Authorization": "Bearer good"}).json()
    assert body == {
        "deep_link": "https://t.me/bot?start=token_for_real_owner",
        "expires_at": "2026-07-03T12:15:00+00:00",
    }

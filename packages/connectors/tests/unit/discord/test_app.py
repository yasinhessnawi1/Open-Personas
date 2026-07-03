"""The Discord connector ASGI app (Spec C3 ⛔) — issue-route authz + callback fail-closed.

Driven through FastAPI's TestClient with injected fakes. Asserts: the issue route
derives the owner from the verified JWT (never the request body); the OAuth callback
binds on a good state but **fails closed** (400, no bind) on a bad state or a failed
exchange — never an attacker-chosen binding (the CSRF-class boundary, D-C3-4).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from persona.auth.jwt_verifier import AuthenticatedUser
from persona.errors import AuthenticationError
from persona_connectors.discord.app import build_discord_app
from persona_connectors.errors import DiscordApiError, LinkTokenInvalidError

_ISSUE = "/v1/connectors/discord/link"
_CALLBACK = "/discord/oauth/callback"


def _app(
    *,
    issued_for: list[str] | None = None,
    completed: list[tuple[str, str]] | None = None,
    jwt_owner: str | None = "user_from_token",
) -> TestClient:
    issued = issued_for if issued_for is not None else []
    done = completed if completed is not None else []

    async def issue_authorize_url(owner_id: str) -> str:
        issued.append(owner_id)
        return f"https://discord.com/oauth2/authorize?client_id=c&state=token_for_{owner_id}"

    async def complete_oauth(code: str, state: str) -> str:
        if state == "bad-state":
            raise LinkTokenInvalidError("invalid state", context={"platform": "discord"})
        if code == "fail-exchange":
            raise DiscordApiError("oauth exchange failed", context={"step": "exchange"})
        done.append((code, state))
        return "owner-bound"

    async def verify_jwt(token: str) -> AuthenticatedUser:
        if jwt_owner is None or token == "bad":
            raise AuthenticationError("invalid token")
        return AuthenticatedUser(id=jwt_owner, email=None)

    app = build_discord_app(
        issue_authorize_url=issue_authorize_url,
        complete_oauth=complete_oauth,
        verify_jwt=verify_jwt,
    )
    return TestClient(app)


# --- issue route authorization ---


def test_issue_derives_owner_from_verified_jwt_not_body() -> None:
    """The authorize URL binds to the JWT's owner — a body 'owner_id' is ignored."""
    issued: list[str] = []
    client = _app(issued_for=issued, jwt_owner="real_owner")
    resp = client.post(
        _ISSUE,
        json={"owner_id": "attacker_chosen_owner"},  # MUST be ignored
        headers={"Authorization": "Bearer good"},
    )
    assert resp.status_code == 200
    assert resp.json()["authorize_url"].endswith("state=token_for_real_owner")
    assert issued == ["real_owner"]  # owner from the token, not the body


def test_issue_carries_server_authoritative_expiry() -> None:
    """C6-D-8: the authorize-URL response carries expires_at = issue_time + ttl."""
    fixed = datetime(2026, 7, 3, 12, 0, 0, tzinfo=UTC)

    async def issue_authorize_url(owner_id: str) -> str:
        return f"https://discord.com/oauth2/authorize?state=token_for_{owner_id}"

    async def complete_oauth(_code: str, _state: str) -> str:
        return "owner-bound"

    async def verify_jwt(_token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id="real_owner", email=None)

    app = build_discord_app(
        issue_authorize_url=issue_authorize_url,
        complete_oauth=complete_oauth,
        verify_jwt=verify_jwt,
        link_ttl=timedelta(minutes=15),
        now=lambda: fixed,
    )
    body = TestClient(app).post(_ISSUE, headers={"Authorization": "Bearer good"}).json()
    assert body == {
        "authorize_url": "https://discord.com/oauth2/authorize?state=token_for_real_owner",
        "expires_at": "2026-07-03T12:15:00+00:00",
    }


def test_issue_requires_a_bearer_token() -> None:
    resp = _app().post(_ISSUE, json={})
    assert resp.status_code == 401


def test_issue_rejects_an_invalid_jwt() -> None:
    issued: list[str] = []
    resp = _app(issued_for=issued).post(_ISSUE, json={}, headers={"Authorization": "Bearer bad"})
    assert resp.status_code == 401
    assert issued == []  # no URL minted on a failed verify


@pytest.mark.parametrize("auth", ["", "Token good", "good"])
def test_issue_rejects_malformed_authorization(auth: str) -> None:
    resp = _app().post(_ISSUE, json={}, headers={"Authorization": auth})
    assert resp.status_code == 401


# --- OAuth callback (the binding WRITE) ---


def test_callback_binds_on_a_good_state() -> None:
    completed: list[tuple[str, str]] = []
    client = _app(completed=completed)
    resp = client.get(_CALLBACK, params={"code": "auth-code", "state": "good-state"})
    assert resp.status_code == 200
    assert "linked" in resp.text.lower()
    assert completed == [("auth-code", "good-state")]


def test_callback_missing_params_fails_closed() -> None:
    completed: list[tuple[str, str]] = []
    client = _app(completed=completed)
    resp = client.get(_CALLBACK, params={"code": "auth-code"})  # no state
    assert resp.status_code == 400
    assert completed == []  # never attempted a bind


def test_callback_bad_state_fails_closed_no_bind() -> None:
    """A tampered/replayed/expired state → 400, no bind (the CSRF-class boundary)."""
    completed: list[tuple[str, str]] = []
    client = _app(completed=completed)
    resp = client.get(_CALLBACK, params={"code": "auth-code", "state": "bad-state"})
    assert resp.status_code == 400
    assert "didn't work" in resp.text.lower()
    assert completed == []  # nothing bound


def test_callback_failed_exchange_fails_closed() -> None:
    resp = _app().get(_CALLBACK, params={"code": "fail-exchange", "state": "good-state"})
    assert resp.status_code == 400

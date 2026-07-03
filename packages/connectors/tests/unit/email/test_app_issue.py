"""The email connector's issue route (Spec C5 + C6) — owner-from-JWT + the C6 fields.

The reversed C5 flow (D-C5-X): the web shows an issued code and the PUBLIC inbound
address to email it to, plus a server-authoritative ``expires_at`` (C6-D-7/D-8). Driven
through FastAPI's TestClient with injected fakes; the inbound webhook is exercised
elsewhere (``test_full_flow_email``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from persona.auth.jwt_verifier import AuthenticatedUser
from persona.errors import AuthenticationError
from persona_connectors.email.app import build_email_app

_ISSUE = "/v1/connectors/email/link"


async def _on_inbound(_parsed: object) -> None: ...


async def _issue_code(owner_id: str) -> str:
    return f"CODE-{owner_id}"


async def _verify_jwt(token: str) -> AuthenticatedUser:
    if token == "bad":
        raise AuthenticationError("invalid token")
    return AuthenticatedUser(id="owner_a", email=None)


def test_issue_route_carries_destination_and_server_authoritative_expiry() -> None:
    fixed = datetime(2026, 7, 3, 12, 0, 0, tzinfo=UTC)
    app = build_email_app(
        webhook_auth=None,
        on_inbound=_on_inbound,  # type: ignore[arg-type]
        issue_code=_issue_code,
        verify_jwt=_verify_jwt,
        destination="personas@openpersona.app",
        link_ttl=timedelta(minutes=15),
        now=lambda: fixed,
    )
    body = TestClient(app).post(_ISSUE, headers={"Authorization": "Bearer good"}).json()
    assert body == {
        "code": "CODE-owner_a",
        "destination": "personas@openpersona.app",
        "expires_at": "2026-07-03T12:15:00+00:00",
    }


def test_issue_route_derives_owner_from_jwt_and_requires_bearer() -> None:
    app = build_email_app(
        webhook_auth=None,
        on_inbound=_on_inbound,  # type: ignore[arg-type]
        issue_code=_issue_code,
        verify_jwt=_verify_jwt,
        destination="personas@openpersona.app",
    )
    client = TestClient(app)
    # owner from the verified token, never the body
    ok = client.post(
        _ISSUE, headers={"Authorization": "Bearer good"}, json={"owner_id": "attacker"}
    )
    assert ok.status_code == 200
    assert ok.json()["code"] == "CODE-owner_a"
    assert client.post(_ISSUE).status_code == 401  # no bearer
    assert client.post(_ISSUE, headers={"Authorization": "Bearer bad"}).status_code == 401

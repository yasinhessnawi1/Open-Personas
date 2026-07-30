"""The email connector's issue route (Spec C5 + C6) — owner-from-JWT + the C6 fields.

The reversed C5 flow (D-C5-X): the web shows an issued code and the PUBLIC inbound
address to email it to, plus a server-authoritative ``expires_at`` (C6-D-7/D-8). Driven
through FastAPI's TestClient with injected fakes; the inbound webhook is exercised
elsewhere (``test_full_flow_email``).
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from loguru import logger as _loguru
from persona.auth.jwt_verifier import AuthenticatedUser
from persona.errors import AuthenticationError
from persona_connectors._postmark.webhook import PostmarkWebhookAuth
from persona_connectors.email.app import build_email_app
from pydantic import SecretStr

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


# --- R9-077: the webhook's 200 no-op must say WHY (observability before any parse fix) ---

_WEBHOOK = "/email/webhook"
_AUTH = PostmarkWebhookAuth(username="pm", password=SecretStr("pw"))
_BASIC = {"Authorization": "Basic " + base64.b64encode(b"pm:pw").decode()}


def _webhook_app(seen: list[object]) -> object:
    async def on_inbound(parsed: object) -> None:
        seen.append(parsed)

    return build_email_app(
        webhook_auth=_AUTH,
        on_inbound=on_inbound,  # type: ignore[arg-type]
        issue_code=_issue_code,
        verify_jwt=_verify_jwt,
    )


def test_unparsable_payload_logs_the_missing_field_and_still_200s() -> None:
    """Production shape: `POST /email/webhook 200` and then nothing, forever, with no
    record of which branch swallowed the message. The 200 is deliberate (Postmark must
    not retry a bad body) — the SILENCE was the defect."""
    seen: list[object] = []
    records: list[str] = []
    sink_id = _loguru.add(records.append, level="WARNING")
    try:
        response = TestClient(_webhook_app(seen)).post(  # type: ignore[arg-type]
            _WEBHOOK, json={"TextBody": "hi"}, headers=_BASIC
        )
    finally:
        _loguru.remove(sink_id)

    assert response.status_code == 200  # unchanged — Postmark must not retry
    assert seen == []  # nothing was dispatched
    blob = "".join(records)
    assert "could not be parsed" in blob
    assert "missing_field=sender" in blob


def test_a_dispatched_inbound_is_logged_without_the_address_or_body() -> None:
    """Positive evidence for the operator pass: this payload DID reach the flow."""
    seen: list[object] = []
    payload = {
        "From": "Bob@Example.com",
        "FromFull": {"Email": "Bob@Example.com", "Name": "Bob"},
        "MailboxHash": "astrid",
        "MessageID": "postmark-uuid-1",
        "Subject": "Hi",
        "TextBody": "a private sentence",
        "Headers": [],
    }
    records: list[str] = []
    sink_id = _loguru.add(records.append, level="INFO")
    try:
        response = TestClient(_webhook_app(seen)).post(  # type: ignore[arg-type]
            _WEBHOOK, json=payload, headers=_BASIC
        )
    finally:
        _loguru.remove(sink_id)

    assert response.status_code == 200
    assert len(seen) == 1
    blob = "".join(records)
    assert "email inbound parsed" in blob
    assert "persona_tag=astrid" in blob
    assert "bob@example.com" not in blob.lower()  # fingerprinted, never the address
    assert "a private sentence" not in blob

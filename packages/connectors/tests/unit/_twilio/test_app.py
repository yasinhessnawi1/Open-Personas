"""The shared Twilio connector ASGI app (Spec C4 T13) — inbound + status + issue routes.

The security spine at the REAL route level (not just T3's unit): every Twilio POST is
**signature-verified before the handler acts** — a forged/missing signature returns 403
and the inbound/status handler is NEVER called (reject-before-act survives the wiring).
The issue route derives the owner from the verified JWT, never the request body.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from fastapi.testclient import TestClient
from persona.errors import AuthenticationError
from persona_connectors._twilio.app import build_twilio_app
from pydantic import SecretStr

if TYPE_CHECKING:
    from collections.abc import Mapping

_TOKEN = "twilio-auth-token"  # noqa: S105 — test literal
_TOKEN_SECRET = SecretStr(_TOKEN)


def _sign(url: str, params: Mapping[str, str]) -> str:
    base = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    return base64.b64encode(
        hmac.new(_TOKEN.encode(), base.encode(), hashlib.sha1).digest()
    ).decode()


@dataclass
class _AuthedUser:
    id: str


def _recorder() -> tuple[list[Mapping[str, str]], object]:
    seen: list[Mapping[str, str]] = []

    async def handler(params: Mapping[str, str]) -> None:
        seen.append(params)

    return seen, handler


def _app(*, inbound: object, status: object, token: SecretStr | None = _TOKEN_SECRET) -> object:
    async def issue_code(owner_id: str) -> str:
        return f"CODE-{owner_id}"

    async def verify_jwt(bearer: str) -> _AuthedUser:
        if bearer != "good-token":
            raise AuthenticationError("bad token")
        return _AuthedUser(id="owner_a")

    return build_twilio_app(
        platform="whatsapp",
        auth_token=token,
        on_inbound=inbound,  # type: ignore[arg-type]
        on_status=status,  # type: ignore[arg-type]
        issue_code=issue_code,  # type: ignore[arg-type]
        verify_jwt=verify_jwt,  # type: ignore[arg-type]
    )


def test_valid_signature_drives_the_inbound_handler() -> None:
    seen, inbound = _recorder()
    _, status = _recorder()
    client = TestClient(_app(inbound=inbound, status=status))
    params = {"From": "whatsapp:+15551230000", "Body": "hello", "MessageSid": "SM1"}
    url = "http://testserver/whatsapp/webhook"
    resp = client.post(
        "/whatsapp/webhook", data=params, headers={"X-Twilio-Signature": _sign(url, params)}
    )
    assert resp.status_code == 200
    assert len(seen) == 1  # the handler ran
    assert seen[0]["Body"] == "hello"


def test_forged_signature_is_403_and_handler_never_acts() -> None:
    seen, inbound = _recorder()
    _, status = _recorder()
    client = TestClient(_app(inbound=inbound, status=status))
    params = {"From": "whatsapp:+1", "Body": "spoofed"}
    resp = client.post("/whatsapp/webhook", data=params, headers={"X-Twilio-Signature": "forged=="})
    assert resp.status_code == 403
    assert seen == []  # reject-BEFORE-act: the flow never saw the spoofed input


def test_missing_signature_is_403_and_handler_never_acts() -> None:
    seen, inbound = _recorder()
    _, status = _recorder()
    client = TestClient(_app(inbound=inbound, status=status))
    resp = client.post("/whatsapp/webhook", data={"Body": "x"})
    assert resp.status_code == 403
    assert seen == []


def test_unset_token_fails_closed_rejects_every_request() -> None:
    seen, inbound = _recorder()
    _, status = _recorder()
    client = TestClient(_app(inbound=inbound, status=status, token=None))
    params = {"Body": "x"}
    url = "http://testserver/whatsapp/webhook"
    resp = client.post(
        "/whatsapp/webhook", data=params, headers={"X-Twilio-Signature": _sign(url, params)}
    )
    assert resp.status_code == 403  # no token configured → reject all
    assert seen == []


def test_status_callback_also_signature_gated() -> None:
    _, inbound = _recorder()
    seen, status = _recorder()
    client = TestClient(_app(inbound=inbound, status=status))
    params = {"MessageSid": "SM1", "MessageStatus": "delivered", "NumSegments": "2"}
    url = "http://testserver/whatsapp/status"
    ok = client.post(
        "/whatsapp/status", data=params, headers={"X-Twilio-Signature": _sign(url, params)}
    )
    assert ok.status_code == 200
    assert seen[0]["NumSegments"] == "2"
    forged = client.post("/whatsapp/status", data=params, headers={"X-Twilio-Signature": "nope"})
    assert forged.status_code == 403
    assert len(seen) == 1  # the forged status was rejected before acting


def test_issue_route_owner_from_jwt_not_body() -> None:
    _, inbound = _recorder()
    _, status = _recorder()
    client = TestClient(_app(inbound=inbound, status=status))
    # a valid token → the code is minted for the TOKEN's owner, ignoring any body
    ok = client.post(
        "/v1/connectors/whatsapp/link",
        headers={"Authorization": "Bearer good-token"},
        json={"owner_id": "someone_else"},
    )
    assert ok.status_code == 200
    assert ok.json()["code"] == "CODE-owner_a"  # owner from the verified JWT, not the body
    # no/invalid token → 401
    assert client.post("/v1/connectors/whatsapp/link").status_code == 401
    assert (
        client.post(
            "/v1/connectors/whatsapp/link", headers={"Authorization": "Bearer bad-token"}
        ).status_code
        == 401
    )


def test_issue_route_carries_destination_and_server_authoritative_expiry() -> None:
    """C6-D-7/D-8: the reversed flow needs the PUBLIC destination to text + a server
    ``expires_at`` (issue_time + ttl), so the web countdown can't drift from the real TTL."""

    async def issue_code(owner_id: str) -> str:
        return f"CODE-{owner_id}"

    async def verify_jwt(_bearer: str) -> _AuthedUser:
        return _AuthedUser(id="owner_a")

    _, inbound = _recorder()
    _, status = _recorder()
    fixed = datetime(2026, 7, 3, 12, 0, 0, tzinfo=UTC)
    app = build_twilio_app(
        platform="sms",
        auth_token=_TOKEN_SECRET,
        on_inbound=inbound,  # type: ignore[arg-type]
        on_status=status,  # type: ignore[arg-type]
        issue_code=issue_code,  # type: ignore[arg-type]
        verify_jwt=verify_jwt,  # type: ignore[arg-type]
        destination="+15557654321",
        link_ttl=timedelta(minutes=10),
        now=lambda: fixed,
    )
    body = (
        TestClient(app)
        .post("/v1/connectors/sms/link", headers={"Authorization": "Bearer good-token"})
        .json()
    )
    assert body == {
        "code": "CODE-owner_a",
        "destination": "+15557654321",
        "expires_at": "2026-07-03T12:10:00+00:00",
    }

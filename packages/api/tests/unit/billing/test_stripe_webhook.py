"""Signed webhook endpoint — the payment-system security boundary (Spec M4, T3a).

Adversarial: the ONLY thing between an inbound request and any side effect is the
Stripe signature. These exercise the REAL ``construct_event`` verification (a locally
computed Stripe-Signature over the raw body) with a mock handler — so a rejected
request is PROVEN to never reach a handler (zero side effect), and a verified event is
proven to dispatch with its ``event.id`` (the idempotency anchor) intact. No DB — the
handler is the only writer and the assertions pin whether it runs.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from persona_api.app import create_app
from persona_api.billing import EventDispatcher
from persona_api.config import APIConfig, Edition

_DB = "postgresql+psycopg://super@localhost/persona_shell"
_APP_DB = "postgresql+psycopg://persona_app@localhost/persona_shell"
_SECRET = "whsec_test_secret"
_TYPE = "customer.subscription.updated"


def _sign(payload: bytes, *, secret: str = _SECRET, ts: int | None = None) -> str:
    """Compute a valid Stripe-Signature header (``t=<ts>,v1=<hmac>``) for ``payload``."""
    stamp = ts if ts is not None else int(time.time())
    signed = f"{stamp}.".encode() + payload
    digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={stamp},v1={digest}"


def _event(event_id: str = "evt_1", event_type: str = _TYPE) -> bytes:
    return json.dumps(
        {"id": event_id, "object": "event", "type": event_type, "data": {"object": {}}}
    ).encode()


def _make_client(
    *, handler: MagicMock | None = None, handler_type: str = _TYPE, enabled: bool = True
) -> TestClient:
    cfg = APIConfig(
        database_url=_DB,
        app_database_url=_APP_DB,
        edition=Edition.cloud,
        billing_stripe_enabled=enabled,
        stripe_secret_key="sk_test_x" if enabled else "",
        stripe_webhook_secret=_SECRET,
    )
    app = create_app(cfg)
    app.state.rls_engine = None  # handlers are mocked; no DB
    if handler is not None:
        app.state.webhook_dispatcher = EventDispatcher({handler_type: handler})
    return TestClient(app)


def _post(client: TestClient, payload: bytes, *, sig: str | None) -> object:
    headers = {"Stripe-Signature": sig} if sig is not None else {}
    return client.post("/v1/billing/webhook", content=payload, headers=headers)


# --- the adversarial security boundary ---------------------------------------


def test_valid_signature_reaches_dispatch_with_the_event_id() -> None:
    handler = MagicMock()
    client = _make_client(handler=handler)
    payload = _event("evt_valid")

    resp = _post(client, payload, sig=_sign(payload))

    assert resp.status_code == 200
    assert resp.json() == {"received": True}
    handler.assert_called_once()
    event_arg = handler.call_args[0][0]
    assert event_arg.id == "evt_valid"  # the idempotency anchor flows through
    assert event_arg.type == _TYPE


def test_forged_signature_is_400_and_never_dispatches() -> None:
    handler = MagicMock()
    client = _make_client(handler=handler)
    payload = _event()

    resp = _post(client, payload, sig=_sign(payload, secret="whsec_WRONG"))

    assert resp.status_code == 400
    handler.assert_not_called()  # zero side effect — rejected before any handler


def test_absent_signature_header_is_400_and_never_dispatches() -> None:
    handler = MagicMock()
    client = _make_client(handler=handler)

    resp = _post(client, _event(), sig=None)  # no Stripe-Signature header

    assert resp.status_code == 400
    handler.assert_not_called()


def test_malformed_body_with_a_valid_signature_is_400_and_never_dispatches() -> None:
    handler = MagicMock()
    client = _make_client(handler=handler)
    payload = b'{"id": "evt", "type":'  # truncated JSON; the sig is valid over these bytes

    resp = _post(client, payload, sig=_sign(payload))

    assert resp.status_code == 400
    handler.assert_not_called()


def test_tampered_body_after_signing_is_400() -> None:
    """A valid sig computed over ONE payload does not verify a different body."""
    handler = MagicMock()
    client = _make_client(handler=handler)
    original = _event("evt_a")
    tampered = _event("evt_b")  # attacker swaps the body, keeps the old signature

    resp = _post(client, tampered, sig=_sign(original))

    assert resp.status_code == 400
    handler.assert_not_called()


def test_unknown_event_type_is_200_noop() -> None:
    handler = MagicMock()
    client = _make_client(handler=handler, handler_type=_TYPE)
    payload = _event("evt_unknown", event_type="some.unhandled.type")

    resp = _post(client, payload, sig=_sign(payload))

    assert resp.status_code == 200  # never 4xx an unhandled type (Stripe would retry forever)
    handler.assert_not_called()


def test_replay_passes_the_same_event_id_through() -> None:
    """The skeleton routes a re-delivered event consistently by ``event.id`` (the T3b
    handler dedups on it via grant_idempotent/deduct_idempotent — proven in T1b)."""
    handler = MagicMock()
    client = _make_client(handler=handler)
    payload = _event("evt_replay")
    sig = _sign(payload)

    r1 = _post(client, payload, sig=sig)
    r2 = _post(client, payload, sig=sig)

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert handler.call_count == 2
    ids = [call[0][0].id for call in handler.call_args_list]
    assert ids == ["evt_replay", "evt_replay"]


def test_webhook_is_unauthenticated_signature_is_the_only_auth() -> None:
    """A valid signed event with NO Authorization header succeeds — the signature is
    the auth (Stripe calls server-to-server; the route carries no Clerk/JWT dep)."""
    handler = MagicMock()
    client = _make_client(handler=handler)
    payload = _event("evt_noauth")

    resp = client.post(
        "/v1/billing/webhook",
        content=payload,
        headers={"Stripe-Signature": _sign(payload)},  # no Authorization header
    )

    assert resp.status_code == 200
    handler.assert_called_once()


def test_webhook_404_when_billing_disabled() -> None:
    client = _make_client(enabled=False)
    payload = _event()
    resp = _post(client, payload, sig=_sign(payload))
    assert resp.status_code == 404

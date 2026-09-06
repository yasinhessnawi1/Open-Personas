"""A re-delivered Stripe webhook must not double-grant (Spec M5, T3).

Stripe delivers at-least-once: a timeout, a retry, or a replayed event all land the SAME
payload twice. M4 built two independent idempotency layers for that — the allowance grant
keyed on ``billing_key``, and PAYG lots on ``UNIQUE(source_billing_key)`` — and M4 proved
the PRIMITIVES directly.

What was never proven is the layer a real re-delivery actually travels: the signed HTTP
webhook → verification → dispatch → handler → grant chain. So these tests POST the same
signed event body twice through ``/v1/billing/webhook`` (real HMAC verification, real
dispatcher, real handlers, real DB) and assert the balance moved exactly once. A test that
called the grant helper twice would prove the primitive and miss any double-grant
introduced by the route or the dispatcher above it.

Money duplication is silent and expensive, so it is proven, not assumed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from persona_api.app import create_app
from persona_api.config import APIConfig, Edition
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_DB = "postgresql+psycopg://super@localhost/persona_shell"
_APP_DB = "postgresql+psycopg://persona_app@localhost/persona_shell"
_SECRET = "whsec_m5_redelivery"
_UID = "u_m5_redeliver"
_CUSTOMER = "cus_m5_redeliver"


class _VerifyingGateway:
    """Real signature verification, no network (the webhook's only auth is the HMAC)."""

    publishable_key = "pk_test"
    webhook_secret = _SECRET

    def construct_event(self, *, payload: bytes, sig_header: str) -> object:
        import stripe

        return stripe.Webhook.construct_event(payload, sig_header, _SECRET)


def _sign(payload: bytes) -> str:
    stamp = int(time.time())
    digest = hmac.new(_SECRET.encode(), f"{stamp}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={stamp},v1={digest}"


def _payg_event(event_id: str, pi_id: str, credit_amount: int) -> bytes:
    """A `payment_intent.succeeded` for one of OUR pack purchases (carries payg_credits).

    The ``customer`` field is load-bearing: the handler resolves the owning user from the
    customer→user binding stored at checkout (``_apply_scoped``), so an event without it
    is a deliberate no-op. The fixture seeds that binding below.
    """
    return json.dumps(
        {
            "id": event_id,
            "object": "event",
            "type": "payment_intent.succeeded",
            "data": {
                "object": {
                    "id": pi_id,
                    "object": "payment_intent",
                    "customer": _CUSTOMER,
                    "metadata": {"payg_credits": str(credit_amount)},
                }
            },
        }
    ).encode()


def _client(engine: Engine) -> TestClient:
    cfg = APIConfig(
        database_url=_DB,
        app_database_url=_APP_DB,
        edition=Edition.cloud,
        stripe_webhook_secret=_SECRET,
        stripe_checkout_success_url="https://web/settings/billing?checkout=success",
        stripe_checkout_cancel_url="https://web/settings/billing?checkout=cancel",
        stripe_portal_return_url="https://web/settings/billing",
    )
    app = create_app(cfg)
    app.state.rls_engine = engine
    app.state.admin_engine = engine
    app.state.stripe_gateway = _VerifyingGateway()  # type: ignore[assignment]
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:u, 'r@x.test') ON CONFLICT DO NOTHING"),
            {"u": _UID},
        )
        # The customer -> user binding the real checkout writes. Without it the handler
        # correctly no-ops (it refuses to guess whose money an unknown customer is).
        conn.execute(
            text(
                "INSERT INTO subscription (user_id, plan_code, stripe_customer_id) "
                "VALUES (:u, 'free', :c) ON CONFLICT (user_id) DO UPDATE "
                "SET stripe_customer_id = :c"
            ),
            {"u": _UID, "c": _CUSTOMER},
        )
    return TestClient(app)


def _post(client: TestClient, payload: bytes) -> int:
    return client.post(
        "/v1/billing/webhook",
        content=payload,
        headers={"stripe-signature": _sign(payload), "content-type": "application/json"},
    ).status_code


def _lot_total(engine: Engine) -> tuple[int, int]:
    """(number of PAYG lots, summed remaining) for the test user."""
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT count(*), coalesce(sum(credits_remaining), 0) "
                "FROM payg_grants WHERE user_id = :u"
            ),
            {"u": _UID},
        ).first()
    return (int(row[0]), int(row[1])) if row else (0, 0)


def test_redelivered_payg_webhook_grants_exactly_one_lot(migrated_engine: Engine) -> None:
    """The PAYG half: UNIQUE(source_billing_key) holds through the REAL webhook route."""
    client = _client(migrated_engine)
    payload = _payg_event("evt_m5_redeliver_1", "pi_m5_redeliver_1", 1000)

    assert _post(client, payload) == 200
    after_first = _lot_total(migrated_engine)
    assert after_first == (1, 1000)

    # The re-delivery: byte-identical payload, freshly signed, exactly as Stripe retries.
    assert _post(client, payload) == 200
    assert _lot_total(migrated_engine) == after_first, "a re-delivered webhook double-granted"


def test_five_redeliveries_still_grant_once(migrated_engine: Engine) -> None:
    """Stripe retries with backoff; one retry passing is not evidence that five do."""
    client = _client(migrated_engine)
    payload = _payg_event("evt_m5_redeliver_5", "pi_m5_redeliver_5", 2500)

    for _ in range(5):
        assert _post(client, payload) == 200

    assert _lot_total(migrated_engine) == (1, 2500)


def test_distinct_payments_each_grant_their_own_lot(migrated_engine: Engine) -> None:
    """The guard must key on the PAYMENT, not collapse every pack purchase into one.

    The failure this excludes is an over-broad idempotency key that silently swallows a
    customer's SECOND genuine purchase — which costs them money rather than us.
    """
    client = _client(migrated_engine)
    assert _post(client, _payg_event("evt_m5_a", "pi_m5_a", 500)) == 200
    assert _post(client, _payg_event("evt_m5_b", "pi_m5_b", 1000)) == 200

    lots, total = _lot_total(migrated_engine)
    assert lots == 2
    assert total == 1500


def test_a_forged_redelivery_grants_nothing(migrated_engine: Engine) -> None:
    """The signature is the webhook's ONLY auth, so an unsigned replay must not grant."""
    client = _client(migrated_engine)
    payload = _payg_event("evt_m5_forged", "pi_m5_forged", 5000)

    resp = client.post(
        "/v1/billing/webhook",
        content=payload,
        headers={"stripe-signature": "t=1,v1=deadbeef", "content-type": "application/json"},
    )
    assert resp.status_code == 400
    assert _lot_total(migrated_engine) == (0, 0)


def test_a_non_payg_payment_intent_grants_nothing(migrated_engine: Engine) -> None:
    """A subscription invoice's own PI carries no ``payg_credits`` — it must be a no-op."""
    client = _client(migrated_engine)
    payload = json.dumps(
        {
            "id": "evt_m5_subpi",
            "object": "event",
            "type": "payment_intent.succeeded",
            "data": {"object": {"id": "pi_m5_sub", "object": "payment_intent", "metadata": {}}},
        }
    ).encode()

    assert _post(client, payload) == 200
    assert _lot_total(migrated_engine) == (0, 0)

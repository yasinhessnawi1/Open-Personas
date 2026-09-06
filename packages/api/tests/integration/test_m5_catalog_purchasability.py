"""Every catalog code reaches a real checkout session, and no price comes from the client.

Spec M5 (T3). Two guarantees the round-trip test in ``test_m5_billing_catalog`` only
half-covers, because resolving in a lookup is not the same as reaching a session:

1. **Catalog completeness.** Every plan and pack the catalog SERVES must actually be
   purchasable. A code that renders as a button and then 400s is the dead affordance
   D-M5-3 forbids, and the only way to know is to drive each one to a session.
2. **Server-side price derivation.** The client posts a CODE and nothing else. Price
   ids, credit amounts and dollar values are resolved server-side from config + the
   ``persona.billing.plans`` registry. A client-supplied price is the one bug in this
   task that costs real money, so it is asserted adversarially rather than assumed.

No Stripe network: the fake gateway records what the route asked for. What Stripe does
with a session is the owner's live leg; what WE send it is this file's business.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from persona.billing import PAYG_PACKS, all_plans, payg_pack_code
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig, Edition
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_DB = "postgresql+psycopg://super@localhost/persona_shell"
_APP_DB = "postgresql+psycopg://persona_app@localhost/persona_shell"


class _FakeGateway:
    """Records what the route asked Stripe for (no network)."""

    publishable_key = "pk_test"

    def __init__(self) -> None:
        self.customer_calls: list[dict[str, object]] = []
        self.checkout_calls: list[dict[str, object]] = []
        self.payg_calls: list[dict[str, object]] = []
        self.portal_calls: list[dict[str, object]] = []

    def create_customer(self, *, user_id: str, email: str | None) -> str:
        self.customer_calls.append({"user_id": user_id, "email": email})
        return f"cus_{user_id}"

    def create_subscription_checkout(
        self, *, customer_id: str, price_id: str, success_url: str, cancel_url: str, user_id: str
    ) -> str:
        self.checkout_calls.append(
            {
                "customer_id": customer_id,
                "price_id": price_id,
                "success_url": success_url,
                "cancel_url": cancel_url,
                "user_id": user_id,
            }
        )
        return "https://checkout.stripe.test/sub"

    def create_payg_checkout(
        self,
        *,
        customer_id: str,
        price_id: str,
        credit_amount: int,
        user_id: str,
        success_url: str,
        cancel_url: str,
    ) -> str:
        self.payg_calls.append(
            {
                "customer_id": customer_id,
                "price_id": price_id,
                "credit_amount": credit_amount,
                "user_id": user_id,
                "success_url": success_url,
                "cancel_url": cancel_url,
            }
        )
        return "https://checkout.stripe.test/payg"

    def create_portal_session(self, *, customer_id: str, return_url: str) -> str:
        self.portal_calls.append({"customer_id": customer_id, "return_url": return_url})
        return "https://portal.stripe.test/s"


async def _verify(token: str) -> AuthenticatedUser:
    return AuthenticatedUser(id=token, email=None)


def _client(engine: Engine, gateway: _FakeGateway) -> TestClient:
    """A cloud app with EVERY plan + pack Price configured.

    Deliberately complete: this suite asks whether the catalog and the purchase routes
    agree, so a missing Price id here would mask the very gap being tested.
    """
    cfg = APIConfig(
        database_url=_DB,
        app_database_url=_APP_DB,
        edition=Edition.cloud,
        stripe_price_plus="price_plus_test",
        stripe_price_pro="price_pro_test",
        stripe_price_pack_5="price_pack_5_test",
        stripe_price_pack_10="price_pack_10_test",
        stripe_price_pack_25="price_pack_25_test",
        stripe_price_pack_50="price_pack_50_test",
        stripe_checkout_success_url="https://web/settings/billing?checkout=success",
        stripe_checkout_cancel_url="https://web/settings/billing?checkout=cancel",
        stripe_portal_return_url="https://web/settings/billing",
    )
    app = create_app(cfg)
    app.state.verify_token = _verify  # type: ignore[attr-defined]
    app.state.rls_engine = engine
    app.state.stripe_gateway = gateway
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, email) VALUES ('u_m5_t3','t3@x.test') "
                "ON CONFLICT DO NOTHING"
            )
        )
    return TestClient(app)


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer u_m5_t3"}


# --- 1. every served code reaches a real session ------------------------------


def test_every_served_pack_code_reaches_a_checkout_session(migrated_engine: Engine) -> None:
    """Not just "resolves in a lookup" — actually produces a session, for all four."""
    gateway = _FakeGateway()
    client = _client(migrated_engine, gateway)
    catalog = client.get("/v1/billing/config", headers=_auth()).json()

    for pack in catalog["packs"]:
        resp = client.post(
            "/v1/billing/checkout/pack", json={"pack": pack["code"]}, headers=_auth()
        )
        assert resp.status_code == 200, f"pack {pack['code']} is served but not purchasable"
        assert resp.json()["url"].startswith("https://")

    assert len(gateway.payg_calls) == len(catalog["packs"])


def test_every_purchasable_served_plan_reaches_a_checkout_session(
    migrated_engine: Engine,
) -> None:
    """Every PAID plan in the catalog checks out. Free is excluded by design (below)."""
    gateway = _FakeGateway()
    client = _client(migrated_engine, gateway)
    catalog = client.get("/v1/billing/config", headers=_auth()).json()

    paid = [p for p in catalog["plans"] if p["monthly_price_credits"] > 0]
    assert paid, "the catalog serves no purchasable plan"
    for plan in paid:
        resp = client.post(
            "/v1/billing/checkout", json={"plan_code": plan["code"]}, headers=_auth()
        )
        assert resp.status_code == 200, f"plan {plan['code']} is served but not purchasable"

    assert len(gateway.checkout_calls) == len(paid)


def test_free_is_served_as_a_tier_but_is_not_purchasable(migrated_engine: Engine) -> None:
    """D-M5-4: Free renders as a tier, never a button that errors.

    The catalog lists it (the ladder needs it) and the API refuses to sell it. The UI
    contract is that a zero-price plan gets no CTA; this is the server half.
    """
    gateway = _FakeGateway()
    client = _client(migrated_engine, gateway)
    catalog = client.get("/v1/billing/config", headers=_auth()).json()
    assert any(p["code"] == "free" for p in catalog["plans"])

    resp = client.post("/v1/billing/checkout", json={"plan_code": "free"}, headers=_auth())
    assert resp.status_code == 422  # not in the Literal — rejected at the boundary
    assert gateway.checkout_calls == []


# --- 2. the server derives money; the client never supplies it ----------------


def test_pack_credit_amount_comes_from_the_registry_not_the_request(
    migrated_engine: Engine,
) -> None:
    """The granted credits are looked up server-side, per pack, from the catalog."""
    gateway = _FakeGateway()
    client = _client(migrated_engine, gateway)

    for pack in PAYG_PACKS:
        gateway.payg_calls.clear()
        code = payg_pack_code(pack)
        client.post("/v1/billing/checkout/pack", json={"pack": code}, headers=_auth())
        assert gateway.payg_calls[0]["credit_amount"] == pack.granted_credits


def test_a_client_supplied_price_is_rejected_not_honoured(migrated_engine: Engine) -> None:
    """The money bug this task must not ship.

    ``extra="forbid"`` on the input boundary means smuggled economics are a 422, not a
    silently-ignored field — so a tampered client cannot even probe for one that sticks.
    """
    gateway = _FakeGateway()
    client = _client(migrated_engine, gateway)

    for body in (
        {"pack": "5", "credit_amount": 999_999},
        {"pack": "5", "price_credits": 1},
        {"pack": "5", "price_id": "price_attacker_controlled"},
    ):
        resp = client.post("/v1/billing/checkout/pack", json=body, headers=_auth())
        assert resp.status_code == 422, f"smuggled field accepted: {body}"
    assert gateway.payg_calls == []

    for plan_body in (
        {"plan_code": "pro", "monthly_price_credits": 1},
        {"plan_code": "pro", "price_id": "price_attacker_controlled"},
    ):
        resp = client.post("/v1/billing/checkout", json=plan_body, headers=_auth())
        assert resp.status_code == 422, f"smuggled field accepted: {plan_body}"
    assert gateway.checkout_calls == []


def test_an_unknown_pack_code_never_reaches_stripe(migrated_engine: Engine) -> None:
    """A code outside the registry is refused at the boundary, before any Stripe call."""
    gateway = _FakeGateway()
    client = _client(migrated_engine, gateway)
    resp = client.post("/v1/billing/checkout/pack", json={"pack": "1000000"}, headers=_auth())
    assert resp.status_code == 422
    assert gateway.payg_calls == []


def test_price_ids_are_owner_config_never_client_input(migrated_engine: Engine) -> None:
    """Every Price id sent to Stripe traces to this deployment's own configuration."""
    gateway = _FakeGateway()
    client = _client(migrated_engine, gateway)
    configured = {
        "price_plus_test",
        "price_pro_test",
        "price_pack_5_test",
        "price_pack_10_test",
        "price_pack_25_test",
        "price_pack_50_test",
    }

    for plan in (p for p in all_plans() if p.monthly_price_credits > 0):
        client.post("/v1/billing/checkout", json={"plan_code": str(plan.code)}, headers=_auth())
    for pack in PAYG_PACKS:
        client.post(
            "/v1/billing/checkout/pack", json={"pack": payg_pack_code(pack)}, headers=_auth()
        )

    sent = {str(c["price_id"]) for c in gateway.checkout_calls} | {
        str(c["price_id"]) for c in gateway.payg_calls
    }
    assert sent <= configured
    assert sent, "no price ids were exercised"


def test_an_unconfigured_price_is_an_honest_400_not_a_dead_spinner(
    migrated_engine: Engine,
) -> None:
    """A plan whose Price the owner has not configured refuses clearly (D-M5-4).

    The UI turns this into "we could not open checkout, nothing was charged" rather
    than spinning forever.
    """
    gateway = _FakeGateway()
    cfg = APIConfig(
        database_url=_DB,
        app_database_url=_APP_DB,
        edition=Edition.cloud,
        stripe_price_plus="",  # deliberately unconfigured
        stripe_price_pro="price_pro_test",
        stripe_checkout_success_url="https://web/success",
        stripe_checkout_cancel_url="https://web/cancel",
        stripe_portal_return_url="https://web/account",
    )
    app = create_app(cfg)
    app.state.verify_token = _verify  # type: ignore[attr-defined]
    app.state.rls_engine = migrated_engine
    app.state.stripe_gateway = gateway
    client = TestClient(app)
    with migrated_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, email) VALUES ('u_m5_t3','t3@x.test') "
                "ON CONFLICT DO NOTHING"
            )
        )

    resp = client.post("/v1/billing/checkout", json={"plan_code": "plus"}, headers=_auth())
    assert resp.status_code == 400
    assert gateway.checkout_calls == []


# --- 3. a Stripe outage is a clean 502, never a 500 with a stack trace ---------


class _ExplodingGateway(_FakeGateway):
    """Every Stripe-backed call raises, as a real outage / bad key does.

    Each stub records its call through the inherited recorder before raising: the
    signatures must match the real gateway (or the routes would fail on a TypeError
    instead of the outage under test), and recording keeps the arguments genuinely used
    rather than silenced with a blanket lint exemption.
    """

    def create_customer(self, *, user_id: str, email: str | None) -> str:
        self.customer_calls.append({"user_id": user_id, "email": email})
        raise RuntimeError("stripe is down")

    def create_subscription_checkout(
        self, *, customer_id: str, price_id: str, success_url: str, cancel_url: str, user_id: str
    ) -> str:
        self.checkout_calls.append(
            {
                "customer_id": customer_id,
                "price_id": price_id,
                "success_url": success_url,
                "cancel_url": cancel_url,
                "user_id": user_id,
            }
        )
        raise RuntimeError("stripe is down")

    def create_payg_checkout(
        self,
        *,
        customer_id: str,
        price_id: str,
        credit_amount: int,
        user_id: str,
        success_url: str,
        cancel_url: str,
    ) -> str:
        self.payg_calls.append(
            {
                "customer_id": customer_id,
                "price_id": price_id,
                "credit_amount": credit_amount,
                "user_id": user_id,
                "success_url": success_url,
                "cancel_url": cancel_url,
            }
        )
        raise RuntimeError("stripe is down")

    def create_portal_session(self, *, customer_id: str, return_url: str) -> str:
        self.portal_calls.append({"customer_id": customer_id, "return_url": return_url})
        raise RuntimeError("stripe is down")


def test_a_stripe_outage_is_a_clean_502_on_every_billing_route(
    migrated_engine: Engine,
) -> None:
    """Spec M5 (T3-fix): the pre-existing 500-with-a-stack-trace is closed.

    Before this, a Stripe error escaped the route raw: the caller got a 500 carrying
    internals, and the web had no structured error to render, so the purchase button
    span forever. Now every billing route that talks to Stripe answers 502 with copy
    that says nothing was charged — which is true, because the session was never
    created.

    Asserted on ALL THREE routes: an outage does not politely limit itself to the one
    endpoint a test happens to cover.
    """
    client = _client(migrated_engine, _ExplodingGateway())

    for path, body in (
        ("/v1/billing/checkout", {"plan_code": "pro"}),
        ("/v1/billing/checkout/pack", {"pack": "10"}),
        ("/v1/billing/portal", None),
    ):
        resp = (
            client.post(path, headers=_auth())
            if body is None
            else client.post(path, json=body, headers=_auth())
        )
        assert resp.status_code == 502, f"{path} did not degrade cleanly"
        detail = str(resp.json().get("detail", ""))
        assert "nothing was charged" in detail
        # The leak this closes: no traceback, no SDK internals, no module paths.
        assert "Traceback" not in resp.text
        assert "stripe is down" not in resp.text

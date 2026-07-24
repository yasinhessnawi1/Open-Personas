"""Billing checkout + portal routes (Spec M4, T2b).

Integration: a real ``subscription`` table (the customer is created-or-reused + stored
per-user, RLS-scoped) + a FAKE gateway (no Stripe network — the gateway params are
covered by ``test_stripe_checkout_gateway``). Acceptance: checkout returns a session
url; portal returns a link; unauthenticated → 401; an unpurchasable plan → 400/422;
community / flag-off → 404; the customer is stored on the CALLER's row only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
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
    """Records calls + returns fixed urls (no Stripe network)."""

    publishable_key = "pk_test"

    def __init__(self) -> None:
        self.created_customers: list[tuple[str, str | None]] = []
        self.checkout_calls: list[dict[str, str]] = []
        self.payg_calls: list[dict[str, object]] = []
        self.portal_calls: list[dict[str, str]] = []

    def create_customer(self, *, user_id: str, email: str | None) -> str:
        self.created_customers.append((user_id, email))
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
        return "https://checkout.stripe.test/session_abc"

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
        return "https://checkout.stripe.test/payg_abc"

    def create_portal_session(self, *, customer_id: str, return_url: str) -> str:
        self.portal_calls.append({"customer_id": customer_id, "return_url": return_url})
        return "https://portal.stripe.test/session_xyz"


async def _verify(token: str) -> AuthenticatedUser:
    return AuthenticatedUser(id=token, email=None)


def _seed_user(engine: Engine, uid: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:u, :e) ON CONFLICT DO NOTHING"),
            {"u": uid, "e": f"{uid}@x.test"},
        )


def _make_client(
    engine: Engine,
    *,
    gateway: _FakeGateway | None,
    price_pro: str = "price_pro_test",
    price_pack_5: str = "price_pack_5_test",
) -> TestClient:
    cfg = APIConfig(
        database_url=_DB,
        app_database_url=_APP_DB,
        edition=Edition.cloud,
        stripe_price_plus="price_plus_test",
        stripe_price_pro=price_pro,
        stripe_price_pack_5=price_pack_5,
        stripe_price_pack_10="price_pack_10_test",
        stripe_checkout_success_url="https://web/success",
        stripe_checkout_cancel_url="https://web/cancel",
        stripe_portal_return_url="https://web/account",
    )
    app = create_app(cfg)
    app.state.verify_token = _verify  # type: ignore[attr-defined]
    app.state.rls_engine = engine
    app.state.stripe_gateway = gateway  # None → billing disabled (404)
    return TestClient(app)


def _auth(uid: str = "u1") -> dict[str, str]:
    return {"Authorization": f"Bearer {uid}"}


def test_checkout_returns_session_url_and_stores_the_customer(migrated_engine: Engine) -> None:
    gateway = _FakeGateway()
    client = _make_client(migrated_engine, gateway=gateway)
    _seed_user(migrated_engine, "u1")

    resp = client.post("/v1/billing/checkout", headers=_auth("u1"), json={"plan_code": "plus"})

    assert resp.status_code == 200
    assert resp.json()["url"] == "https://checkout.stripe.test/session_abc"
    assert gateway.checkout_calls[0]["price_id"] == "price_plus_test"
    assert gateway.checkout_calls[0]["success_url"] == "https://web/success"
    # The caller's ONE customer is created + stored on their own subscription row.
    with migrated_engine.begin() as conn:
        cid = conn.execute(
            text("SELECT stripe_customer_id FROM subscription WHERE user_id = 'u1'")
        ).scalar_one()
    assert cid == "cus_u1"


def test_checkout_reuses_the_existing_customer(migrated_engine: Engine) -> None:
    gateway = _FakeGateway()
    client = _make_client(migrated_engine, gateway=gateway)
    _seed_user(migrated_engine, "u1")

    client.post("/v1/billing/checkout", headers=_auth("u1"), json={"plan_code": "plus"})
    client.post("/v1/billing/checkout", headers=_auth("u1"), json={"plan_code": "pro"})

    # Two checkouts, but the customer is created exactly ONCE (reused thereafter).
    assert gateway.created_customers == [("u1", None)]
    assert gateway.checkout_calls[1]["customer_id"] == "cus_u1"


def test_portal_returns_a_link(migrated_engine: Engine) -> None:
    gateway = _FakeGateway()
    client = _make_client(migrated_engine, gateway=gateway)
    _seed_user(migrated_engine, "u1")

    resp = client.post("/v1/billing/portal", headers=_auth("u1"))

    assert resp.status_code == 200
    assert resp.json()["url"] == "https://portal.stripe.test/session_xyz"
    assert gateway.portal_calls[0]["return_url"] == "https://web/account"


def test_checkout_unpurchasable_plan_when_price_unconfigured_is_400(
    migrated_engine: Engine,
) -> None:
    gateway = _FakeGateway()
    client = _make_client(migrated_engine, gateway=gateway, price_pro="")  # pro price not set
    _seed_user(migrated_engine, "u1")

    resp = client.post("/v1/billing/checkout", headers=_auth("u1"), json={"plan_code": "pro"})

    assert resp.status_code == 400
    assert gateway.checkout_calls == []  # nothing created at Stripe


def test_checkout_rejects_a_nonpurchasable_plan_code_at_the_schema(migrated_engine: Engine) -> None:
    """``free`` is not a Stripe subscription — the request schema rejects it (422)."""
    client = _make_client(migrated_engine, gateway=_FakeGateway())
    _seed_user(migrated_engine, "u1")
    resp = client.post("/v1/billing/checkout", headers=_auth("u1"), json={"plan_code": "free"})
    assert resp.status_code == 422


def test_checkout_unauthenticated_is_401(migrated_engine: Engine) -> None:
    client = _make_client(migrated_engine, gateway=_FakeGateway())
    resp = client.post("/v1/billing/checkout", json={"plan_code": "plus"})  # no auth header
    assert resp.status_code == 401


def test_checkout_404_when_billing_disabled(migrated_engine: Engine) -> None:
    """Community / flag-off → no gateway → the whole surface 404s."""
    client = _make_client(migrated_engine, gateway=None)
    _seed_user(migrated_engine, "u1")
    resp = client.post("/v1/billing/checkout", headers=_auth("u1"), json={"plan_code": "plus"})
    assert resp.status_code == 404


def test_checkout_is_scoped_to_the_caller(migrated_engine: Engine) -> None:
    """A user's checkout provisions only THEIR own subscription — never another tenant's."""
    gateway = _FakeGateway()
    client = _make_client(migrated_engine, gateway=gateway)
    _seed_user(migrated_engine, "u1")
    _seed_user(migrated_engine, "u2")

    client.post("/v1/billing/checkout", headers=_auth("u1"), json={"plan_code": "plus"})

    with migrated_engine.begin() as conn:
        u2_row = conn.execute(text("SELECT 1 FROM subscription WHERE user_id = 'u2'")).first()
    assert u2_row is None  # u1's checkout never touched u2


# --- PAYG pack checkout (T4a) --------------------------------------------------


def test_pack_checkout_returns_url_with_the_right_credits(migrated_engine: Engine) -> None:
    gateway = _FakeGateway()
    client = _make_client(migrated_engine, gateway=gateway)
    _seed_user(migrated_engine, "u1")

    resp = client.post("/v1/billing/checkout/pack", headers=_auth("u1"), json={"pack": "5"})

    assert resp.status_code == 200
    assert resp.json()["url"] == "https://checkout.stripe.test/payg_abc"
    assert gateway.payg_calls[0]["price_id"] == "price_pack_5_test"
    assert gateway.payg_calls[0]["credit_amount"] == 500  # $5 → 500 credits (1:1)


def test_pack_checkout_unconfigured_price_is_400(migrated_engine: Engine) -> None:
    gateway = _FakeGateway()
    client = _make_client(migrated_engine, gateway=gateway, price_pack_5="")  # $5 price not set
    _seed_user(migrated_engine, "u1")

    resp = client.post("/v1/billing/checkout/pack", headers=_auth("u1"), json={"pack": "5"})

    assert resp.status_code == 400
    assert gateway.payg_calls == []


def test_pack_checkout_rejects_an_unknown_pack_at_the_schema(migrated_engine: Engine) -> None:
    client = _make_client(migrated_engine, gateway=_FakeGateway())
    _seed_user(migrated_engine, "u1")
    resp = client.post("/v1/billing/checkout/pack", headers=_auth("u1"), json={"pack": "999"})
    assert resp.status_code == 422


def test_pack_checkout_unauthenticated_is_401(migrated_engine: Engine) -> None:
    client = _make_client(migrated_engine, gateway=_FakeGateway())
    resp = client.post("/v1/billing/checkout/pack", json={"pack": "5"})
    assert resp.status_code == 401


def test_pack_checkout_404_when_billing_disabled(migrated_engine: Engine) -> None:
    client = _make_client(migrated_engine, gateway=None)
    _seed_user(migrated_engine, "u1")
    resp = client.post("/v1/billing/checkout/pack", headers=_auth("u1"), json={"pack": "5"})
    assert resp.status_code == 404

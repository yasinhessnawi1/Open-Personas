"""The M5 plan/pack catalog on ``GET /v1/billing/config`` (Spec M5, B3 — D-M5-25).

The catalog exists so the web can never hardcode plan economics (§1c.7): every number
it renders is projected from ``persona.billing.plans``, the owner-locked source of
truth. These tests assert that projection is FAITHFUL — not that it equals a second
copy of the numbers written out here, which would just move the drift into the test
file. Where a literal appears it is guarding a CONTRACT the client depends on (the
pack code posts back to the checkout route; the expiry term is disclosed), not an
economic value.

Also asserts the scope boundary (D-M5-12): the catalog describes the OFFERING only, so
no caller state leaks into it. No DB — config + factory + route projection.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from persona.billing import PAYG_PACKS, all_plans, get_payg_pack, payg_pack_code
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.billing.autotopup import (
    AUTO_TOPUP_AMOUNT_CREDITS,
    AUTO_TOPUP_THRESHOLD_CREDITS,
)
from persona_api.config import APIConfig, Edition

_DB = "postgresql+psycopg://super@localhost/persona_shell"
_APP_DB = "postgresql+psycopg://persona_app@localhost/persona_shell"


def _config(**overrides: object) -> APIConfig:
    base: dict[str, object] = {"database_url": _DB, "app_database_url": _APP_DB}
    base.update(overrides)
    return APIConfig(**base)  # type: ignore[arg-type]


async def _verify(token: str) -> AuthenticatedUser:
    return AuthenticatedUser(id=token, email=None)


def _client(cfg: APIConfig) -> TestClient:
    app = create_app(cfg)
    app.state.verify_token = _verify  # type: ignore[attr-defined]
    app.state.rls_engine = None  # no lifespan / no DB
    return TestClient(app)


def _enabled_client() -> TestClient:
    return _client(
        _config(
            edition=Edition.cloud,
            billing_stripe_enabled=True,
            stripe_secret_key="sk_test_x",
            stripe_publishable_key="pk_test_web",
            stripe_webhook_secret="whsec_x",
        )
    )


def _catalog() -> dict[str, object]:
    resp = _enabled_client().get("/v1/billing/config", headers={"Authorization": "Bearer u1"})
    assert resp.status_code == 200
    return dict(resp.json())


# --- the plan ladder ----------------------------------------------------------


def test_catalog_serves_every_plan_in_ladder_order() -> None:
    """Free, Plus, Pro — in the catalog's own order, so the UI renders the ladder."""
    body = _catalog()
    assert [p["code"] for p in body["plans"]] == [str(p.code) for p in all_plans()]


def test_plan_economics_are_projected_from_the_catalog_not_restated() -> None:
    """Every price + allowance equals ``persona.billing.plans``.

    This is the anti-drift assertion: change a number in the catalog and the endpoint
    follows automatically; change it in only one of the two and this fails.
    """
    body = _catalog()
    served = {p["code"]: p for p in body["plans"]}
    for plan in all_plans():
        row = served[str(plan.code)]
        assert row["monthly_price_credits"] == plan.monthly_price_credits
        assert row["included_allowance_credits"] == plan.included_allowance_credits
        assert row["auto_topup_eligible"] == plan.auto_topup_eligible
        assert row["is_default"] == plan.is_default


def test_exactly_one_plan_is_the_default() -> None:
    """The UI marks a starting tier; two defaults (or none) would make that ambiguous."""
    body = _catalog()
    assert sum(1 for p in body["plans"] if p["is_default"]) == 1


# --- the packs ----------------------------------------------------------------


def test_catalog_serves_every_pack() -> None:
    body = _catalog()
    assert len(body["packs"]) == len(PAYG_PACKS)


def test_pack_economics_are_projected_from_the_catalog() -> None:
    body = _catalog()
    served = {p["code"]: p for p in body["packs"]}
    for pack in PAYG_PACKS:
        row = served[payg_pack_code(pack)]
        assert row["price_credits"] == pack.price_credits
        assert row["granted_credits"] == pack.granted_credits


def test_every_served_pack_code_resolves_at_the_checkout_route() -> None:
    """The CONTRACT that makes the catalog usable.

    A pack rendered from this endpoint must post straight back to
    ``POST /v1/billing/checkout/pack``, which resolves the code via ``get_payg_pack``.
    If the two ever disagree, every pack purchase 400s — so assert the round trip
    rather than trusting that the two derivations stay in step.
    """
    body = _catalog()
    for row in body["packs"]:
        assert get_payg_pack(str(row["code"])) is not None


def test_packs_disclose_their_expiry_term() -> None:
    """D-M5-13: purchased credits really do expire, so the term ships with the offer.

    Asserted as "a positive term is present", not as a hardcoded 12, so the disclosure
    is guarded without pinning the economics.
    """
    body = _catalog()
    assert body["packs"]
    for row in body["packs"]:
        assert isinstance(row["expiry_months"], int)
        assert row["expiry_months"] > 0


# --- the auto-top-up constants (D-M5-8: state behavior truthfully) -------------


def test_catalog_serves_the_auto_topup_constants() -> None:
    """The UI states "at $2 we top up $10" from THESE values, never hardcoded ones."""
    body = _catalog()
    assert body["auto_topup_threshold_credits"] == AUTO_TOPUP_THRESHOLD_CREDITS
    assert body["auto_topup_amount_credits"] == AUTO_TOPUP_AMOUNT_CREDITS


def test_exactly_one_plan_offers_auto_topup() -> None:
    """D-M4-6 offers it on Pro only; the UI shows it as a Pro capability on that basis."""
    body = _catalog()
    assert sum(1 for p in body["plans"] if p["auto_topup_eligible"]) == 1


# --- gating + scope boundary --------------------------------------------------


def test_catalog_is_absent_when_billing_is_disabled() -> None:
    """D-M5-3: no catalog outside cloud + flag + key — the whole surface 404s.

    The community guarantee: there is nothing to render, so there can be no dead
    purchase affordance.
    """
    client = _client(_config(edition=Edition.community, billing_stripe_enabled=True))
    resp = client.get("/v1/billing/config", headers={"Authorization": "Bearer u1"})
    assert resp.status_code == 404


def test_catalog_carries_no_caller_state() -> None:
    """D-M5-12: the catalog describes the OFFERING; the wallet describes the CALLER.

    A balance or plan_code leaking in here would create a second source of truth for
    caller state and let the two responses disagree.
    """
    body = _catalog()
    forbidden = {
        "balance",
        "total_balance",
        "allowance_balance",
        "plan_code",
        "auto_topup_enabled",
        "payg_lots",
        "low_balance",
        "subscription_status",
    }
    assert forbidden.isdisjoint(body.keys())


def test_catalog_never_leaks_the_secret_key() -> None:
    """The catalog additions must not widen the response's secret surface."""
    resp = _enabled_client().get("/v1/billing/config", headers={"Authorization": "Bearer u1"})
    assert "sk_test_x" not in resp.text
    assert "whsec_x" not in resp.text

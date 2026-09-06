"""The M4 dark-flag + edition gate for Stripe billing (Spec M4, T2a).

M4 ships DARK: the Stripe surface is constructed ONLY when the edition is ``cloud``
AND ``PERSONA_BILLING_STRIPE_ENABLED`` is on AND a secret key is present. Community
and flag-off construct ZERO Stripe (``build_stripe_gateway`` returns ``None`` and the
``stripe`` SDK is never imported), and every billing route 404s. No DB — the gate is
config + factory + a route dependency.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.billing import StripeGateway
from persona_api.config import APIConfig, Edition
from persona_api.editions import build_stripe_gateway

_DB = "postgresql+psycopg://super@localhost/persona_shell"
_APP_DB = "postgresql+psycopg://persona_app@localhost/persona_shell"


def _config(**overrides: object) -> APIConfig:
    base: dict[str, object] = {"database_url": _DB, "app_database_url": _APP_DB}
    base.update(overrides)
    return APIConfig(**base)  # type: ignore[arg-type]


# --- the config gate (``stripe_billing_active``) + the factory -----------------


def test_community_never_activates_billing() -> None:
    cfg = _config(edition=Edition.community, billing_stripe_enabled=True, stripe_secret_key="sk_x")
    assert cfg.stripe_billing_active() is False
    assert build_stripe_gateway(cfg) is None


def test_cloud_flag_off_does_not_activate_billing() -> None:
    cfg = _config(edition=Edition.cloud, billing_stripe_enabled=False, stripe_secret_key="sk_x")
    assert cfg.stripe_billing_active() is False
    assert build_stripe_gateway(cfg) is None


def test_cloud_flag_on_but_no_key_does_not_activate_billing() -> None:
    cfg = _config(edition=Edition.cloud, billing_stripe_enabled=True, stripe_secret_key="")
    assert cfg.stripe_billing_active() is False
    assert build_stripe_gateway(cfg) is None


def test_cloud_flag_on_with_key_activates_billing() -> None:
    cfg = _config(
        edition=Edition.cloud,
        billing_stripe_enabled=True,
        stripe_secret_key="sk_test_x",
        stripe_publishable_key="pk_test_x",
        stripe_webhook_secret="whsec_x",
    )
    assert cfg.stripe_billing_active() is True
    gateway = build_stripe_gateway(cfg)
    assert gateway is not None
    assert gateway.publishable_key == "pk_test_x"
    assert gateway.webhook_secret == "whsec_x"


def test_stripe_gateway_construction_is_network_free() -> None:
    """Constructing the client only binds the key — no network I/O (safe at boot)."""
    import stripe

    gateway = StripeGateway(
        secret_key="sk_test_x", publishable_key="pk_test_x", webhook_secret="whsec_x"
    )
    assert isinstance(gateway.client, stripe.StripeClient)


# --- the route gate (TestClient, no DB) ---------------------------------------


async def _verify(token: str) -> AuthenticatedUser:
    return AuthenticatedUser(id=token, email=None)


def _client(cfg: APIConfig) -> TestClient:
    app = create_app(cfg)
    app.state.verify_token = _verify  # type: ignore[attr-defined]
    app.state.rls_engine = None  # no lifespan / no DB
    return TestClient(app)


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer u1"}


def test_billing_config_route_404s_when_disabled() -> None:
    """A flag-off cloud boot builds no gateway → the whole /v1/billing surface 404s."""
    client = _client(_config(edition=Edition.cloud, billing_stripe_enabled=False))
    resp = client.get("/v1/billing/config", headers=_auth())
    assert resp.status_code == 404


def test_billing_config_route_returns_publishable_key_when_enabled() -> None:
    client = _client(
        _config(
            edition=Edition.cloud,
            billing_stripe_enabled=True,
            stripe_secret_key="sk_test_x",
            stripe_publishable_key="pk_test_web",
            stripe_webhook_secret="whsec_x",
        )
    )
    resp = client.get("/v1/billing/config", headers=_auth())
    assert resp.status_code == 200
    # Spec M5 (B3, D-M5-25) made this response additive — it now also carries the
    # plan/pack catalog. Assert the M4 contract BY FIELD rather than by whole-body
    # equality, so this test keeps guarding exactly what it was written to guard (the
    # publishable key is served when billing is enabled) without re-breaking whenever
    # the catalog gains a field. The catalog's own contract lives in
    # ``test_m5_billing_catalog.py``.
    body = resp.json()
    assert body["enabled"] is True
    assert body["publishable_key"] == "pk_test_web"


def test_billing_config_never_leaks_the_secret_key() -> None:
    client = _client(
        _config(
            edition=Edition.cloud,
            billing_stripe_enabled=True,
            stripe_secret_key="sk_test_SECRET",
            stripe_publishable_key="pk_test_web",
            stripe_webhook_secret="whsec_x",
        )
    )
    body = client.get("/v1/billing/config", headers=_auth()).text
    assert "sk_test_SECRET" not in body
    assert "whsec_x" not in body


def test_billing_config_requires_auth() -> None:
    """Auth runs before the gate — an unauthenticated call is 401, not 404."""
    client = _client(
        _config(
            edition=Edition.cloud,
            billing_stripe_enabled=True,
            stripe_secret_key="sk_test_x",
            stripe_publishable_key="pk_test_web",
            stripe_webhook_secret="whsec_x",
        )
    )
    resp = client.get("/v1/billing/config")  # no Authorization header
    assert resp.status_code == 401

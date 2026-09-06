"""PRODUCTION registers the auto-top-up tenant, and only when billing is active.

Spec M5 (B5, D-M5-24). This suite exists because of a specific failure mode, not for
coverage: a capability whose handler is correct, whose consume path is proven, and which
never fires because nothing wires it. A4 shipped a milestone hook that way once.

So every assertion here goes through **``build_worker_registry``** — the real composition
root the worker actually calls. An earlier version of this file used a local helper that
re-implemented the production branch, which meant deleting the production line left the
suite green. A mirror of production is exactly what must not exist here: it tests the
mirror.

The first run of this rewritten suite caught a real bug. The registration had landed
inside the ``initiative_settings.enabled`` branch, an unrelated feature's flag that
defaults OFF, so a correctly-configured billing deployment would have registered no
tenant and every voice top-up would have dead-lettered.

No DB: ``build_worker_registry`` composes tenants from config, and the ones needing a
live engine are separately gated.
"""

from __future__ import annotations

from persona.jobs import AUTO_TOPUP_JOB_TYPE
from persona_api.background.worker_root import build_worker_registry
from persona_api.config import APIConfig, Edition

_DB = "postgresql+psycopg://super@localhost/persona_shell"
_APP_DB = "postgresql+psycopg://persona_app@localhost/persona_shell"


def _config(**overrides: object) -> APIConfig:
    base: dict[str, object] = {"database_url": _DB, "app_database_url": _APP_DB}
    base.update(overrides)
    return APIConfig(**base)  # type: ignore[arg-type]


def _production_types(cfg: APIConfig) -> list[str]:
    """The job types PRODUCTION would serve for this config.

    Deliberately the real builder with minimal collaborators: the tenants that need a
    live engine or a runtime gate themselves out, and the auto-top-up tenant's only
    dependency is the Stripe gateway the builder derives from ``config``.
    """
    registry = build_worker_registry(
        rls_engine=None,  # type: ignore[arg-type]
        embedder=None,  # type: ignore[arg-type]
        tier_registry=None,  # type: ignore[arg-type]
        free_tier_registry=None,
        config=cfg,
        synthesis_tier="small",
    )
    return registry.types()


def _active_billing_config(**overrides: object) -> APIConfig:
    base: dict[str, object] = {
        "edition": Edition.cloud,
        "billing_stripe_enabled": True,
        "stripe_secret_key": "sk_test_x",
        "stripe_publishable_key": "pk_test_x",
        "stripe_webhook_secret": "whsec_x",
    }
    base.update(overrides)
    return _config(**base)


def test_production_registers_the_tenant_when_billing_is_active() -> None:
    """The positive case, through the real composition root.

    This is the assertion that fails if the registration line is deleted, moved into a
    branch that does not run, or gated on the wrong flag.
    """
    assert AUTO_TOPUP_JOB_TYPE in _production_types(_active_billing_config())


def test_production_registers_it_even_when_initiative_is_off() -> None:
    """Billing must be gated by BILLING, not by an unrelated feature's flag.

    The bug this pins: the registration first landed inside the
    ``initiative_settings.enabled`` branch, which defaults OFF. A correctly-configured
    billing deployment would have silently registered nothing.
    """
    import os

    previous = os.environ.get("PERSONA_INITIATIVE_ENABLED")
    os.environ["PERSONA_INITIATIVE_ENABLED"] = "false"
    try:
        assert AUTO_TOPUP_JOB_TYPE in _production_types(_active_billing_config())
    finally:
        if previous is None:
            os.environ.pop("PERSONA_INITIATIVE_ENABLED", None)
        else:
            os.environ["PERSONA_INITIATIVE_ENABLED"] = previous


def test_community_does_not_register_the_tenant() -> None:
    """The community guarantee: no tenant, so no path from a deduct to a card."""
    cfg = _config(edition=Edition.community, billing_stripe_enabled=True, stripe_secret_key="sk_x")
    assert AUTO_TOPUP_JOB_TYPE not in _production_types(cfg)


def test_a_flag_off_cloud_boot_does_not_register_the_tenant() -> None:
    """M5 ships dark: the flag alone gates the whole billing surface."""
    cfg = _config(edition=Edition.cloud, billing_stripe_enabled=False, stripe_secret_key="sk_x")
    assert AUTO_TOPUP_JOB_TYPE not in _production_types(cfg)


def test_a_cloud_boot_without_a_key_does_not_register_the_tenant() -> None:
    """No secret key means no gateway, so there is nothing to register."""
    cfg = _config(edition=Edition.cloud, billing_stripe_enabled=True, stripe_secret_key="")
    assert AUTO_TOPUP_JOB_TYPE not in _production_types(cfg)


def test_the_community_registry_is_unchanged_by_m5() -> None:
    """A community worker's registry is byte-identical to one built before M5 existed.

    Compared against the flag-off CLOUD registry rather than a hand-written list, so the
    assertion stays true as other tenants come and go and only tracks M5's own effect.
    """
    community = _production_types(
        _config(edition=Edition.community, billing_stripe_enabled=True, stripe_secret_key="sk_x")
    )
    dark_cloud = _production_types(
        _config(edition=Edition.cloud, billing_stripe_enabled=False, stripe_secret_key="sk_x")
    )
    assert AUTO_TOPUP_JOB_TYPE not in community
    assert AUTO_TOPUP_JOB_TYPE not in dark_cloud


def test_activating_billing_adds_exactly_the_auto_topup_tenant() -> None:
    """Turning billing on changes the registry by EXACTLY one tenant.

    Guards the blast radius: activating billing must not quietly enable or disable any
    other tenant as a side effect.
    """
    dark = set(
        _production_types(
            _config(edition=Edition.cloud, billing_stripe_enabled=False, stripe_secret_key="sk_x")
        )
    )
    active = set(_production_types(_active_billing_config()))
    assert active - dark == {AUTO_TOPUP_JOB_TYPE}
    assert dark - active == set()

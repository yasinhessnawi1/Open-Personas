"""Wallet renewal + subscription status on ``GET /v1/me/wallet`` (Spec M5, B4 + B6).

Both are EXPOSE-ONLY (D-M5-27): ``current_period_end`` / ``cancel_at_period_end`` /
``status`` are already columns that the Stripe webhook already writes
(``customer.subscription.updated`` → ``upsert_subscription``; ``invoice.payment_failed``
→ ``mark_past_due``). Nothing new is persisted and no webhook changed — the data was
simply unreadable, so a paying customer could not be told when their plan renews, and a
``past_due`` customer was never told their payment failed (§1c.10, §1c.14).

These tests drive the REAL columns through the REAL route. The ``past_due`` case is
written to go through ``mark_past_due`` — the actual function the webhook handler calls
— rather than hand-setting the column, so a change that stops the webhook writing the
status fails here (the A4/V8 real-trigger rule).

Own file rather than edits to ``test_m4_wallet_surface.py``: M4's suite guards M4's
contract, and it should keep failing for M4 reasons only.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig
from persona_api.services import subscription_service
from sqlalchemy import text

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy import Engine

pytestmark = pytest.mark.integration


def _seed_user(engine: Engine, uid: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:u, :e) ON CONFLICT DO NOTHING"),
            {"u": uid, "e": f"{uid}@x.test"},
        )


def _seed_sub_with_period(
    engine: Engine,
    uid: str,
    plan_code: str,
    *,
    period_end_days: int,
    cancel_at_period_end: bool,
) -> None:
    """Seed a subscription carrying the renewal columns the webhook upserts."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO subscription "
                "(user_id, plan_code, current_period_end, cancel_at_period_end) "
                "VALUES (:u, :p, now() + make_interval(days => :d), :c) "
                "ON CONFLICT (user_id) DO UPDATE SET plan_code = :p, "
                "current_period_end = now() + make_interval(days => :d), "
                "cancel_at_period_end = :c"
            ),
            {"u": uid, "p": plan_code, "d": period_end_days, "c": cancel_at_period_end},
        )


def _client(tmp_path: Path, uid: str, su: Engine) -> TestClient | None:
    """The real app over the session-migrated DB, with ``uid`` seeded.

    Takes the migrated superuser engine rather than opening its own, so the schema is
    guaranteed to exist (the ``migrated_engine`` fixture builds it once per session);
    an ad-hoc engine on an unmigrated DB fails deep inside the app's lifespan instead.
    """
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        return None
    cfg = APIConfig(
        app_database_url=app_url,
        audit_root=str(tmp_path / "audit"),
        workspace_root=str(tmp_path / "workspace"),  # type: ignore[arg-type]
    )
    app = create_app(cfg)

    async def _verify(token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id=token, email=None)

    _seed_user(su, uid)
    client = TestClient(app)
    client.__enter__()
    app.state.verify_token = _verify
    return client


def _auth(uid: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {uid}"}


def test_free_user_reports_no_renewal_and_active_status(
    tmp_path: Path, migrated_engine: Engine
) -> None:
    """No subscription row ⇒ nothing renews, nothing cancels, and NOT past_due.

    A free user owes nothing, so reporting them as ``past_due`` would show a payment-failed
    banner to someone who never had a payment.
    """
    uid = "u_m5_wallet_free"
    c = _client(tmp_path, uid, migrated_engine)
    if c is None:
        pytest.skip("APP_DATABASE_URL not set")
    try:
        body = c.get("/v1/me/wallet", headers=_auth(uid)).json()
        assert body["current_period_end"] is None
        assert body["cancel_at_period_end"] is False
        assert body["subscription_status"] == "active"
    finally:
        c.__exit__(None, None, None)


def test_subscribed_user_reports_when_the_allowance_renews(
    tmp_path: Path, migrated_engine: Engine
) -> None:
    """B4: "when does this renew" — the question a paying customer asks on day one."""
    uid = "u_m5_wallet_renews"
    su = migrated_engine
    c = _client(tmp_path, uid, su)
    if c is None:
        pytest.skip("APP_DATABASE_URL not set")
    try:
        _seed_sub_with_period(su, uid, "plus", period_end_days=17, cancel_at_period_end=False)
        body = c.get("/v1/me/wallet", headers=_auth(uid)).json()
        assert body["current_period_end"] is not None
        assert body["cancel_at_period_end"] is False
        assert body["subscription_status"] == "active"
    finally:
        # NB: never dispose ``migrated_engine`` — it is session-scoped and TRUNCATEd
        # per test by the fixture, so disposing it here would break every later test.
        c.__exit__(None, None, None)


def test_cancelling_subscription_is_visible_before_it_lapses(
    tmp_path: Path, migrated_engine: Engine
) -> None:
    """B4: a plan set to stop at period end still reports its end date.

    Both facts matter together — "cancelling" plus "still yours until this date" is the
    honest statement; either alone misleads.
    """
    uid = "u_m5_wallet_cancelling"
    su = migrated_engine
    c = _client(tmp_path, uid, su)
    if c is None:
        pytest.skip("APP_DATABASE_URL not set")
    try:
        _seed_sub_with_period(su, uid, "pro", period_end_days=5, cancel_at_period_end=True)
        body = c.get("/v1/me/wallet", headers=_auth(uid)).json()
        assert body["cancel_at_period_end"] is True
        assert body["current_period_end"] is not None
    finally:
        # NB: never dispose ``migrated_engine`` — it is session-scoped and TRUNCATEd
        # per test by the fixture, so disposing it here would break every later test.
        c.__exit__(None, None, None)


def test_past_due_reaches_the_wallet_through_the_real_webhook_write(
    tmp_path: Path, migrated_engine: Engine
) -> None:
    """B6 + D-M5-14: the silent failure this closes.

    Drives ``subscription_service.mark_past_due`` — the function the
    ``invoice.payment_failed`` handler actually calls — rather than hand-setting the
    column, so this fails if the webhook path stops writing the status. Without the
    exposure a Pro user whose card expires is moved to ``past_due``, their allowance
    quietly stops renewing, and the app never says why they are running dry.
    """
    uid = "u_m5_wallet_past_due"
    su = migrated_engine
    c = _client(tmp_path, uid, su)
    if c is None:
        pytest.skip("APP_DATABASE_URL not set")
    try:
        _seed_sub_with_period(su, uid, "pro", period_end_days=9, cancel_at_period_end=False)
        assert c.get("/v1/me/wallet", headers=_auth(uid)).json()["subscription_status"] == "active"

        # The real webhook-handler call site.
        subscription_service.mark_past_due(su, user_id=uid)

        body = c.get("/v1/me/wallet", headers=_auth(uid)).json()
        assert body["subscription_status"] == "past_due"
        # The plan is unchanged — they are still Pro, just unpaid. Showing "free" here
        # would be a lie that hides the recoverable state.
        assert body["plan_code"] == "pro"
    finally:
        # NB: never dispose ``migrated_engine`` — it is session-scoped and TRUNCATEd
        # per test by the fixture, so disposing it here would break every later test.
        c.__exit__(None, None, None)

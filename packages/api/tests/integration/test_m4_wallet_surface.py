"""The two-bucket wallet surface (Spec M4, T8) — core read + policy + route.

``persona.credits.wallet_snapshot`` is the read-only wallet: the allowance bucket (+ its
month stamp), every LIVE PAYG lot in the FIFO spend order, and the derived total.
``GET /v1/me/wallet`` composes it (RLS-scoped, no business logic) with the caller's
subscription row (``plan_code`` / ``auto_topup_enabled``) + the PER-PLAN low-balance line
(20% of the plan allowance: Free 60 / Plus 400 / Pro 1200). Real DB; the route runs the
real app (the Metered policy self-heals the free monthly allowance on the wallet read).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from persona.credits import wallet_snapshot
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig
from persona_api.editions.credits_policy import UnlimitedCreditsPolicy
from persona_api.middleware.rls_context import make_rls_engine
from sqlalchemy import text

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_MONTH_SQL = "to_char((now() AT TIME ZONE 'UTC'), 'YYYY-MM')"


def _seed_user(engine: Engine, uid: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:u, :e) ON CONFLICT DO NOTHING"),
            {"u": uid, "e": f"{uid}@x.test"},
        )


def _seed_credits(engine: Engine, uid: str, *, balance: int, stamp_month: bool) -> None:
    period = _MONTH_SQL if stamp_month else "NULL"
    with engine.begin() as conn:
        conn.execute(
            text(
                f"INSERT INTO credits (user_id, balance, allowance_period) "
                f"VALUES (:u, :b, {period}) "
                f"ON CONFLICT (user_id) DO UPDATE SET balance = :b, "
                f"allowance_period = {period}"
            ),
            {"u": uid, "b": balance},
        )


def _seed_lot(
    engine: Engine,
    uid: str,
    lot_id: str,
    *,
    remaining: int,
    total: int,
    expires_days: int,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO payg_grants (id, user_id, credits_total, credits_remaining, "
                "granted_at, expires_at, source_billing_key, created_at) "
                "VALUES (:i, :u, :t, :r, now(), now() + make_interval(days => :d), :k, now()) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {
                "i": lot_id,
                "u": uid,
                "t": total,
                "r": remaining,
                "d": expires_days,
                "k": f"pi_{lot_id}",
            },
        )


def _seed_sub(engine: Engine, uid: str, plan_code: str, *, auto_topup: bool = False) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO subscription (user_id, plan_code, auto_topup_enabled) "
                "VALUES (:u, :p, :a) "
                "ON CONFLICT (user_id) DO UPDATE SET plan_code = :p, auto_topup_enabled = :a"
            ),
            {"u": uid, "p": plan_code, "a": auto_topup},
        )


# --- The core read: buckets, FIFO order, live-lot filtering --------------------


def test_wallet_snapshot_reports_buckets_fifo_and_total(migrated_engine: Engine) -> None:
    uid = "u_t8_wallet_core"
    _seed_user(migrated_engine, uid)
    _seed_credits(migrated_engine, uid, balance=120, stamp_month=True)
    # Three lots: drained (excluded), expiring LATER, expiring SOONER (FIFO-first).
    _seed_lot(migrated_engine, uid, "t8_lot_drained", remaining=0, total=500, expires_days=100)
    _seed_lot(migrated_engine, uid, "t8_lot_later", remaining=700, total=1000, expires_days=300)
    _seed_lot(migrated_engine, uid, "t8_lot_sooner", remaining=200, total=500, expires_days=30)

    snap = wallet_snapshot(rls_engine=migrated_engine, user_id=uid)

    assert snap["allowance_balance"] == 120
    assert snap["allowance_period"] is not None  # stamped
    lots = snap["payg_lots"]
    assert isinstance(lots, list)
    # FIFO spend order (oldest-expiring first) and the drained lot is excluded.
    assert [lot["credits_remaining"] for lot in lots] == [200, 700]
    assert snap["total_balance"] == 120 + 200 + 700


def test_wallet_snapshot_excludes_expired_lots(migrated_engine: Engine) -> None:
    uid = "u_t8_wallet_expired"
    _seed_user(migrated_engine, uid)
    _seed_credits(migrated_engine, uid, balance=50, stamp_month=True)
    _seed_lot(migrated_engine, uid, "t8_lot_expired", remaining=400, total=500, expires_days=-1)

    snap = wallet_snapshot(rls_engine=migrated_engine, user_id=uid)

    assert snap["payg_lots"] == []  # expired credits are NOT spendable and NOT shown
    assert snap["total_balance"] == 50


def test_unlimited_policy_wallet_is_the_sentinel(migrated_engine: Engine) -> None:
    """Community: no buckets, no lots, the unmetered sentinel (never touches the tables)."""
    snap = UnlimitedCreditsPolicy().wallet_snapshot(
        rls_engine=migrated_engine, user_id="u_t8_community"
    )
    assert snap["total_balance"] == 1_000_000_000
    assert snap["allowance_period"] is None
    assert snap["payg_lots"] == []


# --- The route: composition + per-plan threshold -------------------------------


def _client(tmp_path: Path, uid: str) -> TestClient | None:
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

    client = TestClient(app)
    client.__enter__()
    app.state.verify_token = _verify
    su = make_rls_engine(os.environ["DATABASE_URL"])
    _seed_user(su, uid)
    su.dispose()
    return client


def _auth(uid: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {uid}"}


def test_wallet_route_fresh_free_user_self_heals_to_monthly_allowance(tmp_path: Path) -> None:
    """A fresh free user's first wallet read: the T6 refresh lands the $3 allowance; the
    per-plan line is the free 60; no lots; plan free; auto-top-up off."""
    uid = "u_t8_route_free"
    c = _client(tmp_path, uid)
    if c is None:
        pytest.skip("APP_DATABASE_URL not set")
    try:
        resp = c.get("/v1/me/wallet", headers=_auth(uid))
        assert resp.status_code == 200
        body = resp.json()
        assert body["allowance_balance"] == 300  # the refreshed free monthly allowance
        assert body["total_balance"] == 300
        assert body["allowance_period"] is not None  # stamped by the self-heal
        assert body["payg_lots"] == []
        assert body["plan_code"] == "free"  # absent subscription row → free
        assert body["auto_topup_enabled"] is False
        assert body["low_balance_threshold"] == 60  # 20% of the free $3 allowance
        assert body["low_balance"] is False  # 300 >= 60
    finally:
        c.__exit__(None, None, None)


def test_wallet_route_pro_user_lots_threshold_and_toggle(tmp_path: Path) -> None:
    """A Pro opted-in user: plan + toggle surfaced; lots in FIFO order; the pro line (1200)
    flags a low total; the paid allowance is NOT touched by the free lazy refresh."""
    uid = "u_t8_route_pro"
    c = _client(tmp_path, uid)
    if c is None:
        pytest.skip("APP_DATABASE_URL not set")
    su = make_rls_engine(os.environ["DATABASE_URL"])
    try:
        _seed_sub(su, uid, "pro", auto_topup=True)
        _seed_credits(su, uid, balance=100, stamp_month=False)  # stale period; paid → untouched
        _seed_lot(su, uid, "t8_pro_lot", remaining=250, total=1000, expires_days=200)

        resp = c.get("/v1/me/wallet", headers=_auth(uid))
        assert resp.status_code == 200
        body = resp.json()
        assert body["plan_code"] == "pro"
        assert body["auto_topup_enabled"] is True
        assert body["allowance_balance"] == 100  # NOT reset to 300 — the paid guard held
        assert [lot["credits_remaining"] for lot in body["payg_lots"]] == [250]
        assert body["total_balance"] == 350
        assert body["low_balance_threshold"] == 1200  # 20% of the pro $60 allowance
        assert body["low_balance"] is True  # 350 < 1200
    finally:
        with su.begin() as conn:
            conn.execute(text("DELETE FROM payg_grants WHERE user_id = :u"), {"u": uid})
        su.dispose()
        c.__exit__(None, None, None)

"""The lazy monthly free-allowance refresh (Spec M4, T6) — on-access, cron-free self-heal.

``persona.credits.refresh_free_allowance_lazy`` overwrites a FREE user's allowance bucket to
the plans-catalog free allowance ($3 = 300) once per UTC month, guarded so a re-access in the
same month is a no-op and a PAID user is NEVER touched. The cloud ``MeteredCreditsPolicy`` fires
it at the top of every metered access (``require_credits`` / ``get_balance`` / the deduct/capture
methods) so a dormant free user always sees the current month's allowance — and the 100_000 seed
is corrected to $3 before the first spend (closing the un-hooked background-deduct path). The
community ``UnlimitedCreditsPolicy`` never refreshes (edition-gated, byte-identical). Real DB, no
mocks; assertions inspect the CONCRETE balance + ``allowance_period`` after the guarded write.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona.billing.plans import default_plan
from persona.credits import refresh_free_allowance_lazy
from persona_api.editions.credits_policy import MeteredCreditsPolicy, UnlimitedCreditsPolicy
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_FREE_ALLOWANCE = default_plan().included_allowance_credits  # 300 = $3 (owner-locked)
_STALE_PERIOD = "2000-01"  # a period that is never the current UTC month


def _current_month() -> str:
    return datetime.now(UTC).strftime("%Y-%m")


def _seed_user(engine: Engine, uid: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:u, :e) ON CONFLICT DO NOTHING"),
            {"u": uid, "e": f"{uid}@x.test"},
        )


def _seed_sub(engine: Engine, uid: str, plan_code: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO subscription (user_id, plan_code) VALUES (:u, :p) "
                "ON CONFLICT (user_id) DO UPDATE SET plan_code = :p"
            ),
            {"u": uid, "p": plan_code},
        )


def _set_credits(engine: Engine, uid: str, *, balance: int, period: str | None) -> None:
    """Force the credits row to a known (balance, allowance_period)."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO credits (user_id, balance, allowance_period) "
                "VALUES (:u, :b, :p) "
                "ON CONFLICT (user_id) DO UPDATE SET balance = :b, allowance_period = :p"
            ),
            {"u": uid, "b": balance, "p": period},
        )


def _row(engine: Engine, uid: str) -> tuple[int, str | None]:
    with engine.begin() as conn:
        r = conn.execute(
            text("SELECT balance, allowance_period FROM credits WHERE user_id = :u"),
            {"u": uid},
        ).first()
    assert r is not None, "credits row missing"
    return int(r[0]), (str(r[1]) if r[1] is not None else None)


def _has_credits_row(engine: Engine, uid: str) -> bool:
    with engine.begin() as conn:
        return (
            conn.execute(text("SELECT 1 FROM credits WHERE user_id = :u"), {"u": uid}).first()
            is not None
        )


# --- The core guarded refresh -------------------------------------------------


def test_free_user_first_access_new_month_resets_to_allowance(migrated_engine: Engine) -> None:
    """A free user whose stored period is stale → allowance OVERWRITTEN to $3, period stamped."""
    uid = "u_t6_free_stale"
    _seed_user(migrated_engine, uid)
    _seed_sub(migrated_engine, uid, "free")
    _set_credits(migrated_engine, uid, balance=5, period=_STALE_PERIOD)  # spent down last month

    landed = refresh_free_allowance_lazy(rls_engine=migrated_engine, user_id=uid)

    assert landed is True
    balance, period = _row(migrated_engine, uid)
    assert balance == _FREE_ALLOWANCE  # overwritten (NOT topped-up: not 5 + 300)
    assert period == _current_month()


def test_reaccess_same_month_is_noop_and_preserves_spend(migrated_engine: Engine) -> None:
    """Re-run in the SAME month → no-op: the guard matches nothing, mid-month spend preserved."""
    uid = "u_t6_free_same_month"
    _seed_user(migrated_engine, uid)
    _seed_sub(migrated_engine, uid, "free")
    # Already refreshed THIS month, then spent 100 of the 300.
    _set_credits(migrated_engine, uid, balance=_FREE_ALLOWANCE - 100, period=_current_month())

    landed = refresh_free_allowance_lazy(rls_engine=migrated_engine, user_id=uid)

    assert landed is False  # idempotent-per-period
    balance, period = _row(migrated_engine, uid)
    assert balance == _FREE_ALLOWANCE - 100  # the spend is NOT wiped by a re-overwrite
    assert period == _current_month()


def test_paid_user_is_never_touched(migrated_engine: Engine) -> None:
    """A paid (plus/pro) user's allowance is NEVER reset by the lazy path (the NOT EXISTS guard)."""
    uid = "u_t6_paid"
    _seed_user(migrated_engine, uid)
    _seed_sub(migrated_engine, uid, "plus")
    # A paid user carries a large allowance + a stale period (paid resets on invoice.paid).
    _set_credits(migrated_engine, uid, balance=1750, period=_STALE_PERIOD)

    landed = refresh_free_allowance_lazy(rls_engine=migrated_engine, user_id=uid)

    assert landed is False
    balance, period = _row(migrated_engine, uid)
    assert balance == 1750  # untouched — NOT clobbered down to the free $3
    assert period == _STALE_PERIOD  # not restamped by the lazy path


def test_pro_user_is_never_touched(migrated_engine: Engine) -> None:
    """The guard excludes every non-free plan, not just 'plus' (pro too)."""
    uid = "u_t6_pro"
    _seed_user(migrated_engine, uid)
    _seed_sub(migrated_engine, uid, "pro")
    _set_credits(migrated_engine, uid, balance=5000, period=_STALE_PERIOD)

    assert refresh_free_allowance_lazy(rls_engine=migrated_engine, user_id=uid) is False
    assert _row(migrated_engine, uid) == (5000, _STALE_PERIOD)


def test_absent_subscription_row_counts_as_free_and_refreshes(migrated_engine: Engine) -> None:
    """No subscription row = free (T5c precedent) → the refresh fires."""
    uid = "u_t6_no_sub"
    _seed_user(migrated_engine, uid)  # NO subscription row seeded
    _set_credits(migrated_engine, uid, balance=1, period=_STALE_PERIOD)

    landed = refresh_free_allowance_lazy(rls_engine=migrated_engine, user_id=uid)

    assert landed is True
    balance, period = _row(migrated_engine, uid)
    assert balance == _FREE_ALLOWANCE
    assert period == _current_month()


def test_new_user_no_row_seeds_then_refreshes_to_allowance(migrated_engine: Engine) -> None:
    """A brand-new free user (no credits row) → ensure_balance seeds, guard overwrites to $3."""
    uid = "u_t6_new_user"
    _seed_user(migrated_engine, uid)
    assert not _has_credits_row(migrated_engine, uid)

    landed = refresh_free_allowance_lazy(rls_engine=migrated_engine, user_id=uid)

    assert landed is True  # NULL seed period IS DISTINCT FROM current month → fires at once
    balance, period = _row(migrated_engine, uid)
    assert balance == _FREE_ALLOWANCE  # the 100_000 seed is never meaningfully spendable
    assert period == _current_month()


# --- The cloud policy wiring (require / balance / deduct all self-heal) --------


def test_metered_require_credits_self_heals_free_allowance(migrated_engine: Engine) -> None:
    """``MeteredCreditsPolicy.require_credits`` fires the refresh before the balance check."""
    uid = "u_t6_policy_require"
    _seed_user(migrated_engine, uid)
    _seed_sub(migrated_engine, uid, "free")
    _set_credits(migrated_engine, uid, balance=0, period=_STALE_PERIOD)  # exhausted last month
    policy = MeteredCreditsPolicy(daily_cap=0)

    balance = policy.require_credits(rls_engine=migrated_engine, user_id=uid)

    assert balance == _FREE_ALLOWANCE  # refreshed → the pre-flight gate passes (not 402)
    assert _row(migrated_engine, uid) == (_FREE_ALLOWANCE, _current_month())


def test_metered_get_balance_self_heals_free_allowance(migrated_engine: Engine) -> None:
    """``MeteredCreditsPolicy.get_balance`` returns the refreshed month allowance (wallet read)."""
    uid = "u_t6_policy_balance"
    _seed_user(migrated_engine, uid)
    _seed_sub(migrated_engine, uid, "free")
    _set_credits(migrated_engine, uid, balance=0, period=_STALE_PERIOD)
    policy = MeteredCreditsPolicy(daily_cap=0)

    assert policy.get_balance(rls_engine=migrated_engine, user_id=uid) == _FREE_ALLOWANCE
    assert _row(migrated_engine, uid) == (_FREE_ALLOWANCE, _current_month())


def test_metered_background_deduct_self_heals_before_charging(migrated_engine: Engine) -> None:
    """The un-hooked background path: ``deduct_idempotent`` on a FRESH free user (no row, no
    prior require) self-heals to $3 FIRST, then charges — so the spend draws from 300, not the
    100_000 seed."""
    uid = "u_t6_bg_deduct"
    _seed_user(migrated_engine, uid)
    _seed_sub(migrated_engine, uid, "free")
    assert not _has_credits_row(migrated_engine, uid)
    policy = MeteredCreditsPolicy(daily_cap=0)

    new_balance = policy.deduct_idempotent(
        rls_engine=migrated_engine,
        user_id=uid,
        amount=50,
        reason="bg_task",
        billing_key="t6-bg-key-1",
    )

    assert new_balance == _FREE_ALLOWANCE - 50  # 300 - 50, NOT 100_000 - 50
    balance, period = _row(migrated_engine, uid)
    assert balance == _FREE_ALLOWANCE - 50
    assert period == _current_month()


def test_metered_deduct_paid_user_not_refreshed(migrated_engine: Engine) -> None:
    """Discrimination through the policy: a paid user's deduct does NOT clobber their allowance
    down to $3 — the refresh no-ops, the deduct draws from their real balance."""
    uid = "u_t6_policy_paid_deduct"
    _seed_user(migrated_engine, uid)
    _seed_sub(migrated_engine, uid, "pro")
    _set_credits(migrated_engine, uid, balance=5000, period=_STALE_PERIOD)
    policy = MeteredCreditsPolicy(daily_cap=0)

    new_balance = policy.deduct(rls_engine=migrated_engine, user_id=uid, amount=100, reason="chat")

    assert new_balance == 5000 - 100  # drew from 5000, refresh did NOT reset to 300
    balance, period = _row(migrated_engine, uid)
    assert balance == 4900
    assert period == _STALE_PERIOD  # paid period untouched by the lazy path


def test_community_policy_never_refreshes(migrated_engine: Engine) -> None:
    """``UnlimitedCreditsPolicy`` (community) never runs the refresh — the DB row is untouched
    and the reported balance is the unmetered sentinel (byte-identical, edition-gated)."""
    uid = "u_t6_community"
    _seed_user(migrated_engine, uid)
    _seed_sub(migrated_engine, uid, "free")
    _set_credits(migrated_engine, uid, balance=7, period=_STALE_PERIOD)
    policy = UnlimitedCreditsPolicy()

    reported = policy.require_credits(rls_engine=migrated_engine, user_id=uid)
    policy.get_balance(rls_engine=migrated_engine, user_id=uid)
    policy.deduct(rls_engine=migrated_engine, user_id=uid, amount=999, reason="chat")

    assert reported == 1_000_000_000  # the unmetered sentinel, not the DB row
    assert _row(migrated_engine, uid) == (7, _STALE_PERIOD)  # DB row NEVER touched

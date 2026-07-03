"""CreditsPolicy day-cap enforcement — atomic, fail-loud, audited (Spec R7, T4/T5).

- ``MeteredCreditsPolicy(daily_cap=N)`` books the day-cap ATOMICALLY-with the credit
  decrement: under cap it deducts normally; over cap it raises
  :class:`DailySpendCapExceededError` and leaves the balance UNTOUCHED (the whole
  transaction rolled back — fail-closed) and writes a durable ``audit_log`` refusal row.
- ``UnlimitedCreditsPolicy`` no-ops (community): never books day-spend, never audits.
- ``daily_cap=0`` (uncapped Metered): behaviour is byte-identical to pre-R7.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from persona.errors import DailySpendCapExceededError
from persona_api.editions.credits_policy import MeteredCreditsPolicy, UnlimitedCreditsPolicy
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_USER = "u_daycap_policy"


@pytest.fixture
def seeded_engine(pg_engine: Engine) -> Engine:
    with pg_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
            {"i": _USER, "e": f"{_USER}@x.test"},
        )
        conn.execute(
            text(
                "INSERT INTO credits (user_id, balance) VALUES (:i, 100000) "
                "ON CONFLICT (user_id) DO UPDATE SET balance = 100000"
            ),
            {"i": _USER},
        )
    return pg_engine


def _balance(engine: Engine, user_id: str) -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                text("SELECT balance FROM credits WHERE user_id = :u"), {"u": user_id}
            ).scalar_one()
        )


def _day_spent(engine: Engine, user_id: str) -> int:
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT spent FROM day_spend "
                "WHERE user_id = :u AND utc_day = (now() AT TIME ZONE 'UTC')::date"
            ),
            {"u": user_id},
        ).scalar_one_or_none()
    return int(row) if row is not None else 0


def _audit_rows(engine: Engine, user_id: str) -> list[dict[str, object]]:
    with engine.begin() as conn:
        return [
            dict(r)
            for r in conn.execute(
                text(
                    "SELECT action, target, metadata FROM audit_log "
                    "WHERE user_id = :u AND action = 'daily_spend_cap_exceeded'"
                ),
                {"u": user_id},
            ).mappings()
        ]


def test_metered_under_cap_deducts_and_books_day_spend(seeded_engine: Engine) -> None:
    policy = MeteredCreditsPolicy(daily_cap=1000)
    policy.deduct(rls_engine=seeded_engine, user_id=_USER, amount=300, reason="chat_turn")
    assert _balance(seeded_engine, _USER) == 100000 - 300
    assert _day_spent(seeded_engine, _USER) == 300


def test_metered_over_cap_raises_leaves_balance_untouched_and_audits(seeded_engine: Engine) -> None:
    policy = MeteredCreditsPolicy(daily_cap=1000)
    # Spend up to the cap.
    policy.deduct(rls_engine=seeded_engine, user_id=_USER, amount=1000, reason="chat_turn")
    balance_at_cap = _balance(seeded_engine, _USER)

    # One more credit breaches the cap → 429-class refusal, balance untouched (atomic
    # rollback), day-spend counter unchanged, AND a durable audit row written.
    with pytest.raises(DailySpendCapExceededError) as ei:
        policy.deduct(rls_engine=seeded_engine, user_id=_USER, amount=1, reason="chat_turn")

    assert _balance(seeded_engine, _USER) == balance_at_cap, "over-cap must not decrement credits"
    assert _day_spent(seeded_engine, _USER) == 1000, "over-cap must not move the day counter"
    # Error context carries cap / spent / reset for the 429 body.
    assert ei.value.context["cap"] == "1000"
    assert ei.value.context["spent"] == "1000"
    assert "reset_epoch" in ei.value.context

    audits = _audit_rows(seeded_engine, _USER)
    assert len(audits) == 1, "exactly one durable refusal audit row"
    assert audits[0]["target"] == _USER
    assert audits[0]["metadata"]["cap"] == "1000"
    assert audits[0]["metadata"]["requested_cost"] == "1"


def test_metered_uncapped_is_pre_r7_identical(seeded_engine: Engine) -> None:
    """daily_cap=0 → no day-spend booking, no cap enforcement (nothing regresses)."""
    policy = MeteredCreditsPolicy(daily_cap=0)
    policy.deduct(rls_engine=seeded_engine, user_id=_USER, amount=50000, reason="big")
    assert _balance(seeded_engine, _USER) == 100000 - 50000
    assert _day_spent(seeded_engine, _USER) == 0, "uncapped policy books no day-spend"


def test_unlimited_policy_never_caps_or_audits(seeded_engine: Engine) -> None:
    """Community: deduct is a no-op returning a large constant — no day-spend, no audit."""
    policy = UnlimitedCreditsPolicy()
    result = policy.deduct(rls_engine=seeded_engine, user_id=_USER, amount=999999, reason="x")
    assert result > 0
    assert _balance(seeded_engine, _USER) == 100000, "community deduct never touches the ledger"
    assert _day_spent(seeded_engine, _USER) == 0
    assert _audit_rows(seeded_engine, _USER) == []

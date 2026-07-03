"""Per-day soft-ramp spend source — the real recorded-cost transition (Spec R7, T6).

Discharges D-23-X: the soft per-day cost-bias ramp now reads a DURABLE cross-session
number — today's recorded ``turn_logs.cost_cents`` for the owner+persona over the UTC
day — instead of the deferred ``0.0``. This proves the honest transition:

- ``RuntimeFactory._sum_day_spent_cents`` sums ONLY today's rows for the exact
  owner+persona (excludes other personas, other owners, and prior UTC days).
- As real recorded turns accumulate the derived number RISES (not hand-set), and
  feeding it through ``routing_budget.effective_weights`` shifts the ramp toward cost.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from persona_api.services.runtime_factory import RuntimeFactory
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_OWNER = "u_ramp"
_OTHER_OWNER = "u_ramp_other"
_PERSONA = "p_ramp"
_PERSONA_OTHER = "p_ramp_other"  # same owner, different persona
_PERSONA_UO = "p_ramp_uo"  # other owner's persona


def _seed_parents(engine: Engine) -> None:
    with engine.begin() as conn:
        for uid in (_OWNER, _OTHER_OWNER):
            conn.execute(
                text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
                {"i": uid, "e": f"{uid}@x.test"},
            )
        for pid, owner in (
            (_PERSONA, _OWNER),
            (_PERSONA_OTHER, _OWNER),
            (_PERSONA_UO, _OTHER_OWNER),
        ):
            conn.execute(
                text(
                    "INSERT INTO personas (id, owner_id, yaml) VALUES (:i, :o, 'y') "
                    "ON CONFLICT DO NOTHING"
                ),
                {"i": pid, "o": owner},
            )
        # conversations: (owner, persona) — the composite FK requires the persona
        # to belong to the same owner.
        for cid, owner, persona in (
            ("c_ramp_1", _OWNER, _PERSONA),
            ("c_ramp_other", _OWNER, _PERSONA_OTHER),
            ("c_ramp_uo", _OTHER_OWNER, _PERSONA_UO),
        ):
            conn.execute(
                text(
                    "INSERT INTO conversations (id, owner_id, persona_id) "
                    "VALUES (:c, :o, :p) ON CONFLICT DO NOTHING"
                ),
                {"c": cid, "o": owner, "p": persona},
            )


def _log_turn(engine: Engine, conv_id: str, *, cost_cents: float, days_ago: int = 0) -> None:
    """Record a real turn_logs row (the durable recorded cost the ramp reads)."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO turn_logs (conversation_id, turn_index, tier_used, model_name, "
                "provider, prompt_tokens, completion_tokens, latency_ms, cost_cents, "
                "tool_calls, history_compacted, created_at) VALUES "
                "(:c, 0, 'daily', 'm', 'p', 10, 10, 1.0, :cost, 0, FALSE, "
                "now() - make_interval(days => :d))"
            ),
            {"c": conv_id, "cost": cost_cents, "d": days_ago},
        )


@pytest.fixture
def seeded(pg_engine: Engine) -> Engine:
    _seed_parents(pg_engine)
    return pg_engine


def test_sum_is_scoped_to_owner_persona_and_today(seeded: Engine) -> None:
    engine = seeded
    # Target owner+persona: two rows today (7.5 + 2.5 = 10.0).
    _log_turn(engine, "c_ramp_1", cost_cents=7.5)
    _log_turn(engine, "c_ramp_1", cost_cents=2.5)
    # Noise that must be EXCLUDED:
    _log_turn(engine, "c_ramp_1", cost_cents=99.0, days_ago=2)  # prior UTC day
    _log_turn(engine, "c_ramp_other", cost_cents=50.0)  # same owner, other persona
    _log_turn(engine, "c_ramp_uo", cost_cents=50.0)  # other owner

    total = RuntimeFactory._sum_day_spent_cents(engine, owner_id=_OWNER, persona_id=_PERSONA)
    assert total == pytest.approx(10.0), "only today's rows for the exact owner+persona count"


def test_derived_number_rises_and_moves_the_ramp(seeded: Engine) -> None:
    """The honest transition: recorded turns accumulate → the derived day-spend rises
    → the soft ramp biases toward cost. Never a hand-set number."""
    from persona_runtime.routing import routing_budget
    from persona_runtime.routing.scoring import ProfileWeights

    engine = seeded
    cap = 500.0
    base = ProfileWeights(cost=0.34, quality=0.33, latency=0.33)

    # No turns yet → 0.0 → ramp unbiased (== pre-R7 behaviour).
    d0 = RuntimeFactory._sum_day_spent_cents(engine, owner_id=_OWNER, persona_id=_PERSONA)
    w0 = routing_budget.effective_weights(base, day_spent_cents=d0, max_cents_per_day=cap)
    assert d0 == 0.0
    assert w0.cost == base.cost

    # Log turns up to just below the cap → derived number rises, ramp shifts to cost.
    for _ in range(48):
        _log_turn(engine, "c_ramp_1", cost_cents=10.0)  # 48 * 10 = 480 (96% of cap)
    d1 = RuntimeFactory._sum_day_spent_cents(engine, owner_id=_OWNER, persona_id=_PERSONA)
    w1 = routing_budget.effective_weights(base, day_spent_cents=d1, max_cents_per_day=cap)
    assert d1 == pytest.approx(480.0)
    assert w1.cost > w0.cost, "the ramp must bias toward cost as recorded day-spend rises"

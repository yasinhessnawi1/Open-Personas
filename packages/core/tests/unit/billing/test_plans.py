"""Plan catalog + ``$``↔credit presentation (Spec M4, T1c).

No DB — pure config + arithmetic. Pins: the ``$``↔credit round-trip, the catalog is
frozen/immutable, Free is the permanent default, the Free model-set carries the
no-paid-fallback flag, and the owner-locked Phase-4 numbers.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from persona.billing.plans import (
    DEFAULT_PLAN_CODE,
    PAYG_PACKS,
    PLANS,
    PaygPack,
    Plan,
    PlanCode,
    all_plans,
    credits_to_dollars,
    default_plan,
    dollars_to_credits,
    format_dollars,
    get_plan,
)
from pydantic import ValidationError

# --- $↔credit helper round-trips --------------------------------------------


def test_dollars_to_credits_multiplies_by_100() -> None:
    assert dollars_to_credits(Decimal("5.00")) == 500
    assert dollars_to_credits(5) == 500
    assert dollars_to_credits("2.50") == 250
    assert dollars_to_credits(Decimal("0")) == 0


def test_credits_to_dollars_divides_by_100() -> None:
    assert credits_to_dollars(500) == Decimal("5")
    assert credits_to_dollars(550) == Decimal("5.5")
    assert credits_to_dollars(0) == Decimal("0")


def test_dollar_credit_round_trip() -> None:
    for dollars in ("5.00", "15.00", "0.01", "60.00"):
        credit_amount = dollars_to_credits(Decimal(dollars))
        assert credits_to_dollars(credit_amount) == Decimal(dollars)
    for credit_amount in (0, 1, 300, 2000, 5000):
        assert dollars_to_credits(credits_to_dollars(credit_amount)) == credit_amount


def test_format_dollars_is_two_decimal() -> None:
    assert format_dollars(500) == "$5.00"
    assert format_dollars(550) == "$5.50"
    assert format_dollars(0) == "$0.00"
    assert format_dollars(6000) == "$60.00"


# --- catalog is frozen / immutable ------------------------------------------


def test_plan_is_frozen() -> None:
    plan = PLANS[PlanCode.free]
    with pytest.raises(ValidationError):
        plan.monthly_price_credits = 999  # type: ignore[misc]


def test_payg_pack_is_frozen() -> None:
    with pytest.raises(ValidationError):
        PAYG_PACKS[0].price_credits = 999  # type: ignore[misc]


def test_plans_registry_is_read_only() -> None:
    """``PLANS`` is a MappingProxyType — a mutation attempt raises, not silently mutates."""
    with pytest.raises(TypeError):
        PLANS[PlanCode.free] = PLANS[PlanCode.pro]  # type: ignore[index]


def test_plan_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        Plan(  # type: ignore[call-arg]
            code=PlanCode.free,
            monthly_price_credits=0,
            included_allowance_credits=300,
            model_set=PLANS[PlanCode.free].model_set,
            payg_eligible=True,
            auto_topup_eligible=False,
            is_default=True,
            bogus="x",
        )


# --- Free is the permanent default ------------------------------------------


def test_free_is_the_permanent_default() -> None:
    assert DEFAULT_PLAN_CODE is PlanCode.free
    assert default_plan().code is PlanCode.free
    assert default_plan().is_default is True
    # Exactly one default (Free); the paid plans are not defaults.
    assert [p.code for p in all_plans() if p.is_default] == [PlanCode.free]


# --- the Free model-set carries the no-paid-fallback flag -------------------


def test_free_model_set_forbids_paid_fallback() -> None:
    assert PLANS[PlanCode.free].model_set.allows_paid_fallback is False
    # Free references the FREE-ONLY env var names, never the paid tiers.
    assert PLANS[PlanCode.free].model_set.frontier_models_env == "PERSONA_FREE_FRONTIER_MODELS"
    assert PLANS[PlanCode.free].model_set.mid_models_env == "PERSONA_FREE_MID_MODELS"


def test_paid_plans_allow_fallback_and_reference_prod_tiers() -> None:
    for code in (PlanCode.plus, PlanCode.pro):
        ms = PLANS[code].model_set
        assert ms.allows_paid_fallback is True
        assert ms.frontier_models_env == "PERSONA_FRONTIER_MODELS"
        assert ms.mid_models_env == "PERSONA_MID_MODELS"


# --- owner-locked Phase-4 numbers (D-M4-R1) ---------------------------------


def test_plan_numbers_are_the_locked_phase4_values() -> None:
    free, plus, pro = all_plans()
    assert (free.monthly_price_credits, free.included_allowance_credits) == (0, 300)  # $0 / $3
    assert (plus.monthly_price_credits, plus.included_allowance_credits) == (1500, 2000)  # $15/$20
    assert (pro.monthly_price_credits, pro.included_allowance_credits) == (5000, 6000)  # $50/$60
    # $ views agree.
    assert free.included_allowance_dollars == Decimal("3")
    assert plus.monthly_price_dollars == Decimal("15")
    assert pro.monthly_price_dollars == Decimal("50")


def test_auto_topup_is_pro_only() -> None:
    assert PLANS[PlanCode.pro].auto_topup_eligible is True
    assert PLANS[PlanCode.plus].auto_topup_eligible is False
    assert PLANS[PlanCode.free].auto_topup_eligible is False


def test_payg_packs_are_the_locked_sizes_with_12mo_expiry() -> None:
    assert [p.price_credits for p in PAYG_PACKS] == [500, 1000, 2500, 5000]  # $5/$10/$25/$50
    for pack in PAYG_PACKS:
        assert pack.expiry_months == 12
        assert pack.granted_credits == pack.price_credits  # 1:1 purchase
    assert PAYG_PACKS[0].price_dollars == Decimal("5")


def test_payg_pack_rejects_nonpositive_price() -> None:
    with pytest.raises(ValidationError):
        PaygPack(price_credits=0)


# --- accessors ---------------------------------------------------------------


def test_get_plan_accepts_enum_and_string() -> None:
    assert get_plan(PlanCode.plus) is PLANS[PlanCode.plus]
    assert get_plan("pro") is PLANS[PlanCode.pro]


def test_get_plan_unknown_raises() -> None:
    with pytest.raises(ValueError, match="not a valid"):
        get_plan("enterprise")


# --- per-plan low-balance threshold (Spec M4, T8) -----------------------------


def test_low_balance_threshold_is_20_percent_of_allowance() -> None:
    """The per-plan warning line (replaces the retired flat 10_000): 20% of the
    included allowance — Free 60 ($0.60), Plus 400 ($4), Pro 1200 ($12)."""
    assert PLANS[PlanCode.free].low_balance_threshold_credits == 60
    assert PLANS[PlanCode.plus].low_balance_threshold_credits == 400
    assert PLANS[PlanCode.pro].low_balance_threshold_credits == 1200

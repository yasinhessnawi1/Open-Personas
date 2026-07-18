"""The one credit formula (Spec M3, T1a — D-M3-2/3/4).

``credits = max(floor, ceil(MARKUP × (provider_cents + infra_flat_cents)))`` at
1 credit = 1¢, with M2's ``Decimal(str(cost))`` printed-value ceil discipline
(float representation noise never manufactures an extra credit).
"""

from __future__ import annotations

import pytest
from persona.billing.formula import BillingConfig, credits_charged


def test_pure_passthrough_ceils_a_fractional_cost() -> None:
    # markup 1.0, no infra: a 5.3c cost rounds UP to 6 credits (ceil semantics).
    assert credits_charged(provider_cents=5.3, infra_flat_cents=0.0, markup=1.0, floor=1) == 6


def test_exact_integer_cost_does_not_gain_a_credit_from_float_noise() -> None:
    # The M2 discipline: an exact-integer cents value stays integer (no +1).
    assert credits_charged(provider_cents=9.0, infra_flat_cents=0.0, markup=1.0, floor=1) == 9


def test_subcent_cost_is_floored_not_lost() -> None:
    # A 0.0105c chat turn ceils to 1 and the floor holds it at 1 (never 0).
    assert credits_charged(provider_cents=0.0105, infra_flat_cents=0.0, markup=1.0, floor=1) == 1


def test_infra_only_surface_charges_the_infra_ceil() -> None:
    # Zero provider cost + a 0.1c infra flat → ceil(0.1) = 1 credit (floor 0).
    assert credits_charged(provider_cents=0.0, infra_flat_cents=0.1, markup=1.0, floor=0) == 1


def test_infra_is_additive_on_a_provider_surface() -> None:
    # 5.3c provider + 1.0c infra = 6.3c → ceil = 7.
    assert credits_charged(provider_cents=5.3, infra_flat_cents=1.0, markup=1.0, floor=1) == 7


def test_markup_scales_the_whole_cost_before_the_ceil() -> None:
    # markup 1.4 × 5.0c = 7.0c → 7 (the M4 margin knob; M3 ships 1.0).
    assert credits_charged(provider_cents=5.0, infra_flat_cents=0.0, markup=1.4, floor=1) == 7


def test_floor_dominates_a_near_zero_charge() -> None:
    assert credits_charged(provider_cents=0.0, infra_flat_cents=0.0, markup=1.0, floor=1) == 1


def test_negative_inputs_are_clamped_and_never_reduce_the_floor() -> None:
    # A bad upstream value must never OVERCHARGE nor drop below the floor.
    assert credits_charged(provider_cents=-5.0, infra_flat_cents=-2.0, markup=1.0, floor=1) == 1


def test_billing_config_defaults_are_the_ratified_values() -> None:
    cfg = BillingConfig()
    assert cfg.credit_markup == pytest.approx(1.0)
    assert cfg.infra_rate_per_llm_call_cents == pytest.approx(0.1)
    assert cfg.infra_rate_per_image_cents == pytest.approx(1.0)
    assert cfg.infra_rate_per_voice_min_cents == pytest.approx(1.0)
    assert cfg.infra_rate_per_embed_batch_cents == pytest.approx(0.1)
    assert cfg.infra_rate_per_tool_call_cents == pytest.approx(0.1)
    assert cfg.infra_rate_per_sandbox_exec_cents == pytest.approx(1.0)


def test_billing_config_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSONA_CREDIT_MARKUP", "1.4")
    monkeypatch.setenv("PERSONA_INFRA_RATE_PER_IMAGE_CENTS", "2.5")
    cfg = BillingConfig()
    assert cfg.credit_markup == pytest.approx(1.4)
    assert cfg.infra_rate_per_image_cents == pytest.approx(2.5)

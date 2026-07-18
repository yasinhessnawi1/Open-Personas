"""The image ceiling-pre-deduct true-up (Spec M3, T3a — D-M3-10 / D-M3-R4).

``_true_up_image_charge`` reconciles the ceiling already pre-deducted against the
REAL provider cost priced from the generation's token usage: the common OVERAGE
case (real < ceiling → refund), the DELTA case (real > ceiling → capture the
extra, floored), the EXACT case, and the UNPRICED fallback (→ floor). Basis-
agnostic — an OpenRouter ``usage.cost`` actual is priced without a resolver.
"""

from __future__ import annotations

from typing import Any

import pytest
from persona.billing import BillingConfig
from persona.imagegen import GeneratedImage, GenerationResult
from persona_api.imagegen.service import _true_up_image_charge

_ENGINE: Any = object()  # a sentinel; the recording policy never touches it


class _RecordingPolicy:
    """Records the single true-up ledger call (deduct / capture / refund)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def deduct(self, **kw: object) -> int:
        self.calls.append(("deduct", kw))
        return 0

    def capture_up_to(self, **kw: object) -> tuple[int, int]:
        self.calls.append(("capture_up_to", kw))
        return int(kw["amount"]), 0  # type: ignore[call-overload]

    def refund(self, **kw: object) -> int:
        self.calls.append(("refund", kw))
        return 0


def _result(
    *, provider: str = "openrouter", model: str = "openai/gpt-5.4-image-2", cost_usd: float | None
) -> GenerationResult:
    return GenerationResult(
        images=[GeneratedImage(media_type="image/png", width=1, height=1)],
        provider=provider,
        model=model,
        latency_ms=1.0,
        prompt_tokens=100,
        completion_tokens=1000,
        cost_usd=cost_usd,
    )


def _trueup(policy: _RecordingPolicy, result: GenerationResult, *, ceiling: int) -> None:
    _true_up_image_charge(
        policy,  # type: ignore[arg-type]
        _ENGINE,
        "u1",
        result=result,
        ceiling=ceiling,
        floor=1,
        cost_source=None,
        billing_config=BillingConfig(),
    )


def test_overage_refunds_the_difference() -> None:
    # 3¢ actual → ceil(3.0) = 3 credits; ceiling 50 → refund the 47 overage.
    policy = _RecordingPolicy()
    _trueup(policy, _result(cost_usd=0.03), ceiling=50)
    method, kw = policy.calls[-1]
    assert method == "refund"
    assert kw["amount"] == 47
    assert kw["reason"] == "image_gen_trueup:actual_openrouter"
    assert kw["cost_cents"] == pytest.approx(3.0)
    assert kw["cost_basis"] == "actual_openrouter"


def test_delta_captures_the_extra_floored() -> None:
    # 60¢ actual → 60 credits > ceiling 50 → capture the extra 10 (floored, never overdraws).
    policy = _RecordingPolicy()
    _trueup(policy, _result(cost_usd=0.60), ceiling=50)
    method, kw = policy.calls[-1]
    assert method == "capture_up_to"
    assert kw["amount"] == 10
    assert kw["reason"] == "image_gen:actual_openrouter"
    assert kw["cost_cents"] == pytest.approx(60.0)


def test_exact_records_the_cost_with_a_zero_adjustment() -> None:
    # 50¢ actual → 50 credits == ceiling → a zero-credit deduct records the cost.
    policy = _RecordingPolicy()
    _trueup(policy, _result(cost_usd=0.50), ceiling=50)
    method, kw = policy.calls[-1]
    assert method == "deduct"
    assert kw["amount"] == 0
    assert kw["cost_basis"] == "actual_openrouter"


def test_unpriced_falls_to_the_floor_and_refunds() -> None:
    # No usage.cost + an off-table model → unpriced → charge falls to the floor (1);
    # ceiling 50 → refund 49.
    policy = _RecordingPolicy()
    _trueup(policy, _result(provider="fake", model="fake-1", cost_usd=None), ceiling=50)
    method, kw = policy.calls[-1]
    assert method == "refund"
    assert kw["amount"] == 49
    assert kw["cost_cents"] == pytest.approx(0.0)
    assert kw["cost_basis"] == "unpriced"

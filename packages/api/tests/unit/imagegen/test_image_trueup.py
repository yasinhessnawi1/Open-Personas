"""The image ceiling-pre-deduct true-up (Spec M3, T3a — D-M3-10 / D-M3-R4).

``_true_up_image_charge`` reconciles the ceiling already pre-deducted against the
REAL provider cost priced from the generation's token usage: the common OVERAGE
case (real < ceiling → refund), the DELTA case (real > ceiling → capture the
extra, floored), the EXACT case, and the UNPRICED fallback (→ floor). Basis-
agnostic — an OpenRouter ``usage.cost`` actual is priced without a resolver.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from loguru import logger as _loguru_logger
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


# --- a backend that reports no cost is still priced, not floored (M-track sweep) ----------


def test_a_registry_priced_image_bills_its_real_cost_not_the_floor() -> None:
    """The headline defect: only the OpenRouter backend sets ``cost_usd``, so every other
    image priced at 0.0 and billed the 1-credit floor. 50 pre-deducted, 49 refunded, a
    roughly 4 cent image recovered as 1 cent. With the registry consulted, the same result
    with NO provider-reported cost bills the row's price."""
    policy = _RecordingPolicy()
    _trueup(policy, _result(provider="openrouter", model="gpt-image-2", cost_usd=None), ceiling=50)

    method, kw = policy.calls[-1]
    assert method == "refund"
    assert kw["cost_cents"] == pytest.approx(4.0), "the image was not priced from the registry"
    assert kw["cost_basis"] == "estimate_catalog"
    assert kw["amount"] == 46, "refunded as though the image had cost the floor"


def test_a_provider_reported_actual_still_wins_over_the_registry() -> None:
    """What we actually paid beats a catalog estimate; the registry is the fallback only."""
    policy = _RecordingPolicy()
    _trueup(policy, _result(provider="openrouter", model="gpt-image-2", cost_usd=0.09), ceiling=50)

    _, kw = policy.calls[-1]
    assert kw["cost_cents"] == pytest.approx(9.0)
    assert kw["cost_basis"] == "actual_openrouter"


def test_an_unpriceable_image_says_so_instead_of_billing_the_floor_quietly() -> None:
    """A provider with no actual and no row is a REAL gap in the money path. It still floors,
    because inventing a price would overcharge a person, but it must name itself: the original
    defect survived precisely because an unpriced image looked exactly like a cheap one."""
    captured: list[str] = []
    sink_id = _loguru_logger.add(lambda msg: captured.append(str(msg)), level="WARNING")
    try:
        policy = _RecordingPolicy()
        _trueup(policy, _result(provider="fake", model="fake-1", cost_usd=None), ceiling=50)
    finally:
        _loguru_logger.remove(sink_id)

    _, kw = policy.calls[-1]
    assert kw["cost_basis"] == "unpriced"
    assert any("fake" in line for line in captured), (
        "an image we cannot price was billed the floor with no signal naming the provider"
    )


# --- the backends that report nothing bill their vendor's real price, not the floor -------


def test_an_openai_image_bills_its_published_price_not_the_floor() -> None:
    """gpt-image-1 at 1024x1024 medium costs $0.042. Floored, it was recovered as 1 cent."""
    policy = _RecordingPolicy()
    _trueup(policy, _result(provider="openai", model="gpt-image-1", cost_usd=None), ceiling=50)

    method, kw = policy.calls[-1]
    assert method == "refund"
    assert kw["cost_cents"] == pytest.approx(4.2), "the image was not priced from the registry"
    assert kw["cost_basis"] == "estimate_static"
    assert kw["amount"] == 45, "refunded as though a 4.2 cent image had cost the floor"


def test_a_fal_image_bills_its_published_price_not_the_floor() -> None:
    """FLUX 1.1 [pro] is $0.04 per megapixel and a 1024 square is one megapixel."""
    policy = _RecordingPolicy()
    _trueup(
        policy,
        _result(provider="fal", model="fal-ai/flux-pro/v1.1", cost_usd=None),
        ceiling=50,
    )

    method, kw = policy.calls[-1]
    assert method == "refund"
    assert kw["cost_cents"] == pytest.approx(4.0), "the image was not priced from the registry"
    assert kw["cost_basis"] == "estimate_static"
    assert kw["amount"] == 46, "refunded as though a 4 cent image had cost the floor"


def test_a_cloudflare_image_records_its_real_sub_cent_cost() -> None:
    """flux-1-schnell really does cost less than a cent, so the floor is the honest CHARGE.

    What was wrong was the recorded cost and basis: 0.0 cents "unpriced" says we do not know
    what an image cost, and we do. The ledger row is the thing spend reporting reads, so the
    price has to land on it even when the credit charge does not move.
    """
    policy = _RecordingPolicy()
    _trueup(
        policy,
        _result(provider="cloudflare", model="@cf/black-forest-labs/flux-1-schnell", cost_usd=None),
        ceiling=50,
    )

    _, kw = policy.calls[-1]
    assert kw["cost_cents"] == pytest.approx(0.06336), "the image was recorded as unpriced"
    assert kw["cost_basis"] == "estimate_static"
    assert kw["amount"] == 49, "a sub-cent image still settles to the 1-credit floor"


def test_an_nvidia_image_still_floors_and_still_warns() -> None:
    """NVIDIA publishes no per-image price, so the gap stays open AND stays loud."""
    captured: list[str] = []
    sink_id = _loguru_logger.add(lambda msg: captured.append(str(msg)), level="WARNING")
    try:
        policy = _RecordingPolicy()
        _trueup(
            policy,
            _result(provider="nvidia", model="nvidia/flux.2-klein-4b", cost_usd=None),
            ceiling=50,
        )
    finally:
        _loguru_logger.remove(sink_id)

    _, kw = policy.calls[-1]
    assert kw["cost_basis"] == "unpriced"
    assert any("nvidia" in line for line in captured), (
        "an nvidia image we cannot price was billed the floor with no signal naming it"
    )


# --- R9-198: a registry price is per IMAGE, so it scales with the images returned ---------


def _n_images(result: GenerationResult, n: int) -> GenerationResult:
    """The same generation, carrying ``n`` images instead of one."""
    image = result.images[0]
    return result.model_copy(update={"images": [image] * n})


def _registry_result(provider: str, model: str, *, images: int) -> GenerationResult:
    """A backend that reports nothing (no tokens, no actual), so only the row prices it."""
    single = GenerationResult(
        images=[GeneratedImage(media_type="image/png", width=1, height=1)],
        provider=provider,
        model=model,
        latency_ms=1.0,
    )
    return _n_images(single, images)


_REGISTRY_PRICED = [
    pytest.param("openai", "gpt-image-1", 4.2, id="openai"),
    pytest.param("fal", "fal-ai/flux-pro/v1.1", 4.0, id="fal"),
    pytest.param("cloudflare", "@cf/black-forest-labs/flux-1-schnell", 0.06336, id="cloudflare"),
    pytest.param("openrouter", "gpt-image-2", 4.0, id="openrouter-row"),
]


@pytest.mark.parametrize(("provider", "model", "per_image"), _REGISTRY_PRICED)
def test_two_registry_priced_images_cost_twice_one(
    provider: str, model: str, per_image: float
) -> None:
    """The headline R9-198 defect: the row's per-image price went straight into the charge,
    so a two-image call billed the cost of one. A two-image OpenAI call costs 8.4 cents."""
    one, two = _RecordingPolicy(), _RecordingPolicy()
    _trueup(one, _registry_result(provider, model, images=1), ceiling=100)
    _trueup(two, _registry_result(provider, model, images=2), ceiling=100)

    _, one_kw = one.calls[-1]
    _, two_kw = two.calls[-1]
    assert one_kw["cost_cents"] == pytest.approx(per_image)
    assert two_kw["cost_cents"] == pytest.approx(2 * per_image), "two images billed as one"
    assert two_kw["cost_basis"] == one_kw["cost_basis"], "the count must not change the basis"


def test_a_two_image_openai_call_charges_nine_credits_not_five() -> None:
    """The arithmetic the issue quotes, end to end through the credit formula: 2 x 4.2 cents
    is 8.4 cents, which ceils to 9 credits; the bug billed 4.2 cents, 5 credits. A ceiling of
    50 per image was pre-deducted for two (100), so 91 comes back, not 95."""
    policy = _RecordingPolicy()
    _trueup(policy, _registry_result("openai", "gpt-image-1", images=2), ceiling=100)

    method, kw = policy.calls[-1]
    assert method == "refund"
    assert kw["cost_cents"] == pytest.approx(8.4)
    assert kw["amount"] == 91, "refunded as though only one of the two images was made"


def test_a_provider_returning_fewer_images_than_asked_is_charged_for_what_it_returned() -> None:
    """Two were requested (so two ceilings, 100, were pre-deducted) but the provider sent back
    one. The person pays for the one image they got, not the two they asked for."""
    policy = _RecordingPolicy()
    _trueup(policy, _registry_result("openai", "gpt-image-1", images=1), ceiling=100)

    method, kw = policy.calls[-1]
    assert method == "refund"
    assert kw["cost_cents"] == pytest.approx(4.2), "charged for an image that never came back"
    assert kw["amount"] == 95


def test_an_unpriced_multi_image_call_still_floors_once() -> None:
    """No row and no actual: zero times two is still zero, so the charge is the floor, not
    a doubled floor. The floor is a per-call minimum, not a per-image price."""
    policy = _RecordingPolicy()
    _trueup(policy, _registry_result("nvidia", "nvidia/flux.2-klein-4b", images=2), ceiling=100)

    _, kw = policy.calls[-1]
    assert kw["cost_basis"] == "unpriced"
    assert kw["cost_cents"] == pytest.approx(0.0)
    assert kw["amount"] == 99


# --- ...but a cost the provider reported for the WHOLE call is never multiplied -----------


def test_a_provider_reported_actual_for_a_two_image_call_is_not_doubled() -> None:
    """OpenRouter's ``usage.cost`` is what the whole request cost us, however many images it
    carried. Multiplying it by the count would charge a person twice for one bill."""
    policy = _RecordingPolicy()
    two = _n_images(_result(provider="openrouter", model="gpt-image-2", cost_usd=0.09), 2)
    _trueup(policy, two, ceiling=100)

    _, kw = policy.calls[-1]
    assert kw["cost_basis"] == "actual_openrouter"
    assert kw["cost_cents"] == pytest.approx(9.0), "a whole-call actual was multiplied"
    assert kw["amount"] == 91


class _TokenPricedSource:
    """A resolver that prices the served model per TOKEN (a whole-call estimate), no network."""

    def resolve_with_source(
        self,
        model_id: str,  # noqa: ARG002 (the CostSource signature)
        *,
        allow_fetch: bool = True,  # noqa: ARG002 (the CostSource signature)
    ) -> tuple[SimpleNamespace, str]:
        meta = SimpleNamespace(cost_input_per_1k_tokens=1.0, cost_output_per_1k_tokens=2.0)
        return meta, "static"


def test_a_token_priced_estimate_for_a_two_image_call_is_not_doubled() -> None:
    """A token estimate is priced over the call's own usage, which already covers every image
    the call produced: 100 prompt tokens at 1 cent per 1k plus 1000 completion tokens at
    2 cents per 1k is 2.1 cents for the call. Only the per-image row scales with the count."""
    policy = _RecordingPolicy()
    two = _n_images(_result(provider="openai", model="gpt-image-1", cost_usd=None), 2)
    _true_up_image_charge(
        policy,  # type: ignore[arg-type]
        _ENGINE,
        "u1",
        result=two,
        ceiling=100,
        floor=1,
        cost_source=_TokenPricedSource(),  # type: ignore[arg-type]
        billing_config=BillingConfig(),
    )

    _, kw = policy.calls[-1]
    assert kw["cost_basis"] == "estimate_static"
    assert kw["cost_cents"] == pytest.approx(2.1), "a whole-call token estimate was multiplied"

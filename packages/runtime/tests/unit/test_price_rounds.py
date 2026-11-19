"""Each model round of a turn is priced at the model that served it (R9-246).

A turn with tool use is several model calls, and the calls of one turn can be served
by different models (a tool round by the primary, the follow-up by a fallback). The
text TurnLog summed every round's tokens and priced the sum at the LAST round's
model, so the same turn shape was over- or under-charged in chat while voice already
priced per round. :func:`price_rounds` is now the one per-round pricing function for
both surfaces.

Rules pinned here (D-ORCH-11-18-h):

* each round: :func:`compute_turn_cost` at its own served pair, with its own
  OpenRouter actual (gated on that round's provider);
* the sum is rounded ONCE to micro-cents; the charge ceils it once, per turn;
* D3, the basis label: the least certain basis among the priced rounds
  (catalog, then static, then actual), so a partial actual never labels the whole
  turn; ``unpriced`` only when no round was priced;
* D4: an unpriced round inside a priced turn adds 0 cents and logs a warning that
  names its provider and model;
* D-ORCH-11-18-s (reverses D1 of D-ORCH-11-18-h): a failed try that reported
  provider tokens IS charged, at the model that made it, inside the same single
  rounding and single ceil; a failed try that reported no tokens costs nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import pytest
from loguru import logger as _loguru_logger
from persona.backends.model_metadata import ModelMetadata
from persona.backends.types import BackendAttempt, TokenUsage
from persona.billing import BillingConfig, credits_charged
from persona_runtime.cost import PricedRound, compute_turn_cost, price_rounds

if TYPE_CHECKING:
    from collections.abc import Iterator


def _rate(input_cents: float, output_cents: float) -> ModelMetadata:
    return ModelMetadata(
        cost_input_per_1k_tokens=input_cents,
        cost_output_per_1k_tokens=output_cents,
        latency_p50_ms=100.0,
        quality_benchmark=0.5,
        tools_supported=True,
        vision_supported=False,
        context_length=8192,
    )


#: Rates as the static tables hold them for the worked scenarios (cents per 1k):
#: deepseek-chat 0.027 / 0.11, claude-sonnet-4-6 0.30 / 1.50.
_STATIC = {
    "deepseek/deepseek-chat": _rate(0.027, 0.11),
    "anthropic/claude-sonnet-4-6": _rate(0.30, 1.50),
    "vendor/tiny": _rate(0.1, 0.0),
}
_CATALOG = {"z-ai/glm-4.6": _rate(0.05, 0.2)}


class _Source:
    def resolve_with_source(
        self, model_id: str, *, allow_fetch: bool = True
    ) -> tuple[ModelMetadata, Literal["static", "catalog"]] | None:
        assert allow_fetch is False  # pricing never fetches (Spec M2 invariant)
        if model_id in _STATIC:
            return _STATIC[model_id], "static"
        if model_id in _CATALOG:
            return _CATALOG[model_id], "catalog"
        return None


_SRC = _Source()


def _round(
    provider: str,
    model: str,
    prompt: int,
    completion: int,
    cost_usd: float | None = None,
    failed_tries: tuple[BackendAttempt, ...] = (),
) -> PricedRound:
    return PricedRound(
        prompt_tokens=prompt,
        completion_tokens=completion,
        cost_usd=cost_usd,
        provider=provider,
        model=model,
        failed_tries=failed_tries,
    )


@pytest.fixture
def logs() -> Iterator[list[tuple[str, str]]]:
    captured: list[tuple[str, str]] = []
    sink = _loguru_logger.add(
        lambda m: captured.append((m.record["level"].name, m.record["message"])), level="DEBUG"
    )
    try:
        yield captured
    finally:
        _loguru_logger.remove(sink)


def test_a_primary_tool_round_and_a_fallback_follow_up_are_priced_each_at_its_own_model() -> None:
    """Overcharge direction: 3.538 cents, not 6.0 (17k in + 600 out all at Sonnet)."""
    rounds = [
        _round("deepseek", "deepseek-chat", 8000, 200),  # 0.216 + 0.022 = 0.238
        _round("anthropic", "claude-sonnet-4-6", 9000, 400),  # 2.7 + 0.6 = 3.3
    ]
    cents, basis = price_rounds(rounds, source=_SRC)
    assert cents == pytest.approx(3.538)
    assert basis == "estimate_static"
    old, _ = compute_turn_cost(
        provider="anthropic",
        model="claude-sonnet-4-6",
        prompt_tokens=17000,
        completion_tokens=600,
        source=_SRC,
    )
    assert old == pytest.approx(6.0)  # what the last-round rule charged


def test_the_reverse_shape_is_no_longer_undercharged() -> None:
    """Undercharge direction: 2.987 cents, not 0.525 (all at deepseek)."""
    rounds = [
        _round("anthropic", "claude-sonnet-4-6", 8000, 200),  # 2.4 + 0.3 = 2.7
        _round("deepseek", "deepseek-chat", 9000, 400),  # 0.243 + 0.044 = 0.287
    ]
    cents, _ = price_rounds(rounds, source=_SRC)
    assert cents == pytest.approx(2.987)


def test_one_rounds_openrouter_actual_is_kept_beside_another_rounds_estimate() -> None:
    """The M2 aggregate dropped every actual on a mixed turn; per round, none is lost."""
    rounds = [
        _round("openrouter", "z-ai/glm-4.6", 8000, 200, cost_usd=0.05),  # actual: 5.0 cents
        _round("anthropic", "claude-sonnet-4-6", 9000, 400),  # 3.3
    ]
    cents, basis = price_rounds(rounds, source=_SRC)
    assert cents == pytest.approx(8.3)
    # D3: a partial actual never labels the whole turn.
    assert basis == "estimate_static"


def test_a_non_openrouter_cost_is_not_taken_as_an_actual() -> None:
    """The actual arm stays gated on the ROUND's provider (Spec M2 review M1)."""
    rounds = [_round("anthropic", "claude-sonnet-4-6", 1000, 0, cost_usd=99.0)]
    assert price_rounds(rounds, source=_SRC) == (pytest.approx(0.3), "estimate_static")


@pytest.mark.parametrize(
    ("provider", "model", "cost_usd"),
    [
        ("anthropic", "claude-sonnet-4-6", None),
        ("openrouter", "z-ai/glm-4.6", None),
        ("openrouter", "z-ai/glm-4.6", 0.0123),
        ("vendor", "unknown-model", None),
    ],
)
def test_a_one_round_turn_prices_exactly_as_before(
    provider: str, model: str, cost_usd: float | None
) -> None:
    single = compute_turn_cost(
        provider=provider,
        model=model,
        prompt_tokens=1234,
        completion_tokens=567,
        actual_cost_usd=cost_usd,
        source=_SRC,
    )
    assert price_rounds([_round(provider, model, 1234, 567, cost_usd)], source=_SRC) == single


@pytest.mark.parametrize(
    ("models", "expected"),
    [
        (
            [("openrouter", "z-ai/glm-4.6", 0.01), ("openrouter", "z-ai/glm-4.6", 0.02)],
            "actual_openrouter",
        ),
        (
            [("openrouter", "z-ai/glm-4.6", 0.01), ("anthropic", "claude-sonnet-4-6", None)],
            "estimate_static",
        ),
        (
            [("anthropic", "claude-sonnet-4-6", None), ("openrouter", "z-ai/glm-4.6", None)],
            "estimate_catalog",
        ),
        ([("openrouter", "z-ai/glm-4.6", 0.01), ("vendor", "unknown", None)], "actual_openrouter"),
        ([("vendor", "unknown", None), ("vendor", "unknown-2", None)], "unpriced"),
    ],
)
def test_the_turn_basis_is_the_least_certain_priced_one(
    models: list[tuple[str, str, float | None]], expected: str
) -> None:
    rounds = [_round(p, m, 100, 10, c) for p, m, c in models]
    assert price_rounds(rounds, source=_SRC)[1] == expected


def test_an_unpriced_round_in_a_priced_turn_adds_nothing_and_says_which(
    logs: list[tuple[str, str]],
) -> None:
    rounds = [
        _round("anthropic", "claude-sonnet-4-6", 9000, 400),
        _round("vendor", "mystery-model", 5000, 500),
    ]
    cents, basis = price_rounds(rounds, source=_SRC)
    assert (cents, basis) == (pytest.approx(3.3), "estimate_static")
    warnings = [m for level, m in logs if level == "WARNING" and "unpriced round" in m]
    assert len(warnings) == 1
    assert "provider=vendor" in warnings[0]
    assert "model=mystery-model" in warnings[0]


def test_each_unpriced_round_is_named(logs: list[tuple[str, str]]) -> None:
    rounds = [
        _round("anthropic", "claude-sonnet-4-6", 9000, 400),
        _round("vendor", "mystery-a", 1, 1),
        _round("vendor", "mystery-a", 1, 1),
    ]
    price_rounds(rounds, source=_SRC)
    assert len([m for level, m in logs if level == "WARNING" and "unpriced round" in m]) == 2


def test_no_round_and_an_empty_model_price_at_zero_unpriced() -> None:
    assert price_rounds([], source=_SRC) == (0.0, "unpriced")
    assert price_rounds([_round("", "", 100, 10)], source=_SRC) == (0.0, "unpriced")


def test_the_sum_is_rounded_once_and_the_charge_ceils_once() -> None:
    """Three rounds of 0.1 cents are one credit, never three (ceil per round)."""
    rounds = [_round("vendor", "tiny", 1000, 0) for _ in range(3)]
    cents, _ = price_rounds(rounds, source=_SRC)
    assert cents == 0.3  # exactly: rounded once to micro-cents, no float noise
    charge = credits_charged(
        provider_cents=cents,
        infra_flat_cents=0.0,
        markup=BillingConfig().credit_markup,
        floor=1,
    )
    assert charge == 1


def _failed(
    provider: str, model: str, prompt: int, completion: int = 0, cost_usd: float | None = None
) -> BackendAttempt:
    return BackendAttempt(
        index=0,
        provider=provider,
        model=model,
        error_class="EmptyCompletionError",
        usage=TokenUsage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
            cost_usd=cost_usd,
        ),
    )


def test_a_failed_try_that_reported_tokens_is_charged_at_its_own_model(
    logs: list[tuple[str, str]],
) -> None:
    """D-ORCH-11-18-s: the answer (deepseek, 0.238) plus the wasted try (Sonnet, 2.7)."""
    wasted = _failed("anthropic", "claude-sonnet-4-6", 9000)
    rounds = [_round("deepseek", "deepseek-chat", 8000, 200, failed_tries=(wasted,))]
    cents, basis = price_rounds(rounds, source=_SRC, surface="chat")
    assert cents == pytest.approx(2.938)
    assert basis == "estimate_static"
    lines = [m for level, m in logs if level == "INFO" and "failed model try charged" in m]
    assert len(lines) == 1
    assert "cost_cents=2.7" in lines[0]
    assert "model=claude-sonnet-4-6" in lines[0]
    assert "surface=chat" in lines[0]
    assert "prompt_tokens=9000" in lines[0]


def test_a_failed_try_that_reported_no_tokens_costs_nothing_and_labels_nothing(
    logs: list[tuple[str, str]],
) -> None:
    no_usage = BackendAttempt(index=0, provider="x", model="y", error_class="RateLimitError")
    zero = _failed("openrouter", "z-ai/glm-4.6", 0)  # a catalog model, zero tokens
    rounds = [_round("deepseek", "deepseek-chat", 8000, 200, failed_tries=(no_usage, zero))]
    assert price_rounds(rounds, source=_SRC) == (pytest.approx(0.238), "estimate_static")
    assert not [m for _level, m in logs if "failed model try" in m]


def test_failed_tries_are_summed_inside_the_one_rounding_and_the_one_ceil() -> None:
    """A 0.1 cent answer and three 0.1 cent failed tries: 0.4 cents, one credit, not four."""
    tries = tuple(_failed("vendor", "tiny", 1000) for _ in range(3))
    cents, _ = price_rounds([_round("vendor", "tiny", 1000, 0, failed_tries=tries)], source=_SRC)
    assert cents == 0.4
    charge = credits_charged(
        provider_cents=cents, infra_flat_cents=0.0, markup=BillingConfig().credit_markup, floor=1
    )
    assert charge == 1


def test_a_failed_trys_basis_labels_the_turn_too() -> None:
    """D3 over every priced part: an actual answer after a statically priced failed try."""
    wasted = _failed("anthropic", "claude-sonnet-4-6", 1000)
    rounds = [_round("openrouter", "z-ai/glm-4.6", 100, 10, cost_usd=0.01, failed_tries=(wasted,))]
    cents, basis = price_rounds(rounds, source=_SRC)
    assert cents == pytest.approx(1.0 + 0.3)
    assert basis == "estimate_static"


def test_a_failed_trys_own_actual_is_used() -> None:
    wasted = _failed("openrouter", "z-ai/glm-4.6", 5000, cost_usd=0.004)
    rounds = [_round("openrouter", "z-ai/glm-4.6", 100, 10, cost_usd=0.01, failed_tries=(wasted,))]
    assert price_rounds(rounds, source=_SRC) == (pytest.approx(1.4), "actual_openrouter")


def test_an_unpriced_failed_try_adds_nothing_and_says_which(logs: list[tuple[str, str]]) -> None:
    wasted = _failed("vendor", "mystery-model", 9000)
    rounds = [_round("deepseek", "deepseek-chat", 8000, 200, failed_tries=(wasted,))]
    assert price_rounds(rounds, source=_SRC) == (pytest.approx(0.238), "estimate_static")
    warnings = [m for level, m in logs if level == "WARNING" and "unpriced failed model try" in m]
    assert len(warnings) == 1
    assert "provider=vendor" in warnings[0]
    assert "model=mystery-model" in warnings[0]


def test_a_failed_try_is_charged_even_when_no_model_served_the_round() -> None:
    """A voice round with no served model still owes the tokens its failed try burned."""
    wasted = _failed("anthropic", "claude-sonnet-4-6", 1000)
    assert price_rounds([_round("", "", 0, 0, failed_tries=(wasted,))], source=_SRC) == (
        pytest.approx(0.3),
        "estimate_static",
    )

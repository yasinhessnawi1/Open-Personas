"""An authoring draft is priced at the model that served it (R9-296).

When ``PERSONA_AUTHORING_MODEL`` is unset or does not resolve, the authoring backend
is the frontier tier's :class:`MultiModelChatBackend`, whose own ``provider_name`` /
``model_name`` are its PRIMARY. ``_price_usage`` priced from those names, so a draft a
fallback served was charged at the primary's rate (and the reverse overcharged).

Driven through a REAL chain whose primary genuinely fails inside ``chat_stream``, over
the streamed draft (the path the route bills), on its first attempt and on the repair
attempt that a validation failure triggers. Rates are the static table's:
``claude-sonnet-5`` 0.30 / 1.50 and ``claude-haiku-4-5`` 0.10 / 0.50 cents per 1k.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from persona.backends.errors import RateLimitError
from persona.backends.multi_model import MultiModelChatBackend
from persona.backends.types import StreamChunk, TokenUsage
from persona_api.services.authoring_service import AuthoringCost, stream_authoring_draft

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

GOOD = """\
schema_version: "1.0"
identity:
  name: Lex
  role: Legal information assistant
  background: A general legal-information assistant for everyday questions.
  constraints:
    - Do not fabricate information; say when you don't know.
---QUESTIONS---
[{"section": "identity", "question": "Which legal area?"}]"""
BAD = GOOD.replace("---QUESTIONS---", "hobbies:\n  - chess\n---QUESTIONS---")


class _Member:
    """A chain member: raises a quota 429 on the calls listed in ``fail_on``, else streams."""

    supports_native_tools = False
    supports_vision = False

    def __init__(
        self,
        provider: str,
        model: str,
        contents: list[str],
        *,
        fail_on: tuple[int, ...] = (),
        cost_usd: float | None = None,
    ) -> None:
        self.provider_name = provider
        self.model_name = model
        self._contents = list(contents)
        self._fail_on = fail_on
        self._cost_usd = cost_usd
        self.calls = 0

    async def chat_stream(self, messages: object, **_kwargs: object) -> AsyncIterator[StreamChunk]:  # noqa: ARG002
        call = self.calls
        self.calls += 1
        if call in self._fail_on:
            raise RateLimitError(
                "429", context={"status_code": "429", "reason": "insufficient_quota"}
            )
        yield StreamChunk(delta=self._contents.pop(0))
        yield StreamChunk(
            delta="",
            is_final=True,
            usage=TokenUsage(
                prompt_tokens=2000,
                completion_tokens=800,
                total_tokens=2800,
                cost_usd=self._cost_usd,
            ),
        )


async def _cost(chain: MultiModelChatBackend) -> AuthoringCost:
    events = [e async for e in stream_authoring_draft(chain, "lawyer", [], [])]
    costs = [payload for kind, payload in events if kind == "cost"]
    assert len(costs) == 1
    cost = costs[0]
    assert isinstance(cost, AuthoringCost)
    return cost


@pytest.mark.asyncio
async def test_a_draft_a_fallback_served_is_priced_at_the_fallback() -> None:
    """Haiku is primary and fails; Sonnet answers: 1.8 cents, not haiku's 0.6."""
    primary = _Member("anthropic", "claude-haiku-4-5", [], fail_on=(0,))
    fallback = _Member("anthropic", "claude-sonnet-5", [GOOD])
    cost = await _cost(MultiModelChatBackend([primary, fallback]))  # type: ignore[list-item]
    assert cost.cost_cents == pytest.approx(1.8)  # 2 x 0.30 + 0.8 x 1.50
    assert cost.cost_basis == "estimate_static"


@pytest.mark.asyncio
async def test_each_attempt_is_priced_at_the_model_that_served_it() -> None:
    """First attempt on the fallback (Sonnet, 1.8), the repair on the primary (Haiku, 0.6)."""
    primary = _Member("anthropic", "claude-haiku-4-5", [GOOD], fail_on=(0,))
    fallback = _Member("anthropic", "claude-sonnet-5", [BAD])
    cost = await _cost(MultiModelChatBackend([primary, fallback]))  # type: ignore[list-item]
    assert (primary.calls, fallback.calls) == (2, 1)
    assert cost.cost_cents == pytest.approx(2.4)
    assert (cost.prompt_tokens, cost.completion_tokens) == (4000, 1600)


@pytest.mark.asyncio
async def test_a_turn_with_an_actual_and_an_estimate_is_labelled_the_estimate() -> None:
    """D3: the first attempt's OpenRouter actual, the repair's static estimate."""
    # The routed model answers the first attempt and fails the repair; Sonnet answers it.
    routed = _Member("openrouter", "z-ai/glm-4.6", [BAD], fail_on=(1,), cost_usd=0.01)
    static = _Member("anthropic", "claude-sonnet-5", [GOOD])
    cost = await _cost(MultiModelChatBackend([routed, static]))  # type: ignore[list-item]
    assert cost.cost_cents == pytest.approx(1.0 + 1.8)
    assert cost.cost_basis == "estimate_static"


@pytest.mark.asyncio
async def test_an_estimate_then_an_actual_is_still_labelled_the_estimate() -> None:
    """D3, the other order: Sonnet's estimate first, the routed model's actual on the repair."""
    routed = _Member("openrouter", "z-ai/glm-4.6", [GOOD], fail_on=(0,), cost_usd=0.01)
    static = _Member("anthropic", "claude-sonnet-5", [BAD])
    cost = await _cost(MultiModelChatBackend([routed, static]))  # type: ignore[list-item]
    assert (routed.calls, static.calls) == (2, 1)
    assert cost.cost_cents == pytest.approx(1.8 + 1.0)
    assert cost.cost_basis == "estimate_static"


@pytest.mark.asyncio
async def test_a_failed_try_that_cost_tokens_is_in_the_drafts_charge() -> None:
    """D-ORCH-11-18-s: Haiku's empty completion (0.6 cents) plus Sonnet's draft (1.8)."""
    primary = _Member("anthropic", "claude-haiku-4-5", [""])  # billed, but no reply
    fallback = _Member("anthropic", "claude-sonnet-5", [GOOD])
    chain = MultiModelChatBackend([primary, fallback], max_retries_per_backend=0)  # type: ignore[list-item]
    cost = await _cost(chain)
    assert cost.cost_cents == pytest.approx(2.4)
    assert (cost.prompt_tokens, cost.completion_tokens) == (4000, 1600)
    assert cost.cost_basis == "estimate_static"


@pytest.mark.asyncio
async def test_the_drafts_two_attempts_are_rounded_once_with_no_float_noise() -> None:
    """0.1 + 0.2 cents is 0.30000000000000004 in floats; the recorded cost must be 0.3."""
    first = _Member("openrouter", "z-ai/glm-4.6", [BAD], fail_on=(1,), cost_usd=0.001)
    repair = _Member("openrouter", "qwen/qwen3-30b-a3b", [GOOD], cost_usd=0.002)
    cost = await _cost(MultiModelChatBackend([first, repair]))  # type: ignore[list-item]
    assert cost.cost_cents == 0.3

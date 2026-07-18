"""OpenRouter image-gen usage/cost capture (Spec M3, T3a).

The token-metered ``openai/gpt-5.4-image-2`` rides chat-completions, so the
response carries a ``usage`` object (tokens + a ``cost`` extra with usage
accounting opted in). ``_extract_usage`` surfaces it fail-open onto
``GenerationResult`` for the billing path.
"""

from __future__ import annotations

from types import SimpleNamespace

from persona.imagegen.openrouter_image import _extract_usage


def test_extract_usage_reads_tokens_and_cost() -> None:
    response = SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=1000, cost=0.03)
    )
    assert _extract_usage(response) == (100, 1000, 0.03)


def test_extract_usage_absent_usage_is_zero_none() -> None:
    assert _extract_usage(SimpleNamespace(usage=None)) == (0, 0, None)
    assert _extract_usage(SimpleNamespace()) == (0, 0, None)


def test_extract_usage_missing_cost_keeps_tokens() -> None:
    # Usage accounting off / no cost → tokens still surface, cost is None (the
    # billing path then prices via the resolver-chain estimate).
    response = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=5, completion_tokens=50))
    assert _extract_usage(response) == (5, 50, None)


def test_extract_usage_is_fail_open_on_garbage() -> None:
    # Bool/negative/malformed values never raise — they degrade to 0 / None.
    response = SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=True, completion_tokens=-3, cost="nope")
    )
    assert _extract_usage(response) == (0, 0, None)

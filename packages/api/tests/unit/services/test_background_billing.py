"""The shared background-LLM owner-billing helper (Spec M3, T5).

``bill_background_llm`` prices a background op's real cost, charges the owner
idempotently (keyed on the surface's natural key), and is FAIL-SOFT + a NO-OP
when there's nothing to bill.
"""

from __future__ import annotations

from typing import Any

import pytest
from persona_api.services.background_billing import bill_background_llm

_ENGINE: Any = object()


class _FakePolicy:
    def __init__(self, *, raises: bool = False) -> None:
        self.calls: list[dict[str, object]] = []
        self._raises = raises

    def capture_up_to_idempotent(self, **kw: object) -> tuple[int, int]:
        self.calls.append(kw)
        if self._raises:
            msg = "boom"
            raise RuntimeError(msg)
        return int(kw["amount"]), 0  # type: ignore[call-overload]


def _bill(policy: _FakePolicy | None, **over: object) -> None:
    kw: dict[str, object] = {
        "credits_policy": policy,
        "rls_engine": _ENGINE,
        "owner_id": "u1",
        "provider": "openrouter",
        "model": "x",
        "prompt_tokens": 100,
        "completion_tokens": 1000,
        "cost_usd": 0.03,
        "surface": "graph_consolidation",
        "billing_key": "graph_consolidation:bucket-1",
    }
    kw.update(over)
    bill_background_llm(**kw)  # type: ignore[arg-type]


def test_owner_billed_at_real_cost_with_the_surface_key() -> None:
    policy = _FakePolicy()
    _bill(policy)
    kw = policy.calls[-1]
    assert kw["amount"] == 3  # ceil(3.0¢) at markup 1.0
    assert kw["reason"] == "graph_consolidation:actual_openrouter"
    assert kw["cost_cents"] == pytest.approx(3.0)
    assert kw["cost_basis"] == "actual_openrouter"
    assert kw["billing_key"] == "graph_consolidation:bucket-1"
    assert kw["user_id"] == "u1"


def test_noop_without_a_policy_or_engine() -> None:
    _bill(None)  # no policy → nothing raised, nothing to assert (helper returns)
    policy = _FakePolicy()
    _bill(policy, rls_engine=None)
    assert policy.calls == []


def test_no_real_call_charges_nothing() -> None:
    policy = _FakePolicy()
    _bill(policy, prompt_tokens=0, completion_tokens=0, cost_usd=None)
    assert policy.calls == []


def test_unpriced_falls_to_the_floor() -> None:
    policy = _FakePolicy()
    _bill(
        policy, provider="fake", model="fake-1", cost_usd=None, prompt_tokens=1, completion_tokens=1
    )
    kw = policy.calls[-1]
    assert kw["amount"] == 1  # floor
    assert kw["cost_basis"] == "unpriced"


def test_fail_soft_swallows_billing_errors() -> None:
    policy = _FakePolicy(raises=True)
    # Must NOT raise — a billing hiccup can never break the background op.
    _bill(policy)
    assert len(policy.calls) == 1  # it was attempted, the error was swallowed

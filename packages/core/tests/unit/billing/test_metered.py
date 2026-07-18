"""The MeteredBilling seam (Spec M3, T1a — D-M3-1/2).

Unit-level: a fake :class:`LedgerPort` records the call it received so we can
assert the seam prices correctly (formula), routes to the right ledger method
(mode × billing_key), and passes the true pre-markup ``cost_cents`` + ``cost_basis``
through — WITHOUT a database (the real ledger is exercised in the api
integration idempotency test).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from persona.billing.formula import BillingConfig
from persona.billing.metered import MeteredBilling

_ENGINE: object = object()  # a sentinel; the fake ledger never touches it


@dataclass
class _FakeLedger:
    """Records the single ledger call MeteredBilling makes."""

    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    balance: int = 1000

    def deduct(self, **kw: object) -> int:
        self.calls.append(("deduct", kw))
        return self.balance - int(kw["amount"])  # type: ignore[call-overload]

    def capture_up_to(self, **kw: object) -> tuple[int, int]:
        self.calls.append(("capture_up_to", kw))
        amount = int(kw["amount"])  # type: ignore[call-overload]
        captured = min(amount, self.balance)
        return captured, self.balance - captured

    def deduct_idempotent(self, **kw: object) -> int:
        self.calls.append(("deduct_idempotent", kw))
        return self.balance - int(kw["amount"])  # type: ignore[call-overload]

    def capture_up_to_idempotent(self, **kw: object) -> tuple[int, int]:
        self.calls.append(("capture_up_to_idempotent", kw))
        amount = int(kw["amount"])  # type: ignore[call-overload]
        captured = min(amount, self.balance)
        return captured, self.balance - captured


def _billing(ledger: _FakeLedger) -> MeteredBilling:
    return MeteredBilling(ledger=ledger, config=BillingConfig())


def test_strict_no_key_routes_to_deduct_and_prices_via_the_formula() -> None:
    ledger = _FakeLedger()
    result = _billing(ledger).charge(
        rls_engine=_ENGINE,  # type: ignore[arg-type]
        user_id="u1",
        provider_cents=5.3,
        infra_flat_cents=0.0,
        basis="estimate_static",
        reason="chat_turn",
        floor=1,
        mode="strict",
    )
    method, kw = ledger.calls[-1]
    assert method == "deduct"
    assert result.charged == 6  # ceil(5.3)
    assert kw["amount"] == 6
    # The true pre-markup provider cost + basis ride the ledger row (D-M3-12).
    assert kw["cost_cents"] == 5.3
    assert kw["cost_basis"] == "estimate_static"
    assert result.cost_cents == 5.3
    assert result.captured is None


def test_capture_mode_routes_to_capture_up_to_and_returns_captured() -> None:
    ledger = _FakeLedger(balance=3)
    result = _billing(ledger).charge(
        rls_engine=_ENGINE,  # type: ignore[arg-type]
        user_id="u1",
        provider_cents=5.3,
        infra_flat_cents=0.0,
        basis="actual_openrouter",
        reason="chat_turn",
        floor=1,
        mode="capture",
    )
    method, _kw = ledger.calls[-1]
    assert method == "capture_up_to"
    assert result.charged == 6
    assert result.captured == 3  # balance short → floored capture
    assert result.new_balance == 0


def test_billing_key_selects_the_idempotent_variant() -> None:
    ledger = _FakeLedger()
    _billing(ledger).charge(
        rls_engine=_ENGINE,  # type: ignore[arg-type]
        user_id="u1",
        provider_cents=0.0,
        infra_flat_cents=1.0,
        basis="infra_flat",
        reason="voice",
        floor=0,
        mode="capture",
        billing_key="voice:sess-1:turn-1",
    )
    method, kw = ledger.calls[-1]
    assert method == "capture_up_to_idempotent"
    assert kw["billing_key"] == "voice:sess-1:turn-1"


def test_infra_flat_surface_records_zero_provider_cost_with_infra_basis() -> None:
    ledger = _FakeLedger()
    result = _billing(ledger).charge(
        rls_engine=_ENGINE,  # type: ignore[arg-type]
        user_id="u1",
        provider_cents=0.0,
        infra_flat_cents=1.0,
        basis="infra_flat",
        reason="sandbox",
        floor=0,
        mode="strict",
    )
    _method, kw = ledger.calls[-1]
    assert result.charged == 1  # ceil(1.0 infra) = 1
    assert kw["cost_cents"] == 0.0  # provider truth is 0; basis marks it infra
    assert kw["cost_basis"] == "infra_flat"


def test_price_does_not_record() -> None:
    ledger = _FakeLedger()
    charge = _billing(ledger).price(provider_cents=5.3, infra_flat_cents=1.0, floor=1)
    assert charge == 7
    assert ledger.calls == []  # pre-flight price makes no ledger move

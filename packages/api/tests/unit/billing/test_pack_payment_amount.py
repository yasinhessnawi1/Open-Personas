"""A Checkout pack grants only what was actually received for it (review follow-up to M3).

``payment_intent.succeeded`` granted the stamped ``payg_credits`` alone, the same "grant on
the stamp" shape that let a $0 top-up invoice grant $10. The PaymentIntent now has to show
the money arrived: USD, and ``amount_received`` at least the pack's price.

At least, not equal: Checkout runs with Stripe Tax on, and a PaymentIntent carries the
session's TOTAL, so with tax-exclusive pricing it is the price plus tax. It has no
pre-tax subtotal to compare, and Checkout offers no promotion codes, so nothing can
legitimately bring a pack's total below its price.
"""

# ruff: noqa: ANN401
from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
import stripe
from loguru import logger as _loguru_logger
from persona_api.billing import WebhookContext, handlers
from persona_api.config import APIConfig, Edition

if TYPE_CHECKING:
    from collections.abc import Iterator


class _Grants:
    def __init__(self) -> None:
        self.lots: list[dict[str, Any]] = []

    def grant_payg_lot_idempotent(self, **kwargs: Any) -> int:
        self.lots.append(kwargs)
        return len(self.lots)


def _deliver(pi: dict[str, Any]) -> _Grants:
    grants = _Grants()
    context = WebhookContext(
        rls_engine=MagicMock(),
        admin_engine=MagicMock(),
        credits_policy=grants,  # type: ignore[arg-type]  # the recording double
        config=APIConfig(
            database_url="postgresql+psycopg://super@localhost/persona_shell",
            app_database_url="postgresql+psycopg://persona_app@localhost/persona_shell",
            edition=Edition.cloud,
            billing_stripe_enabled=True,
            stripe_secret_key="sk_test_x",
            stripe_webhook_secret="whsec_x",
        ),
    )
    event = stripe.Event.construct_from(
        {
            "id": "evt_1",
            "object": "event",
            "type": "payment_intent.succeeded",
            "data": {"object": pi},
        },
        "sk_test_x",
    )
    handlers.handle_payment_intent_succeeded(event, context)
    return grants


def _pack_pi(
    *, stamped_credits: int, amount_received: int, currency: str = "usd"
) -> dict[str, Any]:
    return {
        "id": "pi_pack_1",
        "object": "payment_intent",
        "customer": "cus_1",
        "currency": currency,
        "amount_received": amount_received,
        "metadata": {"user_id": "u1", "payg_credits": str(stamped_credits)},
    }


@pytest.fixture(autouse=True)
def _resolved_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        handlers.subscription_service,
        "resolve_user_by_customer",
        lambda _engine, *, stripe_customer_id: "u1" if stripe_customer_id == "cus_1" else None,
    )


@pytest.fixture
def warnings() -> Iterator[list[tuple[str, dict[str, Any]]]]:
    """Every WARNING the webhook handlers log, as ``(message, extra)``."""
    captured: list[tuple[str, dict[str, Any]]] = []
    sink_id = _loguru_logger.add(
        lambda m: captured.append((m.record["message"], dict(m.record["extra"]))),
        level="WARNING",
        filter=lambda record: record["extra"].get("component") == "api.billing.handlers",
    )
    yield captured
    _loguru_logger.remove(sink_id)


@pytest.mark.parametrize(
    ("stamped_credits", "amount_received"),
    [
        pytest.param(500, 500, id="five_dollar_pack_tax_inclusive_or_untaxed"),
        pytest.param(2500, 2500, id="twenty_five_dollar_pack"),
        pytest.param(1000, 1250, id="ten_dollar_pack_with_exclusive_tax_on_top"),
    ],
)
def test_a_paid_pack_grants_exactly_that_pack(stamped_credits: int, amount_received: int) -> None:
    grants = _deliver(_pack_pi(stamped_credits=stamped_credits, amount_received=amount_received))

    (lot,) = grants.lots
    assert lot["credit_amount"] == stamped_credits
    assert lot["source_billing_key"] == "pi_pack_1"
    assert lot["reason"] == "payg_topup"


@pytest.mark.parametrize(
    ("stamped_credits", "amount_received", "currency"),
    [
        pytest.param(1000, 0, "usd", id="nothing_received"),
        pytest.param(1000, 999, "usd", id="one_cent_short"),
        pytest.param(5000, 1000, "usd", id="a_bigger_stamp_than_the_payment"),
        pytest.param(1000, 1000, "eur", id="another_currency"),
        pytest.param(700, 700, "usd", id="no_pack_grants_that_amount"),
    ],
)
def test_a_pack_payment_that_does_not_cover_the_pack_grants_nothing(
    warnings: list[tuple[str, dict[str, Any]]],
    stamped_credits: int,
    amount_received: int,
    currency: str,
) -> None:
    grants = _deliver(
        _pack_pi(
            stamped_credits=stamped_credits, amount_received=amount_received, currency=currency
        )
    )

    assert grants.lots == []
    ((message, extra),) = warnings
    assert "not paid for its pack" in message
    assert extra["payment_intent_id"] == "pi_pack_1"
    # The id only: no amount, currency, customer or user in the record.
    assert {k for k in extra if not k.startswith("_")} == {"component", "payment_intent_id"}


def test_a_pack_payment_without_amounts_grants_nothing() -> None:
    pi = _pack_pi(stamped_credits=1000, amount_received=1000)
    del pi["amount_received"]

    assert _deliver(pi).lots == []


def test_a_pack_paid_in_usd_spelled_in_upper_case_still_grants() -> None:
    grants = _deliver(_pack_pi(stamped_credits=1000, amount_received=1000, currency="USD"))

    (lot,) = grants.lots
    assert lot["credit_amount"] == 1000

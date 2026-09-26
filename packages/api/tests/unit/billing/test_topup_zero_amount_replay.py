"""A cleaned-up auto top-up draft can never grant credits it was not paid for (review M3).

The path this closes, end to end:

1. ``finalize_invoice`` fails without Stripe saving a result for its idempotency key (a
   connection error), so the gateway cleans up: the pack's line comes off, then the draft
   delete fails. A draft remains with no line but still stamped ``payg_credits``.
2. Within the same UTC hour a new crossing replays the hour-keyed chain: ``invoices.create``
   and ``invoice_items.create`` return their saved responses (the same draft, the line
   already deleted), so no new line is attached, and ``finalize`` really runs this time.
3. The draft finalizes at $0, Stripe marks a $0 invoice paid, and ``invoice.paid`` fires.
   The grant used to read ``metadata.payg_credits`` alone, so it granted $10 for nothing.

Two independent defences, each pinned on its own and together:

- the gateway clears the credit stamp on a draft it could not delete;
- the webhook grants only when the invoice's subtotal is the pack's price and the invoice
  is fully settled (``amount_remaining == 0``, status ``paid``). Settled, not "paid at
  least the price": Stripe applies a customer credit balance before charging the card,
  and ``amount_paid`` excludes it, so a top-up paid partly or wholly from that balance
  still has to deliver its pack.

And the replay itself is told apart from a real failure: a line or draft that is already
gone is logged as already cleaned up, not as a draft kept.

A stateful Stripe fake models just enough of Stripe for this: idempotent replays of
create calls, a result saved for a request that executed and not for one that failed to
connect, drafts that only accept deletes and metadata edits, and a $0 invoice being
marked paid at finalize. Nothing here reaches Stripe.
"""

# ruff: noqa: ANN401
from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
import stripe
from loguru import logger as _loguru_logger
from persona_api.billing import StripeGateway, WebhookContext, handlers
from persona_api.config import APIConfig, Edition

if TYPE_CHECKING:
    from collections.abc import Iterator

_PRICE = "price_pack_10_test"
_KEY = "autotopup:u1:2026-07-24-15"
_PACK_CENTS = 1000  # the $10 pack's Price, as Stripe bills it


def _missing(what: str) -> stripe.InvalidRequestError:
    return stripe.InvalidRequestError(
        f"No such {what}", param="id", code="resource_missing", http_status=404
    )


class _Invoice:
    """The fields of a Stripe invoice this flow reads or changes."""

    def __init__(self, invoice_id: str, metadata: dict[str, str]) -> None:
        self.id = invoice_id
        self.status = "draft"
        self.metadata = dict(metadata)
        self.items: list[str] = []
        self.amount_paid = 0
        self.payments = MagicMock()
        self.payments.data = []

    @property
    def subtotal(self) -> int:
        return _PACK_CENTS * len(self.items)

    def as_event_object(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "object": "invoice",
            "customer": "cus_1",
            "currency": "usd",
            "status": self.status,
            "metadata": dict(self.metadata),
            "subtotal": self.subtotal,
            "amount_paid": self.amount_paid,
            "amount_remaining": 0 if self.status == "paid" else self.subtotal - self.amount_paid,
            "lines": {"data": [{"pricing": {"price": _PRICE}} for _ in self.items]},
        }


class _StatefulStripe:
    """Just enough of Stripe to replay an hour-keyed top-up against a half-cleaned draft."""

    def __init__(
        self,
        *,
        finalize_failures: int = 0,
        invoice_delete_failures: int = 0,
        metadata_update_fails: bool = False,
    ) -> None:
        self.invoices_by_id: dict[str, _Invoice] = {}
        self.deleted_invoice_ids: set[str] = set()
        self.live_items: dict[str, str] = {}  # item id -> invoice id
        self.saved: dict[str, Any] = {}  # idempotency key -> saved response
        self.calls: list[str] = []
        self._finalize_failures = finalize_failures
        self._invoice_delete_failures = invoice_delete_failures
        self._metadata_update_fails = metadata_update_fails
        self._seq = 0

        self.invoices = MagicMock()
        self.invoices.create.side_effect = self._create
        self.invoices.finalize_invoice.side_effect = self._finalize
        self.invoices.delete.side_effect = self._delete
        self.invoices.update.side_effect = self._update
        self.invoices.pay.side_effect = self._pay
        self.invoices.void_invoice.side_effect = lambda *_a, **_k: None
        self.invoice_items = MagicMock()
        self.invoice_items.create.side_effect = self._item_create
        self.invoice_items.delete.side_effect = self._item_delete
        self.payment_intents = MagicMock()

    def _next(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}_{self._seq}"

    def _invoice(self, invoice_id: str) -> _Invoice:
        if invoice_id in self.deleted_invoice_ids or invoice_id not in self.invoices_by_id:
            raise _missing("invoice")
        return self.invoices_by_id[invoice_id]

    def _create(self, *, params: dict[str, Any], options: dict[str, Any]) -> Any:
        self.calls.append("invoices.create")
        key = options["idempotency_key"]
        if key not in self.saved:
            invoice = _Invoice(self._next("in"), params["metadata"])
            self.invoices_by_id[invoice.id] = invoice
            self.saved[key] = MagicMock(id=invoice.id)
        return self.saved[key]

    def _item_create(self, *, params: dict[str, Any], options: dict[str, Any]) -> Any:
        self.calls.append("invoice_items.create")
        key = options["idempotency_key"]
        if key not in self.saved:
            invoice = self._invoice(params["invoice"])
            item_id = self._next("ii")
            invoice.items.append(item_id)
            self.live_items[item_id] = invoice.id
            self.saved[key] = MagicMock(id=item_id)
        return self.saved[key]

    def _finalize(self, invoice_id: str, **kwargs: Any) -> Any:
        self.calls.append("invoices.finalize_invoice")
        key = kwargs["options"]["idempotency_key"]
        if key in self.saved:
            return self.saved[key]
        if self._finalize_failures > 0:
            # The request never reached Stripe, so nothing is saved for this key.
            self._finalize_failures -= 1
            raise stripe.APIConnectionError("connection reset")
        invoice = self._invoice(invoice_id)
        invoice.status = "open"
        if invoice.subtotal == 0:
            invoice.status = "paid"  # Stripe marks a $0 invoice paid at finalize
        self.saved[key] = invoice
        return invoice

    def _item_delete(self, item_id: str, **_kwargs: Any) -> Any:
        self.calls.append("invoice_items.delete")
        invoice_id = self.live_items.pop(item_id, None)
        if invoice_id is None:
            raise _missing("invoiceitem")
        self.invoices_by_id[invoice_id].items.remove(item_id)
        return MagicMock(id=item_id, deleted=True)

    def _delete(self, invoice_id: str, **_kwargs: Any) -> Any:
        self.calls.append("invoices.delete")
        invoice = self._invoice(invoice_id)
        if self._invoice_delete_failures > 0:
            self._invoice_delete_failures -= 1
            raise stripe.APIConnectionError("connection reset")
        if invoice.status != "draft":
            raise stripe.InvalidRequestError("only drafts can be deleted", param=None)
        self.deleted_invoice_ids.add(invoice_id)
        return MagicMock(id=invoice_id, deleted=True)

    def _update(self, invoice_id: str, **kwargs: Any) -> Any:
        self.calls.append("invoices.update")
        invoice = self._invoice(invoice_id)
        if self._metadata_update_fails:
            raise stripe.APIConnectionError("connection reset")
        for name, value in kwargs["params"].get("metadata", {}).items():
            if value == "":
                invoice.metadata.pop(name, None)  # Stripe unsets a key set to ""
            else:
                invoice.metadata[name] = value
        return invoice

    def _pay(self, invoice_id: str, **_kwargs: Any) -> Any:
        self.calls.append("invoices.pay")
        invoice = self._invoice(invoice_id)
        if invoice.status == "paid":
            raise stripe.InvalidRequestError("Invoice is already paid", param=None)
        invoice.amount_paid = invoice.subtotal
        invoice.status = "paid"
        return invoice


def _gateway(fake: _StatefulStripe) -> StripeGateway:
    gateway = StripeGateway(
        secret_key="sk_test_x",
        publishable_key="pk_test_x",
        webhook_secret="wh",
        autotopup_price_id=_PRICE,
    )
    gateway._client = fake  # type: ignore[assignment]  # noqa: SLF001 (no network fires)
    return gateway


def _topup(gateway: StripeGateway) -> tuple[str, str]:
    return gateway.create_off_session_topup(
        customer_id="cus_1", credit_amount=1000, user_id="u1", idempotency_key=_KEY
    )


class _Grants:
    """A credits policy double recording every lot granted."""

    def __init__(self) -> None:
        self.lots: list[dict[str, Any]] = []

    def grant_payg_lot_idempotent(self, **kwargs: Any) -> int:
        self.lots.append(kwargs)
        return len(self.lots)

    def reset_allowance_idempotent(self, **_kwargs: Any) -> int:  # pragma: no cover
        msg = "a top-up invoice must never reach the allowance reset"
        raise AssertionError(msg)


def _deliver_invoice_paid(invoice: dict[str, Any]) -> _Grants:
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
            stripe_price_pack_10=_PRICE,
        ),
    )
    event = stripe.Event.construct_from(
        {"id": "evt_1", "object": "event", "type": "invoice.paid", "data": {"object": invoice}},
        "sk_test_x",
    )
    handlers.handle_invoice_paid(event, context)
    return grants


@pytest.fixture(autouse=True)
def _resolved_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        handlers.subscription_service,
        "resolve_user_by_customer",
        lambda _engine, *, stripe_customer_id: "u1" if stripe_customer_id == "cus_1" else None,
    )


@pytest.fixture
def records() -> Iterator[list[tuple[str, str, dict[str, Any]]]]:
    """Every gateway and webhook record at INFO or above, as ``(level, message, extra)``."""
    captured: list[tuple[str, str, dict[str, Any]]] = []
    sink_id = _loguru_logger.add(
        lambda m: captured.append(
            (m.record["level"].name, m.record["message"], dict(m.record["extra"]))
        ),
        level="INFO",
        filter=lambda record: (
            record["extra"].get("component") in {"api.billing.gateway", "api.billing.handlers"}
        ),
    )
    yield captured
    _loguru_logger.remove(sink_id)


def _stamped_topup_invoice(
    *,
    subtotal: int,
    amount_paid: int,
    amount_remaining: int = 0,
    status: str = "paid",
    starting_balance: int = 0,
    currency: str = "usd",
) -> dict[str, Any]:
    return {
        "id": "in_topup",
        "object": "invoice",
        "customer": "cus_1",
        "currency": currency,
        "status": status,
        "metadata": {"user_id": "u1", "payg_credits": "1000", "source": "auto_topup"},
        "subtotal": subtotal,
        "starting_balance": starting_balance,
        "amount_paid": amount_paid,
        "amount_remaining": amount_remaining,
        "lines": {"data": [{"pricing": {"price": _PRICE}}] if subtotal else []},
    }


# --- the exact replay ------------------------------------------------------------


def test_the_same_hour_replay_of_a_half_cleaned_draft_grants_nothing() -> None:
    fake = _StatefulStripe(finalize_failures=1, invoice_delete_failures=1)
    gateway = _gateway(fake)

    # 1. Finalize never reaches Stripe; the line comes off; the draft delete fails.
    with pytest.raises(stripe.APIConnectionError):
        _topup(gateway)
    (invoice,) = fake.invoices_by_id.values()
    assert invoice.status == "draft"
    assert invoice.items == []
    assert "payg_credits" not in invoice.metadata  # the stamp is cleared on the survivor

    # 2. Same hour: the hour-keyed calls replay onto that draft, and finalize runs for real.
    with pytest.raises(stripe.InvalidRequestError):
        _topup(gateway)  # pay refuses an invoice Stripe already marked paid
    assert invoice.status == "paid"
    assert (invoice.subtotal, invoice.amount_paid) == (0, 0)

    # 3. invoice.paid for that $0 invoice grants nothing.
    assert _deliver_invoice_paid(invoice.as_event_object()).lots == []


def test_the_replay_grants_nothing_even_when_the_stamp_could_not_be_cleared() -> None:
    # Defence 1 alone: the stamp survives, and the webhook's amount check still refuses.
    fake = _StatefulStripe(
        finalize_failures=1, invoice_delete_failures=1, metadata_update_fails=True
    )
    gateway = _gateway(fake)
    with pytest.raises(stripe.APIConnectionError):
        _topup(gateway)
    with pytest.raises(stripe.InvalidRequestError):
        _topup(gateway)
    (invoice,) = fake.invoices_by_id.values()
    assert invoice.metadata["payg_credits"] == "1000"

    assert _deliver_invoice_paid(invoice.as_event_object()).lots == []


# --- defence 1: the webhook grants only what was paid for ------------------------


@pytest.mark.parametrize(
    ("subtotal", "amount_paid", "amount_remaining", "status"),
    [
        pytest.param(0, 0, 0, "paid", id="zero_invoice"),
        pytest.param(_PACK_CENTS, 600, 400, "open", id="truly_unpaid_remainder"),
        pytest.param(_PACK_CENTS, 600, 400, "paid", id="a_remainder_whatever_the_status"),
        pytest.param(_PACK_CENTS, 0, 0, "open", id="settled_amounts_but_not_paid"),
        pytest.param(500, 500, 0, "paid", id="a_smaller_line_than_the_stamp"),
        pytest.param(2 * _PACK_CENTS, 2 * _PACK_CENTS, 0, "paid", id="not_the_packs_one_line"),
    ],
)
def test_a_topup_invoice_not_settled_for_its_pack_grants_nothing(
    records: list[tuple[str, str, dict[str, Any]]],
    subtotal: int,
    amount_paid: int,
    amount_remaining: int,
    status: str,
) -> None:
    grants = _deliver_invoice_paid(
        _stamped_topup_invoice(
            subtotal=subtotal,
            amount_paid=amount_paid,
            amount_remaining=amount_remaining,
            status=status,
        )
    )

    assert grants.lots == []
    warnings = [r for r in records if r[0] == "WARNING"]
    assert len(warnings) == 1
    assert warnings[0][2]["invoice_id"] == "in_topup"


def test_a_topup_invoice_in_another_currency_grants_nothing() -> None:
    invoice = _stamped_topup_invoice(subtotal=_PACK_CENTS, amount_paid=_PACK_CENTS)
    invoice["currency"] = "eur"

    assert _deliver_invoice_paid(invoice).lots == []


@pytest.mark.parametrize(
    ("amount_paid", "starting_balance", "currency"),
    [
        pytest.param(_PACK_CENTS, 0, "usd", id="tax_inclusive_or_untaxed"),
        pytest.param(_PACK_CENTS + 250, 0, "usd", id="exclusive_tax_on_top"),
        # Stripe applies the customer's credit balance before the card; amount_paid
        # excludes it (review HIGH: this used to take the balance and grant nothing).
        pytest.param(600, -400, "usd", id="paid_partly_from_the_customer_balance"),
        pytest.param(0, -_PACK_CENTS, "usd", id="paid_wholly_from_the_customer_balance"),
        # A coupon we put on a customer deliberately still delivers the pack.
        pytest.param(800, 0, "usd", id="a_discount_we_granted"),
        pytest.param(_PACK_CENTS, 0, "USD", id="currency_in_upper_case"),
    ],
)
def test_a_settled_topup_invoice_grants_exactly_its_lot(
    amount_paid: int, starting_balance: int, currency: str
) -> None:
    grants = _deliver_invoice_paid(
        _stamped_topup_invoice(
            subtotal=_PACK_CENTS,
            amount_paid=amount_paid,
            starting_balance=starting_balance,
            currency=currency,
        )
    )

    (lot,) = grants.lots
    assert lot["credit_amount"] == 1000
    assert lot["source_billing_key"] == "in_topup"


# --- defence 2: a draft that survives cleanup loses its stamp --------------------


def test_a_draft_that_cannot_be_deleted_has_its_credit_stamp_cleared(
    records: list[tuple[str, str, dict[str, Any]]],
) -> None:
    fake = _StatefulStripe(finalize_failures=1, invoice_delete_failures=1)

    with pytest.raises(stripe.APIConnectionError):
        _topup(_gateway(fake))

    assert fake.calls[-3:] == ["invoice_items.delete", "invoices.delete", "invoices.update"]
    ((invoice),) = fake.invoices_by_id.values()
    assert "payg_credits" not in invoice.metadata
    assert invoice.metadata["source"] == "auto_topup"  # still routed as a top-up
    gateway_records = [r for r in records if r[2].get("component") == "api.billing.gateway"]
    ((level, message, extra),) = gateway_records
    assert level == "WARNING"
    assert "could not be deleted" in message
    assert "cleared" in message
    # The invoice id and the error class only: no amount, customer, item or user id.
    assert {k for k in extra if not k.startswith("_")} == {"component", "invoice_id", "error"}


# --- the replay after a clean delete is reported as already cleaned --------------


def test_a_replay_after_a_clean_delete_logs_already_cleaned_not_draft_kept(
    records: list[tuple[str, str, dict[str, Any]]],
) -> None:
    fake = _StatefulStripe(finalize_failures=1)
    gateway = _gateway(fake)
    with pytest.raises(stripe.APIConnectionError):
        _topup(gateway)
    assert fake.deleted_invoice_ids == {"in_1"}
    records.clear()

    # Same hour: the replayed finalize meets a draft that no longer exists.
    with pytest.raises(stripe.InvalidRequestError):
        _topup(gateway)

    ((level, message, extra),) = [
        r for r in records if r[2].get("component") == "api.billing.gateway"
    ]
    assert level == "INFO"
    assert "already cleaned up" in message
    assert extra["invoice_id"] == "in_1"

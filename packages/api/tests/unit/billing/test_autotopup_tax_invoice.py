"""The auto top-up is a taxed Stripe Invoice, not a bare PaymentIntent (R9-177 B9).

Both Checkout paths collect Stripe Tax; the off-session auto top-up used to create a bare
``PaymentIntent`` and collect none, so the same pack bought two ways was taxed once and the
rights holder carried the VAT on every auto top-up. ``PaymentIntent`` cannot take
``automatic_tax``; an Invoice can, and paying it off-session with the saved card is Stripe's
documented route. These tests pin the wire shape with a fake ``StripeClient`` (no network):

- the invoice is created with ``automatic_tax`` on and bills the SAME Price the Checkout
  path uses (never an ad-hoc amount), so tax code and price have one truth;
- a retried trigger for the same hourly bucket derives every idempotency key from the same
  root, so Stripe replays the same invoice and never charges twice;
- ``user_id`` / ``payg_credits`` / ``source`` ride the invoice AND its PaymentIntent;
- a card needing 3DS maps to the existing on-session fallback (``REQUIRES_ACTION``);
- a finalize failure (Stripe Tax cannot place the customer, say) removes the pack's line and
  deletes the draft, so no orphan drafts pile up, and the caller still sees the original
  error; a finalized invoice is voided on a refused payment and never deleted;
- ``invoice.paid`` grants the top-up lot exactly once, keyed on the invoice id, while a
  subscription invoice is untouched by the new branch and the invoice's own
  ``payment_intent.succeeded`` never double-grants.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
import stripe
from loguru import logger as _loguru_logger
from persona.billing import get_payg_pack
from persona_api.billing import StripeGateway, WebhookContext, handlers
from persona_api.billing import autotopup as autotopup_module
from persona_api.billing.autotopup import (
    AUTO_TOPUP_AMOUNT_CREDITS,
    AUTO_TOPUP_PACK_KEY,
    AutoTopupOutcome,
    maybe_auto_topup,
)
from persona_api.billing.gateway import (
    AutoTopupPriceNotConfiguredError,
    OffSessionAuthenticationRequiredError,
    OffSessionChargeFailedError,
)
from persona_api.config import APIConfig, Edition
from persona_api.editions.factory import build_stripe_gateway

if TYPE_CHECKING:
    from collections.abc import Iterator

_PRICE = "price_pack_10_test"
_KEY = "autotopup:u1:2026-07-24-15"
_META = {"user_id": "u1", "payg_credits": "1000", "source": "auto_topup"}
_FIXED_NOW = datetime(2026, 7, 24, 15, 30, tzinfo=UTC)


# --- a fake StripeClient that honours idempotency keys ------------------------


class _FakeInvoice:
    def __init__(self, invoice_id: str, status: str, pi_id: str | None = "pi_inv_1") -> None:
        self.id = invoice_id
        self.status = status
        # The 2025+ API shape: ``invoice.payments.data[n].payment.payment_intent``.
        self.payments = MagicMock()
        self.payments.data = []
        if pi_id is not None:
            payment = MagicMock()
            payment.payment.payment_intent = pi_id
            self.payments.data = [payment]


class _FakeStripe:
    """Records every call and replays a create by its idempotency key, as Stripe does."""

    def __init__(
        self,
        *,
        pay_error: Exception | None = None,
        finalize_error: Exception | None = None,
        delete_error: Exception | None = None,
        item_delete_error: Exception | None = None,
    ) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self._by_key: dict[str, Any] = {}
        self._pay_error = pay_error
        self._finalize_error = finalize_error
        self._delete_error = delete_error
        self._item_delete_error = item_delete_error
        self._invoice_seq = 0

        self.invoices = MagicMock()
        self.invoices.create.side_effect = self._invoice_create
        self.invoices.finalize_invoice.side_effect = self._finalize
        self.invoices.pay.side_effect = self._pay
        self.invoices.void_invoice.side_effect = self._void
        self.invoices.delete.side_effect = self._delete
        self.invoice_items = MagicMock()
        self.invoice_items.create.side_effect = self._item_create
        self.invoice_items.delete.side_effect = self._item_delete
        self.payment_intents = MagicMock()
        self.payment_intents.update.side_effect = self._pi_update
        self.payment_intents.create.side_effect = AssertionError("no bare PaymentIntent")

    def _record(self, name: str, *args: Any, **kwargs: Any) -> str | None:  # noqa: ANN401
        self.calls.append((name, args, kwargs))
        return kwargs.get("options", {}).get("idempotency_key")

    def _invoice_create(self, *, params: dict[str, Any], options: dict[str, Any]) -> _FakeInvoice:
        key = self._record("invoices.create", params=params, options=options)
        assert key is not None
        if key not in self._by_key:
            self._invoice_seq += 1
            self._by_key[key] = _FakeInvoice(f"in_{self._invoice_seq}", "draft")
        return self._by_key[key]

    def _item_create(self, *, params: dict[str, Any], options: dict[str, Any]) -> MagicMock:
        self._record("invoice_items.create", params=params, options=options)
        item = MagicMock()
        item.id = "ii_1"
        return item

    def _finalize(self, invoice_id: str, **kwargs: Any) -> _FakeInvoice:  # noqa: ANN401
        self._record("invoices.finalize_invoice", invoice_id, **kwargs)
        if self._finalize_error is not None:
            raise self._finalize_error
        return _FakeInvoice(invoice_id, "open")

    def _delete(self, invoice_id: str, **kwargs: Any) -> _FakeInvoice:  # noqa: ANN401
        self._record("invoices.delete", invoice_id, **kwargs)
        if self._delete_error is not None:
            raise self._delete_error
        return _FakeInvoice(invoice_id, "deleted")

    def _item_delete(self, item_id: str, **kwargs: Any) -> MagicMock:  # noqa: ANN401
        self._record("invoice_items.delete", item_id, **kwargs)
        if self._item_delete_error is not None:
            raise self._item_delete_error
        return MagicMock()

    def _pi_update(self, pi_id: str, **kwargs: Any) -> MagicMock:  # noqa: ANN401
        self._record("payment_intents.update", pi_id, **kwargs)
        return MagicMock()

    def _pay(self, invoice_id: str, **kwargs: Any) -> _FakeInvoice:  # noqa: ANN401
        self._record("invoices.pay", invoice_id, **kwargs)
        if self._pay_error is not None:
            raise self._pay_error
        return _FakeInvoice(invoice_id, "paid")

    def _void(self, invoice_id: str, **kwargs: Any) -> _FakeInvoice:  # noqa: ANN401
        self._record("invoices.void_invoice", invoice_id, **kwargs)
        return _FakeInvoice(invoice_id, "void")

    def named(self, name: str) -> list[tuple[str, tuple[Any, ...], dict[str, Any]]]:
        return [c for c in self.calls if c[0] == name]


def _gateway(fake: _FakeStripe, *, price_id: str = _PRICE) -> StripeGateway:
    gateway = StripeGateway(
        secret_key="sk_test_x",
        publishable_key="pk_test_x",
        webhook_secret="wh",
        autotopup_price_id=price_id,
    )
    gateway._client = fake  # type: ignore[assignment]  # noqa: SLF001 — no network fires
    return gateway


def _topup(gateway: StripeGateway, *, key: str = _KEY) -> tuple[str, str]:
    return gateway.create_off_session_topup(
        customer_id="cus_1",
        credit_amount=AUTO_TOPUP_AMOUNT_CREDITS,
        user_id="u1",
        idempotency_key=key,
    )


# --- the invoice is taxed and bills the Checkout price -----------------------


def test_topup_is_an_invoice_with_automatic_tax_on_the_checkout_price() -> None:
    fake = _FakeStripe()

    invoice_id, status = _topup(_gateway(fake))

    assert (invoice_id, status) == ("in_1", "paid")
    fake.payment_intents.create.assert_not_called()  # the bare PI is gone

    ((_, _, create_kw),) = fake.named("invoices.create")
    params = create_kw["params"]
    assert params["customer"] == "cus_1"
    assert params["automatic_tax"] == {"enabled": True}  # the whole point of B9
    assert params["collection_method"] == "charge_automatically"
    assert params["auto_advance"] is False  # we finalize and pay ourselves, no dunning
    assert params["pending_invoice_items_behavior"] == "exclude"  # nothing stray rides along

    ((_, _, item_kw),) = fake.named("invoice_items.create")
    item = item_kw["params"]
    assert item["invoice"] == "in_1"
    assert item["customer"] == "cus_1"
    assert item["pricing"] == {"price": _PRICE}  # the SAME Price object Checkout bills
    assert item["quantity"] == 1
    assert "amount" not in item  # never an ad-hoc amount: tax code + price are one truth
    assert "unit_amount_decimal" not in item

    ((_, fin_args, fin_kw),) = fake.named("invoices.finalize_invoice")
    assert fin_args == ("in_1",)
    assert fin_kw["params"] == {"auto_advance": False}

    ((_, pay_args, pay_kw),) = fake.named("invoices.pay")
    assert pay_args == ("in_1",)
    assert pay_kw["params"] == {"off_session": True}


def test_the_call_order_is_create_item_finalize_stamp_pay() -> None:
    fake = _FakeStripe()

    _topup(_gateway(fake))

    assert [c[0] for c in fake.calls] == [
        "invoices.create",
        "invoice_items.create",
        "invoices.finalize_invoice",
        "payment_intents.update",
        "invoices.pay",
    ]


def test_metadata_rides_the_invoice_the_item_and_its_payment_intent() -> None:
    fake = _FakeStripe()

    _topup(_gateway(fake))

    ((_, _, create_kw),) = fake.named("invoices.create")
    assert create_kw["params"]["metadata"] == _META
    ((_, _, item_kw),) = fake.named("invoice_items.create")
    assert item_kw["params"]["metadata"] == _META
    ((_, pi_args, pi_kw),) = fake.named("payment_intents.update")
    assert pi_args == ("pi_inv_1",)  # the invoice's own PI, resolved after finalize
    assert pi_kw["params"] == {"metadata": _META}


def test_a_finalized_invoice_without_a_visible_pi_still_pays() -> None:
    """The grant is anchored on the invoice, so a PI we cannot see is a stamp we skip,
    never a charge we skip."""
    fake = _FakeStripe()
    fake.invoices.finalize_invoice.side_effect = lambda invoice_id, **_kw: _FakeInvoice(
        invoice_id, "open", pi_id=None
    )

    invoice_id, status = _topup(_gateway(fake))

    assert (invoice_id, status) == ("in_1", "paid")
    assert fake.named("payment_intents.update") == []
    assert len(fake.named("invoices.pay")) == 1


# --- a retried trigger reuses the hourly key ---------------------------------


def test_a_retried_trigger_derives_every_key_from_the_same_hourly_root() -> None:
    fake = _FakeStripe()
    gateway = _gateway(fake)

    first = _topup(gateway)
    second = _topup(gateway)

    assert first == second == ("in_1", "paid")  # Stripe replays the same invoice
    keys = [c[2]["options"]["idempotency_key"] for c in fake.calls]
    assert all(k.startswith(_KEY + ":") for k in keys)
    # Every distinct request in the chain has its own key (Stripe rejects one key
    # reused across different endpoints), and the retry repeats them exactly.
    first_keys, second_keys = keys[:5], keys[5:]
    assert len(set(first_keys)) == 5
    assert first_keys == second_keys
    assert len(fake.named("invoices.create")) == 2  # both calls hit Stripe, same invoice


def test_a_different_hour_is_a_different_invoice() -> None:
    fake = _FakeStripe()
    gateway = _gateway(fake)

    first = _topup(gateway, key="autotopup:u1:2026-07-24-15")
    second = _topup(gateway, key="autotopup:u1:2026-07-24-16")

    assert first[0] != second[0]


# --- 3DS keeps the on-session fallback ----------------------------------------


@pytest.mark.parametrize(
    "stripe_code", ["authentication_required", "invoice_payment_intent_requires_action"]
)
def test_a_card_needing_3ds_raises_the_domain_outcome_and_voids_the_invoice(
    stripe_code: str,
) -> None:
    """Stripe spells 3DS two ways depending on the endpoint; both are the same outcome."""
    fake = _FakeStripe(pay_error=stripe.CardError("needs auth", "payment_method", stripe_code))

    with pytest.raises(OffSessionAuthenticationRequiredError) as excinfo:
        _topup(_gateway(fake))

    assert excinfo.value.code == "authentication_required"  # what the caller keys on
    assert excinfo.value.context["invoice_id"] == "in_1"
    ((_, void_args, _),) = fake.named("invoices.void_invoice")
    assert void_args == ("in_1",)  # no open obligation lingers behind the prompt


def test_a_declined_card_is_a_domain_failure_not_a_3ds_prompt() -> None:
    fake = _FakeStripe(pay_error=stripe.CardError("declined", "payment_method", "card_declined"))

    with pytest.raises(OffSessionChargeFailedError) as excinfo:
        _topup(_gateway(fake))

    assert excinfo.value.context["code"] == "card_declined"
    assert getattr(excinfo.value, "code", None) != "authentication_required"
    assert len(fake.named("invoices.void_invoice")) == 1


def test_a_void_failure_never_hides_the_3ds_outcome() -> None:
    fake = _FakeStripe(
        pay_error=stripe.CardError("needs auth", "payment_method", "authentication_required")
    )
    fake.invoices.void_invoice.side_effect = stripe.APIConnectionError("blip")

    with pytest.raises(OffSessionAuthenticationRequiredError):
        _topup(_gateway(fake))


# --- a finalize failure deletes its draft (R9-177 B9 edge) -----------------------


@pytest.fixture
def gateway_records() -> Iterator[list[tuple[str, str, dict[str, Any]]]]:
    """Every record the gateway logs at INFO or above, as ``(level, message, extra)``."""
    captured: list[tuple[str, str, dict[str, Any]]] = []
    sink_id = _loguru_logger.add(
        lambda m: captured.append(
            (m.record["level"].name, m.record["message"], dict(m.record["extra"]))
        ),
        level="INFO",
        filter=lambda record: record["extra"].get("component") == "api.billing.gateway",
    )
    yield captured
    _loguru_logger.remove(sink_id)


def _tax_location_error() -> stripe.InvalidRequestError:
    """What finalize raises when Stripe Tax cannot place the customer."""
    return stripe.InvalidRequestError(
        "The customer's location is not recognized.",
        param=None,
        code="customer_tax_location_invalid",
    )


def _no_other_ids(extra: dict[str, Any]) -> bool:
    """A cleanup record names the invoice id and nothing else that identifies or prices."""
    values = " ".join(str(v) for k, v in extra.items() if k != "invoice_id")
    return not any(token in values for token in ("cus_", "ii_", "pi_", "u1", "1000", _PRICE))


@pytest.mark.parametrize(
    "finalize_error",
    [
        pytest.param(_tax_location_error(), id="stripe_tax_cannot_place_the_customer"),
        pytest.param(stripe.APIConnectionError("blip"), id="connection_error"),
    ],
)
def test_a_finalize_failure_deletes_the_draft_and_raises_the_original(
    gateway_records: list[tuple[str, str, dict[str, Any]]], finalize_error: Exception
) -> None:
    fake = _FakeStripe(finalize_error=finalize_error)

    with pytest.raises(type(finalize_error)) as excinfo:
        _topup(_gateway(fake))

    assert excinfo.value is finalize_error  # the caller still sees the real failure
    # The line comes off first, then the one draft this call created is deleted. Nothing
    # is paid, voided or stamped.
    assert [c[0] for c in fake.calls] == [
        "invoices.create",
        "invoice_items.create",
        "invoices.finalize_invoice",
        "invoice_items.delete",
        "invoices.delete",
    ]
    assert fake.named("invoice_items.delete")[0][1] == ("ii_1",)
    ((_, delete_args, _),) = fake.named("invoices.delete")
    assert delete_args == ("in_1",)
    ((level, message, extra),) = gateway_records
    assert level == "INFO"
    assert "deleted" in message
    assert extra["invoice_id"] == "in_1"
    assert _no_other_ids(extra)


def test_a_draft_delete_failure_never_hides_the_finalize_failure(
    gateway_records: list[tuple[str, str, dict[str, Any]]],
) -> None:
    finalize_error = _tax_location_error()
    fake = _FakeStripe(
        finalize_error=finalize_error, delete_error=stripe.APIConnectionError("blip")
    )

    with pytest.raises(stripe.InvalidRequestError) as excinfo:
        _topup(_gateway(fake))

    assert excinfo.value is finalize_error
    assert len(fake.named("invoices.delete")) == 1
    ((level, message, extra),) = gateway_records
    assert level == "WARNING"
    assert "could not be deleted" in message
    assert extra["invoice_id"] == "in_1"
    assert _no_other_ids(extra)


def test_a_line_that_cannot_be_removed_leaves_the_draft_in_place(
    gateway_records: list[tuple[str, str, dict[str, Any]]],
) -> None:
    # Deleting the draft while its line is still attached could hand the pack's line back
    # to the customer's pending items, where a later invoice would bill it. A draft that
    # keeps its line is harmless, so the draft stays.
    finalize_error = _tax_location_error()
    fake = _FakeStripe(
        finalize_error=finalize_error, item_delete_error=stripe.APIConnectionError("blip")
    )

    with pytest.raises(stripe.InvalidRequestError) as excinfo:
        _topup(_gateway(fake))

    assert excinfo.value is finalize_error
    assert len(fake.named("invoice_items.delete")) == 1
    assert fake.named("invoices.delete") == []
    ((level, _message, extra),) = gateway_records
    assert level == "WARNING"
    assert extra["invoice_id"] == "in_1"
    assert _no_other_ids(extra)


def test_a_successful_topup_deletes_nothing() -> None:
    fake = _FakeStripe()

    assert _topup(_gateway(fake)) == ("in_1", "paid")

    assert fake.named("invoices.delete") == []
    assert fake.named("invoice_items.delete") == []


def test_a_refused_payment_voids_the_finalized_invoice_and_never_deletes_it() -> None:
    fake = _FakeStripe(pay_error=stripe.CardError("declined", "payment_method", "card_declined"))

    with pytest.raises(OffSessionChargeFailedError):
        _topup(_gateway(fake))

    assert len(fake.named("invoices.void_invoice")) == 1
    assert fake.named("invoices.delete") == []
    assert fake.named("invoice_items.delete") == []


def test_maybe_auto_topup_maps_3ds_to_requires_action(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real trigger chain: the gateway's domain exception reaches the caller's
    existing branch, so the user gets the on-session prompt, exactly as before."""
    fake = _FakeStripe(
        pay_error=stripe.CardError("needs auth", "payment_method", "authentication_required")
    )
    gateway = _gateway(fake)
    monkeypatch.setattr(
        autotopup_module.subscription_service,
        "get_subscription",
        lambda _engine, *, user_id: {  # noqa: ARG005
            "plan_code": "pro",
            "status": "active",
            "auto_topup_enabled": True,
            "stripe_customer_id": "cus_1",
        },
    )
    notified: list[AutoTopupOutcome] = []
    monkeypatch.setattr(
        autotopup_module,
        "_notify_outcome",
        lambda _engine, *, user_id, outcome, now: notified.append(outcome),  # noqa: ARG005
    )

    outcome = maybe_auto_topup(
        rls_engine=MagicMock(),
        gateway=gateway,
        user_id="u1",
        old_balance=250,
        new_balance=50,
        now=_FIXED_NOW,
    )

    assert outcome is AutoTopupOutcome.REQUIRES_ACTION
    assert notified == [AutoTopupOutcome.REQUIRES_ACTION]


def test_maybe_auto_topup_charges_through_the_invoice(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeStripe()
    gateway = _gateway(fake)
    monkeypatch.setattr(
        autotopup_module.subscription_service,
        "get_subscription",
        lambda _engine, *, user_id: {  # noqa: ARG005
            "plan_code": "pro",
            "status": "active",
            "auto_topup_enabled": True,
            "stripe_customer_id": "cus_1",
        },
    )

    outcome = maybe_auto_topup(
        rls_engine=MagicMock(),
        gateway=gateway,
        user_id="u1",
        old_balance=250,
        new_balance=50,
        now=_FIXED_NOW,
    )

    assert outcome is AutoTopupOutcome.CHARGED
    ((_, _, create_kw),) = fake.named("invoices.create")
    assert create_kw["options"]["idempotency_key"] == _KEY + ":invoice"
    assert create_kw["params"]["metadata"]["payg_credits"] == str(AUTO_TOPUP_AMOUNT_CREDITS)


# --- the price is the pack's, and it is wired from config ---------------------


def test_the_auto_topup_bills_the_pack_that_grants_the_same_credits() -> None:
    pack = get_payg_pack(AUTO_TOPUP_PACK_KEY)
    assert pack is not None
    assert pack.granted_credits == AUTO_TOPUP_AMOUNT_CREDITS


def test_production_wires_the_pack_price_into_the_gateway() -> None:
    cfg = APIConfig(
        database_url="postgresql+psycopg://super@localhost/persona_shell",
        app_database_url="postgresql+psycopg://persona_app@localhost/persona_shell",
        edition=Edition.cloud,
        billing_stripe_enabled=True,
        stripe_secret_key="sk_test_x",
        stripe_publishable_key="pk_test_x",
        stripe_webhook_secret="whsec_x",
        stripe_price_pack_10=_PRICE,
    )

    gateway = build_stripe_gateway(cfg)

    assert gateway is not None
    assert gateway.autotopup_price_id == _PRICE


def test_an_unconfigured_pack_price_refuses_before_any_stripe_call() -> None:
    fake = _FakeStripe()

    with pytest.raises(AutoTopupPriceNotConfiguredError):
        _topup(_gateway(fake, price_id=""))

    assert fake.calls == []


# --- the webhook resolves a top-up invoice to one grant ------------------------


class _RecordingPolicy:
    """Dedupes on the billing key, the way the real ledger gate does."""

    def __init__(self) -> None:
        self.payg_keys: list[str] = []
        self.payg_calls: list[dict[str, Any]] = []
        self.allowance_resets: list[dict[str, Any]] = []

    def grant_payg_lot_idempotent(self, **kwargs: Any) -> int:  # noqa: ANN401
        self.payg_calls.append(kwargs)
        key = kwargs["source_billing_key"]
        if key not in self.payg_keys:
            self.payg_keys.append(key)
        return len(self.payg_keys)

    def reset_allowance_idempotent(self, **kwargs: Any) -> int:  # noqa: ANN401
        self.allowance_resets.append(kwargs)
        return 0


def _context(policy: _RecordingPolicy) -> WebhookContext:
    cfg = APIConfig(
        database_url="postgresql+psycopg://super@localhost/persona_shell",
        app_database_url="postgresql+psycopg://persona_app@localhost/persona_shell",
        edition=Edition.cloud,
        billing_stripe_enabled=True,
        stripe_secret_key="sk_test_x",
        stripe_webhook_secret="whsec_x",
        stripe_price_plus="price_plus_test",
        stripe_price_pack_10=_PRICE,
    )
    return WebhookContext(
        rls_engine=MagicMock(),
        admin_engine=MagicMock(),
        credits_policy=policy,  # type: ignore[arg-type]  # the recording double
        config=cfg,
    )


def _event(event_type: str, obj: dict[str, Any], *, event_id: str = "evt_1") -> stripe.Event:
    return stripe.Event.construct_from(
        {"id": event_id, "object": "event", "type": event_type, "data": {"object": obj}},
        "sk_test_x",
    )


def _topup_invoice(*, invoice_id: str = "in_topup") -> dict[str, Any]:
    return {
        "id": invoice_id,
        "object": "invoice",
        "customer": "cus_1",
        "currency": "usd",
        # A paid $10 pack: the grant checks these before it trusts the stamp (review M3).
        "status": "paid",
        "subtotal": 1000,
        "amount_paid": 1000,
        "amount_remaining": 0,
        "metadata": dict(_META),
        "lines": {"data": [{"pricing": {"price": _PRICE}}]},
    }


def _subscription_invoice() -> dict[str, Any]:
    return {
        "id": "in_sub",
        "object": "invoice",
        "customer": "cus_1",
        "metadata": {},
        "lines": {"data": [{"price": {"id": "price_plus_test"}}]},
        "subscription_details": {"metadata": {"user_id": "u1"}},
    }


@pytest.fixture
def resolved_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        handlers.subscription_service,
        "resolve_user_by_customer",
        lambda _engine, *, stripe_customer_id: "u1" if stripe_customer_id == "cus_1" else None,
    )


@pytest.mark.usefixtures("resolved_user")
def test_invoice_paid_grants_the_topup_lot_exactly_once_on_redelivery() -> None:
    policy = _RecordingPolicy()
    context = _context(policy)

    for event_id in ("evt_1", "evt_1", "evt_2"):
        handlers.handle_invoice_paid(
            _event("invoice.paid", _topup_invoice(), event_id=event_id), context
        )

    assert policy.payg_keys == ["in_topup"]  # one lot, keyed on the INVOICE id
    assert len(policy.payg_calls) == 3  # every delivery reaches the gate; the gate dedupes
    first = policy.payg_calls[0]
    assert first["user_id"] == "u1"
    assert first["credit_amount"] == AUTO_TOPUP_AMOUNT_CREDITS  # the stamped credits, tax-free
    assert first["reason"] == "auto_topup"
    assert first["cost_basis"] == "topup_payg"
    assert policy.allowance_resets == []  # never mistaken for a subscription renewal


@pytest.mark.usefixtures("resolved_user")
def test_invoice_paid_leaves_a_subscription_invoice_on_the_renewal_path() -> None:
    policy = _RecordingPolicy()

    handlers.handle_invoice_paid(_event("invoice.paid", _subscription_invoice()), _context(policy))

    assert policy.payg_calls == []
    (reset,) = policy.allowance_resets
    assert reset["billing_key"] == "in_sub"
    assert reset["reason"] == "subscription_renewal"


@pytest.mark.usefixtures("resolved_user")
def test_invoice_paid_tripwire_refuses_a_topup_invoice_for_another_user() -> None:
    policy = _RecordingPolicy()
    invoice = _topup_invoice()
    invoice["metadata"]["user_id"] = "u_other"  # DB says cus_1 is u1; the stamp disagrees

    handlers.handle_invoice_paid(_event("invoice.paid", invoice), _context(policy))

    assert policy.payg_calls == []


@pytest.mark.usefixtures("resolved_user")
def test_a_refused_topup_invoice_never_marks_the_subscription_past_due(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``invoice.payment_failed`` fires for the voided top-up too; it is not a missed
    renewal and must not touch the plan."""
    marked: list[str] = []
    monkeypatch.setattr(
        handlers.subscription_service,
        "mark_past_due",
        lambda _engine, *, user_id: marked.append(user_id),
    )
    policy = _RecordingPolicy()

    handlers.handle_invoice_payment_failed(
        _event("invoice.payment_failed", _topup_invoice()), _context(policy)
    )
    handlers.handle_invoice_payment_failed(
        _event("invoice.payment_failed", _subscription_invoice()), _context(policy)
    )

    assert marked == ["u1"]  # the renewal path is unchanged; the top-up was skipped


@pytest.mark.usefixtures("resolved_user")
def test_the_invoices_own_payment_intent_never_double_grants() -> None:
    """The invoice's PI carries the same metadata (for the tripwire), and the
    ``payment_intent.succeeded`` handler must leave it to ``invoice.paid``."""
    policy = _RecordingPolicy()
    pi = {
        "id": "pi_inv_1",
        "object": "payment_intent",
        "customer": "cus_1",
        "metadata": dict(_META),
    }

    handlers.handle_payment_intent_succeeded(
        _event("payment_intent.succeeded", pi), _context(policy)
    )

    assert policy.payg_calls == []


@pytest.mark.usefixtures("resolved_user")
def test_a_checkout_pack_payment_intent_still_grants() -> None:
    """The Checkout pack path is unchanged: its PI has no ``source`` stamp."""
    policy = _RecordingPolicy()
    pi = {
        "id": "pi_pack_1",
        "object": "payment_intent",
        "customer": "cus_1",
        "currency": "usd",
        "amount_received": 500,  # a paid $5 pack: the grant checks it (review follow-up)
        "metadata": {"user_id": "u1", "payg_credits": "500"},
    }

    handlers.handle_payment_intent_succeeded(
        _event("payment_intent.succeeded", pi), _context(policy)
    )

    assert policy.payg_keys == ["pi_pack_1"]


@pytest.mark.usefixtures("resolved_user")
def test_a_topup_invoice_with_bad_credits_is_a_noop() -> None:
    policy = _RecordingPolicy()
    for bad in ("", "abc", "0", "-5"):
        invoice = _topup_invoice()
        invoice["metadata"]["payg_credits"] = bad
        handlers.handle_invoice_paid(_event("invoice.paid", invoice), _context(policy))

    assert policy.payg_calls == []
    assert policy.allowance_resets == []

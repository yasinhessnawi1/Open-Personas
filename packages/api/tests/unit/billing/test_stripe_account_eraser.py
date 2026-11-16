"""The Stripe side of an account erasure, against a fake client (R9-284 T9).

Nothing reaches Stripe. Pins: the subscription is cancelled at once with the owner's named
parameters, an ended one is not cancelled again, every write carries an idempotency key derived
from the erasure id, Stripe's "already gone" counts as done, and anything else is raised.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import stripe
from persona_api.billing.account_erasure import ERASURE_CANCEL_PARAMS, StripeAccountEraser

_ERASURE = "erasure_0123"


def _gone() -> stripe.InvalidRequestError:
    return stripe.InvalidRequestError("No such object", param=None, code="resource_missing")


class _Listed:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def auto_paging_iter(self) -> list[Any]:
        return list(self._items)


class _Fake:
    """Records every write; ``fail`` maps a call name to the exception it raises."""

    def __init__(
        self,
        *,
        subscriptions: list[tuple[str, str]] = (),  # type: ignore[assignment]
        methods: list[str] = (),  # type: ignore[assignment]
        sessions: list[str] = (),  # type: ignore[assignment]
        fail: dict[str, Exception] | None = None,
    ) -> None:
        self.writes: list[tuple[str, str, Any, Any]] = []
        self._subs = [SimpleNamespace(id=i, status=s) for i, s in subscriptions]
        self._methods = [SimpleNamespace(id=m) for m in methods]
        self._sessions = [SimpleNamespace(id=c) for c in sessions]
        self._fail = fail or {}
        self.subscriptions = SimpleNamespace(list=self._list_subs, cancel=self._cancel)
        self.checkout = SimpleNamespace(
            sessions=SimpleNamespace(list=self._list_sessions, expire=self._expire)
        )
        self.customers = SimpleNamespace(
            payment_methods=SimpleNamespace(list=self._list_methods), update=self._update
        )
        self.payment_methods = SimpleNamespace(detach=self._detach)

    def _maybe_fail(self, name: str) -> None:
        if name in self._fail:
            raise self._fail[name]

    def _list_sessions(self, *, params: Any) -> _Listed:  # noqa: ANN401
        self._maybe_fail("list_sessions")
        assert params["status"] == "open", "only an open Checkout can still become a sale"
        return _Listed(self._sessions)

    def _expire(self, session_id: str, *, options: Any) -> None:  # noqa: ANN401
        self._maybe_fail("expire")
        self.writes.append(("expire", session_id, None, options))

    def _list_subs(self, *, params: Any) -> _Listed:  # noqa: ANN401
        self._maybe_fail("list_subs")
        assert params["status"] == "all", "ended subscriptions are listed too, then skipped"
        return _Listed(self._subs)

    def _cancel(self, sub_id: str, *, params: Any, options: Any) -> None:  # noqa: ANN401
        self._maybe_fail("cancel")
        self.writes.append(("cancel", sub_id, params, options))

    def _list_methods(self, customer: str, *, params: Any) -> _Listed:  # noqa: ANN401, ARG002
        self._maybe_fail("list_methods")
        return _Listed(self._methods)

    def _detach(self, pm_id: str, *, options: Any) -> None:  # noqa: ANN401
        self._maybe_fail("detach")
        self.writes.append(("detach", pm_id, None, options))

    def _update(self, customer: str, *, params: Any, options: Any) -> None:  # noqa: ANN401
        self._maybe_fail("update")
        self.writes.append(("update", customer, params, options))


def test_it_cancels_detaches_and_relabels_under_keys_from_the_erasure() -> None:
    fake = _Fake(subscriptions=[("sub_1", "active")], methods=["pm_1", "pm_2"], sessions=["cs_1"])

    counts = StripeAccountEraser(fake).erase_customer("cus_1", erasure_id=_ERASURE)

    assert counts == {
        "checkout_sessions_expired": 1,
        "subscriptions_cancelled": 1,
        "payment_methods_detached": 2,
        "customers_relabelled": 1,
    }
    assert fake.writes == [
        ("expire", "cs_1", None, {"idempotency_key": f"erase:{_ERASURE}:expire:cs_1"}),
        (
            "cancel",
            "sub_1",
            ERASURE_CANCEL_PARAMS,
            {"idempotency_key": f"erase:{_ERASURE}:cancel:sub_1"},
        ),
        ("detach", "pm_1", None, {"idempotency_key": f"erase:{_ERASURE}:detach:pm_1"}),
        ("detach", "pm_2", None, {"idempotency_key": f"erase:{_ERASURE}:detach:pm_2"}),
        (
            "update",
            "cus_1",
            {"metadata": {"user_id": _ERASURE}},
            {"idempotency_key": f"erase:{_ERASURE}:relabel:cus_1"},
        ),
    ]


def test_the_cancel_is_at_once_with_no_proration_and_no_final_invoice() -> None:
    assert ERASURE_CANCEL_PARAMS == {"prorate": False, "invoice_now": False}


def test_a_resumed_erasure_cancels_nothing_already_ended() -> None:
    fake = _Fake(subscriptions=[("sub_1", "canceled"), ("sub_2", "incomplete_expired")])

    counts = StripeAccountEraser(fake).erase_customer("cus_1", erasure_id=_ERASURE)

    assert counts["subscriptions_cancelled"] == 0
    assert [w for w in fake.writes if w[0] == "cancel"] == []


_CALLS = ["list_sessions", "expire", "list_subs", "cancel", "list_methods", "detach", "update"]


@pytest.mark.parametrize("call", _CALLS)
def test_already_gone_counts_as_done(call: str) -> None:
    fake = _Fake(
        subscriptions=[("sub_1", "active")],
        methods=["pm_1"],
        sessions=["cs_1"],
        fail={call: _gone()},
    )

    counts = StripeAccountEraser(fake).erase_customer("cus_1", erasure_id=_ERASURE)

    assert all(n >= 0 for n in counts.values())
    assert sum(counts.values()) == 3, "the gone object is not counted as changed, the rest are"


@pytest.mark.parametrize("call", _CALLS)
def test_any_other_stripe_error_is_raised(call: str) -> None:
    error = stripe.APIConnectionError("network down")
    fake = _Fake(
        subscriptions=[("sub_1", "active")], methods=["pm_1"], sessions=["cs_1"], fail={call: error}
    )

    with pytest.raises(stripe.APIConnectionError):
        StripeAccountEraser(fake).erase_customer("cus_1", erasure_id=_ERASURE)


def test_another_invalid_request_is_raised() -> None:
    error = stripe.InvalidRequestError("bad", param="x", code="parameter_invalid_empty")
    fake = _Fake(subscriptions=[("sub_1", "active")], fail={"cancel": error})

    with pytest.raises(stripe.InvalidRequestError):
        StripeAccountEraser(fake).erase_customer("cus_1", erasure_id=_ERASURE)


# ---- a subscription made for an erased account (review M4) -------------------------------


class _Customers:
    def __init__(self, metadata: dict[str, str] | None, *, status: str = "active") -> None:
        self.metadata = metadata
        self.status = status
        self.cancelled: list[tuple[str, Any, Any]] = []

    def client(self) -> SimpleNamespace:
        def retrieve(customer_id: str) -> SimpleNamespace:  # noqa: ARG001
            if self.metadata is None:
                raise _gone()
            return SimpleNamespace(metadata=self.metadata)

        def cancel(sub_id: str, *, params: Any, options: Any) -> None:  # noqa: ANN401
            self.cancelled.append((sub_id, params, options))

        def retrieve_subscription(sub_id: str) -> SimpleNamespace:
            return SimpleNamespace(id=sub_id, status=self.status)

        return SimpleNamespace(
            customers=SimpleNamespace(retrieve=retrieve),
            subscriptions=SimpleNamespace(cancel=cancel, retrieve=retrieve_subscription),
        )


def test_a_subscription_for_an_erased_customer_is_cancelled_at_once() -> None:
    from persona_api.billing.account_erasure import cancel_if_erased  # noqa: PLC0415

    fake = _Customers({"user_id": _ERASURE})

    assert cancel_if_erased(fake.client(), customer_id="cus_1", subscription_id="sub_new")
    assert fake.cancelled == [
        ("sub_new", ERASURE_CANCEL_PARAMS, {"idempotency_key": f"erase:{_ERASURE}:cancel:sub_new"})
    ]


@pytest.mark.parametrize("metadata", [{"user_id": "user_2abc"}, {}, None])
def test_a_live_or_unknown_customer_is_left_alone(metadata: dict[str, str] | None) -> None:
    from persona_api.billing.account_erasure import cancel_if_erased  # noqa: PLC0415

    fake = _Customers(metadata)

    assert not cancel_if_erased(fake.client(), customer_id="cus_1", subscription_id="sub_new")
    assert fake.cancelled == []


def test_the_webhook_cancels_a_subscription_made_for_an_erased_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """customer.subscription.updated for a customer no user owns, relabelled by an erasure."""
    from persona_api.billing import handlers  # noqa: PLC0415

    fake = _Customers({"user_id": _ERASURE})
    monkeypatch.setattr(
        handlers.subscription_service, "resolve_user_by_customer", lambda *_a, **_k: None
    )
    written: list[str] = []
    monkeypatch.setattr(handlers, "_apply_scoped", lambda *_a: written.append("x"))
    context = SimpleNamespace(admin_engine=None, stripe_client=fake.client(), config=None)
    event = SimpleNamespace(data=SimpleNamespace(object={"id": "sub_new", "customer": "cus_1"}))

    handlers.handle_subscription_updated(event, context)  # type: ignore[arg-type]
    handlers.handle_subscription_created(event, context)  # type: ignore[arg-type]

    assert [c[0] for c in fake.cancelled] == ["sub_new", "sub_new"]
    assert written == [], "nothing is written for an account that no longer exists"


def test_a_known_customer_costs_no_stripe_call(monkeypatch: pytest.MonkeyPatch) -> None:
    from persona_api.billing import handlers  # noqa: PLC0415

    monkeypatch.setattr(
        handlers.subscription_service, "resolve_user_by_customer", lambda *_a, **_k: "user_live"
    )
    client = SimpleNamespace()  # any Stripe call would fail on this
    context = SimpleNamespace(admin_engine=None, stripe_client=client)

    assert not handlers._cancelled_for_an_erased_account(  # noqa: SLF001
        context,  # type: ignore[arg-type]
        {"customer": "cus_live"},
        "sub_1",
    )


def test_a_payment_method_already_detached_counts_as_done() -> None:
    """Reviewer's survivor M24: Stripe answers payment_method_unexpected_state for a method
    detached meanwhile (a second run, or the user in the portal); that is done, not a failure."""
    detached = stripe.InvalidRequestError(
        "The payment method is not attached to a customer",
        param="payment_method",
        code="payment_method_unexpected_state",
    )
    fake = _Fake(methods=["pm_1", "pm_2"], fail={"detach": detached})

    counts = StripeAccountEraser(fake).erase_customer("cus_1", erasure_id=_ERASURE)

    assert counts["payment_methods_detached"] == 0
    assert counts["customers_relabelled"] == 1, "the erasure carried on past it"


def test_customer_subscription_created_is_routed() -> None:
    """Review M-2: the guard only runs for events the dispatcher routes."""
    from persona_api.billing import handlers  # noqa: PLC0415

    registry = handlers.lifecycle_handlers()

    assert registry["customer.subscription.created"] is handlers.handle_subscription_created


def _erased_context(monkeypatch: pytest.MonkeyPatch, fake: _Customers) -> SimpleNamespace:
    from persona_api.billing import handlers  # noqa: PLC0415

    monkeypatch.setattr(
        handlers.subscription_service, "resolve_user_by_customer", lambda *_a, **_k: None
    )
    monkeypatch.setattr(handlers, "_apply_scoped", lambda *_a: pytest.fail("wrote for it"))
    return SimpleNamespace(admin_engine=None, stripe_client=fake.client(), config=None)


def test_a_completed_checkout_for_an_erased_customer_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review M-2: checkout.session.completed names the new subscription; it is cancelled."""
    from persona_api.billing import handlers  # noqa: PLC0415

    fake = _Customers({"user_id": _ERASURE})
    context = _erased_context(monkeypatch, fake)
    session = {"customer": "cus_1", "subscription": "sub_from_checkout"}

    handlers.handle_checkout_completed(
        SimpleNamespace(data=SimpleNamespace(object=session)),  # type: ignore[arg-type]
        context,  # type: ignore[arg-type]
    )

    assert [c[0] for c in fake.cancelled] == ["sub_from_checkout"]


def test_an_updated_subscription_for_an_erased_customer_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from persona_api.billing import handlers  # noqa: PLC0415

    fake = _Customers({"user_id": _ERASURE})
    context = _erased_context(monkeypatch, fake)

    handlers.handle_subscription_updated(
        SimpleNamespace(data=SimpleNamespace(object={"id": "sub_upd", "customer": "cus_1"})),  # type: ignore[arg-type]
        context,  # type: ignore[arg-type]
    )

    assert [c[0] for c in fake.cancelled] == ["sub_upd"]


@pytest.mark.parametrize("status", ["canceled", "incomplete_expired"])
def test_an_ended_subscription_of_an_erased_customer_is_left_alone(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    """Review L-2: our own cancel's later events name an ended subscription; it is not
    cancelled again, and nothing claims it was."""
    from persona_api.billing import account_erasure  # noqa: PLC0415

    logged: list[str] = []
    monkeypatch.setattr(
        account_erasure._log,
        "warning",
        lambda message, **_k: logged.append(message),  # noqa: SLF001
    )
    fake = _Customers({"user_id": _ERASURE}, status=status)

    assert not account_erasure.cancel_if_erased(
        fake.client(), customer_id="cus_1", subscription_id="sub_old"
    )
    assert fake.cancelled == []
    assert logged == [], "no false 'cancelled' log"

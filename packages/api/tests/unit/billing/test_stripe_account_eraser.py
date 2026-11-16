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
        fail: dict[str, Exception] | None = None,
    ) -> None:
        self.writes: list[tuple[str, str, Any, Any]] = []
        self._subs = [SimpleNamespace(id=i, status=s) for i, s in subscriptions]
        self._methods = [SimpleNamespace(id=m) for m in methods]
        self._fail = fail or {}
        self.subscriptions = SimpleNamespace(list=self._list_subs, cancel=self._cancel)
        self.customers = SimpleNamespace(
            payment_methods=SimpleNamespace(list=self._list_methods), update=self._update
        )
        self.payment_methods = SimpleNamespace(detach=self._detach)

    def _maybe_fail(self, name: str) -> None:
        if name in self._fail:
            raise self._fail[name]

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
    fake = _Fake(subscriptions=[("sub_1", "active")], methods=["pm_1", "pm_2"])

    counts = StripeAccountEraser(fake).erase_customer("cus_1", erasure_id=_ERASURE)

    assert counts == {
        "subscriptions_cancelled": 1,
        "payment_methods_detached": 2,
        "customers_relabelled": 1,
    }
    assert fake.writes == [
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


@pytest.mark.parametrize("call", ["list_subs", "cancel", "list_methods", "detach", "update"])
def test_already_gone_counts_as_done(call: str) -> None:
    fake = _Fake(subscriptions=[("sub_1", "active")], methods=["pm_1"], fail={call: _gone()})

    counts = StripeAccountEraser(fake).erase_customer("cus_1", erasure_id=_ERASURE)

    assert all(n >= 0 for n in counts.values())
    assert sum(counts.values()) < 3, "the gone object is not counted as changed"


@pytest.mark.parametrize("call", ["list_subs", "cancel", "list_methods", "detach", "update"])
def test_any_other_stripe_error_is_raised(call: str) -> None:
    error = stripe.APIConnectionError("network down")
    fake = _Fake(subscriptions=[("sub_1", "active")], methods=["pm_1"], fail={call: error})

    with pytest.raises(stripe.APIConnectionError):
        StripeAccountEraser(fake).erase_customer("cus_1", erasure_id=_ERASURE)


def test_another_invalid_request_is_raised() -> None:
    error = stripe.InvalidRequestError("bad", param="x", code="parameter_invalid_empty")
    fake = _Fake(subscriptions=[("sub_1", "active")], fail={"cancel": error})

    with pytest.raises(stripe.InvalidRequestError):
        StripeAccountEraser(fake).erase_customer("cus_1", erasure_id=_ERASURE)

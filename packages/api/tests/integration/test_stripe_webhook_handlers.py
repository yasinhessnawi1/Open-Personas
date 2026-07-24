"""Subscription lifecycle handlers — the adversarial gate (Spec M4, T3b).

Real DB + real signed events + real handlers. Proves the money-correctness + security
invariants: ``invoice.paid`` resets the allowance EXACTLY once (a re-delivery after the
user spends does NOT re-inflate — the billing_key gate); cancel/past_due/subscribe apply
correctly; an unknown customer is a safe no-op; a metadata.user_id tripwire mismatch
refuses; writes are RLS-scoped to the resolved user (the admin engine does ONLY reads).

Wiring: ``rls_engine`` = a ``make_rls_engine`` persona_app engine (ContextVar-scoped, the
handler writes here); ``admin_engine`` = a separate superuser engine (the read-only
customer→user resolve); ``migrated_engine`` (superuser) seeds + asserts.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient
from persona_api.app import create_app
from persona_api.config import APIConfig, Edition
from persona_api.middleware.rls_context import make_rls_engine
from sqlalchemy import create_engine, event, text

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_DB = "postgresql+psycopg://super@localhost/persona_shell"
_SECRET = "whsec_test_secret"
_CUS_A = "cus_A"
_PERIOD_START = 1_700_000_000
_PERIOD_END = 1_702_592_000


def _sign(payload: bytes) -> str:
    ts = int(time.time())
    digest = hmac.new(_SECRET.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={digest}"


def _event(event_type: str, obj: dict[str, Any], *, event_id: str = "evt_1") -> bytes:
    return json.dumps(
        {"id": event_id, "object": "event", "type": event_type, "data": {"object": obj}}
    ).encode()


def _invoice(
    *,
    invoice_id: str = "in_1",
    customer: str = _CUS_A,
    price: str = "price_plus_test",
    metadata: dict[str, str] | None = None,
) -> dict[str, Any]:
    obj: dict[str, Any] = {
        "id": invoice_id,
        "object": "invoice",
        "customer": customer,
        "lines": {"data": [{"price": {"id": price}}]},
    }
    if metadata is not None:
        obj["subscription_details"] = {"metadata": metadata}
    return obj


def _subscription(
    *,
    customer: str = _CUS_A,
    price: str = "price_plus_test",
    status: str = "active",
    period_start: int = _PERIOD_START,
    period_end: int = _PERIOD_END,
    cancel: bool = False,
) -> dict[str, Any]:
    return {
        "id": "sub_1",
        "object": "subscription",
        "customer": customer,
        "status": status,
        "current_period_start": period_start,
        "current_period_end": period_end,
        "cancel_at_period_end": cancel,
        "items": {"data": [{"price": {"id": price}}]},
    }


@pytest.fixture
def webhook(migrated_engine: Engine) -> Iterator[tuple[TestClient, Engine, list[str]]]:
    """A billing app wired for the webhook: (client, seed/assert engine, admin-SQL log)."""
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    rls_engine = make_rls_engine(app_url.replace("+asyncpg", "+psycopg"))
    admin_engine = create_engine(_admin_url())
    admin_sql: list[str] = []

    @event.listens_for(admin_engine, "before_cursor_execute")
    def _record(_conn: Any, _cursor: Any, statement: str, *_a: Any, **_k: Any) -> None:  # noqa: ANN401, ARG001
        admin_sql.append(statement)

    cfg = APIConfig(
        database_url=_DB,
        app_database_url=app_url,
        edition=Edition.cloud,
        billing_stripe_enabled=True,
        stripe_secret_key="sk_test_x",
        stripe_webhook_secret=_SECRET,
        stripe_price_plus="price_plus_test",
        stripe_price_pro="price_pro_test",
    )
    app = create_app(cfg)
    app.state.rls_engine = rls_engine
    app.state.admin_engine = admin_engine
    client = TestClient(app)
    yield client, migrated_engine, admin_sql
    rls_engine.dispose()
    admin_engine.dispose()


def _admin_url() -> str:
    url = os.environ["DATABASE_URL"]
    return url.replace("+asyncpg", "+psycopg")


def _seed(engine: Engine, uid: str, customer_id: str, *, balance: int = 300) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:u, :e) ON CONFLICT DO NOTHING"),
            {"u": uid, "e": f"{uid}@x.test"},
        )
        conn.execute(
            text(
                "INSERT INTO subscription (user_id, stripe_customer_id) VALUES (:u, :c) "
                "ON CONFLICT (user_id) DO UPDATE SET stripe_customer_id = :c"
            ),
            {"u": uid, "c": customer_id},
        )
        conn.execute(
            text(
                "INSERT INTO credits (user_id, balance) VALUES (:u, :b) "
                "ON CONFLICT (user_id) DO UPDATE SET balance = :b"
            ),
            {"u": uid, "b": balance},
        )


def _balance(engine: Engine, uid: str) -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                text("SELECT balance FROM credits WHERE user_id = :u"), {"u": uid}
            ).scalar_one()
        )


def _sub_row(engine: Engine, uid: str) -> dict[str, Any]:
    with engine.begin() as conn:
        return dict(
            conn.execute(text("SELECT * FROM subscription WHERE user_id = :u"), {"u": uid})
            .mappings()
            .one()
        )


def _post(client: TestClient, payload: bytes) -> object:
    return client.post(
        "/v1/billing/webhook", content=payload, headers={"Stripe-Signature": _sign(payload)}
    )


# --- the load-bearing money tests --------------------------------------------


def test_invoice_paid_resets_allowance_exactly_once_on_5x_redelivery(
    webhook: tuple[TestClient, Engine, list[str]],
) -> None:
    client, engine, _ = webhook
    _seed(engine, "user_a", _CUS_A, balance=300)
    payload = _event("invoice.paid", _invoice(invoice_id="in_1"), event_id="evt_1")

    for _ in range(5):
        assert _post(client, payload).status_code == 200  # type: ignore[attr-defined]

    assert _balance(engine, "user_a") == 2000  # reset to the Plus allowance, not 5× re-inflated
    with engine.begin() as conn:
        reset_rows = conn.execute(
            text("SELECT count(*) FROM credit_transactions WHERE billing_key = 'in_1'")
        ).scalar_one()
    assert reset_rows == 1  # the billing_key gate fired exactly once


def test_invoice_paid_gate_holds_after_the_user_spends(
    webhook: tuple[TestClient, Engine, list[str]],
) -> None:
    """The specific double-grant the gate prevents: reset → spend → SAME invoice
    re-delivered → NOT re-inflated (the user's spend is preserved)."""
    client, engine, _ = webhook
    _seed(engine, "user_a", _CUS_A, balance=300)
    payload = _event("invoice.paid", _invoice(invoice_id="in_1"))

    assert _post(client, payload).status_code == 200  # type: ignore[attr-defined]
    assert _balance(engine, "user_a") == 2000  # reset

    # The user spends 500 of the allowance.
    with engine.begin() as conn:
        conn.execute(text("UPDATE credits SET balance = balance - 500 WHERE user_id = 'user_a'"))
    assert _balance(engine, "user_a") == 1500

    # Stripe re-delivers the SAME invoice.paid → the gate blocks → NO re-inflation.
    assert _post(client, payload).status_code == 200  # type: ignore[attr-defined]
    assert _balance(engine, "user_a") == 1500  # preserved (NOT re-inflated to 2000)


def test_invoice_paid_only_touches_the_resolved_user(
    webhook: tuple[TestClient, Engine, list[str]],
) -> None:
    client, engine, _ = webhook
    _seed(engine, "user_a", _CUS_A, balance=300)
    _seed(engine, "user_b", "cus_B", balance=300)

    _post(client, _event("invoice.paid", _invoice(customer=_CUS_A)))

    assert _balance(engine, "user_a") == 2000  # the resolved user reset
    assert _balance(engine, "user_b") == 300  # the other tenant untouched


# --- the other lifecycle transitions -----------------------------------------


def test_checkout_completed_binds_ids(webhook: tuple[TestClient, Engine, list[str]]) -> None:
    client, engine, _ = webhook
    _seed(engine, "user_a", _CUS_A)
    obj = {"object": "checkout.session", "customer": _CUS_A, "subscription": "sub_1"}

    assert _post(client, _event("checkout.session.completed", obj)).status_code == 200  # type: ignore[attr-defined]

    row = _sub_row(engine, "user_a")
    assert row["stripe_subscription_id"] == "sub_1"
    assert row["status"] == "active"


def test_subscription_updated_sets_plan_and_period(
    webhook: tuple[TestClient, Engine, list[str]],
) -> None:
    client, engine, _ = webhook
    _seed(engine, "user_a", _CUS_A)

    assert (
        _post(
            client, _event("customer.subscription.updated", _subscription(price="price_pro_test"))
        ).status_code
        == 200
    )  # type: ignore[attr-defined]

    row = _sub_row(engine, "user_a")
    assert row["plan_code"] == "pro"
    assert row["status"] == "active"
    assert row["current_period_start"] is not None


def test_subscription_updated_monotonic_guard_ignores_stale(
    webhook: tuple[TestClient, Engine, list[str]],
) -> None:
    client, engine, _ = webhook
    _seed(engine, "user_a", _CUS_A)
    # Newer period first (pro), then a STALE older-period event (plus) — the stale one is ignored.
    _post(
        client,
        _event(
            "customer.subscription.updated",
            _subscription(price="price_pro_test", period_start=_PERIOD_START + 1000),
        ),
    )
    _post(
        client,
        _event(
            "customer.subscription.updated",
            _subscription(price="price_plus_test", period_start=_PERIOD_START),
        ),
    )
    assert _sub_row(engine, "user_a")["plan_code"] == "pro"  # stale plus did NOT regress it


def test_subscription_deleted_drops_to_free_no_grant(
    webhook: tuple[TestClient, Engine, list[str]],
) -> None:
    client, engine, _ = webhook
    _seed(engine, "user_a", _CUS_A, balance=2000)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE subscription SET plan_code='pro', stripe_subscription_id='sub_1' "
                "WHERE user_id='user_a'"
            )
        )

    assert (
        _post(client, _event("customer.subscription.deleted", _subscription())).status_code == 200
    )  # type: ignore[attr-defined]

    row = _sub_row(engine, "user_a")
    assert row["plan_code"] == "free"
    assert row["status"] == "canceled"
    assert row["stripe_subscription_id"] is None
    assert _balance(engine, "user_a") == 2000  # NO grant/change on cancel


def test_payment_failed_marks_past_due(webhook: tuple[TestClient, Engine, list[str]]) -> None:
    client, engine, _ = webhook
    _seed(engine, "user_a", _CUS_A, balance=2000)

    assert _post(client, _event("invoice.payment_failed", _invoice())).status_code == 200  # type: ignore[attr-defined]

    assert _sub_row(engine, "user_a")["status"] == "past_due"
    assert _balance(engine, "user_a") == 2000  # no grant


# --- security: unknown customer, tripwire, no admin write --------------------


def test_unknown_customer_is_a_safe_noop(webhook: tuple[TestClient, Engine, list[str]]) -> None:
    client, engine, _ = webhook
    _seed(engine, "user_a", _CUS_A, balance=300)

    resp = _post(client, _event("invoice.paid", _invoice(customer="cus_UNKNOWN")))

    assert resp.status_code == 200  # type: ignore[attr-defined]
    assert _balance(engine, "user_a") == 300  # zero write


def test_metadata_tripwire_mismatch_refuses(
    webhook: tuple[TestClient, Engine, list[str]],
) -> None:
    """An invoice for cus_A (→ user_a) whose stamped metadata.user_id says user_b → refuse."""
    client, engine, _ = webhook
    _seed(engine, "user_a", _CUS_A, balance=300)

    resp = _post(
        client, _event("invoice.paid", _invoice(customer=_CUS_A, metadata={"user_id": "user_b"}))
    )

    assert resp.status_code == 200  # type: ignore[attr-defined]
    assert _balance(engine, "user_a") == 300  # refused → NO reset, zero write


def test_no_write_goes_through_the_admin_engine(
    webhook: tuple[TestClient, Engine, list[str]],
) -> None:
    """The admin engine does ONLY the read-only customer→user resolve — never a write."""
    client, engine, admin_sql = webhook
    _seed(engine, "user_a", _CUS_A, balance=300)
    admin_sql.clear()  # ignore any connection-setup statements before the request

    _post(client, _event("invoice.paid", _invoice()))

    writes = [s for s in admin_sql if s.strip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]
    assert writes == [], f"admin_engine must be read-only in the webhook path, saw: {writes}"
    assert any("SELECT" in s.upper() for s in admin_sql)  # it DID resolve (a read happened)


# --- PAYG lot grant on payment_intent.succeeded (T4a) -------------------------


def _payment_intent(
    *,
    pi_id: str = "pi_1",
    customer: str = _CUS_A,
    payg_credits: str | None = "500",
    meta_user: str = "user_a",
) -> dict[str, Any]:
    metadata: dict[str, str] = {"user_id": meta_user}
    if payg_credits is not None:
        metadata["payg_credits"] = payg_credits
    return {"id": pi_id, "object": "payment_intent", "customer": customer, "metadata": metadata}


def _payg(engine: Engine, uid: str) -> tuple[int, int]:
    """(number of lots, Σ credits_remaining) for a user."""
    with engine.begin() as conn:
        rows = (
            conn.execute(
                text("SELECT credits_remaining FROM payg_grants WHERE user_id = :u"), {"u": uid}
            )
            .scalars()
            .all()
        )
    return len(rows), sum(int(r) for r in rows)


def test_payment_intent_grants_a_payg_lot_leaving_allowance_untouched(
    webhook: tuple[TestClient, Engine, list[str]],
) -> None:
    client, engine, _ = webhook
    _seed(engine, "user_a", _CUS_A, balance=300)

    resp = _post(client, _event("payment_intent.succeeded", _payment_intent(payg_credits="500")))

    assert resp.status_code == 200  # type: ignore[attr-defined]
    assert _balance(engine, "user_a") == 300  # allowance untouched (PAYG is a separate bucket)
    lots, total = _payg(engine, "user_a")
    assert (lots, total) == (1, 500)  # one lot of 500 credits


def test_payment_intent_redelivered_5x_grants_one_lot(
    webhook: tuple[TestClient, Engine, list[str]],
) -> None:
    client, engine, _ = webhook
    _seed(engine, "user_a", _CUS_A, balance=300)
    payload = _event(
        "payment_intent.succeeded", _payment_intent(pi_id="pi_dup", payg_credits="1000")
    )

    for _ in range(5):
        assert _post(client, payload).status_code == 200  # type: ignore[attr-defined]

    lots, total = _payg(engine, "user_a")
    assert (lots, total) == (1, 1000)  # exactly ONE lot despite 5 deliveries


def test_payment_intent_unknown_customer_is_a_noop(
    webhook: tuple[TestClient, Engine, list[str]],
) -> None:
    client, engine, _ = webhook
    _seed(engine, "user_a", _CUS_A, balance=300)

    resp = _post(client, _event("payment_intent.succeeded", _payment_intent(customer="cus_X")))

    assert resp.status_code == 200  # type: ignore[attr-defined]
    assert _payg(engine, "user_a") == (0, 0)  # no lot granted


def test_payment_intent_without_payg_metadata_is_a_noop(
    webhook: tuple[TestClient, Engine, list[str]],
) -> None:
    """A subscription's own PI (no ``payg_credits`` metadata) does not grant a lot."""
    client, engine, _ = webhook
    _seed(engine, "user_a", _CUS_A, balance=300)

    resp = _post(client, _event("payment_intent.succeeded", _payment_intent(payg_credits=None)))

    assert resp.status_code == 200  # type: ignore[attr-defined]
    assert _payg(engine, "user_a") == (0, 0)

"""The auto-top-up toggle, and the concurrency guard on automatic spending (Spec M5, T4a).

B1 closes §1c.5: M4 shipped the auto-top-up ENGINE and the column it reads, but no way
to set it, so the engine faithfully checked a flag nothing could turn on.

The load-bearing property is that the ROUTE and the ENGINE agree about eligibility. They
share ``_is_auto_topup_eligible`` (D-M5-28), and these tests assert the agreement
behaviourally: whatever the route persists, the engine honours; whatever the route
refuses, the engine would have refused too. A route with its own copy of the rule could
arm a toggle the engine silently ignores for ever.

Auto-top-up is the first thing in this product that spends money with no human present,
so the concurrency case is proven rather than reasoned about.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.billing.autotopup import AutoTopupOutcome, maybe_auto_topup
from persona_api.config import APIConfig, Edition
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_DB = "postgresql+psycopg://super@localhost/persona_shell"
_APP_DB = "postgresql+psycopg://persona_app@localhost/persona_shell"
_UID = "u_m5_toggle"


class _FakeGateway:
    """Records off-session charges; no Stripe network."""

    publishable_key = "pk_test"

    def __init__(self) -> None:
        self.topups: list[dict[str, object]] = []

    def create_off_session_topup(
        self, *, customer_id: str, credit_amount: int, user_id: str, idempotency_key: str
    ) -> tuple[str, str]:
        self.topups.append(
            {
                "customer_id": customer_id,
                "credit_amount": credit_amount,
                "user_id": user_id,
                "idempotency_key": idempotency_key,
            }
        )
        # Stripe returns the SAME PaymentIntent for a repeated idempotency key, which is
        # what makes a concurrent double-trigger a single charge. Model that faithfully.
        return (f"pi_{idempotency_key}", "succeeded")


async def _verify(token: str) -> AuthenticatedUser:
    return AuthenticatedUser(id=token, email=None)


def _client(engine: Engine, gateway: _FakeGateway | None) -> TestClient:
    cfg = APIConfig(database_url=_DB, app_database_url=_APP_DB, edition=Edition.cloud)
    app = create_app(cfg)
    app.state.verify_token = _verify  # type: ignore[attr-defined]
    app.state.rls_engine = engine
    app.state.stripe_gateway = gateway
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:u, 't@x.test') ON CONFLICT DO NOTHING"),
            {"u": _UID},
        )
    return TestClient(app)


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {_UID}"}


def _seed_plan(
    engine: Engine, plan: str, *, status: str = "active", customer: str | None = None
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO subscription (user_id, plan_code, status, stripe_customer_id) "
                "VALUES (:u, :p, :s, :c) ON CONFLICT (user_id) DO UPDATE "
                "SET plan_code = :p, status = :s, stripe_customer_id = :c"
            ),
            {"u": _UID, "p": plan, "s": status, "c": customer},
        )


def _stored_flag(engine: Engine) -> bool:
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT auto_topup_enabled FROM subscription WHERE user_id = :u"), {"u": _UID}
        ).scalar_one_or_none()
    return bool(row)


# --- the toggle itself --------------------------------------------------------


def test_a_pro_user_can_arm_auto_topup(migrated_engine: Engine) -> None:
    client = _client(migrated_engine, _FakeGateway())
    _seed_plan(migrated_engine, "pro")

    resp = client.patch("/v1/me/billing/auto-topup", json={"enabled": True}, headers=_auth())
    assert resp.status_code == 200
    assert resp.json()["enabled"] is True
    assert _stored_flag(migrated_engine) is True


def test_the_response_states_what_it_armed(migrated_engine: Engine) -> None:
    """The UI must be able to say "at $2 we add $10" without hardcoding either number."""
    from persona_api.billing.autotopup import (
        AUTO_TOPUP_AMOUNT_CREDITS,
        AUTO_TOPUP_THRESHOLD_CREDITS,
    )

    client = _client(migrated_engine, _FakeGateway())
    _seed_plan(migrated_engine, "pro")
    body = client.patch("/v1/me/billing/auto-topup", json={"enabled": True}, headers=_auth()).json()
    assert body["threshold_credits"] == AUTO_TOPUP_THRESHOLD_CREDITS
    assert body["amount_credits"] == AUTO_TOPUP_AMOUNT_CREDITS


def test_a_free_user_cannot_arm_it(migrated_engine: Engine) -> None:
    """The built-but-unreachable failure, refused at the door instead of stored (D-M5-28)."""
    client = _client(migrated_engine, _FakeGateway())
    _seed_plan(migrated_engine, "free")

    resp = client.patch("/v1/me/billing/auto-topup", json={"enabled": True}, headers=_auth())
    assert resp.status_code == 409
    assert _stored_flag(migrated_engine) is False


def test_a_past_due_pro_user_cannot_arm_it(migrated_engine: Engine) -> None:
    """An unpaid subscription must not start automatic charges on the failed card."""
    client = _client(migrated_engine, _FakeGateway())
    _seed_plan(migrated_engine, "pro", status="past_due")

    resp = client.patch("/v1/me/billing/auto-topup", json={"enabled": True}, headers=_auth())
    assert resp.status_code == 409
    assert _stored_flag(migrated_engine) is False


def test_disarming_is_always_allowed(migrated_engine: Engine) -> None:
    """Even from an ineligible state: a user must be able to stop automatic spending.

    The alternative is a charge they cannot switch off, which is the worse failure.
    """
    client = _client(migrated_engine, _FakeGateway())
    _seed_plan(migrated_engine, "pro")
    client.patch("/v1/me/billing/auto-topup", json={"enabled": True}, headers=_auth())
    _seed_plan(migrated_engine, "pro", status="past_due")  # plan lapses while armed

    resp = client.patch("/v1/me/billing/auto-topup", json={"enabled": False}, headers=_auth())
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False
    assert _stored_flag(migrated_engine) is False


def test_community_has_no_toggle(migrated_engine: Engine) -> None:
    """No card, no charge, nothing to arm — the surface is absent, not merely disabled."""
    client = _client(migrated_engine, None)
    _seed_plan(migrated_engine, "pro")
    resp = client.patch("/v1/me/billing/auto-topup", json={"enabled": True}, headers=_auth())
    assert resp.status_code == 404


def test_the_toggle_rejects_smuggled_amounts(migrated_engine: Engine) -> None:
    """A client that could name its own top-up amount would be naming its own charge."""
    client = _client(migrated_engine, _FakeGateway())
    _seed_plan(migrated_engine, "pro")
    for body in (
        {"enabled": True, "amount_credits": 999_999},
        {"enabled": True, "threshold_credits": 100_000},
    ):
        assert (
            client.patch("/v1/me/billing/auto-topup", json=body, headers=_auth()).status_code == 422
        )


# --- route and engine agree (the real-trigger rule) ---------------------------


def test_what_the_route_arms_the_engine_honours(migrated_engine: Engine) -> None:
    """Drive the REAL endpoint, then drive the REAL engine — never a hand-set column.

    This is the assertion that makes B1 more than a column write: the flag the route
    persisted is the flag the engine acts on.
    """
    gateway = _FakeGateway()
    client = _client(migrated_engine, gateway)
    _seed_plan(migrated_engine, "pro", customer="cus_toggle")

    client.patch("/v1/me/billing/auto-topup", json={"enabled": True}, headers=_auth())

    outcome = maybe_auto_topup(
        rls_engine=migrated_engine,
        gateway=gateway,  # type: ignore[arg-type]
        user_id=_UID,
        old_balance=250,
        new_balance=150,  # a genuine crossing of the $2 line
    )
    assert outcome is AutoTopupOutcome.CHARGED
    assert len(gateway.topups) == 1


def test_what_the_route_disarms_the_engine_refuses(migrated_engine: Engine) -> None:
    gateway = _FakeGateway()
    client = _client(migrated_engine, gateway)
    _seed_plan(migrated_engine, "pro", customer="cus_toggle")

    client.patch("/v1/me/billing/auto-topup", json={"enabled": True}, headers=_auth())
    client.patch("/v1/me/billing/auto-topup", json={"enabled": False}, headers=_auth())

    outcome = maybe_auto_topup(
        rls_engine=migrated_engine,
        gateway=gateway,  # type: ignore[arg-type]
        user_id=_UID,
        old_balance=250,
        new_balance=150,
    )
    assert outcome is AutoTopupOutcome.NOT_ELIGIBLE
    assert gateway.topups == []


# --- concurrency: the threshold cannot double-charge --------------------------


def test_concurrent_triggers_produce_one_charge(migrated_engine: Engine) -> None:
    """Two surfaces crossing the threshold at once must not charge the card twice.

    Chat, voice and a background task can all deduct within the same instant, and each
    fires its own trigger. The guard is the hourly outbound idempotency key: Stripe
    returns the SAME PaymentIntent for a repeated key, so concurrent triggers collapse
    to one charge, and the webhook then grants that PI's lot exactly once.

    Asserted with real threads rather than sequential calls, because the failure being
    excluded is a race.
    """
    gateway = _FakeGateway()
    client = _client(migrated_engine, gateway)
    _seed_plan(migrated_engine, "pro", customer="cus_toggle")
    client.patch("/v1/me/billing/auto-topup", json={"enabled": True}, headers=_auth())

    now = datetime.now(UTC)

    def _fire() -> AutoTopupOutcome:
        return maybe_auto_topup(
            rls_engine=migrated_engine,
            gateway=gateway,  # type: ignore[arg-type]
            user_id=_UID,
            old_balance=250,
            new_balance=150,
            now=now,  # the same instant: the worst case for the hourly key
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = [f.result() for f in [pool.submit(_fire) for _ in range(4)]]

    assert all(o is AutoTopupOutcome.CHARGED for o in outcomes)
    # Four triggers, but ONE payment: every call reused the same idempotency key, so
    # Stripe returns one PaymentIntent and the webhook grants one lot.
    assert len({str(t["idempotency_key"]) for t in gateway.topups}) == 1
    assert len({f"pi_{t['idempotency_key']}" for t in gateway.topups}) == 1


def test_a_second_crossing_in_the_same_hour_does_not_double_charge(
    migrated_engine: Engine,
) -> None:
    """A rare same-hour second crossing reuses the PI rather than charging again."""
    gateway = _FakeGateway()
    client = _client(migrated_engine, gateway)
    _seed_plan(migrated_engine, "pro", customer="cus_toggle")
    client.patch("/v1/me/billing/auto-topup", json={"enabled": True}, headers=_auth())

    now = datetime.now(UTC)
    for _ in range(3):
        maybe_auto_topup(
            rls_engine=migrated_engine,
            gateway=gateway,  # type: ignore[arg-type]
            user_id=_UID,
            old_balance=250,
            new_balance=150,
            now=now,
        )

    assert len({str(t["idempotency_key"]) for t in gateway.topups}) == 1


# --- failure degrades honestly, never retries for ever ------------------------


class _DecliningGateway(_FakeGateway):
    """The card is declined / the network is down on every attempt."""

    def create_off_session_topup(
        self, *, customer_id: str, credit_amount: int, user_id: str, idempotency_key: str
    ) -> tuple[str, str]:
        self.topups.append(
            {
                "customer_id": customer_id,
                "credit_amount": credit_amount,
                "user_id": user_id,
                "idempotency_key": idempotency_key,
            }
        )
        raise RuntimeError("card_declined")


class _ThreeDSGateway(_FakeGateway):
    """The card needs 3DS, which an off-session charge cannot complete."""

    def create_off_session_topup(
        self, *, customer_id: str, credit_amount: int, user_id: str, idempotency_key: str
    ) -> tuple[str, str]:
        self.topups.append(
            {
                "customer_id": customer_id,
                "credit_amount": credit_amount,
                "user_id": user_id,
                "idempotency_key": idempotency_key,
            }
        )
        exc = RuntimeError("authentication required")
        exc.code = "authentication_required"  # type: ignore[attr-defined]
        raise exc


def test_a_declined_card_fails_soft_and_does_not_retry(migrated_engine: Engine) -> None:
    """One attempt, an ERROR outcome, no exception into the caller's turn.

    The failure this excludes is a retry loop hammering a declined card: the trigger
    fires once per crossing, reports what happened, and stops.
    """
    gateway = _DecliningGateway()
    client = _client(migrated_engine, gateway)
    _seed_plan(migrated_engine, "pro", customer="cus_toggle")
    client.patch("/v1/me/billing/auto-topup", json={"enabled": True}, headers=_auth())

    outcome = maybe_auto_topup(
        rls_engine=migrated_engine,
        gateway=gateway,  # type: ignore[arg-type]
        user_id=_UID,
        old_balance=250,
        new_balance=150,
    )
    assert outcome is AutoTopupOutcome.ERROR
    assert len(gateway.topups) == 1  # attempted ONCE, not retried


def test_a_card_needing_3ds_asks_for_a_human_instead_of_retrying(
    migrated_engine: Engine,
) -> None:
    """3DS cannot be completed off-session, so the honest answer is REQUIRES_ACTION.

    T4b turns this into an on-session prompt. Retrying would fail identically for ever.
    """
    gateway = _ThreeDSGateway()
    client = _client(migrated_engine, gateway)
    _seed_plan(migrated_engine, "pro", customer="cus_toggle")
    client.patch("/v1/me/billing/auto-topup", json={"enabled": True}, headers=_auth())

    outcome = maybe_auto_topup(
        rls_engine=migrated_engine,
        gateway=gateway,  # type: ignore[arg-type]
        user_id=_UID,
        old_balance=250,
        new_balance=150,
    )
    assert outcome is AutoTopupOutcome.REQUIRES_ACTION
    assert len(gateway.topups) == 1


def test_an_armed_user_with_no_saved_card_reports_no_customer(migrated_engine: Engine) -> None:
    """Opted in but never checked out: prompt on-session rather than fail silently."""
    gateway = _FakeGateway()
    client = _client(migrated_engine, gateway)
    _seed_plan(migrated_engine, "pro", customer=None)
    client.patch("/v1/me/billing/auto-topup", json={"enabled": True}, headers=_auth())

    outcome = maybe_auto_topup(
        rls_engine=migrated_engine,
        gateway=gateway,  # type: ignore[arg-type]
        user_id=_UID,
        old_balance=250,
        new_balance=150,
    )
    assert outcome is AutoTopupOutcome.NO_CUSTOMER
    assert gateway.topups == []

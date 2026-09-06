"""An auto-top-up that needs a human actually reaches one (Spec M5, T4b — B2).

§1c.6: ``maybe_auto_topup`` already computed ``REQUIRES_ACTION`` and ``NO_CUSTOMER``
and then returned them into the void. The caller logged the value and dropped it, so
the two outcomes that REQUIRE the cardholder reached nobody: the charge never
completed, the balance ran out, and the app never said why.

These tests drive the REAL path — the trigger runs, and the assertion reads the
``notifications`` table the bell actually renders from. Asserting on the function's
return value would prove the enum is computed, which was never the broken part.

Transport is notifications, not the live event stream (D-M5-26): the trigger fires from
a detached background thread after a turn, when the user may have no stream open. A
dropped message there is exactly the user whose card silently stopped working.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from persona_api.billing.autotopup import AutoTopupOutcome, maybe_auto_topup
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_UID = "u_m5_notify"


class _Gateway:
    """A saved card that charges cleanly."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def _record(
        self, customer_id: str, credit_amount: int, user_id: str, idempotency_key: str
    ) -> None:
        """Record every argument, so the stubs use what the real gateway is given."""
        self.calls.append(
            {
                "customer_id": customer_id,
                "credit_amount": credit_amount,
                "user_id": user_id,
                "idempotency_key": idempotency_key,
            }
        )

    def create_off_session_topup(
        self, *, customer_id: str, credit_amount: int, user_id: str, idempotency_key: str
    ) -> tuple[str, str]:
        self._record(customer_id, credit_amount, user_id, idempotency_key)
        return (f"pi_{idempotency_key}", "succeeded")


class _ThreeDSGateway(_Gateway):
    """The card needs 3DS, which an off-session charge cannot complete."""

    def create_off_session_topup(
        self, *, customer_id: str, credit_amount: int, user_id: str, idempotency_key: str
    ) -> tuple[str, str]:
        self._record(customer_id, credit_amount, user_id, idempotency_key)
        exc = RuntimeError("authentication required")
        exc.code = "authentication_required"  # type: ignore[attr-defined]
        raise exc


class _DecliningGateway(_Gateway):
    """A plain decline: transient, retried by the next crossing, not the user's problem."""

    def create_off_session_topup(
        self, *, customer_id: str, credit_amount: int, user_id: str, idempotency_key: str
    ) -> tuple[str, str]:
        self._record(customer_id, credit_amount, user_id, idempotency_key)
        raise RuntimeError("card_declined")


def _seed(engine: Engine, *, customer: str | None, armed: bool = True) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:u, 'n@x.test') ON CONFLICT DO NOTHING"),
            {"u": _UID},
        )
        conn.execute(
            text(
                "INSERT INTO subscription "
                "(user_id, plan_code, status, stripe_customer_id, auto_topup_enabled) "
                "VALUES (:u, 'pro', 'active', :c, :a) ON CONFLICT (user_id) DO UPDATE "
                "SET plan_code='pro', status='active', stripe_customer_id=:c, "
                "auto_topup_enabled=:a"
            ),
            {"u": _UID, "c": customer, "a": armed},
        )


def _bell(engine: Engine) -> list[dict[str, object]]:
    """What the bell would render: the real notifications rows for this user."""
    with engine.begin() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT kind, level, message_key, ref_id FROM notifications "
                    "WHERE owner_id = :u ORDER BY created_at"
                ),
                {"u": _UID},
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


def _fire(engine: Engine, gateway: object, *, now: datetime | None = None) -> AutoTopupOutcome:
    """A genuine crossing of the $2 line."""
    return maybe_auto_topup(
        rls_engine=engine,
        gateway=gateway,  # type: ignore[arg-type]
        user_id=_UID,
        old_balance=250,
        new_balance=150,
        now=now,
    )


# --- the outcome that most needs a human --------------------------------------


def test_a_card_needing_3ds_reaches_the_user(migrated_engine: Engine) -> None:
    """The load-bearing case. Off-session cannot complete 3DS, so only the cardholder can.

    Silence here means a top-up that never lands and a balance that runs out with no
    explanation, which is the §1c.6 failure in full.
    """
    _seed(migrated_engine, customer="cus_notify")
    outcome = _fire(migrated_engine, _ThreeDSGateway())

    assert outcome is AutoTopupOutcome.REQUIRES_ACTION
    bell = _bell(migrated_engine)
    assert len(bell) == 1
    assert bell[0]["kind"] == "auto_topup"
    assert bell[0]["message_key"] == "notifications.autoTopup.requiresAction"
    assert bell[0]["level"] == "warning"


def test_an_armed_user_with_no_card_reaches_the_user(migrated_engine: Engine) -> None:
    """Opted in and nothing can happen: the user asked for this, so they are told."""
    _seed(migrated_engine, customer=None)
    outcome = _fire(migrated_engine, _Gateway())

    assert outcome is AutoTopupOutcome.NO_CUSTOMER
    bell = _bell(migrated_engine)
    assert len(bell) == 1
    assert bell[0]["message_key"] == "notifications.autoTopup.noCustomer"


# --- what must NOT notify -----------------------------------------------------


def test_a_successful_top_up_is_silent(migrated_engine: Engine) -> None:
    """The credits simply appear.

    A bell entry for every successful top-up trains people to ignore the bell, and the
    one that matters then arrives into a muted channel.
    """
    _seed(migrated_engine, customer="cus_notify")
    assert _fire(migrated_engine, _Gateway()) is AutoTopupOutcome.CHARGED
    assert _bell(migrated_engine) == []


def test_a_transient_decline_does_not_alarm_the_user(migrated_engine: Engine) -> None:
    """ERROR is a provider hiccup the next crossing retries; nothing for a user to do."""
    _seed(migrated_engine, customer="cus_notify")
    assert _fire(migrated_engine, _DecliningGateway()) is AutoTopupOutcome.ERROR
    assert _bell(migrated_engine) == []


def test_a_non_crossing_deduct_notifies_nothing(migrated_engine: Engine) -> None:
    """Most deducts are not crossings. They must not touch the bell at all."""
    _seed(migrated_engine, customer="cus_notify")
    outcome = maybe_auto_topup(
        rls_engine=migrated_engine,
        gateway=_Gateway(),  # type: ignore[arg-type]
        user_id=_UID,
        old_balance=150,
        new_balance=100,  # already below the line: not a crossing
    )
    assert outcome is AutoTopupOutcome.NOT_CROSSED
    assert _bell(migrated_engine) == []


def test_an_opted_out_user_is_never_notified(migrated_engine: Engine) -> None:
    """Someone who did not ask for automatic spending hears nothing about it."""
    _seed(migrated_engine, customer="cus_notify", armed=False)
    assert _fire(migrated_engine, _ThreeDSGateway()) is AutoTopupOutcome.NOT_ELIGIBLE
    assert _bell(migrated_engine) == []


# --- repeated triggers must not spam the bell ---------------------------------


def test_repeated_triggers_in_one_hour_produce_one_bell_entry(
    migrated_engine: Engine,
) -> None:
    """Several surfaces can cross at once, and each fires its own trigger.

    The ref_id is the SAME hourly key the outbound Stripe call uses, so one crossing
    episode is one bell entry no matter how many triggers observed it. Without this the
    user gets a burst of identical warnings for a single problem.
    """
    _seed(migrated_engine, customer="cus_notify")
    now = datetime.now(UTC)
    for _ in range(4):
        assert _fire(migrated_engine, _ThreeDSGateway(), now=now) is (
            AutoTopupOutcome.REQUIRES_ACTION
        )

    assert len(_bell(migrated_engine)) == 1


def test_a_later_episode_notifies_again(migrated_engine: Engine) -> None:
    """Dedup must not silence a genuinely NEW problem.

    A failure an hour later is a second episode the user has not been told about, so
    over-broad dedup would hide it. This is the mirror of the double-charge guard: the
    expensive direction there is charging twice, here it is warning never.
    """
    _seed(migrated_engine, customer="cus_notify")
    now = datetime.now(UTC)
    _fire(migrated_engine, _ThreeDSGateway(), now=now)
    _fire(migrated_engine, _ThreeDSGateway(), now=now + timedelta(hours=2))

    bell = _bell(migrated_engine)
    assert len(bell) == 2
    assert bell[0]["ref_id"] != bell[1]["ref_id"]


def test_the_bell_write_never_breaks_the_turn(migrated_engine: Engine) -> None:
    """Billing is enrichment; the conversation is the work.

    A feed-write failure must be swallowed, and the trigger must still report what
    happened, so the caller's completed turn is never broken by a bell problem.
    """
    _seed(migrated_engine, customer="cus_notify")
    with migrated_engine.begin() as conn:
        conn.execute(text("ALTER TABLE notifications RENAME TO notifications_hidden"))
    try:
        outcome = _fire(migrated_engine, _ThreeDSGateway())
        assert outcome is AutoTopupOutcome.REQUIRES_ACTION  # reported despite the failure
    finally:
        with migrated_engine.begin() as conn:
            conn.execute(text("ALTER TABLE notifications_hidden RENAME TO notifications"))

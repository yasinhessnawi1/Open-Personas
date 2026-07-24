"""Pro opt-in auto-top-up — the adversarial gate (Spec M4, T7b).

``maybe_auto_topup`` fires ONE off-session $10 charge when a Pro, opted-in user's balance
CROSSES below $2 — and NEVER for anyone else. The grant is not done here: the off-session
PaymentIntent carries ``metadata.payg_credits`` and the existing ``payment_intent.succeeded``
webhook grants the PAYG lot idempotent on the PI id (proven in T4a). These tests assert the
CONCRETE decision + the Stripe call (mocked — NO real charges):

- crossing (Pro, opted-in) → exactly ONE ``create_off_session_topup`` call → CHARGED;
- a retried trigger for the SAME crossing → the SAME hourly ``idempotency_key`` (Stripe then
  returns the same PaymentIntent ⇒ one charge; the grant is PI-idempotent on top);
- non-Pro (free / plus) → NEVER calls Stripe (NOT_ELIGIBLE);
- Pro opted-OUT → NEVER calls Stripe (NOT_ELIGIBLE);
- 3DS ``authentication_required`` → REQUIRES_ACTION, no crash, no raise;
- not a crossing / no saved card / community (no gateway) → no Stripe call;
- the gateway builds an off-session, confirmed PI for $10 with the grant metadata + the key.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from persona_api.billing.autotopup import (
    AUTO_TOPUP_AMOUNT_CREDITS,
    AutoTopupOutcome,
    maybe_auto_topup,
)
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_FIXED_NOW = datetime(2026, 7, 24, 15, 30, tzinfo=UTC)  # → hourly bucket 2026-07-24-15


class _CardAuthRequiredError(Exception):
    """Mimics ``stripe.CardError`` for a 3DS-required off-session charge (has ``.code``)."""

    code = "authentication_required"


class _FakeGateway:
    """Records ``create_off_session_topup`` calls; configurable to raise 3DS / a generic error."""

    def __init__(self, *, raise_3ds: bool = False, raise_error: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self._raise_3ds = raise_3ds
        self._raise_error = raise_error

    def create_off_session_topup(
        self, *, customer_id: str, credit_amount: int, user_id: str, idempotency_key: str
    ) -> tuple[str, str]:
        self.calls.append(
            {
                "customer_id": customer_id,
                "credit_amount": credit_amount,
                "user_id": user_id,
                "idempotency_key": idempotency_key,
            }
        )
        if self._raise_3ds:
            raise _CardAuthRequiredError
        if self._raise_error:
            msg = "network blip"
            raise RuntimeError(msg)
        return f"pi_{len(self.calls)}", "succeeded"


def _seed(
    engine: Engine,
    uid: str,
    *,
    plan_code: str = "pro",
    enabled: bool = True,
    customer: str | None = "cus_test",
    status: str = "active",
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:u, :e) ON CONFLICT DO NOTHING"),
            {"u": uid, "e": f"{uid}@x.test"},
        )
        conn.execute(
            text(
                "INSERT INTO subscription "
                "(user_id, plan_code, status, stripe_customer_id, auto_topup_enabled) "
                "VALUES (:u, :p, :s, :c, :a) "
                "ON CONFLICT (user_id) DO UPDATE SET plan_code = :p, status = :s, "
                "stripe_customer_id = :c, auto_topup_enabled = :a"
            ),
            {"u": uid, "p": plan_code, "s": status, "c": customer, "a": enabled},
        )


def _fire(
    engine: Engine, gw: object, uid: str, *, old: int = 250, new: int = 50
) -> AutoTopupOutcome:
    return maybe_auto_topup(
        rls_engine=engine,
        gateway=gw,  # type: ignore[arg-type] — the fake mirrors the StripeGateway method
        user_id=uid,
        old_balance=old,
        new_balance=new,
        now=_FIXED_NOW,
    )


# --- Fires exactly once for a Pro, opted-in crossing --------------------------


def test_pro_opted_in_crossing_charges_once(migrated_engine: Engine) -> None:
    uid = "u_t7b_pro_on"
    _seed(migrated_engine, uid, plan_code="pro", enabled=True)
    gw = _FakeGateway()

    outcome = _fire(migrated_engine, gw, uid)

    assert outcome is AutoTopupOutcome.CHARGED
    assert len(gw.calls) == 1  # exactly ONE off-session charge
    call = gw.calls[0]
    assert call["credit_amount"] == AUTO_TOPUP_AMOUNT_CREDITS == 1000  # $10
    assert call["customer_id"] == "cus_test"
    assert call["idempotency_key"] == f"autotopup:{uid}:2026-07-24-15"  # hourly bucket


def test_retried_trigger_same_crossing_reuses_idempotency_key(migrated_engine: Engine) -> None:
    """Retry/re-delivery of the SAME crossing → the SAME hourly idempotency_key, so Stripe
    returns the SAME PaymentIntent (one charge). The grant is PI-idempotent on top (T4a)."""
    uid = "u_t7b_retry"
    _seed(migrated_engine, uid, plan_code="pro", enabled=True)
    gw = _FakeGateway()

    first = _fire(migrated_engine, gw, uid)
    second = _fire(migrated_engine, gw, uid)  # a retried trigger for the same low-balance episode

    assert first is AutoTopupOutcome.CHARGED
    assert second is AutoTopupOutcome.CHARGED
    assert len(gw.calls) == 2
    # The load-bearing anti-double-charge contract: identical key ⇒ Stripe dedupes to one PI.
    assert gw.calls[0]["idempotency_key"] == gw.calls[1]["idempotency_key"]


# --- NEVER fires for the ineligible (no Stripe call at all) --------------------


def test_non_pro_never_fires(migrated_engine: Engine) -> None:
    for plan in ("free", "plus"):
        uid = f"u_t7b_{plan}"
        _seed(migrated_engine, uid, plan_code=plan, enabled=True)  # even "opted in", non-Pro
        gw = _FakeGateway()

        outcome = _fire(migrated_engine, gw, uid)

        assert outcome is AutoTopupOutcome.NOT_ELIGIBLE, plan
        assert gw.calls == [], f"{plan} must NEVER reach Stripe"


def test_pro_opted_out_never_fires(migrated_engine: Engine) -> None:
    uid = "u_t7b_pro_off"
    _seed(migrated_engine, uid, plan_code="pro", enabled=False)  # Pro but opted OUT
    gw = _FakeGateway()

    outcome = _fire(migrated_engine, gw, uid)

    assert outcome is AutoTopupOutcome.NOT_ELIGIBLE
    assert gw.calls == [], "an opted-out Pro user must NEVER reach Stripe"


def test_inactive_pro_never_fires(migrated_engine: Engine) -> None:
    uid = "u_t7b_pro_pastdue"
    _seed(migrated_engine, uid, plan_code="pro", enabled=True, status="past_due")
    gw = _FakeGateway()

    assert _fire(migrated_engine, gw, uid) is AutoTopupOutcome.NOT_ELIGIBLE
    assert gw.calls == []


# --- Crossing guard: only the actual crossing fires ---------------------------


def test_no_crossing_when_already_below(migrated_engine: Engine) -> None:
    """Already below $2 (old < 200) → NOT a crossing → no-op, no Stripe (fires once per episode)."""
    uid = "u_t7b_already_low"
    _seed(migrated_engine, uid, plan_code="pro", enabled=True)
    gw = _FakeGateway()

    outcome = _fire(migrated_engine, gw, uid, old=150, new=40)  # was already < 200

    assert outcome is AutoTopupOutcome.NOT_CROSSED
    assert gw.calls == []


def test_no_crossing_when_still_above(migrated_engine: Engine) -> None:
    uid = "u_t7b_still_ok"
    _seed(migrated_engine, uid, plan_code="pro", enabled=True)
    gw = _FakeGateway()

    outcome = _fire(migrated_engine, gw, uid, old=500, new=250)  # still >= 200 after the deduct

    assert outcome is AutoTopupOutcome.NOT_CROSSED
    assert gw.calls == []


# --- 3DS + no-card + community fallbacks (graceful, no crash) ------------------


def test_3ds_required_falls_back_on_session(migrated_engine: Engine) -> None:
    uid = "u_t7b_3ds"
    _seed(migrated_engine, uid, plan_code="pro", enabled=True)
    gw = _FakeGateway(raise_3ds=True)

    outcome = _fire(migrated_engine, gw, uid)  # must NOT raise

    assert outcome is AutoTopupOutcome.REQUIRES_ACTION  # → web surfaces an on-session prompt
    assert len(gw.calls) == 1


def test_generic_stripe_error_is_swallowed(migrated_engine: Engine) -> None:
    """A network/Stripe error must be logged + swallowed — never break the completed turn."""
    uid = "u_t7b_err"
    _seed(migrated_engine, uid, plan_code="pro", enabled=True)
    gw = _FakeGateway(raise_error=True)

    outcome = _fire(migrated_engine, gw, uid)  # must NOT raise

    assert outcome is AutoTopupOutcome.ERROR


def test_pro_opted_in_no_saved_card_prompts_on_session(migrated_engine: Engine) -> None:
    uid = "u_t7b_nocard"
    _seed(migrated_engine, uid, plan_code="pro", enabled=True, customer=None)
    gw = _FakeGateway()

    outcome = _fire(migrated_engine, gw, uid)

    assert outcome is AutoTopupOutcome.NO_CUSTOMER
    assert gw.calls == [], "no saved card → no off-session charge attempt"


def test_community_no_gateway_is_disabled(migrated_engine: Engine) -> None:
    uid = "u_t7b_community"
    _seed(migrated_engine, uid, plan_code="pro", enabled=True)

    outcome = maybe_auto_topup(
        rls_engine=migrated_engine,
        gateway=None,  # community / flag-off
        user_id=uid,
        old_balance=250,
        new_balance=50,
        now=_FIXED_NOW,
    )

    assert outcome is AutoTopupOutcome.DISABLED


def test_absent_subscription_row_never_fires(migrated_engine: Engine) -> None:
    """No subscription row = free (T5c) → never eligible, never Stripe."""
    uid = "u_t7b_norow"
    with migrated_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:u, :e) ON CONFLICT DO NOTHING"),
            {"u": uid, "e": f"{uid}@x.test"},
        )
    gw = _FakeGateway()

    assert _fire(migrated_engine, gw, uid) is AutoTopupOutcome.NOT_ELIGIBLE
    assert gw.calls == []


# --- The gateway builds the correct off-session Stripe request -----------------


class _FakePI:
    def __init__(self, pid: str, status: str) -> None:
        self.id = pid
        self.status = status


class _RecordingPaymentIntents:
    def __init__(self) -> None:
        self.params: dict[str, Any] | None = None
        self.options: dict[str, Any] | None = None

    def create(self, *, params: dict[str, Any], options: dict[str, Any]) -> _FakePI:
        self.params = params
        self.options = options
        return _FakePI("pi_live_1", "succeeded")


class _FakeStripeClient:
    def __init__(self, recorder: _RecordingPaymentIntents) -> None:
        self.payment_intents = recorder


def test_gateway_builds_off_session_confirmed_ten_dollar_pi() -> None:
    """The gateway sends an off-session, confirmed $10 PI whose metadata carries the grant
    (payg_credits) + user_id, keyed by the idempotency_key — so the existing
    payment_intent.succeeded webhook grants 1000 credits idempotent on the PI id."""
    from persona_api.billing.gateway import StripeGateway

    gw = StripeGateway(
        secret_key="sk_test_dummy", publishable_key="pk_test", webhook_secret="whsec"
    )
    recorder = _RecordingPaymentIntents()
    gw._client = _FakeStripeClient(recorder)  # type: ignore[assignment]  # swap the whole client

    pi_id, status = gw.create_off_session_topup(
        customer_id="cus_x",
        credit_amount=AUTO_TOPUP_AMOUNT_CREDITS,
        user_id="u_gw",
        idempotency_key="autotopup:u_gw:2026-07-24-15",
    )

    assert (pi_id, status) == ("pi_live_1", "succeeded")
    assert recorder.params is not None
    p = recorder.params
    assert p["amount"] == 1000  # $10 in cents (1 credit = 1¢)
    assert p["currency"] == "usd"
    assert p["customer"] == "cus_x"
    assert p["off_session"] is True
    assert p["confirm"] is True
    assert p["metadata"]["payg_credits"] == "1000"  # the webhook grants exactly this
    assert p["metadata"]["user_id"] == "u_gw"
    assert recorder.options == {"idempotency_key": "autotopup:u_gw:2026-07-24-15"}

"""StripeGateway checkout / portal / customer param wiring (Spec M4, T2b).

No network: the underlying ``StripeClient`` is replaced with a mock so we can assert
the EXACT params the gateway sends — Stripe Tax on, subscription mode, the price/customer,
and the customer-create idempotency key. (The route-level behaviour is covered by the
integration test with a fake gateway.)
"""

from __future__ import annotations

from unittest.mock import MagicMock

from persona_api.billing import StripeGateway


def _gateway_with_mock_client() -> StripeGateway:
    gateway = StripeGateway(
        secret_key="sk_test_x", publishable_key="pk_test_x", webhook_secret="wh"
    )
    gateway._client = MagicMock()  # noqa: SLF001 — inject a mock so no network fires
    return gateway


def test_subscription_checkout_enables_stripe_tax_in_subscription_mode() -> None:
    gateway = _gateway_with_mock_client()
    session = MagicMock()
    session.url = "https://checkout.stripe/session_1"
    gateway._client.checkout.sessions.create.return_value = session  # noqa: SLF001

    url = gateway.create_subscription_checkout(
        customer_id="cus_1",
        price_id="price_plus",
        success_url="s_url",
        cancel_url="c_url",
        user_id="u1",
    )

    assert url == "https://checkout.stripe/session_1"
    params = gateway._client.checkout.sessions.create.call_args.kwargs["params"]  # noqa: SLF001
    assert params["mode"] == "subscription"
    assert params["automatic_tax"] == {"enabled": True}  # Stripe Tax on (D-M4-3)
    assert params["customer"] == "cus_1"
    assert params["line_items"] == [{"price": "price_plus", "quantity": 1}]
    assert params["success_url"] == "s_url"
    assert params["cancel_url"] == "c_url"
    # T3b tripwire: user_id stamped on the session AND the subscription metadata.
    assert params["metadata"] == {"user_id": "u1"}
    assert params["subscription_data"]["metadata"] == {"user_id": "u1"}


def test_create_customer_is_idempotent_on_the_user() -> None:
    gateway = _gateway_with_mock_client()
    customer = MagicMock()
    customer.id = "cus_9"
    gateway._client.customers.create.return_value = customer  # noqa: SLF001

    customer_id = gateway.create_customer(user_id="u9", email="u9@x.test")

    assert customer_id == "cus_9"
    call = gateway._client.customers.create.call_args.kwargs  # noqa: SLF001
    assert call["options"]["idempotency_key"] == "customer:u9"  # no duplicate customers
    assert call["params"]["metadata"] == {"user_id": "u9"}
    assert call["params"]["email"] == "u9@x.test"


def test_payg_checkout_is_payment_mode_with_saved_card_and_stamped_credits() -> None:
    gateway = _gateway_with_mock_client()
    session = MagicMock()
    session.url = "https://checkout.stripe/payg_1"
    gateway._client.checkout.sessions.create.return_value = session  # noqa: SLF001

    url = gateway.create_payg_checkout(
        customer_id="cus_1",
        price_id="price_pack_5",
        credit_amount=500,
        user_id="u1",
        success_url="s_url",
        cancel_url="c_url",
    )

    assert url == "https://checkout.stripe/payg_1"
    params = gateway._client.checkout.sessions.create.call_args.kwargs["params"]  # noqa: SLF001
    assert params["mode"] == "payment"  # one-time (not subscription)
    assert params["automatic_tax"] == {"enabled": True}
    assert params["line_items"] == [{"price": "price_pack_5", "quantity": 1}]
    # setup_future_usage is VALID in payment mode → the card is saved (Pro auto-top-up).
    assert params["payment_intent_data"]["setup_future_usage"] == "off_session"
    # The exact (tax-free) credits + user are stamped on session AND payment_intent.
    assert params["metadata"] == {"user_id": "u1", "payg_credits": "500"}
    assert params["payment_intent_data"]["metadata"] == {"user_id": "u1", "payg_credits": "500"}


def test_portal_session_returns_url_with_customer_and_return_url() -> None:
    gateway = _gateway_with_mock_client()
    session = MagicMock()
    session.url = "https://portal.stripe/session_1"
    gateway._client.billing_portal.sessions.create.return_value = session  # noqa: SLF001

    url = gateway.create_portal_session(customer_id="cus_1", return_url="r_url")

    assert url == "https://portal.stripe/session_1"
    params = gateway._client.billing_portal.sessions.create.call_args.kwargs["params"]  # noqa: SLF001
    assert params == {"customer": "cus_1", "return_url": "r_url"}

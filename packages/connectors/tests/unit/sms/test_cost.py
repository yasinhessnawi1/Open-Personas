"""SMS per-segment cost (Spec C4 T12) — the single-ingestion-path payoff (criterion 9).

The Phase-2 single-handler decision cashes in here: SMS cost reads the **same**
``parse_status_callback`` T9 built for window-rejection ingestion — ONE seam, both
channels, not a parallel copy. The proof it is the shared function: it honours the
legacy ``SmsSid``/``SmsStatus`` aliases that only ``parse_status_callback`` knows.

The price is in CENTS here and everywhere downstream. It used to be a unit-free
``price_per_segment`` that the one test below seeded with a DOLLAR figure while the
billing seam it now feeds counts in cents, which is the shape of the money bug the
2026-09-14 audit found: every number right, only the word wrong.
"""

from __future__ import annotations

from persona_connectors.sms.cost import cost_from_status_callback


def test_cost_reads_num_segments_via_the_shared_parser() -> None:
    cost = cost_from_status_callback(
        {"MessageSid": "SM1", "MessageStatus": "delivered", "NumSegments": "3"}
    )
    assert cost.message_sid == "SM1"
    assert cost.segments == 3
    assert cost.cost_cents is None  # no price configured → segment count only


def test_cost_computes_price_per_segment_when_configured() -> None:
    cost = cost_from_status_callback(
        {"MessageSid": "SM1", "MessageStatus": "delivered", "NumSegments": "3"},
        price_per_segment_cents=0.83,
    )
    assert cost.segments == 3
    assert cost.cost_cents is not None
    assert abs(cost.cost_cents - 2.49) < 1e-9  # 3 × 0.83¢ (US long-code outbound)


def test_cost_missing_segments_is_zero_not_a_crash() -> None:
    cost = cost_from_status_callback({"MessageSid": "SM1", "MessageStatus": "queued"})
    assert cost.segments == 0


def test_cost_honours_the_sms_sid_alias_proving_shared_parser_reuse() -> None:
    # only parse_status_callback (T9) knows the legacy SmsSid/SmsStatus aliases — if the
    # cost path resolves them, it is reusing that one seam, not a parallel copy.
    cost = cost_from_status_callback(
        {"SmsSid": "SM9", "SmsStatus": "delivered", "NumSegments": "2"}
    )
    assert cost.message_sid == "SM9"
    assert cost.segments == 2


# ----- which callback is the one that costs money -------------------------------------


def test_a_sent_callback_is_billable() -> None:
    """Twilio charges when the message goes to the carrier, which is ``sent``."""
    cost = cost_from_status_callback(
        {"MessageSid": "SM1", "MessageStatus": "sent", "NumSegments": "2"}
    )
    assert cost.billable is True


def test_a_queued_callback_is_not_yet_a_cost() -> None:
    """It arrives FIRST and already carries NumSegments; billing on it would charge for a
    message that may still be rejected before it ever reaches a carrier."""
    cost = cost_from_status_callback(
        {"MessageSid": "SM1", "MessageStatus": "queued", "NumSegments": "2"}
    )
    assert cost.segments == 2
    assert cost.billable is False


def test_a_failed_send_is_never_billable() -> None:
    cost = cost_from_status_callback(
        {"MessageSid": "SM1", "MessageStatus": "failed", "NumSegments": "2", "ErrorCode": "30008"}
    )
    assert cost.billable is False


def test_a_delivered_callback_with_no_segments_is_not_billable() -> None:
    """Nothing to multiply; a cost of zero segments is not a cost."""
    cost = cost_from_status_callback({"MessageSid": "SM1", "MessageStatus": "delivered"})
    assert cost.billable is False

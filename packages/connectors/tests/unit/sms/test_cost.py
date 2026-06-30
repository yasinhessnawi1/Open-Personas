"""SMS per-segment cost (Spec C4 T12) — the single-ingestion-path payoff (criterion 9).

The Phase-2 single-handler decision cashes in here: SMS cost reads the **same**
``parse_status_callback`` T9 built for window-rejection ingestion — ONE seam, both
channels, not a parallel copy. The proof it is the shared function: it honours the
legacy ``SmsSid``/``SmsStatus`` aliases that only ``parse_status_callback`` knows.
"""

from __future__ import annotations

from persona_connectors.sms.cost import cost_from_status_callback


def test_cost_reads_num_segments_via_the_shared_parser() -> None:
    cost = cost_from_status_callback(
        {"MessageSid": "SM1", "MessageStatus": "delivered", "NumSegments": "3"}
    )
    assert cost.message_sid == "SM1"
    assert cost.segments == 3
    assert cost.cost is None  # no price configured → segment count only


def test_cost_computes_price_per_segment_when_configured() -> None:
    cost = cost_from_status_callback(
        {"MessageSid": "SM1", "NumSegments": "3"}, price_per_segment=0.0083
    )
    assert cost.segments == 3
    assert cost.cost is not None
    assert abs(cost.cost - 0.0249) < 1e-9  # 3 × $0.0083 (US per-segment)


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

"""The shared Twilio delivery-status ingestion (Spec C4 T9) — the reactive core.

ONE place recognizes the WhatsApp out-of-window codes (Twilio 63016 / Meta 131047)
and maps them to a window-distinct ``failed`` — used by BOTH arrival paths:
- the **sync** create-reply ``error_code`` (the 400-path), and
- the **async** status-callback (the queued-then-failed path).

The 2×2 matrix (window-code vs other-code × sync vs async) is proven here for the
classifier; the connector send-path proves the sync side end-to-end. T12 reuses
``parse_status_callback`` (the ``NumSegments`` field) for per-segment SMS cost — the
single-ingestion-path decision. The map is **total** — every input yields an explicit
outcome, never a crash or a silent drop (the degraded dropped/garbage-callback edge).
"""

from __future__ import annotations

from persona.delivery import DeliveryOutcome
from persona_connectors._twilio.status import (
    WINDOW_CLOSED_DETAIL,
    map_delivery,
    parse_status_callback,
    window_closed,
)


def test_window_codes_are_recognized_others_are_not() -> None:
    assert window_closed(63016)  # Twilio: outside the messaging window
    assert window_closed(131047)  # Meta Cloud: re-engagement message required
    assert not window_closed(30007)  # a generic carrier failure
    assert not window_closed(None)


# --- the mapping: window-code (sync OR async) → failed + the window-distinct detail ---


def test_window_code_maps_to_failed_with_template_required_detail() -> None:
    for code in (63016, 131047):
        result = map_delivery(channel="whatsapp", status="failed", error_code=code)
        assert result.outcome is DeliveryOutcome.FAILED
        assert (
            result.detail == WINDOW_CLOSED_DETAIL
        )  # the recognizable window signal (→ T10 template)


def test_other_failure_code_stays_plain_failed_not_window() -> None:
    result = map_delivery(channel="whatsapp", status="failed", error_code=30007)
    assert result.outcome is DeliveryOutcome.FAILED
    assert "30007" in (result.detail or "")
    assert result.detail != WINDOW_CLOSED_DETAIL  # a generic failure is NOT the window path


def test_delivered_and_sent_statuses_map_to_delivered() -> None:
    assert (
        map_delivery(channel="sms", status="delivered", error_code=None).outcome
        is DeliveryOutcome.DELIVERED
    )
    assert (
        map_delivery(channel="sms", status="sent", error_code=None).outcome
        is DeliveryOutcome.DELIVERED
    )


def test_in_flight_status_is_pending_the_sent_but_unconfirmed_state() -> None:
    # queued/accepted = Twilio took it, not yet confirmed → PENDING (sent-but-unconfirmed)
    assert (
        map_delivery(channel="sms", status="queued", error_code=None).outcome
        is DeliveryOutcome.PENDING
    )


# --- parsing the async callback (form-encoded) ---


def test_parse_status_callback_extracts_sid_status_code_and_segments() -> None:
    cb = parse_status_callback(
        {"MessageSid": "SM1", "MessageStatus": "failed", "ErrorCode": "63016", "NumSegments": "3"}
    )
    assert cb.message_sid == "SM1"
    assert cb.status == "failed"
    assert cb.error_code == 63016
    assert cb.num_segments == 3  # the seam T12 reuses for per-segment cost


def test_async_window_callback_recognized_on_the_async_path() -> None:
    cb = parse_status_callback(
        {"MessageSid": "SM2", "MessageStatus": "failed", "ErrorCode": "131047"}
    )
    result = map_delivery(channel="whatsapp", status=cb.status, error_code=cb.error_code)
    assert result.outcome is DeliveryOutcome.FAILED
    assert result.detail == WINDOW_CLOSED_DETAIL


def test_async_other_failure_callback_stays_plain_failed() -> None:
    cb = parse_status_callback(
        {"MessageSid": "SM3", "MessageStatus": "undelivered", "ErrorCode": "30008"}
    )
    result = map_delivery(channel="whatsapp", status=cb.status, error_code=cb.error_code)
    assert result.outcome is DeliveryOutcome.FAILED
    assert "30008" in (result.detail or "")
    assert result.detail != WINDOW_CLOSED_DETAIL


def test_empty_or_garbage_callback_never_crashes_or_silently_drops() -> None:
    # the degraded dropped/garbage-callback edge: a missing/garbage callback must yield a
    # defined PENDING (sent-but-unconfirmed), never a crash and never a silent drop.
    cb = parse_status_callback({})
    assert cb.message_sid == ""
    assert cb.error_code is None
    result = map_delivery(channel="whatsapp", status=cb.status, error_code=cb.error_code)
    assert result.outcome is DeliveryOutcome.PENDING
    # a non-numeric ErrorCode is tolerated (no crash)
    assert parse_status_callback({"ErrorCode": "not-a-number"}).error_code is None

"""Shared Twilio delivery-status ingestion (Spec C4 T9) — the reactive-window core.

The single place that recognizes WhatsApp's **out-of-window** rejection and maps any
Twilio delivery signal to a :class:`~persona.delivery.DeliveryResult`. It serves BOTH
arrival paths of a rejection (C4-R-2 / D-C4-6):

- **sync** — the create-reply ``error_code`` on :class:`~persona_connectors._twilio.
  client.TwilioMessageResult` (the 400-path the connector inspects inline), and
- **async** — the Twilio **status callback** (the common queued-then-failed path),
  parsed by :func:`parse_status_callback`.

Both feed :func:`map_delivery`, so the window-vs-generic recognition lives in ONE
function regardless of how the rejection arrives. The window codes are **Twilio 63016**
("outside the messaging window — use a template") and **Meta Cloud 131047**
("re-engagement message"); a window rejection maps to ``failed`` with the distinct
:data:`WINDOW_CLOSED_DETAIL` so T10 can route it to the template re-engagement, while a
generic failure stays plain ``failed``.

**The map is total** — every input yields an explicit outcome (delivered / pending /
failed), never a crash or a silent drop. A dropped, delayed, or garbage callback maps
to ``pending`` (sent-but-unconfirmed); the originated content is already durably
persisted upstream (C0 D-C0-4), so a missing callback is **degraded observability, not
data loss**. **T12** reuses :func:`parse_status_callback` (``num_segments``) for
per-segment SMS cost — the single-ingestion-path decision.

Pure + api-free.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.delivery import DeliveryOutcome, DeliveryResult
from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "WINDOW_CLOSED_DETAIL",
    "WINDOW_ERROR_CODES",
    "StatusCallback",
    "map_delivery",
    "parse_status_callback",
    "window_closed",
]

# The out-of-window rejection codes (C4-R-2): Twilio's WhatsApp window error + Meta
# Cloud's re-engagement error. A free-form send beyond the 24h window fails with one of
# these — distinct from any generic carrier/transport failure.
WINDOW_ERROR_CODES = frozenset({63016, 131047})

# The distinct detail a window rejection carries — the recognizable signal T10 routes to
# the approved-template re-engagement (vs a generic failure, which stays plain failed).
WINDOW_CLOSED_DETAIL = "send-window closed; template required"

_DELIVERED_STATUSES = frozenset({"sent", "delivered"})
_FAILED_STATUSES = frozenset({"failed", "undelivered"})


def window_closed(error_code: int | None) -> bool:
    """Whether a Twilio/Meta ``error_code`` is the out-of-window rejection (T9)."""
    return error_code is not None and error_code in WINDOW_ERROR_CODES


class StatusCallback(BaseModel):
    """A parsed Twilio status callback, narrowed to what the adapters need.

    Attributes:
        message_sid: The message resource SID this callback is about.
        status: The lower-cased Twilio ``MessageStatus`` (``queued`` / ``sent`` /
            ``delivered`` / ``undelivered`` / ``failed`` / …).
        error_code: The Twilio/Meta error code on a failure, else ``None``.
        num_segments: The billed segment count (``NumSegments``), or ``None`` — the seam
            T12 reuses for per-segment SMS cost.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    message_sid: str
    status: str
    error_code: int | None = None
    num_segments: int | None = None


def _as_int(value: str | None) -> int | None:
    """Parse a form-field int, tolerating absent / empty / non-numeric values (no crash)."""
    if value is None:
        return None
    text = value.strip()
    if text.startswith("-"):
        text = text[1:]
    return int(value) if text.isdigit() else None


def parse_status_callback(params: Mapping[str, str]) -> StatusCallback:
    """Parse a Twilio status-callback form POST into a :class:`StatusCallback` (total).

    Tolerates missing / empty / non-numeric fields — a dropped or garbage callback never
    raises; it yields a defined value the mapper treats as ``pending`` (the degraded
    sent-but-unconfirmed edge). ``MessageSid``/``MessageStatus`` fall back to the legacy
    ``SmsSid``/``SmsStatus`` aliases.
    """
    sid = params.get("MessageSid") or params.get("SmsSid") or ""
    status = (params.get("MessageStatus") or params.get("SmsStatus") or "").strip().lower()
    return StatusCallback(
        message_sid=sid,
        status=status,
        error_code=_as_int(params.get("ErrorCode")),
        num_segments=_as_int(params.get("NumSegments")),
    )


def map_delivery(*, channel: str, status: str | None, error_code: int | None) -> DeliveryResult:
    """Map a Twilio delivery signal to a :class:`DeliveryResult` (the single ingestion map).

    Window-closed ``error_code`` → ``failed`` + :data:`WINDOW_CLOSED_DETAIL` (the T10
    template signal); any other ``error_code`` → plain ``failed`` (code in the detail);
    a ``failed``/``undelivered`` status → ``failed``; ``sent``/``delivered`` →
    ``delivered``; anything else (``queued``/``accepted``/empty) → ``pending``
    (sent-but-unconfirmed). **Never a silent drop.**
    """
    if window_closed(error_code):
        return DeliveryResult(
            outcome=DeliveryOutcome.FAILED, channel=channel, detail=WINDOW_CLOSED_DETAIL
        )
    if error_code is not None:
        return DeliveryResult(
            outcome=DeliveryOutcome.FAILED,
            channel=channel,
            detail=f"send rejected (code {error_code})",
        )
    norm = (status or "").strip().lower()
    if norm in _FAILED_STATUSES:
        return DeliveryResult(
            outcome=DeliveryOutcome.FAILED, channel=channel, detail=f"send {norm}"
        )
    if norm in _DELIVERED_STATUSES:
        return DeliveryResult(outcome=DeliveryOutcome.DELIVERED, channel=channel)
    return DeliveryResult(
        outcome=DeliveryOutcome.PENDING,
        channel=channel,
        detail=f"awaiting confirmation ({norm or 'unknown'})",
    )

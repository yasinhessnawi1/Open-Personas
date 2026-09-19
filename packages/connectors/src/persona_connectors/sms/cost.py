"""SMS per-segment cost recording (Spec C4 T12) — reusing the T9 single ingestion path.

SMS is the one channel where verbosity costs money (criterion 9). The billed unit is the
**segment**, reported by Twilio on the delivery **status callback** (``NumSegments``).
This module reads it through the **same** :func:`~persona_connectors._twilio.status.
parse_status_callback` that T9 built for window-rejection ingestion — the single-handler
decision from Phase 2: one seam serves both the WhatsApp rejection mapping and the SMS
cost, never two parallel parsers. Pure + api-free (plus persona-core logging).

**The price is in CENTS, and it is deployment config.** Twilio's per-segment price varies
by destination country and by account, so there is no number this module could carry that
would be right for an operator it has never met. It is read from
``PERSONA_CONNECTORS_SMS_PRICE_PER_SEGMENT_CENTS`` and threaded in; with no price
configured :attr:`SmsCost.cost_cents` is ``None`` and the charge site says so out loud
rather than inventing a figure (the ``image_pricing`` precedent: undercharging costs the
house money, a guessed price overcharges a person).

The unit is named in the identifiers because the audit of 2026-09-14 found a money bug
where every number was right and only the word was wrong. The old ``price_per_segment`` /
``cost`` pair was unit-free while the billing seam counts in cents, and the one test that
exercised it seeded a DOLLAR figure.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.delivery import DeliveryOutcome
from persona.logging import get_logger
from pydantic import BaseModel, ConfigDict

from persona_connectors._twilio.status import map_delivery, parse_status_callback
from persona_connectors.sms.inbound import PLATFORM

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["SmsCost", "cost_from_status_callback", "record_sms_cost"]

_log = get_logger("connectors.sms_cost")


class SmsCost(BaseModel):
    """The billed cost of one SMS, from its delivery status callback.

    Attributes:
        message_sid: The message resource SID. Globally unique at Twilio, which is what
            makes it a safe idempotency root for the charge (the conflict gate is unique
            on the billing key alone).
        segments: The billed segment count (``NumSegments``); 0 if not reported.
        status: The lower-cased Twilio ``MessageStatus`` this callback carried.
        billable: Whether this callback is the one that costs money: a positive segment
            count AND a delivered outcome through the shared :func:`map_delivery`. Twilio
            charges when the message goes to the carrier, so a ``queued`` callback (which
            arrives first, already carrying ``NumSegments``) is not yet a cost and a
            ``failed`` one never becomes one.
        cost_cents: ``segments × price_per_segment_cents`` when a price is configured,
            else ``None`` (the segment count alone is the cost driver; price is
            deployment config, see the module docstring).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    message_sid: str
    segments: int
    status: str
    billable: bool
    cost_cents: float | None = None


def cost_from_status_callback(
    params: Mapping[str, str], *, price_per_segment_cents: float | None = None
) -> SmsCost:
    """Read the billed segment count (and optional cost in cents) from a status callback.

    Reuses :func:`parse_status_callback` (T9) — the single ingestion seam — so the
    ``NumSegments`` extraction and the ``SmsSid``/``SmsStatus`` aliasing are shared with
    the window-rejection path, never re-implemented. :attr:`SmsCost.billable` runs the
    same :func:`map_delivery` map for the same reason: whether a callback is a cost is a
    delivery-outcome question, and a second copy of that map is how two call sites come
    to disagree about which callback was the send.
    """
    callback = parse_status_callback(params)
    segments = callback.num_segments or 0
    outcome = map_delivery(channel=PLATFORM, status=callback.status, error_code=callback.error_code)
    cost_cents = (
        None if price_per_segment_cents is None else round(segments * price_per_segment_cents, 6)
    )
    return SmsCost(
        message_sid=callback.message_sid,
        segments=segments,
        status=callback.status,
        billable=segments > 0 and outcome.outcome is DeliveryOutcome.DELIVERED,
        cost_cents=cost_cents,
    )


def record_sms_cost(
    params: Mapping[str, str], *, price_per_segment_cents: float | None = None
) -> SmsCost:
    """Record (log) the per-segment SMS cost from a status callback, returning it (criterion 9)."""
    cost = cost_from_status_callback(params, price_per_segment_cents=price_per_segment_cents)
    _log.info(
        "sms cost (sid={sid} status={status} segments={segments} cost_cents={cost} billable={b})",
        sid=cost.message_sid,
        status=cost.status,
        segments=cost.segments,
        cost=cost.cost_cents,
        b=cost.billable,
    )
    return cost

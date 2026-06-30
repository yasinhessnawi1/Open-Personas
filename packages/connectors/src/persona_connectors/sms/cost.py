"""SMS per-segment cost recording (Spec C4 T12) — reusing the T9 single ingestion path.

SMS is the one channel where verbosity costs money (criterion 9). The billed unit is the
**segment**, reported by Twilio on the delivery **status callback** (``NumSegments``).
This module reads it through the **same** :func:`~persona_connectors._twilio.status.
parse_status_callback` that T9 built for window-rejection ingestion — the single-handler
decision from Phase 2: one seam serves both the WhatsApp rejection mapping and the SMS
cost, never two parallel parsers. Pure + api-free (plus persona-core logging).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.logging import get_logger
from pydantic import BaseModel, ConfigDict

from persona_connectors._twilio.status import parse_status_callback

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["SmsCost", "cost_from_status_callback", "record_sms_cost"]

_log = get_logger("connectors.sms_cost")


class SmsCost(BaseModel):
    """The billed cost of one SMS, from its delivery status callback.

    Attributes:
        message_sid: The message resource SID.
        segments: The billed segment count (``NumSegments``); 0 if not reported.
        cost: ``segments × price_per_segment`` when a price is configured, else ``None``
            (the segment count alone is the cost driver — price is deployment config).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    message_sid: str
    segments: int
    cost: float | None = None


def cost_from_status_callback(
    params: Mapping[str, str], *, price_per_segment: float | None = None
) -> SmsCost:
    """Read the billed segment count (and optional cost) from a Twilio status callback.

    Reuses :func:`parse_status_callback` (T9) — the single ingestion seam — so the
    ``NumSegments`` extraction and the ``SmsSid``/``SmsStatus`` aliasing are shared with
    the window-rejection path, never re-implemented.
    """
    callback = parse_status_callback(params)
    segments = callback.num_segments or 0
    cost = round(segments * price_per_segment, 6) if price_per_segment is not None else None
    return SmsCost(message_sid=callback.message_sid, segments=segments, cost=cost)


def record_sms_cost(
    params: Mapping[str, str], *, price_per_segment: float | None = None
) -> SmsCost:
    """Record (log) the per-segment SMS cost from a status callback, returning it (criterion 9)."""
    cost = cost_from_status_callback(params, price_per_segment=price_per_segment)
    _log.info(
        "sms cost (sid={sid} segments={segments} cost={cost})",
        sid=cost.message_sid,
        segments=cost.segments,
        cost=cost.cost,
    )
    return cost

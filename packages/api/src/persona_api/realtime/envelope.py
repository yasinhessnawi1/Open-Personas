"""Spec A11 T1 — the SSE transport envelope: ``epoch:seq`` ids + frame formatting.

The domain events (:mod:`persona_api.realtime.events`) are pure. This layer wraps
them for the wire: a server-authoritative ``id: {epoch}:{seq}`` on every data frame
(the ``Last-Event-ID`` cursor — A11-D-3), and the control/heartbeat frames. Frame
shape mirrors :func:`chat_service._sse` (``event:``/``data:`` + a blank-line
terminator) so the existing web SSE parser consumes both streams identically; A11
adds the ``id:`` line the parser will start surfacing (T6).
"""

from __future__ import annotations

import json
from typing import Literal

from persona_api.realtime.events import ChannelEvent, ReadyControl, ResyncControl

__all__ = [
    "HEARTBEAT",
    "HEARTBEAT_INTERVAL_SECONDS",
    "format_event_id",
    "frame_data",
    "frame_ready",
    "frame_resync",
    "parse_event_id",
]

#: A bare SSE comment line — invisible to the client parser (it skips ``:`` lines),
#: sent every :data:`HEARTBEAT_INTERVAL_SECONDS` to keep the idle ``/v1/me/events``
#: stream under the Fly proxy's ~60-75s idle reap (A11-R-2).
HEARTBEAT = b": hb\n\n"

#: The keep-alive cadence — 15s is a ≥4× margin under the ~60s Fly SSE idle timeout
#: and also defeats browser / corporate-proxy idle limits (A11-R-2).
HEARTBEAT_INTERVAL_SECONDS = 15.0


def format_event_id(epoch: str, seq: int) -> str:
    """The ``Last-Event-ID`` wire value: ``{epoch}:{seq}``."""
    return f"{epoch}:{seq}"


def parse_event_id(raw: str) -> tuple[str, int] | None:
    """Parse ``{epoch}:{seq}`` → ``(epoch, seq)``; ``None`` if malformed.

    Splits on the LAST colon (the epoch is opaque and colon-free in practice, but
    ``rpartition`` is robust regardless). A malformed cursor resolves to a RESYNC
    upstream — never a silent gap.
    """
    epoch, sep, seq_str = raw.rpartition(":")
    if not sep or not epoch:
        return None
    try:
        return epoch, int(seq_str)
    except ValueError:
        return None


def _frame(event_id: str | None, name: str, payload: dict[str, object]) -> bytes:
    """Format one SSE frame; ``event_id=None`` omits the ``id:`` line (control frames
    are out-of-band and carry no replayable id)."""
    prefix = f"id: {event_id}\n" if event_id is not None else ""
    return f"{prefix}event: {name}\ndata: {json.dumps(payload)}\n\n".encode()


def frame_data(epoch: str, seq: int, event: ChannelEvent) -> bytes:
    """Frame a data event with its ``id: {epoch}:{seq}`` cursor; the SSE ``event:``
    name is the payload ``type`` so the client routes on it."""
    return _frame(format_event_id(epoch, seq), event.type, event.model_dump(mode="json"))


def frame_ready(epoch: str, latest_seq: int) -> bytes:
    """The open-baseline control frame (no id)."""
    payload = ReadyControl(epoch=epoch, latest_seq=latest_seq).model_dump(mode="json")
    return _frame(None, "ready", payload)


def frame_resync(
    epoch: str, latest_seq: int, reason: Literal["epoch_changed", "ring_gap"]
) -> bytes:
    """The restart-safe resume control frame (no id) — the client full-refetches."""
    payload = ResyncControl(epoch=epoch, latest_seq=latest_seq, reason=reason).model_dump(
        mode="json"
    )
    return _frame(None, "resync", payload)

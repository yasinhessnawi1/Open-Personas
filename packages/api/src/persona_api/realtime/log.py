"""Spec A11 T1 — the per-user event log + the reconnect resolver (A11-D-3).

:class:`UserEventLog` owns one user's monotonic ``seq`` counter and a bounded ring
of the last N framed events. :meth:`record` is the **synchronous** publish critical
section (read/increment seq, frame, append ring) — it never awaits, so a bus
publish can fan a frame to every open tab without blocking on any one of them
(T1 impl requirement). :meth:`resume` is the restart-safe resolver: it maps a
reconnecting client's ``Last-Event-ID`` to REPLAY (in-ring) / RESYNC (prior epoch or
fell-off-ring → client full-refetches) / READY (fresh connect), never a silent gap.

The ring is in-memory (no migration — A11-D-3): a process restart drops it, which
is a *refetch*, not data loss (durability lives in P6 + conversation reads). The
per-process ``epoch`` makes a stale cross-restart cursor detectable.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Literal

from persona_api.realtime.envelope import (
    format_event_id,
    frame_data,
    frame_ready,
    frame_resync,
    parse_event_id,
)
from persona_api.realtime.events import ChannelEvent

__all__ = ["RecordedEvent", "ResumePlan", "UserEventLog"]

#: Default ring depth (A11-D-3, N≈64) — how far back a reconnect can replay before
#: it must RESYNC to a durable refetch.
DEFAULT_RING_SIZE = 64


@dataclass(frozen=True, slots=True)
class RecordedEvent:
    """The result of :meth:`UserEventLog.record`: the assigned ``seq``, its
    ``epoch:seq`` id, and the ready-to-send SSE frame."""

    seq: int
    event_id: str
    frame: bytes


@dataclass(frozen=True, slots=True)
class ResumePlan:
    """What the SSE endpoint writes to a (re)connecting client before going live.

    ``kind`` names the case; ``frames`` are the exact bytes to write in order
    (replayed data frames, or a single control frame); ``reason`` is set only for a
    RESYNC. An empty ``replay`` (``frames=()``) means "caught up — just go live".
    """

    kind: Literal["ready", "replay", "resync"]
    frames: tuple[bytes, ...]
    reason: Literal["epoch_changed", "ring_gap"] | None = None


class UserEventLog:
    """One user's monotonic sequence + bounded ring of framed events.

    Args:
        epoch: The server-authoritative per-process token; a restart mints a new one
            so a stale cross-restart ``Last-Event-ID`` resolves to RESYNC.
        ring_size: How many recent frames to retain for replay (default
            :data:`DEFAULT_RING_SIZE`).
    """

    def __init__(self, *, epoch: str, ring_size: int = DEFAULT_RING_SIZE) -> None:
        self._epoch = epoch
        self._seq = 0
        self._ring: deque[tuple[int, bytes]] = deque(maxlen=ring_size)

    @property
    def epoch(self) -> str:
        return self._epoch

    @property
    def latest_seq(self) -> int:
        """The last seq assigned (0 before any record)."""
        return self._seq

    def record(self, event: ChannelEvent) -> RecordedEvent:
        """Assign the next ``seq``, frame the event, append it to the ring, return
        the frame. Fully synchronous — the publish critical section never awaits."""
        self._seq += 1
        seq = self._seq
        frame = frame_data(self._epoch, seq, event)
        self._ring.append((seq, frame))
        return RecordedEvent(seq=seq, event_id=format_event_id(self._epoch, seq), frame=frame)

    def resume(self, last_event_id: str | None) -> ResumePlan:
        """Resolve a reconnecting client's ``Last-Event-ID`` to a :class:`ResumePlan`.

        Fresh connect (no id) → READY with the ``{epoch, latest_seq}`` baseline.
        Otherwise: a prior-epoch or malformed cursor → RESYNC; a cursor still covered
        by the ring → REPLAY the frames after it; a cursor that fell off the ring or
        claims an unseen seq → RESYNC (``ring_gap``). Caught-up → empty REPLAY.
        """
        if last_event_id is None:
            return ResumePlan(kind="ready", frames=(frame_ready(self._epoch, self._seq),))

        parsed = parse_event_id(last_event_id)
        if parsed is None:
            return self._resync("ring_gap")
        epoch, seq = parsed
        if epoch != self._epoch:
            return self._resync("epoch_changed")
        if seq < 0 or seq > self._seq:
            # Claims a seq from this epoch we never issued — corrupt cursor.
            return self._resync("ring_gap")
        if seq == self._seq:
            return ResumePlan(kind="replay", frames=())  # caught up → live

        oldest = self._ring[0][0] if self._ring else self._seq + 1
        if seq < oldest - 1:
            return self._resync("ring_gap")  # fell off the ring
        frames = tuple(frame for s, frame in self._ring if s > seq)
        return ResumePlan(kind="replay", frames=frames)

    def _resync(self, reason: Literal["epoch_changed", "ring_gap"]) -> ResumePlan:
        return ResumePlan(
            kind="resync",
            frames=(frame_resync(self._epoch, self._seq, reason),),
            reason=reason,
        )

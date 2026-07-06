"""Spec A11 T2 — the in-process fan-out bus (A11-D-1 option B, the floor).

:class:`UserEventChannel` is the registry the SSE endpoint and the delivery/notify
paths share on ``app.state``: the endpoint :meth:`subscribe`s a per-tab
:class:`Subscription`; the delivery paths :meth:`publish` an event, which the
channel records on the owner's :class:`UserEventLog` and fans out to every open tab.

The publish critical section is **fully synchronous** — no ``await`` between reading
the seq, recording, and fanning out — so one slow tab can never block delivery to
the others. Each tab's queue is **bounded**; on overflow the tab is CLOSED and
dropped (``put_nowait``, never ``await put``). The dropped client reconnects and its
stale ``Last-Event-ID`` resolves to a ``ring_gap`` resync (durable refetch).

Edition seam (A11-D-5): this in-process bus is the whole mechanism for both editions
as deployed today (community SQLite in-process; cloud single Fly Machine in-process).
A future api/worker split adds a cross-process transport (option A) that feeds each
instance's channel via the same :meth:`publish` — no contract change.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

from persona_api.realtime.envelope import HEARTBEAT
from persona_api.realtime.events import ChannelEvent
from persona_api.realtime.log import DEFAULT_RING_SIZE, ResumePlan, UserEventLog

__all__ = ["DEFAULT_QUEUE_MAXSIZE", "Subscription", "UserEventChannel"]

#: Per-tab queue bound. Generous enough to absorb a burst while a tab renders, small
#: enough that a wedged tab is dropped promptly rather than buffering unboundedly.
DEFAULT_QUEUE_MAXSIZE = 256


class Subscription:
    """One open tab's bounded frame queue (one SSE connection).

    :meth:`offer` is the synchronous, non-blocking producer side used by
    :meth:`UserEventChannel.publish`; :meth:`next` is the async consumer side the
    SSE endpoint drains. Overflow (a full queue) closes the subscription — the
    endpoint then ends the response and the client reconnects.
    """

    def __init__(self, *, owner_id: str, maxsize: int) -> None:
        self.owner_id = owner_id
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=maxsize)
        self._closed = asyncio.Event()

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def qsize(self) -> int:
        return self._queue.qsize()

    def offer(self, frame: bytes) -> bool:
        """Enqueue a frame without blocking. Returns ``False`` (and CLOSES the tab)
        if the queue is full or already closed — the caller drops this subscription.
        """
        if self._closed.is_set():
            return False
        try:
            self._queue.put_nowait(frame)
        except asyncio.QueueFull:
            self._closed.set()
            return False
        return True

    def close(self) -> None:
        """Mark the subscription closed; wakes an idle :meth:`next` with ``None``."""
        self._closed.set()

    async def next(self, *, timeout: float | None = None) -> bytes | None:
        """Await the next frame; return :data:`~persona_api.realtime.envelope.HEARTBEAT`
        if ``timeout`` seconds elapse with nothing ready (the SSE keep-alive), or
        ``None`` when the subscription is closed and drained.

        The timeout is applied INSIDE the wait — never as an external
        ``wait_for(next())`` — so a frame that completes ``queue.get()`` is always
        returned, never cancelled-away between dequeue and return (no lost frame).
        Queued frames drain before a close is honoured.
        """
        if not self._queue.empty():
            return self._queue.get_nowait()
        if self._closed.is_set():
            return None
        get_task = asyncio.ensure_future(self._queue.get())
        close_task = asyncio.ensure_future(self._closed.wait())
        try:
            done, _ = await asyncio.wait(
                {get_task, close_task}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            if not get_task.done():
                get_task.cancel()
            if not close_task.done():
                close_task.cancel()
        if not done:
            # Timeout elapsed with nothing ready — get_task never dequeued (it is
            # only in `done` if it completed), so cancelling it above loses nothing.
            return HEARTBEAT
        if get_task in done and not get_task.cancelled():
            return get_task.result()
        # Closed fired; hand back a frame that raced in between the checks, if any.
        if not self._queue.empty():
            return self._queue.get_nowait()
        return None


class UserEventChannel:
    """Per-user fan-out registry over N :class:`UserEventLog`s (the in-process bus).

    Args:
        epoch: The server-authoritative per-process token stamped into every event
            id; defaults to a fresh ``uuid4`` so a restart is detectable (A11-D-3).
        ring_size: Per-user replay ring depth (:data:`DEFAULT_RING_SIZE`).
        queue_maxsize: Per-tab queue bound (:data:`DEFAULT_QUEUE_MAXSIZE`).
    """

    def __init__(
        self,
        *,
        epoch: str | None = None,
        ring_size: int = DEFAULT_RING_SIZE,
        queue_maxsize: int = DEFAULT_QUEUE_MAXSIZE,
    ) -> None:
        self._epoch = epoch or uuid4().hex
        self._ring_size = ring_size
        self._queue_maxsize = queue_maxsize
        self._logs: dict[str, UserEventLog] = {}
        self._subs: dict[str, set[Subscription]] = {}

    @property
    def epoch(self) -> str:
        return self._epoch

    def has_subscribers(self, owner_id: str) -> bool:
        return bool(self._subs.get(owner_id))

    def connection_count(self, owner_id: str) -> int:
        return len(self._subs.get(owner_id, ()))

    def subscribe(self, owner_id: str) -> Subscription:
        """Register a new tab connection for ``owner_id`` (creates the owner's log on
        the first tab). The SSE endpoint calls :meth:`resume` for the initial frames.
        """
        sub = Subscription(owner_id=owner_id, maxsize=self._queue_maxsize)
        self._subs.setdefault(owner_id, set()).add(sub)
        self._logs.setdefault(owner_id, UserEventLog(epoch=self._epoch, ring_size=self._ring_size))
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        """Drop a tab; when the owner's LAST tab closes, clear its log so a later
        reconnect with a stale cursor resolves to a refetch (A11-D-3), not a gap."""
        sub.close()
        subs = self._subs.get(sub.owner_id)
        if subs is None:
            return
        subs.discard(sub)
        if not subs:
            del self._subs[sub.owner_id]
            self._logs.pop(sub.owner_id, None)

    def publish(self, owner_id: str, event: ChannelEvent) -> None:
        """Record + fan the event out to every open tab of ``owner_id`` — SYNC, no
        await. No open tab → no-op (the durable row is the caller's, present on next
        connect). A tab whose bounded queue overflows is closed and dropped here."""
        subs = self._subs.get(owner_id)
        if not subs:
            return
        recorded = self._logs[owner_id].record(event)
        dead = [sub for sub in subs if not sub.offer(recorded.frame)]
        for sub in dead:
            subs.discard(sub)
        if not subs:
            del self._subs[owner_id]
            self._logs.pop(owner_id, None)

    def resume(self, owner_id: str, last_event_id: str | None) -> ResumePlan:
        """The reconnect resolver for ``owner_id`` (delegates to the owner's log; a
        fresh/absent log baselines at seq 0 → a ready/resync as appropriate)."""
        log = self._logs.get(owner_id)
        if log is None:
            log = self._logs.setdefault(
                owner_id, UserEventLog(epoch=self._epoch, ring_size=self._ring_size)
            )
        return log.resume(last_event_id)

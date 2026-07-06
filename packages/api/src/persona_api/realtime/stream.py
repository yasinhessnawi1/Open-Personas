"""Spec A11 T3 — the SSE stream generator for ``GET /v1/me/events``.

Factored out of the route so it is unit-testable without the HTTP/auth machinery.
On (re)connect it subscribes a tab, resolves the client's ``Last-Event-ID`` to the
initial frames (READY / REPLAY / RESYNC — A11-D-3), then streams live frames with a
15s heartbeat, ending cleanly when the tab is closed (overflow) or the client
disconnects (the generator is cancelled → the ``finally`` unsubscribes).

``subscribe`` and ``resume`` are called back-to-back with **no await between them**:
both are synchronous, so no ``publish`` can interleave (single event loop) — the
ring snapshot and the live queue hand off contiguously, with neither a gap nor a
duplicate at the seam.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona_api.realtime.envelope import HEARTBEAT_INTERVAL_SECONDS

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from persona_api.realtime.channel import UserEventChannel

__all__ = ["stream_user_events"]


async def stream_user_events(
    channel: UserEventChannel,
    owner_id: str,
    last_event_id: str | None,
    *,
    heartbeat_seconds: float = HEARTBEAT_INTERVAL_SECONDS,
) -> AsyncIterator[bytes]:
    """Yield SSE frames for ``owner_id``'s live channel (the me-scope comes from the
    verified token at the call site — never a request param).

    Args:
        channel: The process-wide in-process bus (``app.state.event_channel``).
        owner_id: The authenticated user id (RLS scope).
        last_event_id: The client's ``Last-Event-ID`` header, or ``None`` on a fresh
            connect.
        heartbeat_seconds: Idle keep-alive cadence (injectable for tests).
    """
    sub = channel.subscribe(owner_id)
    plan = channel.resume(owner_id, last_event_id)  # sync — atomic w.r.t. publish
    try:
        for frame in plan.frames:
            yield frame
        while True:
            chunk = await sub.next(timeout=heartbeat_seconds)
            if chunk is None:
                break  # tab closed (overflow) → end; the client reconnects
            yield chunk
    finally:
        channel.unsubscribe(sub)

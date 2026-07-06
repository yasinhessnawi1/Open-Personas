"""Spec A11 T3 — the SSE stream generator (``stream_user_events``).

Drives the generator directly (no HTTP) to prove: the READY baseline on a fresh
connect; live frame delivery; the 15s heartbeat on idle; REPLAY / RESYNC on
reconnect per the T1 resume table; owner-scoping; and clean unsubscribe on exit
(close or client disconnect).
"""

from __future__ import annotations

import asyncio

import pytest
from persona_api.realtime.channel import UserEventChannel
from persona_api.realtime.envelope import HEARTBEAT
from persona_api.realtime.events import NotificationCreatedEvent
from persona_api.realtime.stream import stream_user_events

EPOCH = "streamepoch"


def _evt(n: int) -> NotificationCreatedEvent:
    return NotificationCreatedEvent(notification_id=f"n{n}", kind="schedule_fire", ref_id=f"s{n}")


@pytest.mark.asyncio
async def test_fresh_connect_opens_with_a_ready_baseline() -> None:
    ch = UserEventChannel(epoch=EPOCH)
    gen = stream_user_events(ch, "u1", None, heartbeat_seconds=30)
    first = await gen.__anext__()
    assert b"event: ready\n" in first
    assert b'"latest_seq": 0' in first
    await gen.aclose()


@pytest.mark.asyncio
async def test_live_publish_is_streamed_after_ready() -> None:
    ch = UserEventChannel(epoch=EPOCH)
    gen = stream_user_events(ch, "u1", None, heartbeat_seconds=30)
    await gen.__anext__()  # ready — the subscribe has run
    ch.publish("u1", _evt(1))
    frame = await gen.__anext__()
    assert b"event: notification.created\n" in frame
    assert b"id: streamepoch:1\n" in frame
    await gen.aclose()


@pytest.mark.asyncio
async def test_heartbeat_on_idle() -> None:
    ch = UserEventChannel(epoch=EPOCH)
    gen = stream_user_events(ch, "u1", None, heartbeat_seconds=0.05)
    await gen.__anext__()  # ready
    # No publish → the next chunk is the heartbeat keep-alive.
    chunk = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    assert chunk == HEARTBEAT
    await gen.aclose()


@pytest.mark.asyncio
async def test_reconnect_in_ring_replays_then_goes_live() -> None:
    ch = UserEventChannel(epoch=EPOCH)
    # First connection records seq 1,2 then goes away.
    g1 = stream_user_events(ch, "u1", None, heartbeat_seconds=30)
    await g1.__anext__()  # ready
    ch.publish("u1", _evt(1))
    ch.publish("u1", _evt(2))
    # (Don't drain g1; a second tab keeps the log alive across g1 closing.)
    keep = ch.subscribe("u1")  # hold the log so the reconnect can replay
    await g1.aclose()
    # Reconnect with Last-Event-ID at seq 1 → replays seq 2, no ready.
    g2 = stream_user_events(ch, "u1", f"{EPOCH}:1", heartbeat_seconds=30)
    frame = await g2.__anext__()
    assert b"event: notification.created\n" in frame
    assert b"id: streamepoch:2\n" in frame
    await g2.aclose()
    ch.unsubscribe(keep)


@pytest.mark.asyncio
async def test_reconnect_after_restart_epoch_emits_resync() -> None:
    ch = UserEventChannel(epoch=EPOCH)
    gen = stream_user_events(ch, "u1", "OLDEPOCH:5", heartbeat_seconds=30)
    first = await gen.__anext__()
    assert b"event: resync\n" in first
    assert b'"reason": "epoch_changed"' in first
    await gen.aclose()


@pytest.mark.asyncio
async def test_stream_is_owner_scoped() -> None:
    ch = UserEventChannel(epoch=EPOCH)
    gen = stream_user_events(ch, "u1", None, heartbeat_seconds=0.05)
    await gen.__anext__()  # ready
    ch.publish("someone_else", _evt(1))  # a delivery to another tenant
    # u1's stream must NOT receive it — the next chunk is a heartbeat, not the event.
    chunk = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    assert chunk == HEARTBEAT
    await gen.aclose()


@pytest.mark.asyncio
async def test_stream_ends_when_the_tab_is_closed_by_overflow() -> None:
    ch = UserEventChannel(epoch=EPOCH, queue_maxsize=1)
    gen = stream_user_events(ch, "u1", None, heartbeat_seconds=30)
    await gen.__anext__()  # ready (the subscribe has run)
    ch.publish("u1", _evt(1))  # fills the size-1 queue
    ch.publish("u1", _evt(2))  # overflow → tab closed + dropped
    # Drain the one queued frame, then the stream ends (StopAsyncIteration).
    got = await gen.__anext__()
    assert b"id: streamepoch:1\n" in got
    with pytest.raises(StopAsyncIteration):
        await gen.__anext__()


@pytest.mark.asyncio
async def test_unsubscribe_runs_on_client_disconnect() -> None:
    ch = UserEventChannel(epoch=EPOCH)
    gen = stream_user_events(ch, "u1", None, heartbeat_seconds=30)
    await gen.__anext__()  # ready → subscribed
    assert ch.has_subscribers("u1") is True
    await gen.aclose()  # simulates the client disconnect (generator cancelled)
    assert ch.has_subscribers("u1") is False

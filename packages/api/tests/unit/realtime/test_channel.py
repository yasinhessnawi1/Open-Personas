"""Spec A11 T2 — the in-process bus: ``UserEventChannel`` + ``Subscription``.

The registry wraps N per-user :class:`UserEventLog`s with per-tab BOUNDED queues.
``publish`` is the **synchronous** critical section (record → fan-out via
``put_nowait``); a tab whose queue overflows is CLOSED and dropped (never an
``await put``, never blocking ``record`` for the other tabs) → the client reconnects
and its stale cursor resolves to a ``ring_gap`` resync. RLS scoping is structural:
``publish(owner_id, …)`` fans out only to that owner's subscriptions — tenant A
never sees tenant B's events.
"""

from __future__ import annotations

import asyncio

import pytest
from persona_api.realtime.channel import DEFAULT_QUEUE_MAXSIZE, Subscription, UserEventChannel
from persona_api.realtime.envelope import parse_event_id
from persona_api.realtime.events import NotificationCreatedEvent

EPOCH = "testepoch"


def _evt(n: int) -> NotificationCreatedEvent:
    return NotificationCreatedEvent(notification_id=f"n{n}", kind="schedule_fire", ref_id=f"s{n}")


# --- Subscription: bounded, non-blocking offer, overflow closes -------------


def test_offer_is_nonblocking_and_bounded() -> None:
    sub = Subscription(owner_id="u1", maxsize=2)
    assert sub.offer(b"a") is True
    assert sub.offer(b"b") is True
    # Third exceeds the bound → offer returns False AND closes the tab (no block).
    assert sub.offer(b"c") is False
    assert sub.closed is True


def test_offer_after_close_is_false() -> None:
    sub = Subscription(owner_id="u1", maxsize=8)
    sub.close()
    assert sub.offer(b"a") is False


@pytest.mark.asyncio
async def test_next_yields_offered_frames_in_order() -> None:
    sub = Subscription(owner_id="u1", maxsize=8)
    sub.offer(b"a")
    sub.offer(b"b")
    assert await sub.next() == b"a"
    assert await sub.next() == b"b"


@pytest.mark.asyncio
async def test_next_returns_none_when_closed_and_drained() -> None:
    sub = Subscription(owner_id="u1", maxsize=8)
    sub.offer(b"a")
    sub.close()
    # Queued frames drain first, THEN None signals end-of-stream.
    assert await sub.next() == b"a"
    assert await sub.next() is None


@pytest.mark.asyncio
async def test_next_wakes_on_close_while_idle() -> None:
    sub = Subscription(owner_id="u1", maxsize=8)

    async def close_soon() -> None:
        await asyncio.sleep(0.01)
        sub.close()

    task = asyncio.ensure_future(close_soon())
    # next() is awaiting an empty queue; the close must wake it (not hang).
    assert await asyncio.wait_for(sub.next(), timeout=1.0) is None
    await task


@pytest.mark.asyncio
async def test_next_wakes_on_new_frame_while_idle() -> None:
    sub = Subscription(owner_id="u1", maxsize=8)

    async def offer_soon() -> None:
        await asyncio.sleep(0.01)
        sub.offer(b"z")

    task = asyncio.ensure_future(offer_soon())
    assert await asyncio.wait_for(sub.next(), timeout=1.0) == b"z"
    await task


# --- UserEventChannel: subscribe / publish / fan-out ------------------------


@pytest.mark.asyncio
async def test_publish_fans_out_to_all_of_one_owners_tabs() -> None:
    ch = UserEventChannel(epoch=EPOCH)
    a = ch.subscribe("u1")
    b = ch.subscribe("u1")
    ch.publish("u1", _evt(1))
    fa = await a.next()
    fb = await b.next()
    assert fa == fb is not None
    assert parse_event_id(fa.decode().split("\n")[0].removeprefix("id: ")) == (EPOCH, 1)


@pytest.mark.asyncio
async def test_publish_is_rls_scoped_a_never_sees_bs_events() -> None:
    ch = UserEventChannel(epoch=EPOCH)
    a = ch.subscribe("tenant_a")
    b = ch.subscribe("tenant_b")
    ch.publish("tenant_b", _evt(1))  # a delivery to B
    # B receives it; A's queue stays empty (non-vacuous RLS proof).
    assert await b.next() is not None
    assert a.qsize() == 0


def test_publish_with_no_subscriber_is_a_noop() -> None:
    ch = UserEventChannel(epoch=EPOCH)
    # No open tab → durable-only; nothing recorded/fanned (present-on-connect).
    ch.publish("offline_user", _evt(1))
    assert ch.has_subscribers("offline_user") is False


def test_seq_is_per_user_monotonic_across_publishes() -> None:
    ch = UserEventChannel(epoch=EPOCH)
    ch.subscribe("u1")
    ch.subscribe("u2")
    ch.publish("u1", _evt(1))
    ch.publish("u1", _evt(2))
    ch.publish("u2", _evt(1))
    # Per-user logs are independent: u2 starts at seq 1, not 3.
    assert ch.resume("u1", None).frames[0].decode().find('"latest_seq": 2') != -1
    assert ch.resume("u2", None).frames[0].decode().find('"latest_seq": 1') != -1


# --- overflow closes the slow tab without harming the others ----------------


@pytest.mark.asyncio
async def test_overflow_closes_only_the_slow_tab_the_drained_one_keeps_flowing() -> None:
    # A slow tab (never drained, size-2 queue) overflows and closes; a fast tab that
    # drains each frame is never harmed — publish never blocks on the slow one.
    ch = UserEventChannel(epoch=EPOCH, queue_maxsize=2)
    slow = ch.subscribe("u1")
    fast = ch.subscribe("u1")
    for i in range(1, 5):  # 4 publishes; `fast` drains each, `slow` never does
        ch.publish("u1", _evt(i))
        got = await fast.next()  # the fast tab keeps receiving throughout
        assert got is not None
    assert slow.closed is True  # the slow tab overflowed and was dropped
    assert fast.closed is False


@pytest.mark.asyncio
async def test_overflowed_tab_is_dropped_from_the_channel() -> None:
    ch = UserEventChannel(epoch=EPOCH, queue_maxsize=1)
    sub = ch.subscribe("u1")
    ch.publish("u1", _evt(1))  # fills the size-1 queue
    ch.publish("u1", _evt(2))  # overflow → close + drop
    assert sub.closed is True
    assert ch.has_subscribers("u1") is False


# --- unsubscribe cleans up; reconnect after last-tab-close resyncs -----------


def test_unsubscribe_drops_the_sub_and_last_one_clears_the_log() -> None:
    ch = UserEventChannel(epoch=EPOCH)
    a = ch.subscribe("u1")
    b = ch.subscribe("u1")
    ch.publish("u1", _evt(1))
    ch.unsubscribe(a)
    assert ch.has_subscribers("u1") is True  # b still open
    ch.unsubscribe(b)
    assert ch.has_subscribers("u1") is False
    # Log cleared: a reconnect with the old cursor can't be honoured → resync.
    ch.subscribe("u1")
    plan = ch.resume("u1", f"{EPOCH}:1")
    assert plan.kind == "resync"


def test_default_queue_maxsize_is_reasonable() -> None:
    assert DEFAULT_QUEUE_MAXSIZE >= 64


def test_epoch_defaults_to_a_fresh_token_per_channel() -> None:
    a = UserEventChannel()
    b = UserEventChannel()
    assert a.epoch != b.epoch  # distinct process-epochs → cross-restart detectable
    assert len(a.epoch) >= 16

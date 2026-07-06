"""Spec A11 T3 — the ``GET /v1/me/events`` route handler (no HTTP; channel in-memory).

Drives the route function directly and pulls frames off the returned
``StreamingResponse.body_iterator`` — TestClient hangs on an intentionally-infinite
SSE stream (it waits for the response to complete), so the generator-level drive is
both hang-free and stronger. Proves: the SSE media-type + no-cache/anti-buffering
headers, the me-scope taken from the auth dep (not a param), the opening ``ready``
frame + a live push, the ``Last-Event-ID`` header threaded to resume, and the
fail-soft 503 when the channel is unwired.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from persona_api.auth import AuthenticatedUser
from persona_api.realtime.channel import UserEventChannel
from persona_api.realtime.events import NotificationCreatedEvent
from persona_api.routes.me import stream_events

_USER = AuthenticatedUser(id="u1", email=None)


def _request(channel: UserEventChannel | None, *, last_event_id: str | None = None) -> object:
    state = SimpleNamespace()
    if channel is not None:
        state.event_channel = channel
    headers = {"last-event-id": last_event_id} if last_event_id is not None else {}
    return SimpleNamespace(app=SimpleNamespace(state=state), headers=headers)


@pytest.mark.asyncio
async def test_route_returns_sse_streaming_response_with_keepalive_headers() -> None:
    ch = UserEventChannel(epoch="routeepoch")
    resp = await stream_events(_request(ch), _USER)  # type: ignore[arg-type]
    assert isinstance(resp, StreamingResponse)
    assert resp.media_type == "text/event-stream"
    assert resp.headers["cache-control"] == "no-cache"
    assert resp.headers["x-accel-buffering"] == "no"
    await resp.body_iterator.aclose()


@pytest.mark.asyncio
async def test_route_opens_with_ready_then_streams_a_live_event() -> None:
    ch = UserEventChannel(epoch="routeepoch")
    resp = await stream_events(_request(ch), _USER)  # type: ignore[arg-type]
    gen = resp.body_iterator
    ready = await gen.__anext__()
    assert b"event: ready\n" in ready
    assert b'"epoch": "routeepoch"' in ready
    # The subscribe has run → a publish for u1 streams next.
    ch.publish("u1", NotificationCreatedEvent(notification_id="n1", kind="k", ref_id="s1"))
    frame = await gen.__anext__()
    assert b"event: notification.created\n" in frame
    assert b"id: routeepoch:1\n" in frame
    await gen.aclose()


@pytest.mark.asyncio
async def test_route_threads_last_event_id_header_to_resume() -> None:
    ch = UserEventChannel(epoch="routeepoch")
    # A prior-epoch cursor → the stream opens with a resync (restart-safe resume).
    resp = await stream_events(_request(ch, last_event_id="OLDEPOCH:9"), _USER)  # type: ignore[arg-type]
    first = await resp.body_iterator.__anext__()
    assert b"event: resync\n" in first
    assert b'"reason": "epoch_changed"' in first
    await resp.body_iterator.aclose()


@pytest.mark.asyncio
async def test_route_503_when_channel_unwired() -> None:
    with pytest.raises(HTTPException) as exc:
        await stream_events(_request(None), _USER)  # type: ignore[arg-type]
    assert exc.value.status_code == 503

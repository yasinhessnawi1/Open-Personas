"""Spec A11 T2 — the ``LiveSessionRegistry`` adapter over the channel.

``ChannelLiveSessions`` fills the seam ``WebAppDeliverer`` already consumes
(``services/web_deliverer.py``): today the background origination path is wired to
``_NoLiveSessions`` (always ``None`` → PENDING → reload, the R4-C1-23 bug); this
adapter returns a real sink when the owner has an open tab, so a background
``message.delivered`` pushes live. The seam contract is unchanged — A11 only
supplies a non-null registry.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from persona.schema.origination import OriginatedMessage, PersonaIdentityTag
from persona_api.realtime.channel import UserEventChannel
from persona_api.realtime.live_sessions import ChannelLiveSessions
from persona_api.services.web_deliverer import LiveSessionRegistry, LiveSessionSink

EPOCH = "testepoch"


def _msg(*, owner: str = "u1", conversation_id: str | None = "c1") -> OriginatedMessage:
    return OriginatedMessage(
        persona=PersonaIdentityTag(persona_id="p1", display_name="Iris", visual_ref="a.png"),
        owner_user_id=owner,
        content="I finished the digest.",
        conversation_id=conversation_id,
        created_at=datetime.now(UTC),
    )


def test_adapter_satisfies_the_live_session_registry_protocol() -> None:
    ch = UserEventChannel(epoch=EPOCH)
    adapter = ChannelLiveSessions(ch)
    assert isinstance(adapter, LiveSessionRegistry)


def test_lookup_returns_none_when_no_open_tab() -> None:
    ch = UserEventChannel(epoch=EPOCH)
    adapter = ChannelLiveSessions(ch)
    assert adapter.lookup(_msg()) is None  # no subscriber → PENDING, present-on-open


def test_lookup_returns_a_sink_when_the_owner_has_an_open_tab() -> None:
    ch = UserEventChannel(epoch=EPOCH)
    ch.subscribe("u1")
    adapter = ChannelLiveSessions(ch)
    sink = adapter.lookup(_msg(owner="u1"))
    assert sink is not None
    assert isinstance(sink, LiveSessionSink)


def test_lookup_is_owner_scoped() -> None:
    ch = UserEventChannel(epoch=EPOCH)
    ch.subscribe("someone_else")
    adapter = ChannelLiveSessions(ch)
    assert adapter.lookup(_msg(owner="u1")) is None  # u1 has no tab


def test_lookup_returns_none_without_a_conversation_target() -> None:
    ch = UserEventChannel(epoch=EPOCH)
    ch.subscribe("u1")
    adapter = ChannelLiveSessions(ch)
    # No conversation to append to → not live-deliverable (PENDING), not a crash.
    assert adapter.lookup(_msg(owner="u1", conversation_id=None)) is None


@pytest.mark.asyncio
async def test_push_emits_message_delivered_to_the_open_tab() -> None:
    ch = UserEventChannel(epoch=EPOCH)
    tab = ch.subscribe("u1")
    adapter = ChannelLiveSessions(ch)
    sink = adapter.lookup(_msg(owner="u1", conversation_id="c9"))
    assert sink is not None
    await sink.push(_msg(owner="u1", conversation_id="c9"))
    frame = (await tab.next()).decode()
    assert "event: message.delivered\n" in frame
    body = json.loads(frame.split("data: ", 1)[1].rstrip("\n"))
    assert body["type"] == "message.delivered"
    assert body["conversation_id"] == "c9"
    assert body["persona_id"] == "p1"
    assert body["persona_name"] == "Iris"
    assert body["message_id"] is None  # background origination has no persisted id

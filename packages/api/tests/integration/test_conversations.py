"""Conversations + SSE chat (spec 08, T08, KEYSTONE 1, D-08-3).

Drives the real app + Docker Postgres with a fake JWT verifier and a SCRIPTED
ConversationLoop (no real LLM, no T10 runtime factory): the fake loop yields
StreamChunks and mutates the conversation exactly as the real loop does, so we
test the SSE streaming, persist-after-final, and channel passthrough end-to-end.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from persona.backends import StreamChunk, TokenUsage
from persona.schema.conversation import ConversationMessage
from persona.schema.tools import ToolCall, ToolResult
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig
from persona_api.middleware.rls_context import make_rls_engine
from persona_runtime.agentic.events import RunEvent
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator

    from persona.schema.conversation import Conversation
    from persona_runtime.prompt import DocumentContext
    from sqlalchemy import Engine
    from tests.conftest import HashEmbedder384

pytestmark = pytest.mark.integration

_VALID_YAML = """\
schema_version: "1.0"
identity:
  name: Astrid
  role: assistant
  background: |
    A helper.
  language_default: en
  constraints: []
"""


class _ScriptedLoop:
    """A stand-in for ConversationLoop: yields chunks + mutates the conversation
    the way the real loop does (appends user + assistant messages on success).
    Accepts the real ``on_event`` param and fires a ``tier`` event."""

    def __init__(self, reply: str = "Hello there!", *, tier: str = "mid") -> None:
        self._reply = reply
        self._tier = tier

    async def turn(
        self,
        conversation: Conversation,
        user_message: str,
        on_event: Callable[[RunEvent], Awaitable[None]] | None = None,
        *,
        turn_has_image: bool = False,  # noqa: ARG002 — spec-13 T20 compat with real loop kwarg
        images: list[object] | None = None,  # noqa: ARG002 — image-cascade compat with real loop kwarg
        documents: list[object] | None = None,  # noqa: ARG002 — document-cascade compat
        document_context: DocumentContext | None = None,  # noqa: ARG002 — spec-14 compat with real loop kwarg
    ) -> AsyncIterator[StreamChunk]:
        now = datetime.now(UTC)
        if on_event is not None:
            await on_event(RunEvent.tier(self._tier))
        conversation.messages.append(
            ConversationMessage(role="user", content=user_message, created_at=now)
        )
        # stream the reply in two deltas
        yield StreamChunk(delta=self._reply[:5], is_final=False)
        yield StreamChunk(delta=self._reply[5:], is_final=False)
        conversation.messages.append(
            ConversationMessage(role="assistant", content=self._reply, created_at=now)
        )
        yield StreamChunk(
            delta="",
            is_final=True,
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )


class _ToolUsingLoop:
    """A stand-in that dispatches a tool mid-turn — surfaces tool_calling +
    tool_result via on_event exactly as the real loop now does, on a chosen
    (non-frontier) tier. Proves the chat SSE tool-event + real-tier contract."""

    def __init__(self, *, tier: str = "mid") -> None:
        self._tier = tier

    async def turn(
        self,
        conversation: Conversation,
        user_message: str,
        on_event: Callable[[RunEvent], Awaitable[None]] | None = None,
        *,
        turn_has_image: bool = False,  # noqa: ARG002 — spec-13 T20 compat with real loop kwarg
        images: list[object] | None = None,  # noqa: ARG002 — image-cascade compat with real loop kwarg
        documents: list[object] | None = None,  # noqa: ARG002 — document-cascade compat
        document_context: DocumentContext | None = None,  # noqa: ARG002 — spec-14 compat with real loop kwarg
    ) -> AsyncIterator[StreamChunk]:
        now = datetime.now(UTC)
        assert on_event is not None
        await on_event(RunEvent.tier(self._tier))
        conversation.messages.append(
            ConversationMessage(role="user", content=user_message, created_at=now)
        )
        # the model calls web_search; the loop surfaces tool_calling then dispatches
        call = ToolCall(name="web_search", args={"query": "Norwegian tenancy law"}, call_id="c1")
        await on_event(RunEvent.tool_calling(-1, [call]))
        result = ToolResult(
            tool_name="web_search", content="results about husleieloven", call_id="c1"
        )
        await on_event(RunEvent.tool_result(-1, "web_search", result))
        # then the final answer
        conversation.messages.append(
            ConversationMessage(role="assistant", content="Based on my search…", created_at=now)
        )
        yield StreamChunk(delta="Based on my search…", is_final=False)
        yield StreamChunk(
            delta="",
            is_final=True,
            usage=TokenUsage(prompt_tokens=20, completion_tokens=8, total_tokens=28),
        )


class _RoutingLoop:
    """A stand-in that emits a Spec 31 routing summary on the tier event and
    exposes a budget snapshot — proves the additive, SEPARATE routing (D-31-1)
    + budget (D-31-2) fields fold onto the chat `done` event."""

    async def turn(
        self,
        conversation: Conversation,
        user_message: str,
        on_event: Callable[[RunEvent], Awaitable[None]] | None = None,
        *,
        turn_has_image: bool = False,  # noqa: ARG002 — real-loop kwarg compat
        images: list[object] | None = None,  # noqa: ARG002 — real-loop kwarg compat
        documents: list[object] | None = None,  # noqa: ARG002 — document-cascade compat
        document_context: DocumentContext | None = None,  # noqa: ARG002 — real-loop kwarg compat
    ) -> AsyncIterator[StreamChunk]:
        now = datetime.now(UTC)
        assert on_event is not None
        await on_event(
            RunEvent.tier(
                "frontier",
                {
                    "chosen_model": "anthropic/good",
                    "dominant_factor": "quality",
                    "model_fallback_engaged": False,
                    "model_fallback_reason": None,
                },
            )
        )
        conversation.messages.append(
            ConversationMessage(role="user", content=user_message, created_at=now)
        )
        conversation.messages.append(
            ConversationMessage(role="assistant", content="Hi", created_at=now)
        )
        yield StreamChunk(delta="Hi", is_final=False)
        yield StreamChunk(
            delta="",
            is_final=True,
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

    def budget_snapshot(self) -> dict[str, float]:
        return {"session_spent_cents": 1.5, "max_cents_per_session": 50.0}


@pytest.fixture
def client(
    migrated_engine: Engine,  # noqa: ARG001 — schema + grants
    embedder: HashEmbedder384,
    tmp_path: object,
) -> Iterator[tuple[TestClient, str, str]]:
    import os

    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    cfg = APIConfig(
        app_database_url=app_url,
        audit_root=str(tmp_path) + "/audit",
        # Per-test workspace_root so the spec-13 T20 image-upload + image-bearing
        # message tests don't collide with the cwd-relative default.
        workspace_root=str(tmp_path) + "/workspace",  # type: ignore[arg-type]
    )
    app = create_app(cfg)

    async def _fake_verify(token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id=token, email=None)

    async def _build_loop(_persona_id: str) -> _ScriptedLoop:
        return _ScriptedLoop()

    user_id = "user_t08"
    with TestClient(app) as c:
        app.state.verify_token = _fake_verify
        app.state.embedder = embedder
        app.state.build_conversation_loop = _build_loop
        # Drop the lifespan-installed TierRegistry so the persona-detail
        # response surface doesn't lazily instantiate a real chat backend
        # (whose construction raises ``AuthenticationError("missing API key")``
        # when ``ANTHROPIC_API_KEY`` is unset — the standard CI shape).
        # ``_persona_detail`` treats a missing registry as ``capabilities = None``.
        if hasattr(app.state, "tier_registry"):
            app.state.tier_registry = None
        su = make_rls_engine(os.environ["DATABASE_URL"])
        with su.begin() as conn:
            conn.execute(
                text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
                {"i": user_id, "e": f"{user_id}@x.test"},
            )
        su.dispose()
        # create a persona to converse with
        resp = c.post(
            "/v1/personas",
            json={"yaml": _VALID_YAML},
            headers={"Authorization": f"Bearer {user_id}"},
        )
        persona_id = resp.json()["id"]
        yield c, user_id, persona_id
        su = make_rls_engine(os.environ["DATABASE_URL"])
        with su.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE id = :i"), {"i": user_id})
        su.dispose()


def _auth(uid: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {uid}"}


def _new_conversation(c: TestClient, uid: str, persona_id: str) -> str:
    resp = c.post(
        f"/v1/personas/{persona_id}/conversations", json={"title": "t"}, headers=_auth(uid)
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _read_sse(text_body: str) -> list[tuple[str, str]]:
    """Parse an SSE body into (event, data) pairs."""
    events: list[tuple[str, str]] = []
    event = data = None
    for line in text_body.splitlines():
        if line.startswith("event:"):
            event = line.removeprefix("event:").strip()
        elif line.startswith("data:"):
            data = line.removeprefix("data:").strip()
        elif line == "" and event is not None and data is not None:
            events.append((event, data))
            event = data = None
    return events


def test_sse_chat_streams_and_persists(client: tuple[TestClient, str, str]) -> None:
    c, uid, persona_id = client
    conv_id = _new_conversation(c, uid, persona_id)

    resp = c.post(
        f"/v1/conversations/{conv_id}/messages",
        json={"content": "hi"},
        headers=_auth(uid),
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = _read_sse(resp.text)
    kinds = [e for e, _ in events]
    assert "chunk" in kinds
    assert kinds[-1] == "done"
    # reconstruct the reply from the chunk deltas
    import json

    reply = "".join(json.loads(d)["delta"] for e, d in events if e == "chunk")
    assert reply == "Hello there!"

    # persisted: the conversation now has the user + assistant messages
    hist = c.get(f"/v1/conversations/{conv_id}", headers=_auth(uid)).json()
    roles = [m["role"] for m in hist["messages"]]
    assert roles == ["user", "assistant"]
    assert hist["messages"][1]["content"] == "Hello there!"


def test_done_event_has_format_hints(client: tuple[TestClient, str, str]) -> None:
    import json

    c, uid, persona_id = client
    conv_id = _new_conversation(c, uid, persona_id)
    resp = c.post(
        f"/v1/conversations/{conv_id}/messages", json={"content": "hi"}, headers=_auth(uid)
    )
    done = next(json.loads(d) for e, d in _read_sse(resp.text) if e == "done")
    assert done["format_hints"] == {}
    assert done["usage"] == {"prompt_tokens": 10, "completion_tokens": 5}


def test_channel_passthrough_round_trips(client: tuple[TestClient, str, str]) -> None:
    c, uid, persona_id = client
    conv_id = _new_conversation(c, uid, persona_id)
    channel = {
        "platform": "telegram",
        "platform_user_id": "12345",
        "platform_chat_id": "67890",
        "metadata": {"k": "v"},
    }
    resp = c.post(
        f"/v1/conversations/{conv_id}/messages",
        json={"content": "hi", "channel": channel},
        headers=_auth(uid),
    )
    assert resp.status_code == 200
    hist = c.get(f"/v1/conversations/{conv_id}", headers=_auth(uid)).json()
    user_msg = hist["messages"][0]
    assert user_msg["role"] == "user"
    assert user_msg["channel"] == channel


def test_null_channel_is_the_web_ui_case(client: tuple[TestClient, str, str]) -> None:
    c, uid, persona_id = client
    conv_id = _new_conversation(c, uid, persona_id)
    resp = c.post(
        f"/v1/conversations/{conv_id}/messages", json={"content": "hi"}, headers=_auth(uid)
    )
    assert resp.status_code == 200
    hist = c.get(f"/v1/conversations/{conv_id}", headers=_auth(uid)).json()
    assert hist["messages"][0]["channel"] is None


# -- pre-spec-09 patch: delete conversation + auto-title --------------------


def test_delete_conversation_cascades_messages(client: tuple[TestClient, str, str]) -> None:
    c, uid, persona_id = client
    conv_id = _new_conversation(c, uid, persona_id)
    # send a message so there's a row to cascade
    c.post(f"/v1/conversations/{conv_id}/messages", json={"content": "hi"}, headers=_auth(uid))
    # delete it
    assert c.delete(f"/v1/conversations/{conv_id}", headers=_auth(uid)).status_code == 204
    # now gone — a subsequent read is not found
    assert c.get(f"/v1/conversations/{conv_id}", headers=_auth(uid)).status_code == 404


def test_delete_conversation_is_idempotent_404_on_missing(
    client: tuple[TestClient, str, str],
) -> None:
    c, uid, _persona_id = client
    assert c.delete("/v1/conversations/conv_does_not_exist", headers=_auth(uid)).status_code == 404


def test_auto_title_set_on_first_message(client: tuple[TestClient, str, str]) -> None:
    """The first message auto-titles the conversation via the small-tier
    title_builder; subsequent messages don't re-title (best-effort, first-turn)."""
    c, uid, persona_id = client

    titled: list[str] = []

    async def _title_builder(first_message: str) -> str:
        titled.append(first_message)
        return "Norwegian tenancy question"

    c.app.state.title_builder = _title_builder  # type: ignore[attr-defined]

    conv_id = _new_conversation(c, uid, persona_id)
    # default title is "" (created with empty title)
    c.post(
        f"/v1/conversations/{conv_id}/messages",
        json={"content": "help with my lease"},
        headers=_auth(uid),
    )
    title = c.get(f"/v1/conversations/{conv_id}", headers=_auth(uid)).json()["title"]
    assert title == "Norwegian tenancy question"
    assert titled == ["help with my lease"]  # builder saw the first message

    # a second message does NOT re-title (only the first turn)
    c.post(
        f"/v1/conversations/{conv_id}/messages", json={"content": "another one"}, headers=_auth(uid)
    )
    assert len(titled) == 1


def test_auto_title_rejects_prompt_echo(client: tuple[TestClient, str, str]) -> None:
    """R4 T3: a title_builder that echoes the titling INSTRUCTION (a reasoning
    leak) is NOT stored as the title — the sanitiser falls back to the user's
    own opening words, so no conversation ever shows the titling prompt."""
    c, uid, persona_id = client

    async def _echo_title(_first: str) -> str:
        return "We need to output a title of at most 5 words, no quotes, no punctuation, no prose"

    c.app.state.title_builder = _echo_title  # type: ignore[attr-defined]
    conv_id = _new_conversation(c, uid, persona_id)
    c.post(
        f"/v1/conversations/{conv_id}/messages",
        json={"content": "help me understand my Norwegian lease agreement today"},
        headers=_auth(uid),
    )
    title = c.get(f"/v1/conversations/{conv_id}", headers=_auth(uid)).json()["title"]
    assert "at most" not in title.lower()
    assert "no punctuation" not in title.lower()
    assert title == "help me understand my Norwegian lease"


def test_auto_title_failure_is_best_effort(client: tuple[TestClient, str, str]) -> None:
    """A title_builder that raises must not break the turn — the default title
    is kept and the message still persists."""
    c, uid, persona_id = client

    async def _broken_title(_first: str) -> str:
        raise RuntimeError("summariser down")

    c.app.state.title_builder = _broken_title  # type: ignore[attr-defined]
    conv_id = _new_conversation(c, uid, persona_id)
    resp = c.post(
        f"/v1/conversations/{conv_id}/messages", json={"content": "hi"}, headers=_auth(uid)
    )
    assert resp.status_code == 200  # the turn succeeded despite the title failure
    hist = c.get(f"/v1/conversations/{conv_id}", headers=_auth(uid)).json()
    assert [m["role"] for m in hist["messages"]] == ["user", "assistant"]  # both persisted


# -- the gap fix: chat SSE emits tool_calling/tool_result + real tier --------


def test_tool_using_turn_emits_tool_events_and_real_tier(
    client: tuple[TestClient, str, str],
) -> None:
    """A chat turn whose model calls a tool emits tool_calling then tool_result
    (is_error+content, never `error`), in order, BEFORE done — and done.tier is
    the router's actual choice (here 'mid'), not the old hardcoded 'frontier'."""
    import json

    c, uid, persona_id = client

    async def _build_tool_loop(_pid: str) -> _ToolUsingLoop:
        return _ToolUsingLoop(tier="mid")

    c.app.state.build_conversation_loop = _build_tool_loop  # type: ignore[attr-defined]
    conv_id = _new_conversation(c, uid, persona_id)

    resp = c.post(
        f"/v1/conversations/{conv_id}/messages",
        json={"content": "what does Norwegian tenancy law say?"},
        headers=_auth(uid),
    )
    assert resp.status_code == 200
    events = _read_sse(resp.text)
    kinds = [e for e, _ in events]

    # tool_calling then tool_result, both before done (order preserved)
    assert "tool_calling" in kinds
    assert "tool_result" in kinds
    assert kinds.index("tool_calling") < kinds.index("tool_result") < kinds.index("done")

    # tool_calling payload: the shared run-viewer shape (tool_names + tool_calls)
    tool_calling = next(json.loads(d) for e, d in events if e == "tool_calling")
    assert tool_calling["tool_names"] == "web_search"
    assert tool_calling["tool_calls"][0]["name"] == "web_search"
    assert tool_calling["tool_calls"][0]["args"] == {"query": "Norwegian tenancy law"}

    # tool_result payload: is_error + content, NO `error` field (D-03-3)
    tool_result = next(json.loads(d) for e, d in events if e == "tool_result")
    assert tool_result["tool_name"] == "web_search"
    assert tool_result["is_error"] is False
    assert tool_result["content"] == "results about husleieloven"
    assert "error" not in tool_result

    # done.tier is the real router choice, not hardcoded "frontier"
    done = next(json.loads(d) for e, d in events if e == "done")
    assert done["tier"] == "mid"


def test_get_conversation_projects_stream_events_as_events(
    client: tuple[TestClient, str, str],
) -> None:
    """P3 (P3-D-2): the history GET projects the persisted ordered log
    (``messages.stream_events``) onto each assistant message's ``events`` field,
    so a reloaded conversation can reconstruct the interleaved view. The shape is
    the persisted hybrid — RunEvent dumps (``{type, step, data, timestamp}``)
    interleaved with text deltas (``{kind: "text", delta}``) in emission order."""
    import json

    c, uid, persona_id = client

    async def _build_tool_loop(_pid: str) -> _ToolUsingLoop:
        return _ToolUsingLoop(tier="mid")

    c.app.state.build_conversation_loop = _build_tool_loop  # type: ignore[attr-defined]
    conv_id = _new_conversation(c, uid, persona_id)

    resp = c.post(
        f"/v1/conversations/{conv_id}/messages",
        json={"content": "what does Norwegian tenancy law say?"},
        headers=_auth(uid),
    )
    assert resp.status_code == 200
    _ = _read_sse(resp.text)  # drain the stream so the turn finalizes

    hist = c.get(f"/v1/conversations/{conv_id}", headers=_auth(uid)).json()
    assistant = hist["messages"][1]
    assert assistant["role"] == "assistant"

    events = assistant["events"]
    assert isinstance(events, list)
    assert events, "assistant turn carries the persisted log"

    # the RunEvent dumps for the tool call + result are present, in order
    types = [e.get("type") for e in events if "type" in e]
    assert "tool_calling" in types
    assert "tool_result" in types
    assert types.index("tool_calling") < types.index("tool_result")

    tool_calling = next(e for e in events if e.get("type") == "tool_calling")
    assert tool_calling["data"]["tool_calls"][0]["name"] == "web_search"
    tool_result = next(e for e in events if e.get("type") == "tool_result")
    assert tool_result["data"]["tool_name"] == "web_search"
    assert tool_result["data"]["content"] == "results about husleieloven"

    # the reply text rides as interleaved text deltas (the hybrid shape)
    text_deltas = [e for e in events if e.get("kind") == "text"]
    assert text_deltas, "text spans are persisted interleaved with the tool events"
    assert "".join(e["delta"] for e in text_deltas) == "".join(
        json.loads(d)["delta"] for e, d in _read_sse(resp.text) if e == "chunk"
    )


def test_legacy_null_stream_events_projects_events_none(
    client: tuple[TestClient, str, str],
) -> None:
    """P3 (P3-D-2 / P3-D-8, criterion 5): a row with a NULL ``stream_events``
    column projects ``events: null`` — the byte-exact text-only back-compat path
    (the ``tier_used`` nullable-additive precedent). The user message row always
    carries NULL ``stream_events`` (only assistant turns accumulate a log), so it
    is the in-band proxy for a legacy/non-streamed row."""
    c, uid, persona_id = client
    conv_id = _new_conversation(c, uid, persona_id)
    resp = c.post(
        f"/v1/conversations/{conv_id}/messages", json={"content": "hi"}, headers=_auth(uid)
    )
    assert resp.status_code == 200
    _ = _read_sse(resp.text)

    hist = c.get(f"/v1/conversations/{conv_id}", headers=_auth(uid)).json()
    user_msg = hist["messages"][0]
    assert user_msg["role"] == "user"
    # NULL column → events is null (text-only degrade); the rest is unchanged.
    assert user_msg["events"] is None
    assert user_msg["content"] == "hi"


def test_no_tool_turn_done_tier_reflects_router_choice(
    client: tuple[TestClient, str, str],
) -> None:
    """Even a no-tool turn carries the real tier on done (the default scripted
    loop fires tier='mid')."""
    import json

    c, uid, persona_id = client
    conv_id = _new_conversation(c, uid, persona_id)
    resp = c.post(
        f"/v1/conversations/{conv_id}/messages", json={"content": "hi"}, headers=_auth(uid)
    )
    done = next(json.loads(d) for e, d in _read_sse(resp.text) if e == "done")
    assert done["tier"] == "mid"  # _ScriptedLoop fires tier('mid'), not "frontier"
    # Back-compat: a rule-based loop emits neither routing nor budget (D-31-1/2).
    assert "routing" not in done
    assert "budget" not in done


def test_done_carries_routing_and_budget_when_intelligent(
    client: tuple[TestClient, str, str],
) -> None:
    """Spec 31 (D-31-1/2): an intelligent-routing turn folds the concise model
    decision + the per-session budget snapshot onto `done`, as SEPARATE fields;
    the raw score vector never reaches the wire."""
    import json

    c, uid, persona_id = client

    async def _build_routing_loop(_pid: str) -> _RoutingLoop:
        return _RoutingLoop()

    c.app.state.build_conversation_loop = _build_routing_loop  # type: ignore[attr-defined]
    conv_id = _new_conversation(c, uid, persona_id)
    resp = c.post(
        f"/v1/conversations/{conv_id}/messages", json={"content": "hi"}, headers=_auth(uid)
    )
    done = next(json.loads(d) for e, d in _read_sse(resp.text) if e == "done")
    # routing (D-31-1): structured fields, no score vector
    assert done["routing"]["chosen_model"] == "anthropic/good"
    assert done["routing"]["dominant_factor"] == "quality"
    assert done["routing"]["model_fallback_engaged"] is False
    assert "score_vector" not in done["routing"]
    # budget (D-31-2): SEPARATE field; session spend incl. the current turn
    assert done["budget"]["session_spent_cents"] == 1.5
    assert done["budget"]["max_cents_per_session"] == 50.0
    assert "max_cents_per_turn" not in done["budget"]


# -- Spec 13 T20: image-bearing message persistence --------------------------
#
# These tests close §9 criteria #4 (response-reflection half) and #11
# (multi-image messages can enter the system). The migration 004 adds the
# ``messages.images`` JSONB column; PostMessageRequest gains ``images:
# list[ImageRef] | None`` (cap 4 per D-13-5); the route + chat_service
# thread the refs through to _persist_turn.


def _tiny_png() -> bytes:
    """Tiny valid 1×1 PNG (RGB) — same construction as test_image_service."""
    import struct
    import zlib

    def chunk(t: bytes, d: bytes) -> bytes:
        crc = zlib.crc32(t + d) & 0xFFFFFFFF
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", crc)

    ihdr = struct.pack(">II", 1, 1) + bytes([8, 2, 0, 0, 0])
    idat_data = zlib.compress(b"\x00\xff\x00\x00")
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", idat_data)
        + chunk(b"IEND", b"")
    )


def _upload_png(c: TestClient, uid: str, persona_id: str) -> str:
    """Upload a tiny PNG via the uploads route and return the workspace_path."""
    png = _tiny_png()
    files = {"file": ("tiny.png", png, "image/png")}
    resp = c.post(f"/v1/personas/{persona_id}/uploads", files=files, headers=_auth(uid))
    assert resp.status_code == 201, resp.text
    return str(resp.json()["workspace_path"])


def _fetch_images_jsonb(conv_id: str) -> list[object]:
    """Read the ``messages.images`` JSONB column directly (RLS-bypass superuser)."""
    import os

    from persona_api.middleware.rls_context import make_rls_engine
    from sqlalchemy import text as sql_text

    su = make_rls_engine(os.environ["DATABASE_URL"])
    try:
        with su.begin() as conn:
            rows = conn.execute(
                sql_text(
                    "SELECT images FROM messages WHERE conversation_id = :cid "
                    "ORDER BY created_at ASC"
                ),
                {"cid": conv_id},
            ).fetchall()
    finally:
        su.dispose()
    return [r[0] for r in rows]


def test_image_bearing_message_persists_with_images_column(
    client: tuple[TestClient, str, str],
) -> None:
    """The image-bearing POST persists the images JSONB column on the user row.

    §9 criterion #4 (response-reflection half): the chat body's ``images``
    field round-trips through the migration into the messages.images JSONB
    column and survives the persist-after-final discipline.
    """
    c, uid, persona_id = client

    ref = _upload_png(c, uid, persona_id)
    conv_id = _new_conversation(c, uid, persona_id)

    resp = c.post(
        f"/v1/conversations/{conv_id}/messages",
        json={
            "content": "describe this",
            "images": [{"workspace_path": ref, "media_type": "image/png"}],
        },
        headers=_auth(uid),
    )
    assert resp.status_code == 200, resp.text

    images_per_row = _fetch_images_jsonb(conv_id)
    # First row is the user message; second is the assistant reply.
    assert len(images_per_row) == 2, images_per_row
    assert images_per_row[0] == [{"workspace_path": ref, "media_type": "image/png"}]
    # Assistant reply NEVER carries inbound images.
    assert images_per_row[1] is None


def test_multi_image_message_preserves_order(
    client: tuple[TestClient, str, str],
) -> None:
    """§9 criterion #11 e2e proof: 3 image refs persist in caller order.

    This is the LOAD-BEARING multi-image guarantee — the persisted JSONB
    array must mirror the ``images=[ref1, ref2, ref3]`` body verbatim,
    including ordering (chat-display order is a Spec 09 concern that
    depends on this ordering invariant holding here).
    """
    c, uid, persona_id = client

    ref1 = _upload_png(c, uid, persona_id)
    # Three distinct refs would need three distinct uploads; for this test
    # we reuse the same content-addressed ref three times so the assertion
    # focuses on ordering not uniqueness.
    refs = [ref1, ref1, ref1]
    conv_id = _new_conversation(c, uid, persona_id)

    resp = c.post(
        f"/v1/conversations/{conv_id}/messages",
        json={
            "content": "compare these",
            "images": [{"workspace_path": r, "media_type": "image/png"} for r in refs],
        },
        headers=_auth(uid),
    )
    assert resp.status_code == 200, resp.text

    images_per_row = _fetch_images_jsonb(conv_id)
    assert images_per_row[0] == [{"workspace_path": r, "media_type": "image/png"} for r in refs]


def test_text_only_path_byte_for_byte_unchanged(
    client: tuple[TestClient, str, str],
) -> None:
    """T03/T13 regression: text-only POSTs persist ``images=NULL``.

    The legacy text-only chat body must remain byte-for-byte unchanged —
    no images field implies the persisted images JSONB column is NULL,
    matching the spec-08 row shape exactly. This is the T20 acceptance
    invariant.
    """
    c, uid, persona_id = client
    conv_id = _new_conversation(c, uid, persona_id)

    resp = c.post(
        f"/v1/conversations/{conv_id}/messages", json={"content": "hi"}, headers=_auth(uid)
    )
    assert resp.status_code == 200

    images_per_row = _fetch_images_jsonb(conv_id)
    assert images_per_row == [None, None], (
        "text-only POST must persist images=NULL on every row (T03/T13 invariant)"
    )


def test_images_field_rejects_over_cap(client: tuple[TestClient, str, str]) -> None:
    """D-13-5 cap: 5 images in one message is a 422 validation error."""
    c, uid, persona_id = client
    conv_id = _new_conversation(c, uid, persona_id)

    over_cap = [
        {"workspace_path": f"uploads/x{i}.png", "media_type": "image/png"} for i in range(5)
    ]
    resp = c.post(
        f"/v1/conversations/{conv_id}/messages",
        json={"content": "too many", "images": over_cap},
        headers=_auth(uid),
    )
    assert resp.status_code == 422, resp.text


def test_images_field_rejects_when_extra_forbid_intent_held(
    client: tuple[TestClient, str, str],
) -> None:
    """``extra="forbid"`` on PostMessageRequest stays enforced after T20."""
    c, uid, persona_id = client
    conv_id = _new_conversation(c, uid, persona_id)

    resp = c.post(
        f"/v1/conversations/{conv_id}/messages",
        json={"content": "hi", "some_unknown_field": "nope"},
        headers=_auth(uid),
    )
    assert resp.status_code == 422, resp.text


# -- last-message preview on the LIST endpoint (sidebar chat previews) -------


def _list_conversations(c: TestClient, uid: str) -> list[dict[str, object]]:
    resp = c.get("/v1/conversations", headers=_auth(uid))
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_list_preview_is_none_when_no_messages(client: tuple[TestClient, str, str]) -> None:
    """A freshly created conversation (no messages) lists with both
    last-message fields as None — the UI falls back to the title."""
    c, uid, persona_id = client
    conv_id = _new_conversation(c, uid, persona_id)

    item = next(row for row in _list_conversations(c, uid) if row["id"] == conv_id)
    assert item["last_message_preview"] is None
    assert item["last_message_role"] is None


def test_list_preview_reflects_latest_message_and_role(
    client: tuple[TestClient, str, str],
) -> None:
    """After a turn the LIST preview is the MOST RECENT message (the assistant
    reply, here) with its role — not the user's first message."""
    c, uid, persona_id = client
    conv_id = _new_conversation(c, uid, persona_id)
    c.post(f"/v1/conversations/{conv_id}/messages", json={"content": "hi"}, headers=_auth(uid))

    item = next(row for row in _list_conversations(c, uid) if row["id"] == conv_id)
    # _ScriptedLoop appends user("hi") then assistant("Hello there!"); the latest
    # message is the assistant reply.
    assert item["last_message_preview"] == "Hello there!"
    assert item["last_message_role"] == "assistant"


def test_list_preview_truncates_long_message(client: tuple[TestClient, str, str]) -> None:
    """A long assistant reply is trimmed + truncated server-side to the preview
    cap with a trailing ellipsis — the full body never ships in the list."""
    c, uid, persona_id = client

    long_reply = "word " * 200  # ~1000 chars, well over the 120 cap

    async def _build_long_loop(_pid: str) -> _ScriptedLoop:
        return _ScriptedLoop(reply=long_reply)

    c.app.state.build_conversation_loop = _build_long_loop  # type: ignore[attr-defined]
    conv_id = _new_conversation(c, uid, persona_id)
    c.post(f"/v1/conversations/{conv_id}/messages", json={"content": "go"}, headers=_auth(uid))

    item = next(row for row in _list_conversations(c, uid) if row["id"] == conv_id)
    preview = item["last_message_preview"]
    assert isinstance(preview, str)
    assert len(preview) <= 120
    assert preview.endswith("…")
    assert preview != long_reply


def test_list_preserves_updated_at_desc_ordering(client: tuple[TestClient, str, str]) -> None:
    """The preview join must not disturb the existing updated_at-desc order:
    the most recently active conversation sorts first."""
    c, uid, persona_id = client
    first = _new_conversation(c, uid, persona_id)
    _second = _new_conversation(c, uid, persona_id)
    # Activity on `first` bumps its updated_at past `_second` (persist sets it).
    c.post(f"/v1/conversations/{first}/messages", json={"content": "hi"}, headers=_auth(uid))

    rows = _list_conversations(c, uid)
    ours = [row["id"] for row in rows if row["id"] in {first, _second}]
    assert ours[0] == first, "the conversation with the newest activity sorts first"


def test_list_preview_is_rls_scoped(client: tuple[TestClient, str, str]) -> None:
    """Another tenant's conversations + their message previews never leak into
    the caller's list (RLS scope on the windowed message join)."""
    c, uid, persona_id = client
    # Caller's own conversation with a message.
    mine = _new_conversation(c, uid, persona_id)
    c.post(f"/v1/conversations/{mine}/messages", json={"content": "mine"}, headers=_auth(uid))

    # A second tenant with their own persona + conversation + message.
    other = "user_t08_other"
    su = make_rls_engine(os.environ["DATABASE_URL"])
    with su.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
            {"i": other, "e": f"{other}@x.test"},
        )
    su.dispose()
    other_persona = c.post("/v1/personas", json={"yaml": _VALID_YAML}, headers=_auth(other)).json()[
        "id"
    ]
    other_conv = _new_conversation(c, other, other_persona)
    c.post(
        f"/v1/conversations/{other_conv}/messages",
        json={"content": "secret-other-tenant-body"},
        headers=_auth(other),
    )

    # The caller's list contains only their conversation; no leak of the other
    # tenant's conversation OR its message preview.
    rows = _list_conversations(c, uid)
    ids = {row["id"] for row in rows}
    assert mine in ids
    assert other_conv not in ids
    assert all(row["last_message_preview"] != "secret-other-tenant-body" for row in rows)

    # cleanup the extra tenant
    su = make_rls_engine(os.environ["DATABASE_URL"])
    with su.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id = :i"), {"i": other})
    su.dispose()


# -- Spec P1 reattach surface (T4) — CI-verified (real Postgres) ---------------


def test_active_turn_404_when_no_turn_in_flight(client: tuple[TestClient, str, str]) -> None:
    """With the scripted loop completing synchronously, a fresh conversation has no
    live turn — all three reattach endpoints 404 (the client then reconciles)."""
    c, uid, persona_id = client
    conv = _new_conversation(c, uid, persona_id)

    assert c.get(f"/v1/conversations/{conv}/active-turn", headers=_auth(uid)).status_code == 404
    assert (
        c.get(f"/v1/conversations/{conv}/active-turn/events", headers=_auth(uid)).status_code == 404
    )
    assert (
        c.post(f"/v1/conversations/{conv}/active-turn/cancel", headers=_auth(uid)).status_code
        == 404
    )


def test_active_turn_endpoints_are_rls_scoped(client: tuple[TestClient, str, str]) -> None:
    """A conversation that isn't the caller's is invisible → 404 on every reattach
    endpoint (the get_conversation ownership pre-check), never a cross-tenant leak."""
    c, uid, persona_id = client
    conv = _new_conversation(c, uid, persona_id)
    other = "user_t08_other"
    su = make_rls_engine(os.environ["DATABASE_URL"])
    with su.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
            {"i": other, "e": f"{other}@x.test"},
        )
    su.dispose()
    try:
        assert (
            c.get(f"/v1/conversations/{conv}/active-turn", headers=_auth(other)).status_code == 404
        )
        assert (
            c.post(f"/v1/conversations/{conv}/active-turn/cancel", headers=_auth(other)).status_code
            == 404
        )
    finally:
        su = make_rls_engine(os.environ["DATABASE_URL"])
        with su.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE id = :i"), {"i": other})
        su.dispose()


def test_completed_turn_leaves_no_active_turn(client: tuple[TestClient, str, str]) -> None:
    """After a turn completes (synchronous scripted loop), the conversation has the
    finalized messages and NO active turn — reattach 404s, reconcile via history."""
    c, uid, persona_id = client
    conv = _new_conversation(c, uid, persona_id)
    r = c.post(f"/v1/conversations/{conv}/messages", json={"content": "hi"}, headers=_auth(uid))
    assert r.status_code == 200
    _ = r.text  # drain the SSE so the detached turn finalizes

    assert c.get(f"/v1/conversations/{conv}/active-turn", headers=_auth(uid)).status_code == 404
    hist = c.get(f"/v1/conversations/{conv}", headers=_auth(uid)).json()
    assert [m["role"] for m in hist["messages"]] == ["user", "assistant"]


# -- V9 T1: the origin marker contract (V9-D-3) -----------------------------


def test_create_defaults_origin_to_chat(client: tuple[TestClient, str, str]) -> None:
    """The create endpoint defaults ``origin`` to ``'chat'`` — the request field
    is optional and every text-path conversation is chat-born."""
    c, uid, persona_id = client
    resp = c.post(
        f"/v1/personas/{persona_id}/conversations", json={"title": "t"}, headers=_auth(uid)
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["origin"] == "chat"


def test_create_with_origin_call_round_trips(client: tuple[TestClient, str, str]) -> None:
    """The web marks a call-born conversation ``origin='call'`` at create time
    (V9-D-X-marker-writer-web); it round-trips through the summary AND the detail
    view — the marker is durable, not in-session."""
    c, uid, persona_id = client
    resp = c.post(
        f"/v1/personas/{persona_id}/conversations",
        json={"title": "", "origin": "call"},
        headers=_auth(uid),
    )
    assert resp.status_code == 201, resp.text
    conv_id = resp.json()["id"]
    assert resp.json()["origin"] == "call"

    # durable on the detail view (read back from the DB, not the create response).
    # NB: a call-born conversation is intentionally ABSENT from the chat LIST
    # (V9-D-3 exclusion, covered by test_chat_list_excludes_call_only_conversation)
    # — the detail view is how the Calls surface reads it back.
    detail = c.get(f"/v1/conversations/{conv_id}", headers=_auth(uid)).json()
    assert detail["origin"] == "call"


def test_create_rejects_unknown_origin(client: tuple[TestClient, str, str]) -> None:
    """``origin`` is a closed vocabulary (``chat | call``); anything else is a
    422 at the request boundary (the seam stays closed — no chat code inspecting
    voice state, no free-form origin)."""
    c, uid, persona_id = client
    resp = c.post(
        f"/v1/personas/{persona_id}/conversations",
        json={"title": "", "origin": "sms"},
        headers=_auth(uid),
    )
    assert resp.status_code == 422, resp.text


# -- V9 T2: read-side classification — the chat list excludes call-born (V9-D-3) ---


def _new_conversation_with_origin(
    c: TestClient, uid: str, persona_id: str, origin: str, *, title: str = "t"
) -> str:
    resp = c.post(
        f"/v1/personas/{persona_id}/conversations",
        json={"title": title, "origin": origin},
        headers=_auth(uid),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_chat_list_excludes_call_only_conversation(client: tuple[TestClient, str, str]) -> None:
    """Acceptance #1: a call-only session (origin='call', empty title) NEVER
    appears in the chat list as an empty "Untitled conversation"."""
    c, uid, persona_id = client
    chat_id = _new_conversation_with_origin(c, uid, persona_id, "chat")
    call_id = _new_conversation_with_origin(c, uid, persona_id, "call", title="")

    ids = {row["id"] for row in _list_conversations(c, uid)}
    assert chat_id in ids
    assert call_id not in ids


def test_chat_list_filter_is_marker_only_not_content(
    client: tuple[TestClient, str, str],
) -> None:
    """The only-seam line: the exclusion keys STRICTLY on ``origin``, never on
    content. A call-born conversation that ALSO has text (a mixed call+text
    conversation) is STILL excluded from the chat list — proving the chat list
    reads only the marker, never voice/call state or message presence."""
    c, uid, persona_id = client
    call_id = _new_conversation_with_origin(c, uid, persona_id, "call", title="")
    # give the call-born conversation real text content (a mixed conversation).
    r = c.post(f"/v1/conversations/{call_id}/messages", json={"content": "hi"}, headers=_auth(uid))
    assert r.status_code == 200
    _ = r.text  # drain the SSE so the turn persists

    # Despite having messages, it is excluded — the filter is origin-only.
    ids = {row["id"] for row in _list_conversations(c, uid)}
    assert call_id not in ids
    # Sanity: it really does have messages (so the exclusion is NOT "because empty").
    detail = c.get(f"/v1/conversations/{call_id}", headers=_auth(uid)).json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]


def test_chat_that_got_called_stays_in_chat_list(client: tuple[TestClient, str, str]) -> None:
    """The discipline test (chat-list half): a chat-born conversation keeps its
    IMMUTABLE origin='chat' regardless of any later call, so it STAYS in the chat
    list. (Its appearance in the Calls surface — via the call-record — lands in
    the call-record task; the chat list is wholly independent of call state.)"""
    c, uid, persona_id = client
    chat_id = _new_conversation_with_origin(c, uid, persona_id, "chat")
    # A real text turn — an ordinary chat.
    r = c.post(f"/v1/conversations/{chat_id}/messages", json={"content": "hi"}, headers=_auth(uid))
    assert r.status_code == 200
    _ = r.text

    item = next((row for row in _list_conversations(c, uid) if row["id"] == chat_id), None)
    assert item is not None, "a chat-born conversation must remain in the chat list"
    assert item["origin"] == "chat"


# -- V9 T4: the transcript STORE — voice turns persist to messages (V9-D-1/D-2) ---


def test_voice_turn_persists_to_messages_and_renders(
    client: tuple[TestClient, str, str],
) -> None:
    """The load-bearing STORE (V9-D-1): a committed voice turn is persisted to the
    ``messages`` table by the voice transcript writer — byte-for-byte with a chat
    turn (V9-D-2) — so a call's transcript renders identically under GET
    /v1/conversations/{id} (D-V7-7's recap). Before the write the call-born
    conversation is EMPTY (the gap V9 fills: voice reached only episodic +
    in-memory, never ``messages``)."""
    import os

    from persona_voice.model.transcript import VoiceTranscriptWriter
    from persona_voice.session.state_machine import make_session_rls_engine

    c, uid, persona_id = client
    # a call-born conversation (origin='call'); voice never writes via the api.
    conv_id = _new_conversation_with_origin(c, uid, persona_id, "call", title="")

    # the gap: a call-only conversation has NO messages until the transcript write.
    before = c.get(f"/v1/conversations/{conv_id}", headers=_auth(uid)).json()
    assert before["messages"] == []

    # the voice runtime writes via its session RLS engine (app.current_user_id =
    # uid), exactly as build_agent_session wires the writer.
    engine = make_session_rls_engine(os.environ["APP_DATABASE_URL"], user_id=uid)
    try:
        writer = VoiceTranscriptWriter(engine=engine, conversation_id=conv_id)
        writer.record_turn(
            user_text="what are my rights?",
            heard_text="You have strong rights.",
            truncated=False,
            now=datetime(2026, 6, 25, 12, 0, 0, tzinfo=UTC),
        )
        # byte-for-byte shape, read back under RLS: 2 rows, ``msg_`` ids, the voice
        # marker on channel, streaming_status NULL (final — NOT 'running', so the
        # one-active-turn index is untouched), originated false (solicited).
        with engine.begin() as conn:
            rows = (
                conn.execute(
                    text(
                        "SELECT id, role, content, channel, streaming_status, originated "
                        "FROM messages WHERE conversation_id = :c ORDER BY created_at ASC"
                    ),
                    {"c": conv_id},
                )
                .mappings()
                .all()
            )
    finally:
        engine.dispose()

    assert [r["role"] for r in rows] == ["user", "assistant"]
    assert all(r["id"].startswith("msg_") for r in rows)
    assert rows[0]["content"] == "what are my rights?"
    assert rows[1]["content"] == "You have strong rights."
    assert rows[0]["channel"]["modality"] == "voice"
    assert rows[1]["channel"] == {"modality": "voice", "truncated": "false"}
    # final non-streamed rows → render identically to a finalized chat message.
    assert all(r["streaming_status"] is None for r in rows)
    assert all(r["originated"] is False for r in rows)

    # renders under the SAME read surface as a text chat (D-V7-7's /chat/{id}).
    after = c.get(f"/v1/conversations/{conv_id}", headers=_auth(uid)).json()
    assert [m["role"] for m in after["messages"]] == ["user", "assistant"]
    assert after["messages"][0]["content"] == "what are my rights?"
    assert after["messages"][1]["content"] == "You have strong rights."
    # the MessageView surfaces the voice marker on channel (V9-D-4).
    assert after["messages"][1]["channel"]["modality"] == "voice"

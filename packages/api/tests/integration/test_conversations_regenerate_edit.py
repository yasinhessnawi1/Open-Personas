"""Regenerate + edit-and-rerun (R9-025 leg C) — a REAL retry, not an echo-resend.

The operator pass on the shipped "retry" (wave 2a) found it was a client-side
echo-resend: the SAME preceding user message got re-sent as a brand-new turn, so
the model saw its own just-superseded reply still sitting in context and produced
a confused answer. This drives the REAL HTTP → chat_service → MessagesTurnSink →
Postgres chain (mirrors ``test_conversations.py``'s harness) to prove the fix:
supersede + context-rebuild exclusion + web-listing exclusion + one-active-turn
409 + tail-only 422 + the R9-022 heal-path composition + billing rides the normal
turn path.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from persona.backends import StreamChunk, TokenUsage
from persona.schema.conversation import Conversation, ConversationMessage
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.background.chat_turn_worker import ChatTurnHandle, ChatTurnRegistry
from persona_api.config import APIConfig
from persona_api.errors import TurnAlreadyActiveError
from persona_api.middleware.rls_context import make_rls_engine
from persona_api.services import chat_service
from persona_api.services.chat_turn_sink import MessagesTurnSink
from persona_runtime.agentic.events import RunEvent
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator

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


class _RecordingLoop:
    """A stand-in ConversationLoop that RECORDS the exact (history, user_message)
    it was called with on every invocation — the load-bearing proof that a
    superseded reply never re-enters the model's prompt (not just that it's
    hidden from the listing). ``reply`` is a public, reassignable attribute so a
    test can make each successive turn (send / regenerate / edit) produce a
    DISTINCT reply, to tell "the old one" apart from "the new one" unambiguously.
    """

    def __init__(self, reply: str = "Hello there!") -> None:
        self.reply = reply
        self.calls: list[tuple[list[object], str]] = []

    async def turn(
        self,
        conversation: Conversation,
        user_message: str,
        on_event: Callable[[RunEvent], Awaitable[None]] | None = None,
        *,
        turn_has_image: bool = False,  # noqa: ARG002 — real-loop kwarg compat
        images: list[object] | None = None,  # noqa: ARG002 — real-loop kwarg compat
        documents: list[object] | None = None,  # noqa: ARG002 — real-loop kwarg compat
        document_context: DocumentContext | None = None,  # noqa: ARG002 — real-loop kwarg compat
    ) -> AsyncIterator[StreamChunk]:
        now = datetime.now(UTC)
        # Snapshot exactly what this call's LOADED HISTORY contained — before the
        # loop appends anything — so the test can assert a superseded reply is
        # provably absent, not merely hidden from a LATER read.
        self.calls.append(([m.content for m in conversation.messages], user_message))
        if on_event is not None:
            await on_event(RunEvent.tier("mid"))
        conversation.messages.append(
            ConversationMessage(role="user", content=user_message, created_at=now)
        )
        reply = self.reply
        yield StreamChunk(delta=reply[:5], is_final=False)
        yield StreamChunk(delta=reply[5:], is_final=False)
        conversation.messages.append(
            ConversationMessage(role="assistant", content=reply, created_at=now)
        )
        yield StreamChunk(
            delta="",
            is_final=True,
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )


@pytest.fixture
def client(
    migrated_engine: Engine,  # noqa: ARG001 — schema + grants
    embedder: HashEmbedder384,
    tmp_path: object,
) -> Iterator[tuple[TestClient, str, str, _RecordingLoop]]:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    cfg = APIConfig(
        app_database_url=app_url,
        audit_root=str(tmp_path) + "/audit",
        workspace_root=str(tmp_path) + "/workspace",  # type: ignore[arg-type]
    )
    app = create_app(cfg)

    async def _fake_verify(token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id=token, email=None)

    loop = _RecordingLoop()

    async def _build_loop(_persona_id: str) -> _RecordingLoop:
        return loop

    user_id = "user_r9025c"
    with TestClient(app) as c:
        app.state.verify_token = _fake_verify
        app.state.embedder = embedder
        app.state.build_conversation_loop = _build_loop
        if hasattr(app.state, "tier_registry"):
            app.state.tier_registry = None
        su = make_rls_engine(os.environ["DATABASE_URL"])
        with su.begin() as conn:
            conn.execute(
                text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
                {"i": user_id, "e": f"{user_id}@x.test"},
            )
        su.dispose()
        resp = c.post(
            "/v1/personas",
            json={"yaml": _VALID_YAML},
            headers={"Authorization": f"Bearer {user_id}"},
        )
        persona_id = resp.json()["id"]
        yield c, user_id, persona_id, loop
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


# -- Regenerate ---------------------------------------------------------------


def test_regenerate_new_reply_present_old_excluded_from_listing_and_context(
    client: tuple[TestClient, str, str, _RecordingLoop],
) -> None:
    """The load-bearing fix: the regenerated turn's context never carries the
    superseded reply (proved via the loop's own recorded call history — not just
    that the listing hides it), and the SAME preceding user question is re-fed."""
    c, uid, persona_id, loop = client
    conv_id = _new_conversation(c, uid, persona_id)

    loop.reply = "First answer"
    r1 = c.post(
        f"/v1/conversations/{conv_id}/messages",
        json={"content": "what is the capital of Norway?"},
        headers=_auth(uid),
    )
    assert r1.status_code == 200, r1.text
    hist1 = c.get(f"/v1/conversations/{conv_id}", headers=_auth(uid)).json()
    assert [m["role"] for m in hist1["messages"]] == ["user", "assistant"]
    old_assistant_id = hist1["messages"][1]["id"]
    assert hist1["messages"][1]["content"] == "First answer"

    loop.reply = "Second regenerated answer"
    r2 = c.post(
        f"/v1/conversations/{conv_id}/messages/{old_assistant_id}/regenerate",
        headers=_auth(uid),
    )
    assert r2.status_code == 200, r2.text
    events = _read_sse(r2.text)
    assert events[-1][0] == "done"

    # Web listing: old reply GONE, new reply present, exactly one exchange
    # (visually replaced, not duplicated).
    hist2 = c.get(f"/v1/conversations/{conv_id}", headers=_auth(uid)).json()
    assert [m["role"] for m in hist2["messages"]] == ["user", "assistant"]
    assert hist2["messages"][1]["content"] == "Second regenerated answer"
    assert hist2["messages"][1]["id"] != old_assistant_id
    contents = [m["content"] for m in hist2["messages"]]
    assert "First answer" not in contents

    # Context proof: two turns ran; the regenerate turn's LOADED history + fed
    # user_message never carried "First answer" anywhere, and the SAME preceding
    # user question was re-fed verbatim (context ends exactly at it).
    assert len(loop.calls) == 2
    _first_history, first_user_message = loop.calls[0]
    second_history, second_user_message = loop.calls[1]
    assert first_user_message == "what is the capital of Norway?"
    assert second_user_message == "what is the capital of Norway?"
    assert "First answer" not in second_history

    # The old (superseded) row itself was never deleted — the additive invariant.
    su = make_rls_engine(os.environ["DATABASE_URL"])
    with su.begin() as conn:
        row = (
            conn.execute(
                text("SELECT content, superseded_at FROM messages WHERE id = :id"),
                {"id": old_assistant_id},
            )
            .mappings()
            .first()
        )
    su.dispose()
    assert row is not None
    assert row["content"] == "First answer"
    assert row["superseded_at"] is not None


def test_regenerate_non_tail_target_is_422(
    client: tuple[TestClient, str, str, _RecordingLoop],
) -> None:
    """v1 tail-only scope: regenerating an OLDER (non-last) assistant reply 422s
    and leaves that row completely untouched — no branching of older history."""
    c, uid, persona_id, loop = client
    conv_id = _new_conversation(c, uid, persona_id)

    loop.reply = "Answer 1"
    c.post(f"/v1/conversations/{conv_id}/messages", json={"content": "q1"}, headers=_auth(uid))
    hist1 = c.get(f"/v1/conversations/{conv_id}", headers=_auth(uid)).json()
    first_assistant_id = hist1["messages"][1]["id"]

    loop.reply = "Answer 2"
    c.post(f"/v1/conversations/{conv_id}/messages", json={"content": "q2"}, headers=_auth(uid))

    resp = c.post(
        f"/v1/conversations/{conv_id}/messages/{first_assistant_id}/regenerate",
        headers=_auth(uid),
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["context"]["reason"] == "not_last_assistant_message"

    hist2 = c.get(f"/v1/conversations/{conv_id}", headers=_auth(uid)).json()
    assert [m["role"] for m in hist2["messages"]] == ["user", "assistant", "user", "assistant"]
    assert hist2["messages"][1]["content"] == "Answer 1"  # untouched, not superseded


def test_regenerate_unknown_message_is_422_not_500(
    client: tuple[TestClient, str, str, _RecordingLoop],
) -> None:
    c, uid, persona_id, loop = client
    conv_id = _new_conversation(c, uid, persona_id)
    loop.reply = "hi there"
    c.post(f"/v1/conversations/{conv_id}/messages", json={"content": "hi"}, headers=_auth(uid))

    resp = c.post(
        f"/v1/conversations/{conv_id}/messages/msg_does_not_exist/regenerate",
        headers=_auth(uid),
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["context"]["reason"] == "not_found"


def test_regenerate_bills_the_new_turn_normally(
    client: tuple[TestClient, str, str, _RecordingLoop],
) -> None:
    """M2 bills the new turn like any other turn — the credits ledger moves.

    The harness's scripted ``_RecordingLoop`` (unlike the REAL ``ConversationLoop``
    ``test_m2_cost_e2e.py`` drives) carries no ``last_turn_cost_cents`` — exactly
    the pre-existing ``unpriced``/legacy-loop shape ``ChatTurnRegistry._turn_charge``
    already documents, which flat-floor-charges ``credits_per_turn`` via the SAME
    ``policy.deduct`` call every REAL turn goes through (the app's real,
    cloud-edition-default ``MeteredCreditsPolicy``). Reads the ``credits`` table
    directly via the superuser engine (bypassing RLS — ``CreditsPolicy.get_balance``
    needs the ``current_user_id`` contextvar a real request binds, which a test
    calling it OUTSIDE a request doesn't have) to assert the balance actually
    moves — proving regenerate rides the real deduct call, no bypass, no
    special-cased free regenerate.
    """
    c, uid, persona_id, loop = client
    conv_id = _new_conversation(c, uid, persona_id)

    loop.reply = "Answer 1"
    c.post(f"/v1/conversations/{conv_id}/messages", json={"content": "q1"}, headers=_auth(uid))
    hist1 = c.get(f"/v1/conversations/{conv_id}", headers=_auth(uid)).json()
    assistant_id = hist1["messages"][1]["id"]

    su = make_rls_engine(os.environ["DATABASE_URL"])
    with su.begin() as conn:
        before = conn.execute(
            text("SELECT balance FROM credits WHERE user_id = :u"), {"u": uid}
        ).scalar_one()
    su.dispose()

    loop.reply = "Answer 2 (regenerated)"
    resp = c.post(
        f"/v1/conversations/{conv_id}/messages/{assistant_id}/regenerate", headers=_auth(uid)
    )
    assert resp.status_code == 200, resp.text
    _ = resp.text  # drain

    su = make_rls_engine(os.environ["DATABASE_URL"])
    with su.begin() as conn:
        after = conn.execute(
            text("SELECT balance FROM credits WHERE user_id = :u"), {"u": uid}
        ).scalar_one()
    su.dispose()
    assert after < before, "the regenerate turn must bill through the normal credits path"


def test_regenerate_composes_with_r9_022_heal_orphaned_running_row(
    client: tuple[TestClient, str, str, _RecordingLoop],
) -> None:
    """R9-022 heal-path composition: the conversation's tail is an ORPHANED
    'running' assistant row (a crashed process's leftover, no live in-process
    registry entry — the exact production shape R9-022 fixed for the normal send
    path). Calling regenerate on it heals it (interrupted) THEN runs a real new
    turn — the SAME machinery a normal send already proved."""
    c, uid, persona_id, loop = client
    conv_id = _new_conversation(c, uid, persona_id)

    # A real, healthy prior exchange so there is a genuine preceding user turn.
    loop.reply = "Answer 1"
    c.post(f"/v1/conversations/{conv_id}/messages", json={"content": "q1"}, headers=_auth(uid))

    # Seed the orphan: a SECOND user question with a 'running' assistant row that
    # never got a live registry entry in THIS process (mirrors R9-022's own
    # seeding pattern in test_conversations.py exactly).
    su = make_rls_engine(os.environ["DATABASE_URL"])
    now = datetime.now(UTC)
    orphan_assistant_id = f"msg_orphan_assistant_{conv_id}"
    with su.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO messages (id, conversation_id, role, content, created_at) "
                "VALUES (:id, :cid, 'user', 'q2 (orphaned)', :created_at)"
            ),
            {"id": f"msg_orphan_user_{conv_id}", "cid": conv_id, "created_at": now},
        )
        conn.execute(
            text(
                "INSERT INTO messages "
                "(id, conversation_id, role, content, streaming_status, stream_events, created_at) "
                "VALUES (:id, :cid, 'assistant', '', 'running', '[]'::jsonb, :created_at)"
            ),
            {
                "id": orphan_assistant_id,
                "cid": conv_id,
                "created_at": now + timedelta(microseconds=1),
            },
        )
    su.dispose()

    loop.reply = "Real answer after heal"
    resp = c.post(
        f"/v1/conversations/{conv_id}/messages/{orphan_assistant_id}/regenerate",
        headers=_auth(uid),
    )
    assert resp.status_code == 200, resp.text
    events = _read_sse(resp.text)
    assert events[-1][0] == "done"

    # The orphan healed to 'interrupted' (R9-022's exact terminal value) AND
    # superseded (R9-025 leg C) — both markers present, never left 'running'.
    su = make_rls_engine(os.environ["DATABASE_URL"])
    with su.begin() as conn:
        orphan = (
            conn.execute(
                text("SELECT streaming_status, superseded_at FROM messages WHERE id = :id"),
                {"id": orphan_assistant_id},
            )
            .mappings()
            .first()
        )
    su.dispose()
    assert orphan is not None
    assert orphan["streaming_status"] == "interrupted"
    assert orphan["superseded_at"] is not None

    # A real new reply landed, fed with the orphaned user question's content.
    hist = c.get(f"/v1/conversations/{conv_id}", headers=_auth(uid)).json()
    roles = [m["role"] for m in hist["messages"]]
    assert roles == ["user", "assistant", "user", "assistant"]
    assert hist["messages"][3]["content"] == "Real answer after heal"
    last_call_history, last_user_message = loop.calls[-1]
    assert last_user_message == "q2 (orphaned)"
    # The orphan's own (empty, never-streamed) content never entered context —
    # only the healthy prior exchange did.
    assert last_call_history == ["q1", "Answer 1"]


# -- Edit -----------------------------------------------------------------------


def test_edit_reruns_on_edited_content_supersedes_old_pair_marker_present(
    client: tuple[TestClient, str, str, _RecordingLoop],
) -> None:
    c, uid, persona_id, loop = client
    conv_id = _new_conversation(c, uid, persona_id)

    loop.reply = "Answer to the original question"
    c.post(
        f"/v1/conversations/{conv_id}/messages",
        json={"content": "what is 2+2?"},
        headers=_auth(uid),
    )
    hist1 = c.get(f"/v1/conversations/{conv_id}", headers=_auth(uid)).json()
    old_user_id = hist1["messages"][0]["id"]
    old_assistant_id = hist1["messages"][1]["id"]

    loop.reply = "Answer to the edited question"
    resp = c.patch(
        f"/v1/conversations/{conv_id}/messages/{old_user_id}/edit",
        json={"content": "what is 3+3?"},
        headers=_auth(uid),
    )
    assert resp.status_code == 200, resp.text
    events = _read_sse(resp.text)
    assert events[-1][0] == "done"

    # New pair present; old pair excluded from the listing.
    hist2 = c.get(f"/v1/conversations/{conv_id}", headers=_auth(uid)).json()
    assert [m["role"] for m in hist2["messages"]] == ["user", "assistant"]
    assert hist2["messages"][0]["content"] == "what is 3+3?"
    assert hist2["messages"][1]["content"] == "Answer to the edited question"
    new_user_id = hist2["messages"][0]["id"]
    assert new_user_id != old_user_id
    assert hist2["messages"][1]["id"] != old_assistant_id

    # Context proof: the re-run's fed user_message is the EDITED text, and the
    # ORIGINAL wording never entered the model's prompt for the new turn.
    assert len(loop.calls) == 2
    _first_history, first_user_message = loop.calls[0]
    second_history, second_user_message = loop.calls[1]
    assert first_user_message == "what is 2+2?"
    assert second_user_message == "what is 3+3?"
    assert "what is 2+2?" not in second_history
    assert "Answer to the original question" not in second_history

    su = make_rls_engine(os.environ["DATABASE_URL"])
    with su.begin() as conn:
        old_pair = (
            conn.execute(
                text(
                    "SELECT id, content, superseded_at FROM messages "
                    "WHERE id IN (:u, :a) ORDER BY created_at"
                ),
                {"u": old_user_id, "a": old_assistant_id},
            )
            .mappings()
            .all()
        )
        new_user_row = (
            conn.execute(text("SELECT channel FROM messages WHERE id = :id"), {"id": new_user_id})
            .mappings()
            .first()
        )
    su.dispose()

    # Old pair: BOTH superseded, original wording preserved untouched (never
    # content-mutated) — the additive invariant.
    assert len(old_pair) == 2
    for row in old_pair:
        assert row["superseded_at"] is not None
    contents_by_id = {row["id"]: row["content"] for row in old_pair}
    assert contents_by_id[old_user_id] == "what is 2+2?"
    assert contents_by_id[old_assistant_id] == "Answer to the original question"

    # The marker: the new user row's channel carries the edit provenance.
    assert new_user_row is not None
    assert new_user_row["channel"] == {"edited_from": old_user_id}


def test_edit_on_lone_tail_user_message_with_no_reply_yet(
    client: tuple[TestClient, str, str, _RecordingLoop],
) -> None:
    """The v1 tail rule also accepts a LONE tail user message (no assistant reply
    yet — e.g. seeded directly, mirroring the A6 chat-twin's ``append_user_message``
    shape) — edit supersedes just that one row and reruns."""
    c, uid, persona_id, loop = client
    conv_id = _new_conversation(c, uid, persona_id)

    su = make_rls_engine(os.environ["DATABASE_URL"])
    lone_user_id = f"msg_lone_{conv_id}"
    with su.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO messages (id, conversation_id, role, content, created_at) "
                "VALUES (:id, :cid, 'user', 'a half-typed thought', now())"
            ),
            {"id": lone_user_id, "cid": conv_id},
        )
    su.dispose()

    loop.reply = "A real reply"
    resp = c.patch(
        f"/v1/conversations/{conv_id}/messages/{lone_user_id}/edit",
        json={"content": "a complete thought"},
        headers=_auth(uid),
    )
    assert resp.status_code == 200, resp.text
    hist = c.get(f"/v1/conversations/{conv_id}", headers=_auth(uid)).json()
    assert [m["role"] for m in hist["messages"]] == ["user", "assistant"]
    assert hist["messages"][0]["content"] == "a complete thought"
    assert hist["messages"][1]["content"] == "A real reply"


def test_edit_non_tail_target_is_422(
    client: tuple[TestClient, str, str, _RecordingLoop],
) -> None:
    c, uid, persona_id, loop = client
    conv_id = _new_conversation(c, uid, persona_id)

    loop.reply = "Answer 1"
    c.post(f"/v1/conversations/{conv_id}/messages", json={"content": "q1"}, headers=_auth(uid))
    hist1 = c.get(f"/v1/conversations/{conv_id}", headers=_auth(uid)).json()
    first_user_id = hist1["messages"][0]["id"]

    loop.reply = "Answer 2"
    c.post(f"/v1/conversations/{conv_id}/messages", json={"content": "q2"}, headers=_auth(uid))

    resp = c.patch(
        f"/v1/conversations/{conv_id}/messages/{first_user_id}/edit",
        json={"content": "q1 edited"},
        headers=_auth(uid),
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["context"]["reason"] == "not_last_user_message"


def test_edit_rejects_blank_content(
    client: tuple[TestClient, str, str, _RecordingLoop],
) -> None:
    c, uid, persona_id, loop = client
    conv_id = _new_conversation(c, uid, persona_id)
    loop.reply = "Answer"
    c.post(f"/v1/conversations/{conv_id}/messages", json={"content": "q1"}, headers=_auth(uid))
    hist = c.get(f"/v1/conversations/{conv_id}", headers=_auth(uid)).json()
    user_id = hist["messages"][0]["id"]

    resp = c.patch(
        f"/v1/conversations/{conv_id}/messages/{user_id}/edit",
        json={"content": ""},
        headers=_auth(uid),
    )
    assert resp.status_code == 422, resp.text

    # Untouched — a rejected edit must not have superseded anything.
    hist2 = c.get(f"/v1/conversations/{conv_id}", headers=_auth(uid)).json()
    assert hist2["messages"][0]["content"] == "q1"


# -- One-active-turn 409 (regenerate + edit) — service-level, real Postgres -----
#
# Mirrors this codebase's OWN precedent for testing "a second call while a turn
# is active" (test_chat_turn_service.py / test_concurrency_entry_points.py):
# driving true concurrency through TestClient (whose .post() fully drains the
# SSE body before returning, so the detached task has ALREADY finished by the
# time control returns — see those files' own notes) adds fragility, not
# coverage, over calling the chat_service function directly against a REAL,
# empty ``ChatTurnRegistry`` seeded with a synthetic in-flight handle — the
# exact registry-state shape a genuinely streaming turn leaves behind.


@pytest.mark.asyncio
async def test_regenerate_409_when_a_turn_is_active_supersedes_nothing(
    migrated_engine: Engine,
) -> None:
    engine = migrated_engine
    owner_id = "user_r9025c_409"
    persona_id = "persona_r9025c_409"
    conv_id = "conv_r9025c_409"
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
            {"i": owner_id, "e": f"{owner_id}@x.test"},
        )
        conn.execute(
            text(
                "INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :u, 'y') "
                "ON CONFLICT DO NOTHING"
            ),
            {"p": persona_id, "u": owner_id},
        )
        conn.execute(
            text(
                "INSERT INTO conversations (id, owner_id, persona_id, title) "
                "VALUES (:c, :u, :p, 't') ON CONFLICT DO NOTHING"
            ),
            {"c": conv_id, "u": owner_id, "p": persona_id},
        )
        assistant_id = f"msg_{conv_id}_a"
        conn.execute(
            text(
                "INSERT INTO messages (id, conversation_id, role, content, created_at) "
                "VALUES (:u, :c, 'user', 'q1', now())"
            ),
            {"u": f"msg_{conv_id}_u", "c": conv_id},
        )
        conn.execute(
            text(
                "INSERT INTO messages (id, conversation_id, role, content, created_at) "
                "VALUES (:a, :c, 'assistant', 'a1', now())"
            ),
            {"a": assistant_id, "c": conv_id},
        )

    sink = MessagesTurnSink(engine)
    registry = ChatTurnRegistry(sink=sink, rls_engine=engine)
    # A synthetic in-flight handle — the exact shape a genuinely streaming turn
    # leaves in the registry (keyed by conversation_id, the one-active-turn
    # invariant this codebase enforces everywhere).
    registry._handles[conv_id] = ChatTurnHandle(conv_id, owner_id, "some_other_msg")

    with pytest.raises(TurnAlreadyActiveError):
        await chat_service.regenerate_turn(
            rls_engine=engine,
            sink=sink,
            registry=registry,
            loop_builder=MagicMock(),
            owner_id=owner_id,
            conversation_id=conv_id,
            assistant_message_id=assistant_id,
        )

    # The 409 fired BEFORE any supersede — the target is untouched.
    with engine.begin() as conn:
        row = (
            conn.execute(
                text("SELECT superseded_at FROM messages WHERE id = :id"), {"id": assistant_id}
            )
            .mappings()
            .first()
        )
    assert row is not None
    assert row["superseded_at"] is None


@pytest.mark.asyncio
async def test_edit_409_when_a_turn_is_active_supersedes_nothing(
    migrated_engine: Engine,
) -> None:
    engine = migrated_engine
    owner_id = "user_r9025c_409b"
    persona_id = "persona_r9025c_409b"
    conv_id = "conv_r9025c_409b"
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
            {"i": owner_id, "e": f"{owner_id}@x.test"},
        )
        conn.execute(
            text(
                "INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :u, 'y') "
                "ON CONFLICT DO NOTHING"
            ),
            {"p": persona_id, "u": owner_id},
        )
        conn.execute(
            text(
                "INSERT INTO conversations (id, owner_id, persona_id, title) "
                "VALUES (:c, :u, :p, 't') ON CONFLICT DO NOTHING"
            ),
            {"c": conv_id, "u": owner_id, "p": persona_id},
        )
        user_id = f"msg_{conv_id}_u"
        conn.execute(
            text(
                "INSERT INTO messages (id, conversation_id, role, content, created_at) "
                "VALUES (:u, :c, 'user', 'q1', now())"
            ),
            {"u": user_id, "c": conv_id},
        )

    sink = MessagesTurnSink(engine)
    registry = ChatTurnRegistry(sink=sink, rls_engine=engine)
    registry._handles[conv_id] = ChatTurnHandle(conv_id, owner_id, "some_other_msg")

    with pytest.raises(TurnAlreadyActiveError):
        await chat_service.edit_and_rerun_turn(
            rls_engine=engine,
            sink=sink,
            registry=registry,
            loop_builder=MagicMock(),
            owner_id=owner_id,
            conversation_id=conv_id,
            user_message_id=user_id,
            new_content="q1 edited",
        )

    with engine.begin() as conn:
        row = (
            conn.execute(
                text("SELECT superseded_at, content FROM messages WHERE id = :id"), {"id": user_id}
            )
            .mappings()
            .first()
        )
    assert row is not None
    assert row["superseded_at"] is None
    assert row["content"] == "q1"

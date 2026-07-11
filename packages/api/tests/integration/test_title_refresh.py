"""R9-020 — dynamic self-improving chat titles, end-to-end against real Postgres.

The A4/V8 rule: drive the REAL trigger chain, no hand-forced job payloads. A
real completed chat turn (the real HTTP route → ``start_chat_turn`` → the
detached ``ChatTurnRegistry`` worker → clean completion) is what enqueues
``title_refresh`` when the conversation's message total crosses a threshold —
and a REAL A0 ``Worker`` (real ``JobQueue`` claim → real ``PgTitleRepository``
SQL) is what executes it, on a scripted title generator (the plumbing, not the
model — the synthesis-pipeline test discipline).

Covered here:
- turn 1 (2 messages) enqueues NOTHING; turn 2 (4 messages) enqueues exactly one
  ``title_refresh`` keyed ``title:{conv}:4``; turn 3 does not re-fire;
- the real worker runs the handler: the title is rewritten from the WHOLE
  transcript + ``sidebar.changed`` (reason=conversation.title_updated) is
  published; a re-enqueue of the same threshold is A0's dedup no-op and a
  re-delivery converges (no duplicate write/ping);
- a BAD generation (instruction echo) keeps the existing title untouched;
- the first-turn hook still titles the conversation AND now publishes
  ``sidebar.changed`` (the R9-020 live-update half of the first-turn path).
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from persona.backends import StreamChunk, TokenUsage
from persona.jobs import JobRegistry
from persona.schema.conversation import ConversationMessage
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig
from persona_api.jobs import JobQueue, Worker
from persona_api.jobs.handlers.title_refresh import (
    TitleRefreshJobPayload,
    enqueue_title_refresh,
    register_title_refresh_handler,
    title_refresh_idempotency_key,
)
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

_ECHO = "We need to output a title of at most 5 words, no quotes, no punctuation, no prose"


class _ScriptedLoop:
    """The test_conversations stand-in: streams a reply + mutates the conversation
    exactly as the real loop does (appends user + assistant on success)."""

    def __init__(self, reply: str = "Hello there!", *, tier: str = "mid") -> None:
        self._reply = reply
        self._tier = tier

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
        if on_event is not None:
            await on_event(RunEvent.tier(self._tier))
        conversation.messages.append(
            ConversationMessage(role="user", content=user_message, created_at=now)
        )
        yield StreamChunk(delta=self._reply, is_final=False)
        conversation.messages.append(
            ConversationMessage(role="assistant", content=self._reply, created_at=now)
        )
        yield StreamChunk(
            delta="",
            is_final=True,
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )


class _RecordingChannel:
    """Duck-typed UserEventChannel: records every publish (owner, event)."""

    def __init__(self) -> None:
        self.published: list[tuple[str, object]] = []

    def publish(self, owner_id: str, event: object) -> None:
        self.published.append((owner_id, event))


@pytest.fixture
def client(
    migrated_engine: Engine,  # noqa: ARG001 — schema + grants
    embedder: HashEmbedder384,
    tmp_path: object,
) -> Iterator[tuple[TestClient, str, str]]:
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

    async def _build_loop(_persona_id: str) -> _ScriptedLoop:
        return _ScriptedLoop()

    user_id = "user_r9020"
    with TestClient(app) as c:
        app.state.verify_token = _fake_verify
        app.state.embedder = embedder
        app.state.build_conversation_loop = _build_loop
        if hasattr(app.state, "tier_registry"):
            app.state.tier_registry = None  # keyless CI shape (test_conversations pattern)
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
        yield c, user_id, persona_id
        su = make_rls_engine(os.environ["DATABASE_URL"])
        with su.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE id = :i"), {"i": user_id})
        su.dispose()


def _auth(uid: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {uid}"}


def _new_conversation(c: TestClient, uid: str, persona_id: str) -> str:
    resp = c.post(
        f"/v1/personas/{persona_id}/conversations", json={"title": ""}, headers=_auth(uid)
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _turn(c: TestClient, uid: str, conv_id: str, content: str) -> None:
    resp = c.post(
        f"/v1/conversations/{conv_id}/messages", json={"content": content}, headers=_auth(uid)
    )
    assert resp.status_code == 200, resp.text
    _ = resp.text  # drain the SSE — the detached worker has finalized + enqueued by EOS


def _title_jobs(su: Engine, uid: str) -> list[tuple[str, str]]:
    with su.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT idempotency_key, state FROM jobs "
                "WHERE type = 'title_refresh' AND owner_id = :o ORDER BY created_at"
            ),
            {"o": uid},
        ).all()
    return [(r.idempotency_key, r.state) for r in rows]


def _conversation_title(su: Engine, conv_id: str) -> str:
    with su.begin() as conn:
        return str(
            conn.execute(
                text("SELECT title FROM conversations WHERE id = :c"), {"c": conv_id}
            ).scalar_one()
        )


def _prune_other_jobs(su: Engine, uid: str) -> None:
    """Harness hygiene: this test's registry serves ONLY title_refresh — drop the
    other real enqueues (synthesis/episodic) so run_once claims deterministically.
    The title_refresh rows themselves came from the REAL trigger chain."""
    with su.begin() as conn:
        conn.execute(
            text("DELETE FROM jobs WHERE owner_id = :o AND type <> 'title_refresh'"), {"o": uid}
        )


def _title_worker(
    su: Engine,
    app_rls_engine: Engine,
    generator: Callable[[str], Awaitable[str]],
    channel: _RecordingChannel | None = None,
) -> Worker:
    registry = JobRegistry()
    register_title_refresh_handler(
        registry,
        generator=generator,
        event_channel=channel,  # type: ignore[arg-type]
    )
    return Worker(
        dispatch_engine=su,
        rls_engine=app_rls_engine,
        registry=registry,
        worker_id="w-title",
    )


# ----- the real trigger chain: thresholds fire from real completed turns ------


def test_real_turns_enqueue_title_refresh_exactly_at_the_threshold(
    client: tuple[TestClient, str, str],
) -> None:
    c, uid, persona_id = client

    async def _title_builder(_first: str) -> str:
        return "First message title"

    c.app.state.title_builder = _title_builder  # type: ignore[attr-defined]
    conv_id = _new_conversation(c, uid, persona_id)
    su = make_rls_engine(os.environ["DATABASE_URL"])
    try:
        # Turn 1 → 2 messages: below the first threshold — NO title_refresh job.
        _turn(c, uid, conv_id, "let's plan a board game night")
        assert _title_jobs(su, uid) == []

        # Turn 2 → 4 messages: crosses 4 — the REAL chat turn enqueued the job.
        _turn(c, uid, conv_id, "actually make it a weekly games evening with friends")
        jobs = _title_jobs(su, uid)
        assert [k for k, _s in jobs] == [f"title:{conv_id}:4"], (
            "the real completed turn at message-total 4 enqueues exactly one title_refresh"
        )

        # Turn 3 → 6 messages: between thresholds — no re-fire, still exactly one.
        _turn(c, uid, conv_id, "and add snacks planning")
        assert [k for k, _s in _title_jobs(su, uid)] == [f"title:{conv_id}:4"]
    finally:
        su.dispose()


# ----- the real worker executes the refresh -----------------------------------


def test_worker_refreshes_the_title_and_publishes_and_is_idempotent(
    client: tuple[TestClient, str, str],
) -> None:
    c, uid, persona_id = client

    async def _title_builder(_first: str) -> str:
        return "First message title"

    c.app.state.title_builder = _title_builder  # type: ignore[attr-defined]
    conv_id = _new_conversation(c, uid, persona_id)
    su = make_rls_engine(os.environ["DATABASE_URL"])
    try:
        _turn(c, uid, conv_id, "let's plan a board game night")
        _turn(c, uid, conv_id, "make it weekly, with rotating hosts and snacks")
        assert [k for k, _s in _title_jobs(su, uid)] == [f"title:{conv_id}:4"]
        assert _conversation_title(su, conv_id) == "First message title"

        seen_excerpts: list[str] = []

        async def _generator(excerpt: str) -> str:
            seen_excerpts.append(excerpt)
            return "Weekly board game night"

        channel = _RecordingChannel()
        _prune_other_jobs(su, uid)
        worker = _title_worker(su, c.app.state.rls_engine, _generator, channel)

        assert asyncio.run(worker.run_once()) == 1

        # The refreshed title describes the WHOLE conversation now.
        assert _conversation_title(su, conv_id) == "Weekly board game night"
        # The handler read the real transcript (early AND late turns present).
        assert len(seen_excerpts) == 1
        assert "board game night" in seen_excerpts[0]
        assert "rotating hosts" in seen_excerpts[0]
        # The live ping went out (reason=conversation.title_updated, the owner's).
        assert len(channel.published) == 1
        owner, event = channel.published[0]
        assert owner == uid
        assert getattr(event, "type", None) == "sidebar.changed"
        assert getattr(event, "reason", None) == "conversation.title_updated"
        # The job succeeded (never dead-lettered).
        assert _title_jobs(su, uid) == [(f"title:{conv_id}:4", "succeeded")]

        # Idempotency 1 — the PRODUCER's re-enqueue of the same threshold is
        # A0's ON CONFLICT no-op (same key, no second row).
        assert (
            enqueue_title_refresh(JobQueue(su), owner_id=uid, conversation_id=conv_id, threshold=4)
            is None
        )
        assert len(_title_jobs(su, uid)) == 1

        # Idempotency 2 — a RE-DELIVERY (the synthesis-test pattern: same payload,
        # fresh key) converges: same title regenerated → no write, no second ping.
        payload = TitleRefreshJobPayload(conversation_id=conv_id, threshold=4)
        JobQueue(su).enqueue(
            type="title_refresh",
            owner_id=uid,
            payload=payload.model_dump(),
            idempotency_key=title_refresh_idempotency_key(payload) + ":redelivery",
        )
        assert asyncio.run(worker.run_once()) == 1
        assert _conversation_title(su, conv_id) == "Weekly board game night"
        assert len(channel.published) == 1  # no duplicate sidebar ping
    finally:
        su.dispose()


def test_bad_generation_keeps_the_existing_title(client: tuple[TestClient, str, str]) -> None:
    c, uid, persona_id = client

    async def _title_builder(_first: str) -> str:
        return "A good first title"

    c.app.state.title_builder = _title_builder  # type: ignore[attr-defined]
    conv_id = _new_conversation(c, uid, persona_id)
    su = make_rls_engine(os.environ["DATABASE_URL"])
    try:
        _turn(c, uid, conv_id, "help me draft a rental contract")
        _turn(c, uid, conv_id, "add a clause about the deposit")
        assert _conversation_title(su, conv_id) == "A good first title"

        async def _echo_generator(_excerpt: str) -> str:
            return _ECHO  # the exact R4 bug class — the sanitizer rejects it

        channel = _RecordingChannel()
        _prune_other_jobs(su, uid)
        worker = _title_worker(su, c.app.state.rls_engine, _echo_generator, channel)

        assert asyncio.run(worker.run_once()) == 1

        # KEEP-EXISTING (the R9-020 contract): never regressed to first-words,
        # never the echo — the good title is byte-untouched; no ping either.
        assert _conversation_title(su, conv_id) == "A good first title"
        assert channel.published == []
        # And the job is a clean success (a keep is a no-op, not a failure/retry).
        assert _title_jobs(su, uid) == [(f"title:{conv_id}:4", "succeeded")]
    finally:
        su.dispose()


# ----- the first-turn hook: still titles + now publishes live -----------------


def test_first_turn_title_still_works_and_publishes_sidebar_changed(
    client: tuple[TestClient, str, str],
) -> None:
    c, uid, persona_id = client

    async def _title_builder(first_message: str) -> str:
        assert first_message == "help with my lease"
        return "Norwegian tenancy question"

    channel = _RecordingChannel()
    c.app.state.title_builder = _title_builder  # type: ignore[attr-defined]
    c.app.state.event_channel = channel  # type: ignore[attr-defined]

    conv_id = _new_conversation(c, uid, persona_id)
    # The create itself publishes sidebar.changed (R9-012) — count from here.
    published_before = len(channel.published)
    _turn(c, uid, conv_id, "help with my lease")

    title = c.get(f"/v1/conversations/{conv_id}", headers=_auth(uid)).json()["title"]
    assert title == "Norwegian tenancy question"

    new_events = channel.published[published_before:]
    title_pings = [
        (owner, ev)
        for owner, ev in new_events
        if getattr(ev, "reason", None) == "conversation.title_updated"
    ]
    assert len(title_pings) == 1, "the first-turn title write publishes sidebar.changed"
    owner, event = title_pings[0]
    assert owner == uid
    assert getattr(event, "type", None) == "sidebar.changed"

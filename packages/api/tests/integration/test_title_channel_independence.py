"""Issue #8: a conversation is named because it has content, not because of its channel.

Chat conversations were auto-titled; connector-born and call-born ones were not,
so the list showed them untitled. Two reasons, both fixed here and both driven
through the REAL composition rather than a copy of it:

* the only thing that named a conversation at its FIRST turn was the web route's
  ``title_builder`` hook, which the SSE chat route alone injects. The trigger's
  first crossing was four messages, which short connector exchanges never reach.
  The floor is now one completed exchange, and the trigger has always been in the
  shared chat-turn worker, which is exactly what a connector turn runs on;
* a conversation whose write path never reached a trigger at all (a call whose
  teardown died with the process) had nothing to fall back on. The
  :class:`~persona_api.services.title_backfill.UntitledConversationBackfill`
  sweep now asks the channel-blind question (unnamed, and enough content to
  name) and enqueues the SAME durable ``title_refresh`` job.

Everything below goes through a REAL producer and the REAL A0 worker. The title
generator is scripted: the plumbing is under test, not the model (the
synthesis-pipeline discipline).
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona.backends import StreamChunk, TokenUsage
from persona.jobs import JobRegistry
from persona.schema.conversation import ConversationMessage
from persona_api.config import APIConfig
from persona_api.editions.credits_policy import UnlimitedCreditsPolicy
from persona_api.jobs import JobQueue, Worker
from persona_api.jobs.handlers.title_refresh import register_title_refresh_handler
from persona_api.middleware.rls_context import make_rls_engine
from persona_api.schedules.leadership import SchedulerLeader
from persona_api.services.title_backfill import (
    TITLE_BACKFILL_LOCK_KEY,
    UntitledConversationBackfill,
)
from persona_connectors.composition import ConnectorComposition, build_reply_runner
from persona_connectors.config import ConnectorConfig
from persona_connectors.domain.flow import TurnRequest
from persona_connectors.infra.conversation_store import PostgresConversationStateStore
from persona_runtime.agentic.events import RunEvent
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator

    from persona.schema.conversation import Conversation
    from persona_runtime.prompt import DocumentContext
    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_OWNER = "titles_everywhere_owner"
_PERSONA = "titles_everywhere_persona"


class _ScriptedLoop:
    """The turn stand-in: streams a reply and mutates the conversation as the real loop does."""

    def __init__(self, reply: str = "Sure, happy to help with that.") -> None:
        self._reply = reply

    async def turn(
        self,
        conversation: Conversation,
        user_message: str,
        on_event: Callable[[RunEvent], Awaitable[None]] | None = None,
        *,
        turn_has_image: bool = False,  # noqa: ARG002, real-loop kwarg compat
        images: list[object] | None = None,  # noqa: ARG002, real-loop kwarg compat
        documents: list[object] | None = None,  # noqa: ARG002, real-loop kwarg compat
        document_context: DocumentContext | None = None,  # noqa: ARG002, real-loop kwarg compat
    ) -> AsyncIterator[StreamChunk]:
        now = datetime.now(UTC)
        if on_event is not None:
            await on_event(RunEvent.tier("mid"))
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


class _StubRuntimeFactory:
    """The one thing the connector reply runner asks of the runtime: build a loop."""

    async def build_conversation_loop(self, _persona_id: str) -> _ScriptedLoop:
        return _ScriptedLoop()


@pytest.fixture
def seeded(migrated_engine: Engine) -> Iterator[Engine]:
    """Owner + persona on the cross-tenant engine; torn down after each test."""
    with migrated_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:o, 'titles@example.com')"), {"o": _OWNER}
        )
        conn.execute(
            text(
                "INSERT INTO personas (id, owner_id, yaml) "
                "VALUES (:p, :o, 'schema_version: \"1.0\"')"
            ),
            {"p": _PERSONA, "o": _OWNER},
        )
    yield migrated_engine
    with migrated_engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id = :o"), {"o": _OWNER})


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Engine:  # noqa: ARG001, schema + grants ordering
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set; skipping the channel-independence test")
    return make_rls_engine(app_url.replace("+asyncpg", "+psycopg"))


@pytest.fixture
def api_config(tmp_path: object) -> APIConfig:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set; skipping the channel-independence test")
    return APIConfig(
        app_database_url=app_url,
        audit_root=str(tmp_path) + "/audit",
        workspace_root=str(tmp_path) + "/workspace",  # type: ignore[arg-type]
    )


def _title_jobs(su: Engine) -> list[tuple[str, str]]:
    with su.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT idempotency_key, state FROM jobs "
                "WHERE type = 'title_refresh' AND owner_id = :o ORDER BY created_at"
            ),
            {"o": _OWNER},
        ).all()
    return [(r.idempotency_key, r.state) for r in rows]


def _conversation_title(su: Engine, conversation_id: str) -> str:
    with su.begin() as conn:
        return str(
            conn.execute(
                text("SELECT title FROM conversations WHERE id = :c"), {"c": conversation_id}
            ).scalar_one()
        )


def _prune_other_jobs(su: Engine) -> None:
    """Harness hygiene: this registry serves ONLY title_refresh, so drop the other real
    enqueues (synthesis/episodic) and let run_once claim deterministically. The
    title_refresh rows themselves came from the REAL producers."""
    with su.begin() as conn:
        conn.execute(
            text("DELETE FROM jobs WHERE owner_id = :o AND type <> 'title_refresh'"), {"o": _OWNER}
        )


def _title_worker(
    su: Engine, app_rls_engine: Engine, generator: Callable[[str], Awaitable[str]]
) -> Worker:
    registry = JobRegistry()
    register_title_refresh_handler(registry, generator=generator)
    return Worker(
        dispatch_engine=su, rls_engine=app_rls_engine, registry=registry, worker_id="w-titles"
    )


@pytest.fixture
def backfill(seeded: Engine) -> Iterator[UntitledConversationBackfill]:
    """The REAL sweep, leader lock released afterwards so the next test can lead."""
    leader = SchedulerLeader(seeded, lock_key=TITLE_BACKFILL_LOCK_KEY)
    try:
        yield UntitledConversationBackfill(
            dispatch_engine=seeded, queue=JobQueue(seeded), leader=leader
        )
    finally:
        leader.resign()


def _run_connector_turn(
    *, app_engine: Engine, api_config: APIConfig, su: Engine, conversation_id: str, text_in: str
) -> None:
    """One inbound through the REAL connector reply runner (api's own chat-turn path)."""
    run_turn = build_reply_runner(
        runtime_factory=_StubRuntimeFactory(),  # type: ignore[arg-type]
        rls_engine=app_engine,
        owner_scope=ConnectorComposition(ConnectorConfig()).owner_scope,
        api_config=api_config,
        credits_policy=UnlimitedCreditsPolicy(),
        gateway=None,
        job_queue=JobQueue(su),
    )
    reply = asyncio.run(
        run_turn(
            TurnRequest(
                owner_id=_OWNER,
                conversation_id=conversation_id,
                persona_id=_PERSONA,
                text=text_in,
            )
        )
    )
    assert reply, "the connector turn produced a reply"


# ----- a connector-born conversation is named by its first exchange ------------


def test_a_connector_conversation_is_titled_by_the_real_job_path(
    seeded: Engine, app_engine: Engine, api_config: APIConfig
) -> None:
    # The conversation is created exactly as an inbound Telegram message creates
    # one: the connector's own conversation store, no title, no chat route.
    store = PostgresConversationStateStore(rls_engine=app_engine, dispatch_engine=seeded)
    conversation_id = store.foreground(
        owner_id=_OWNER, platform="telegram", channel_key="tg_chat_1", persona_id=_PERSONA
    ).conversation_id
    assert _conversation_title(seeded, conversation_id) == "", (
        "a connector conversation is born unnamed; there is no first-turn hook here"
    )

    _run_connector_turn(
        app_engine=app_engine,
        api_config=api_config,
        su=seeded,
        conversation_id=conversation_id,
        text_in="can you help me pick a rain jacket for Bergen",
    )

    # ONE completed exchange crossed the floor: the real trigger, in the shared
    # chat-turn worker the connector runs on, enqueued the ordinary job.
    assert [k for k, _s in _title_jobs(seeded)] == [f"title:{conversation_id}:2"]

    async def _generator(_excerpt: str) -> str:
        return "Rain jacket for Bergen"

    _prune_other_jobs(seeded)
    worker = _title_worker(seeded, app_engine, _generator)
    assert asyncio.run(worker.run_once()) == 1
    assert _conversation_title(seeded, conversation_id) == "Rain jacket for Bergen"
    assert _title_jobs(seeded) == [(f"title:{conversation_id}:2", "succeeded")]


def test_the_connector_title_reads_the_real_transcript(
    seeded: Engine, app_engine: Engine, api_config: APIConfig
) -> None:
    store = PostgresConversationStateStore(rls_engine=app_engine, dispatch_engine=seeded)
    conversation_id = store.foreground(
        owner_id=_OWNER, platform="slack", channel_key="slack_c1", persona_id=_PERSONA
    ).conversation_id
    _run_connector_turn(
        app_engine=app_engine,
        api_config=api_config,
        su=seeded,
        conversation_id=conversation_id,
        text_in="remind me what we agreed about the deposit clause",
    )

    seen: list[str] = []

    async def _generator(excerpt: str) -> str:
        seen.append(excerpt)
        return "Deposit clause recap"

    _prune_other_jobs(seeded)
    assert asyncio.run(_title_worker(seeded, app_engine, _generator).run_once()) == 1
    assert len(seen) == 1
    assert "deposit clause" in seen[0], "the handler titled from what was actually said"
    assert _conversation_title(seeded, conversation_id) == "Deposit clause recap"


# ----- a call-born conversation, even when its own writer never ran ------------


def test_a_call_conversation_is_titled_through_the_backfill(
    seeded: Engine, app_engine: Engine, backfill: UntitledConversationBackfill
) -> None:
    # A call whose session-end enqueue never happened (the voice process died with
    # the room). origin='call', a real voice transcript, and no name.
    conversation_id = "conv_call_untitled"
    with seeded.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO conversations (id, owner_id, persona_id, origin, title) "
                "VALUES (:c, :o, :p, 'call', '')"
            ),
            {"c": conversation_id, "o": _OWNER, "p": _PERSONA},
        )
        for role, content in (
            ("user", "what time does the ferry to Stavanger leave"),
            ("assistant", "There is one at 08:00 and one at 16:30."),
        ):
            conn.execute(
                text(
                    "INSERT INTO messages (conversation_id, role, content, channel) "
                    'VALUES (:c, :r, :t, \'{"modality": "voice"}\')'
                ),
                {"c": conversation_id, "r": role, "t": content},
            )

    assert backfill.run_once() == 1
    assert [k for k, _s in _title_jobs(seeded)] == [f"title:{conversation_id}:2"]

    seen: list[str] = []

    async def _generator(excerpt: str) -> str:
        seen.append(excerpt)
        return "Stavanger ferry times"

    worker = _title_worker(seeded, app_engine, _generator)
    assert asyncio.run(worker.run_once()) == 1
    assert _conversation_title(seeded, conversation_id) == "Stavanger ferry times"
    assert "ferry" in seen[0], "for a call the transcript IS the content"


# ----- a chat conversation still is, and an empty one still is not -------------


def test_a_chat_conversation_is_still_titled_and_an_empty_one_is_left_alone(
    seeded: Engine, app_engine: Engine, backfill: UntitledConversationBackfill
) -> None:
    chat_id = "conv_chat_untitled"
    empty_id = "conv_chat_empty"
    with seeded.begin() as conn:
        for cid in (chat_id, empty_id):
            conn.execute(
                text(
                    "INSERT INTO conversations (id, owner_id, persona_id, origin, title) "
                    "VALUES (:c, :o, :p, 'chat', '')"
                ),
                {"c": cid, "o": _OWNER, "p": _PERSONA},
            )
        for role, content in (
            ("user", "help me write a short bio for the conference"),
            ("assistant", "Here is a draft in two sentences."),
        ):
            conn.execute(
                text("INSERT INTO messages (conversation_id, role, content) VALUES (:c, :r, :t)"),
                {"c": chat_id, "r": role, "t": content},
            )

    assert backfill.run_once() == 1, "only the one with content"
    keys = [k for k, _s in _title_jobs(seeded)]
    assert keys == [f"title:{chat_id}:2"]
    assert f"title:{empty_id}:0" not in keys

    async def _generator(_excerpt: str) -> str:
        return "Conference bio draft"

    assert asyncio.run(_title_worker(seeded, app_engine, _generator).run_once()) == 1
    assert _conversation_title(seeded, chat_id) == "Conference bio draft"
    # The empty one is untouched: nothing was said, so there is nothing to name.
    assert _conversation_title(seeded, empty_id) == ""


# ----- reruns converge -------------------------------------------------------


def test_the_backfill_is_idempotent_across_reruns(
    seeded: Engine, app_engine: Engine, backfill: UntitledConversationBackfill
) -> None:
    conversation_id = "conv_rerun"
    with seeded.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO conversations (id, owner_id, persona_id, origin, title) "
                "VALUES (:c, :o, :p, 'chat', '')"
            ),
            {"c": conversation_id, "o": _OWNER, "p": _PERSONA},
        )
        for role, content in (("user", "plan a weekend in Lofoten"), ("assistant", "Two nights?")):
            conn.execute(
                text("INSERT INTO messages (conversation_id, role, content) VALUES (:c, :r, :t)"),
                {"c": conversation_id, "r": role, "t": content},
            )

    assert backfill.run_once() == 1
    # Still undrained: the same key, so A0's ON CONFLICT makes the second pass a no-op.
    assert backfill.run_once() == 0
    assert len(_title_jobs(seeded)) == 1

    calls: list[str] = []

    async def _generator(excerpt: str) -> str:
        calls.append(excerpt)
        return "Weekend in Lofoten"

    worker = _title_worker(seeded, app_engine, _generator)
    assert asyncio.run(worker.run_once()) == 1
    assert _conversation_title(seeded, conversation_id) == "Weekend in Lofoten"

    # Now it has a name, so the sweep stops seeing it at all: no second model call.
    assert backfill.run_once() == 0
    assert len(_title_jobs(seeded)) == 1
    assert len(calls) == 1

"""R9-028 — the voice call session-end title enqueue → REAL A0 worker, end to end.

The cross-layer real-transition ([[feedback_synthetic_harness_real_transition]]):
a ``title_refresh`` job written by the **voice** raw-INSERT writer
(``persona_voice.session.title_enqueue`` — a peer process to api, mirrors V13
D-4-amended) is CLAIMED and PROCESSED by the **real A0 worker** — the same
``Worker.run_once()`` the api composes — which re-reads the call's real
transcript, runs the title generator (a scripted backend — the plumbing, not
the model, the synthesis-pipeline test discipline), and writes the refreshed
title. This is the home of the cross-layer test because the api worker +
title-refresh handler live here; it imports the voice writer to drive the real
seam (mirrors ``test_voice_synthesis_wired``).

R9-020's web trigger only fires at message-count thresholds {4,10,24,50,100};
a short call never crosses them. The voice writer fires ONCE, unconditionally,
at session-end — covered here: exactly one title job from a finalized call,
the real worker titles it, and a re-finalize (the same final count) is an
``ON CONFLICT`` no-op — never a duplicate regen.
"""

# ruff: noqa: ARG001 — fixture-ordering param.
from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

import pytest
from persona.jobs import JobRegistry
from persona_api.jobs import Worker
from persona_api.jobs.handlers.title_refresh import register_title_refresh_handler
from persona_voice.session.title_enqueue import enqueue_voice_title_refresh
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

_OWNER = "voice_title_user"
_PERSONA = "voice_title_persona"
_CONVO = "voice_title_call"


class _RecordingChannel:
    """Duck-typed UserEventChannel: records every publish (owner, event)."""

    def __init__(self) -> None:
        self.published: list[tuple[str, object]] = []

    def publish(self, owner_id: str, event: object) -> None:
        self.published.append((owner_id, event))


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Engine:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set; skipping wired voice-title test")
    from persona_api.middleware.rls_context import make_rls_engine

    return make_rls_engine(app_url.replace("+asyncpg", "+psycopg"))


@pytest.fixture
def seeded(migrated_engine: Engine) -> Engine:
    """Seed the owner + persona + a call-origin conversation + a two-turn transcript."""
    with migrated_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:o, 'vtitle@example.com')"), {"o": _OWNER}
        )
        conn.execute(
            text(
                "INSERT INTO personas (id, owner_id, yaml) "
                "VALUES (:p, :o, 'schema_version: \"1.0\"')"
            ),
            {"p": _PERSONA, "o": _OWNER},
        )
        conn.execute(
            text(
                "INSERT INTO conversations (id, owner_id, persona_id, origin, title) "
                "VALUES (:c, :o, :p, 'call', '')"
            ),
            {"c": _CONVO, "o": _OWNER, "p": _PERSONA},
        )
        # A voice transcript persists to the SAME messages table (V9), channel voice.
        for role, content in (
            ("user", "can you help me plan a trip to Bergen"),
            ("assistant", "Sure — when are you thinking of going?"),
        ):
            conn.execute(
                text(
                    "INSERT INTO messages (conversation_id, role, content, channel) "
                    'VALUES (:c, :r, :t, \'{"modality": "voice"}\')'
                ),
                {"c": _CONVO, "r": role, "t": content},
            )
    return migrated_engine


def _title_worker(
    dispatch_engine: Engine,
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
        dispatch_engine=dispatch_engine,
        rls_engine=app_rls_engine,
        registry=registry,
        worker_id="w-vtitle",
    )


def _conversation_title(su: Engine, conv_id: str) -> str:
    with su.begin() as conn:
        return str(
            conn.execute(
                text("SELECT title FROM conversations WHERE id = :c"), {"c": conv_id}
            ).scalar_one()
        )


def test_a_finalized_call_gets_exactly_one_title_job_and_the_worker_titles_it(
    seeded: Engine, app_engine: Engine
) -> None:
    # The VOICE writer enqueues (not JobQueue/the web turn-end trigger) — the
    # seam under test — fired ONCE at call session-end, keyed on the final count.
    job_id = enqueue_voice_title_refresh(
        seeded, owner_id=_OWNER, conversation_id=_CONVO, message_count=2
    )
    assert job_id is not None

    with seeded.begin() as conn:
        jobs = conn.execute(
            text("SELECT idempotency_key FROM jobs WHERE type = 'title_refresh' AND owner_id = :o"),
            {"o": _OWNER},
        ).all()
    assert [r.idempotency_key for r in jobs] == [f"title:{_CONVO}:2"], (
        "the finalized call enqueues exactly one title_refresh job"
    )

    seen_excerpts: list[str] = []

    async def _generator(excerpt: str) -> str:
        seen_excerpts.append(excerpt)
        return "Bergen trip planning"

    channel = _RecordingChannel()
    worker = _title_worker(seeded, app_engine, _generator, channel)

    # The real worker claims + processes exactly one job.
    assert asyncio.run(worker.run_once()) == 1

    assert _conversation_title(seeded, _CONVO) == "Bergen trip planning"
    assert len(seen_excerpts) == 1
    assert "Bergen" in seen_excerpts[0]

    with seeded.begin() as conn:
        state = conn.execute(
            text("SELECT state FROM jobs WHERE id = :i"), {"i": job_id}
        ).scalar_one()
    assert state == "succeeded"

    # The live ping went out (reason=conversation.title_updated, R9-012).
    assert len(channel.published) == 1
    owner, event = channel.published[0]
    assert owner == _OWNER
    assert getattr(event, "type", None) == "sidebar.changed"
    assert getattr(event, "reason", None) == "conversation.title_updated"


def test_reenqueue_of_the_same_finalized_call_is_a_conflict_noop(
    seeded: Engine, app_engine: Engine
) -> None:
    # A re-finalize (a duplicate teardown call, a retry) re-enqueues with the
    # SAME final message_count → the identical idempotency key → A0's
    # ON CONFLICT no-op. No dup job, no dup regen.
    first = enqueue_voice_title_refresh(
        seeded, owner_id=_OWNER, conversation_id=_CONVO, message_count=2
    )
    second = enqueue_voice_title_refresh(
        seeded, owner_id=_OWNER, conversation_id=_CONVO, message_count=2
    )
    assert first is not None
    assert second is None  # the re-enqueue dedup'd

    async def _generator(_excerpt: str) -> str:
        return "Bergen trip planning"

    worker = _title_worker(seeded, app_engine, _generator)
    assert asyncio.run(worker.run_once()) == 1

    with seeded.begin() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM jobs WHERE type = 'title_refresh' AND owner_id = :o"),
            {"o": _OWNER},
        ).scalar_one()
    assert count == 1  # exactly one job — no duplicate from the re-enqueue
    assert _conversation_title(seeded, _CONVO) == "Bergen trip planning"

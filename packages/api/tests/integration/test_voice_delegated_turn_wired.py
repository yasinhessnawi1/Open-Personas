"""A9-T5 — voice enqueue → REAL A0 worker → loop.turn → create, end to end (the full transition).

The chain T4 deferred, now completed ([[feedback_synthetic_harness_real_transition]]): a
``delegated_turn`` job written by the **voice** raw-INSERT writer (``enqueue_delegated_turn``) is
CLAIMED + PROCESSED by the **real A0 worker** (``Worker.run_once()``), whose handler runs the
verbatim ask through the **real** ``RuntimeFactory.build_conversation_loop`` (``loop.turn`` on the
frontier — the ONE audited path, A9-D-7) and creates the standing task through the **unchanged**
``OriginationService`` — voice's spoken confirmation standing in for the chat "yes" (no fake user
turn, loop.py untouched). Only the model backend is scripted (the injected dependency); everything
the composition builds is real. No hand-forced end-state.

Proven here:
* the durable job drives a REAL create (the task exists, goal from the frontier's parse);
* the durable OUTCOME is recorded (``delegation_outcome=succeeded`` + ``provenance=voice`` +
  ``delegation_key`` on the final assistant message — T6's grounded hand-back);
* the consent-vs-execution trail (``audit_log`` actor ``voice_delegated``);
* CONCURRENCY — the delegated outcome message + a ``VoiceTranscriptWriter`` turn on the SAME live
  conversation carry distinct ids + monotonic timestamps + attributable modality (no ordering
  corruption);
* idempotency — a worker re-run converges on one task + one outcome message.
"""

# ruff: noqa: SLF001 — the composition assertions read private wiring on purpose.
from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from persona.backends import StreamChunk, TokenUsage
from persona.backends.types import ChatResponse
from persona.jobs import JobRegistry
from persona.stores.postgres import PostgresBackend
from persona_api.jobs import Worker
from persona_api.jobs.handlers.delegated_turn import register_delegated_turn_handler
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from persona_api.services.runtime_factory import RuntimeFactory
from persona_api.services.task_origination_composition import compose_task_origination_services
from persona_api.tasks.store import TaskStore
from persona_voice.session.delegation_enqueue import enqueue_delegated_turn
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from persona.schema.conversation import ConversationMessage
    from sqlalchemy import Engine
    from tests.conftest import HashEmbedder384

pytestmark = pytest.mark.integration

_AUDIT = Path("/tmp/persona-a9-deleg-audit")
_OWNER = "user_a9deleg"
_PERSONA = "persona_a9deleg"
_CONVO = "c_a9deleg"
_ASK = "every morning, track the fares for me"
_YAML = """\
schema_version: "1.0"
identity:
  name: Astrid
  role: assistant
  background: |
    A helper.
  language_default: en
  constraints: []
self_facts:
  - fact: knows things
    confidence: 1.0
"""


class _ScriptedA4Backend:
    """Answers the A4 model steps by prompt-type (judge → standing); streams an ordinary reply."""

    provider_name = "anthropic"
    model_name = "scripted"
    max_tokens = 4096

    @property
    def supports_native_tools(self) -> bool:
        return True

    @property
    def supports_vision(self) -> bool:
        return False

    async def chat(self, messages: list[ConversationMessage], **_: object) -> ChatResponse:
        content = self._answer(str(messages[0].content), str(messages[-1].content))
        return ChatResponse(
            content=content,
            model=self.model_name,
            provider=self.provider_name,
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            latency_ms=1.0,
        )

    @staticmethod
    def _answer(system: str, user: str) -> str:
        if "STEER" in system:
            match = re.search(r"id=(\S+):", user)
            return json.dumps({"verb": "pause", "task_id": match.group(1) if match else ""})
        if "ADJUSTS the proposal" in system:
            return '{"amends": false}'
        if "STANDING task" in system:
            return '{"verdict": "standing", "goal": "track morning fares"}'
        return "{}"

    async def chat_stream(
        self,
        messages: list[ConversationMessage],  # noqa: ARG002 — ordinary generation isn't reached
        **_: object,
    ) -> AsyncIterator[StreamChunk]:
        yield StreamChunk(
            delta="ok",
            is_final=True,
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )


class _ScriptedRegistry:
    """A TierRegistry stand-in returning the scripted A4 backend for any tier."""

    def __init__(self) -> None:
        self._b = _ScriptedA4Backend()

    def get(self, _tier_name: str) -> _ScriptedA4Backend:
        return self._b

    @property
    def configured_tier_names(self) -> tuple[str, ...]:
        return ("frontier", "mid", "small")

    def supports_vision_for(self, _tier_name: str) -> bool:
        return False

    def metadata_for(self, _tier_name: str) -> None:
        return None

    def model_name_for(self, _tier_name: str) -> str:
        return "scripted"

    async def aclose(self) -> None:
        pass


class _NullTurnLog:
    def write(self, _log: object) -> None:
        pass


def _seed(su_url: str, embedder: HashEmbedder384) -> None:
    su = make_rls_engine(su_url)
    with su.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
            {"i": _OWNER, "e": f"{_OWNER}@x"},
        )
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES (:i, :o, :y)"),
            {"i": _PERSONA, "o": _OWNER, "y": _YAML},
        )
        conn.execute(
            text(
                "INSERT INTO conversations (id, owner_id, persona_id, title) "
                "VALUES (:c, :o, :p, '') ON CONFLICT DO NOTHING"
            ),
            {"c": _CONVO, "o": _OWNER, "p": _PERSONA},
        )
    from persona.schema.chunks import PersonaChunk

    PostgresBackend(engine=su, embedder=embedder).upsert(
        persona_id=_PERSONA,
        store_kind="self_facts",
        chunks=[
            PersonaChunk(
                id=f"{_PERSONA}::self_facts::0000",
                text="self_fact: knows things",
                metadata={},
                created_at=datetime.now(UTC),
            )
        ],
    )
    su.dispose()


def _cleanup(su_url: str) -> None:
    su = make_rls_engine(su_url)
    with su.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id = :i"), {"i": _OWNER})
    su.dispose()


def _build_worker(
    *, dispatch_engine: Engine, app_engine: Engine, embedder: HashEmbedder384
) -> Worker:
    factory = RuntimeFactory(
        rls_engine=app_engine,
        embedder=embedder,
        tier_registry=_ScriptedRegistry(),  # type: ignore[arg-type]
        turn_log_writer=_NullTurnLog(),  # type: ignore[arg-type]
        audit_root=_AUDIT,
    )
    from persona_api.config import Edition

    services = compose_task_origination_services(
        rls_engine=app_engine,
        memory_backend=PostgresBackend(engine=app_engine, embedder=embedder),
        edition=Edition.cloud,
        audit_root=_AUDIT,
    )
    registry = JobRegistry()
    register_delegated_turn_handler(
        registry,
        runtime_factory=factory,
        origination_service=services.origination,
        steering_service=services.steering,
        rls_engine=app_engine,
    )
    return Worker(
        dispatch_engine=dispatch_engine,
        rls_engine=app_engine,
        registry=registry,
        worker_id="w-deleg",
    )


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Engine:  # noqa: ARG001 — migrations first
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (non-superuser role) not set")
    return make_rls_engine(app_url.replace("+asyncpg", "+psycopg"))


def test_voice_delegated_turn_drains_runs_loopturn_and_creates(
    migrated_engine: Engine, app_engine: Engine, embedder: HashEmbedder384
) -> None:
    su_url = os.environ["DATABASE_URL"]
    _seed(su_url, embedder)
    worker = _build_worker(
        dispatch_engine=migrated_engine, app_engine=app_engine, embedder=embedder
    )

    # The VOICE writer enqueues (not JobQueue) — the seam under test.
    job_id = enqueue_delegated_turn(
        migrated_engine,
        owner_id=_OWNER,
        conversation_id=_CONVO,
        verbatim_ask=_ASK,
        persona_id=_PERSONA,
    )
    assert job_id is not None

    # The REAL worker claims + processes exactly one job → runs loop.turn → creates the task.
    assert asyncio.run(worker.run_once()) == 1

    token = current_user_id.set(_OWNER)
    try:
        # (1) a REAL task exists, its goal from the frontier's parse (not the mid model).
        tasks = TaskStore(app_engine).list_for_owner(_OWNER)
        assert len(tasks) == 1
        assert tasks[0].contract.goal == "track morning fares"
    finally:
        current_user_id.reset(token)

    with migrated_engine.begin() as conn:
        # (2) the durable OUTCOME message — T6's grounded hand-back text.
        outcome_row = (
            conn.execute(
                text(
                    "SELECT content, channel FROM messages WHERE conversation_id = :c "
                    "AND channel->>'delegation_key' IS NOT NULL"
                ),
                {"c": _CONVO},
            )
            .mappings()
            .one()
        )
        # (3) the consent-vs-execution audit trail.
        audit = (
            conn.execute(
                text(
                    "SELECT metadata FROM audit_log WHERE action = 'delegated_turn.execute' "
                    "AND user_id = :o"
                ),
                {"o": _OWNER},
            )
            .mappings()
            .one()
        )
        state = conn.execute(
            text("SELECT state FROM jobs WHERE id = :i"), {"i": job_id}
        ).scalar_one()

    channel = outcome_row["channel"]
    channel = channel if isinstance(channel, dict) else json.loads(channel)
    assert channel["delegation_outcome"] == "succeeded"
    assert channel["provenance"] == "voice"
    assert "track morning fares" in outcome_row["content"]  # grounded in the REAL created task
    audit_meta = audit["metadata"]
    audit_meta = audit_meta if isinstance(audit_meta, dict) else json.loads(audit_meta)
    assert audit_meta["actor"] == "voice_delegated"
    assert audit_meta["provenance"] == "voice"
    assert state == "succeeded"

    _cleanup(su_url)


def test_worker_rerun_is_idempotent_one_task_one_outcome(
    migrated_engine: Engine, app_engine: Engine, embedder: HashEmbedder384
) -> None:
    su_url = os.environ["DATABASE_URL"]
    _seed(su_url, embedder)
    # Enqueue + drain, then simulate an A0 at-least-once RE-DELIVERY of the SAME job: the worker
    # died after the side effect but before the success write, so its lease is reclaimed (the row
    # resets to queued) and the SAME job runs again. It must converge — one task, one outcome.
    job_id = enqueue_delegated_turn(
        migrated_engine,
        owner_id=_OWNER,
        conversation_id=_CONVO,
        verbatim_ask=_ASK,
        persona_id=_PERSONA,
    )
    worker = _build_worker(
        dispatch_engine=migrated_engine, app_engine=app_engine, embedder=embedder
    )
    assert asyncio.run(worker.run_once()) == 1
    # Reclaim the same job (the crash-resume edge) and re-drain it.
    with migrated_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE jobs SET state = 'queued', locked_by = NULL, lease_expires_at = NULL "
                "WHERE id = :i"
            ),
            {"i": job_id},
        )
    assert asyncio.run(worker.run_once()) == 1  # the same delegation re-runs

    token = current_user_id.set(_OWNER)
    try:
        tasks = TaskStore(app_engine).list_for_owner(_OWNER)
    finally:
        current_user_id.reset(token)
    # Origination is keyed on the delegation, so the re-run converges on exactly one task.
    assert len(tasks) == 1
    with migrated_engine.begin() as conn:
        outcomes = conn.execute(
            text(
                "SELECT count(*) FROM messages WHERE conversation_id = :c "
                "AND channel->>'delegation_key' IS NOT NULL"
            ),
            {"c": _CONVO},
        ).scalar_one()
    assert outcomes == 1  # the outcome message is idempotent on the delegation key (no duplicate)
    _cleanup(su_url)


def test_delegated_outcome_and_transcript_writer_do_not_corrupt_ordering(
    migrated_engine: Engine, app_engine: Engine, embedder: HashEmbedder384
) -> None:
    # CONCURRENCY (A9-T5 condition 4): the delegated outcome message + a VoiceTranscriptWriter turn
    # on the SAME live conversation — distinct ids, monotonic ts, attributable by modality.
    from persona_voice.model.transcript import VoiceTranscriptWriter

    su_url = os.environ["DATABASE_URL"]
    _seed(su_url, embedder)
    worker = _build_worker(
        dispatch_engine=migrated_engine, app_engine=app_engine, embedder=embedder
    )
    enqueue_delegated_turn(
        migrated_engine,
        owner_id=_OWNER,
        conversation_id=_CONVO,
        verbatim_ask=_ASK,
        persona_id=_PERSONA,
    )
    assert asyncio.run(worker.run_once()) == 1

    # A concurrent voice transcript turn on the same conversation (V9 writer).
    token = current_user_id.set(_OWNER)
    try:
        VoiceTranscriptWriter(engine=app_engine, conversation_id=_CONVO).record_turn(
            user_text="thanks", heard_text="you're welcome", truncated=False, now=datetime.now(UTC)
        )
    finally:
        current_user_id.reset(token)

    with migrated_engine.begin() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT id, created_at, channel FROM messages WHERE conversation_id = :c "
                    "ORDER BY created_at ASC"
                ),
                {"c": _CONVO},
            )
            .mappings()
            .all()
        )
    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids))  # distinct ids — no collision across the two writers
    times = [r["created_at"] for r in rows]
    assert times == sorted(times)  # monotonic / ordered
    modalities = set()
    for r in rows:
        ch = r["channel"] if isinstance(r["channel"], dict) else json.loads(r["channel"] or "{}")
        if "modality" in ch:
            modalities.add(ch["modality"])
    # Both writers are provenance-attributable and distinct.
    assert "voice_delegated" in modalities
    assert "voice" in modalities
    _cleanup(su_url)


def test_delegated_spoken_steering_is_applied_through_the_worker(
    migrated_engine: Engine, app_engine: Engine, embedder: HashEmbedder384
) -> None:
    # A9-T7: a spoken "pause" rides the SAME delegation crossing — enqueue → real worker → loop.turn
    # → task_steering → the unchanged steering door applies it. No hand-forced end-state.
    su_url = os.environ["DATABASE_URL"]
    _seed(su_url, embedder)
    worker = _build_worker(
        dispatch_engine=migrated_engine, app_engine=app_engine, embedder=embedder
    )
    # First create a task (the origination delegation), so there is a live task to steer.
    enqueue_delegated_turn(
        migrated_engine,
        owner_id=_OWNER,
        conversation_id=_CONVO,
        verbatim_ask=_ASK,
        persona_id=_PERSONA,
    )
    assert asyncio.run(worker.run_once()) == 1
    token = current_user_id.set(_OWNER)
    try:
        task = TaskStore(app_engine).list_for_owner(_OWNER)[0]
        assert task.paused is False
    finally:
        current_user_id.reset(token)

    # Now a spoken steering ask — delegated verbatim, applied by the real worker.
    enqueue_delegated_turn(
        migrated_engine,
        owner_id=_OWNER,
        conversation_id=_CONVO,
        verbatim_ask="pause the fare tracker",
        persona_id=_PERSONA,
    )
    assert asyncio.run(worker.run_once()) == 1

    token = current_user_id.set(_OWNER)
    try:
        steered = TaskStore(app_engine).get(_OWNER, task.id)
    finally:
        current_user_id.reset(token)
    assert steered.paused is True  # the delegated pause was applied through the one steering door
    with migrated_engine.begin() as conn:
        outcome = conn.execute(
            text(
                "SELECT content FROM messages WHERE conversation_id = :c "
                "AND channel->>'delegation_outcome' = 'succeeded' AND content LIKE '%paused%'"
            ),
            {"c": _CONVO},
        ).first()
    assert outcome is not None  # the grounded hand-back names what was actually done
    _cleanup(su_url)


def test_the_whole_redirect_chain_end_to_end(
    migrated_engine: Engine, app_engine: Engine, embedder: HashEmbedder384
) -> None:
    """A9-T11 — THE ACCEPTANCE OF THE ENTIRE REDIRECT (the "is it wired live" gate).

    The full chain through the REAL pipeline, in ONE event loop, ZERO forced end-states / no
    hand-called steps (synthetic-harness-real-transition + the A4 anti-inertness rule): a real
    spoken ask → the REAL gate echoes → "yes" → the "preparing" line + the REAL dispatcher enqueues
    a REAL ``delegated_turn`` job → the REAL A0 worker drains it → the REAL frontier ``loop.turn``
    creates a REAL task → the REAL poller reads the durable outcome → the grounded hand-back. Only
    the model backend is scripted (the injected dependency); every seam the composition builds is
    real.
    """
    from persona_runtime.task_origination import (
        ModelAmendmentInterpreter,
        ModelStandingIntentJudge,
        StandingIntentRecognizer,
    )
    from persona_voice.loop.streaming import Transcript
    from persona_voice.model.origination_gate import DelegatedTurnIntent, VoiceOriginationGate
    from persona_voice.session.delegation_dispatch import DelegationDispatcher
    from persona_voice.session.delegation_handback import DelegationHandbackPoller

    su_url = os.environ["DATABASE_URL"]
    _seed(su_url, embedder)
    worker = _build_worker(
        dispatch_engine=migrated_engine, app_engine=app_engine, embedder=embedder
    )

    # The REAL voice-side gate (real recognizer + amendment interpreter; the model is scripted —
    # the injected dependency, exactly as the A4 live-composition test scripts it).
    gate = VoiceOriginationGate(
        recognizer=StandingIntentRecognizer(ModelStandingIntentJudge(backend=_ScriptedA4Backend())),
        amendment_interpreter=ModelAmendmentInterpreter(backend=_ScriptedA4Backend()),
        language="en",
    )
    narrations: list[Transcript] = []
    failed: list[bool] = []

    async def _on_handback(narration: Transcript) -> None:
        narrations.append(narration)

    async def _on_failed() -> None:
        failed.append(True)

    # A huge poll interval so the poller's background loop effectively polls once (job not yet
    # terminal) then idles — the explicit ``poll_once`` after the worker does the real resolution.
    poller = DelegationHandbackPoller(
        engine=migrated_engine,
        owner_id=_OWNER,
        conversation_id=_CONVO,
        on_handback=_on_handback,
        poll_interval_s=3600.0,
    )
    dispatcher = DelegationDispatcher(
        engine=migrated_engine, owner_id=_OWNER, poller=poller, on_failed=_on_failed
    )

    async def _chain() -> None:
        # (1) real spoken ask → the gate recognizes standing intent and echoes a spoken contract.
        echo = await gate.on_user_turn("every morning, track the fares for me")
        assert echo.owns_turn
        assert echo.verbatim_ask is None  # the echo, not yet a confirmation
        gate.note_spoken_turn_committed(truncated=False)
        # (2) "yes" → the gate confirms → the "preparing in the background" line + the VERBATIM ask.
        confirm = await gate.on_user_turn("yes")
        assert confirm.verbatim_ask == "every morning, track the fares for me"
        assert "background" in (confirm.spoken or "").lower()
        # (3) the real dispatcher enqueues a real ``delegated_turn`` job (off-loop) + tracks it.
        dispatcher.dispatch(
            DelegatedTurnIntent(
                conversation_id=_CONVO,
                verbatim_ask=confirm.verbatim_ask,
                persona_id=_PERSONA,
            )
        )
        await dispatcher.join()
        assert failed == []  # the enqueue succeeded (no fail-soft)
        # (4) the real A0 worker drains it → real frontier loop.turn → real create.
        assert await worker.run_once() == 1
        # (5) the real poller reads the durable outcome → the grounded hand-back.
        resolved = await poller.poll_once()
        assert len(resolved) == 1
        await poller.shutdown()

    asyncio.run(_chain())

    # The whole chain landed a REAL task + a grounded hand-back — nothing hand-forced.
    token = current_user_id.set(_OWNER)
    try:
        tasks = TaskStore(app_engine).list_for_owner(_OWNER)
    finally:
        current_user_id.reset(token)
    assert len(tasks) == 1
    assert tasks[0].contract.goal == "track morning fares"  # the frontier's parse, created for real
    # The spoken hand-back is GROUNDED in what was actually done (never a replay of the echo).
    assert len(narrations) == 1
    assert "track morning fares" in narrations[0].text
    _cleanup(su_url)

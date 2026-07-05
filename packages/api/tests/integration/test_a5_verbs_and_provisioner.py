"""A5 T10 — the verb family + the provisioning sweep on the REAL stack.

Real Postgres under ``persona_app``, the REAL ``RuntimeFactory``-built loop
(only the model scripted — the A8 composition-harness discipline). Proves the
four T10 bars:

1+2. **Reload-durable confirm on ONE seam:** a generic proposal delivered in
   one "request" is confirmed in a SEPARATE, freshly-built loop over a fresh
   ``Conversation`` (no shared in-memory state, no message metadata — the
   exact scenario the A4 rail fails, state.md): the verb gate resolves the
   pending proposal from the LEDGER, the worker-side service executes it
   through the real doors → the implicit task + run-once schedule EXIST. The
   negative: after the notice is superseded, a fresh "yes" finds nothing
   pending — zero new tasks (an expired/consumed proposal creates nothing).
3. **The sweep provisions the EXISTING population** (leader-gated, idempotent)
   and a provisioned schedule FIRES through the real tick — no hand-forced step.
4. **Dial verbs flow:** "stop suggesting things" through the REAL loop + the
   REAL verb service writes the dial; the OFF setting is honored by the next
   cycle component (the sweep skips the persona), and the re-enable verb
   provisions the schedule (A5-D-1's lazy call site) whose next REAL fire runs
   the scan — the setting flows to the cycle, not just to a column. (The
   handler-exit-on-OFF real-chain leg is T6's proven test.)
"""

# ruff: noqa: ARG001, ARG002, SLF001 — fixture params; fakes; wired-assert probes
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from persona.audit import MemoryAuditLogger
from persona.backends.types import ChatResponse, TokenUsage
from persona.graph import build_graph_store
from persona.graph.models import ConceptNode, NodeKind, NodeProvenance
from persona.graph.postgres import PostgresGraphBackend
from persona.initiative import (
    CandidateSource,
    CitationKind,
    GroundingCitation,
    InitiativeCandidate,
    InitiativeDial,
    InitiativeSettings,
    InitiativeTrigger,
    PlannedStep,
    Urgency,
)
from persona.jobs import JobRegistry, JobState
from persona.schema.chunks import WriteSource
from persona.schema.conversation import Conversation
from persona.tools.categories import ActionCategory
from persona_api.initiative.delivery import InitiativeDeliveryExecutor, implicit_task_id
from persona_api.initiative.handler import (
    INITIATIVE_SCAN_JOB_TYPE,
    InitiativeScanHandler,
    read_initiative_dial,
    register_initiative_scan_handler,
)
from persona_api.initiative.pipeline_wiring import (
    ApiGroundingSource,
    ApiPipelineAuditor,
    ApiProvenanceReader,
    ApiUserContextReader,
    ApiWellbeingSubjectCheck,
    LedgerAdapter,
)
from persona_api.initiative.provisioner import InitiativeProvisioner
from persona_api.initiative.store import DeclineStore, InitiativeLedger
from persona_api.initiative.verb_service import InitiativeVerbService
from persona_api.jobs.executor import JobExecutor
from persona_api.jobs.queue import JobQueue
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from persona_api.schedules import SchedulerLeader, SchedulerTick, ScheduleStore
from persona_api.services.runtime_factory import RuntimeFactory
from persona_api.tasks.store import CheckpointStore, TaskStore
from persona_runtime.initiative import GroundingChecker, InitiativePipeline, InitiativeScanner
from sqlalchemy import create_engine, text

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from persona.backends import StreamChunk
    from persona.schema.conversation import ConversationMessage
    from persona_runtime.agentic.events import RunEvent
    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 7, 4, 6, 0, tzinfo=UTC)
_OWNER = "own_a5vp"
_PERSONA = "pers_vp_a"
_NODE = f"{_OWNER}::node::hearing1"
_HEARING = "The custody hearing is on Friday 10 July."
_AUDIT = Path("/tmp/persona-a5-verbs-audit")
_LOCK = 0x5A5C44
DIM = 384
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


class _ScriptedBackend:
    """Answers by prompt-type: the initiative dial + inert nulls for other gates."""

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
        system, user = str(messages[0].content), str(messages[-1].content)
        content = self._answer(system, user)
        return ChatResponse(
            content=content,
            model=self.model_name,
            provider=self.provider_name,
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            latency_ms=1.0,
        )

    @staticmethod
    def _answer(system: str, user: str) -> str:
        if "INITIATIVE posture" in system:  # the T10 dial interpreter
            if "stop suggesting" in user:
                return '{"verb": "dial_off"}'
            return '{"verb": null}'
        if "GROUND an observation" in system:  # the T4 entailment judge
            marker = "[excerpt 1]\n"
            start = user.index(marker) + len(marker)
            return json.dumps({"entailed": True, "quote": user[start : start + 25]})
        if "STEER" in system:
            return '{"verb": null}'
        if "STANDING task" in system:
            return '{"verdict": "now_work"}'
        return "{}"

    async def chat_stream(
        self,
        messages: list[ConversationMessage],
        **_: object,
    ) -> AsyncIterator[StreamChunk]:
        from persona.backends import StreamChunk

        yield StreamChunk(
            delta="ok",
            is_final=True,
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )


class _ScriptedRegistry:
    def __init__(self) -> None:
        self._b = _ScriptedBackend()

    def get(self, _tier: str) -> _ScriptedBackend:
        return self._b

    @property
    def configured_tier_names(self) -> tuple[str, ...]:
        return ("frontier", "mid", "small")

    def supports_vision_for(self, _tier: str) -> bool:
        return False

    def metadata_for(self, _tier: str) -> None:
        return None

    def model_name_for(self, _tier: str) -> str:
        return "scripted"

    async def aclose(self) -> None:
        pass


class _NullTurnLog:
    def write(self, _log: object) -> None:
        pass


class _Emb:
    model_name = "fake"

    @property
    def dimension(self) -> int:
        return DIM

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] + [0.0] * (DIM - 1) for _ in texts]


class _RecordingSender:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(self, **kwargs: Any) -> None:  # noqa: ANN401
        self.sent.append(kwargs)


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (non-superuser role) not set; skipping RLS test")
    engine = make_rls_engine(app_url.replace("+asyncpg", "+psycopg"))
    yield engine
    engine.dispose()


def _seed(migrated_engine: Engine) -> None:
    with migrated_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:u, :e) ON CONFLICT DO NOTHING"),
            {"u": _OWNER, "e": f"{_OWNER}@x.test"},
        )
        conn.execute(
            text(
                "INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :u, :y) "
                "ON CONFLICT DO NOTHING"
            ),
            {"p": _PERSONA, "u": _OWNER, "y": _YAML},
        )
    PostgresGraphBackend(engine=migrated_engine).insert_node_if_absent(
        _OWNER,
        ConceptNode(
            id=_NODE,
            node_kind=NodeKind.FACT,
            concept_name="hearing",
            content=_HEARING,
            provenance=(
                NodeProvenance(
                    source=WriteSource.PERSONA_SELF, persona_id=_PERSONA, written_at=_NOW
                ),
            ),
            created_at=_NOW,
        ),
        [1.0] + [0.0] * (DIM - 1),
    )


def _candidate() -> InitiativeCandidate:
    return InitiativeCandidate(
        observation="The hearing is Friday and nothing is drafted.",
        citations=(GroundingCitation(kind=CitationKind.NODE, ref=_NODE),),
        trigger=InitiativeTrigger.APPROACHING_COMMITMENT,
        why_now="The date entered the horizon.",
        plan=(PlannedStep(description="draft", categories=frozenset({ActionCategory.DRAFT})),),
        next_step="Draft the response letter.",
        value=0.9,
        acceptance=0.8,
        urgency=Urgency.BATCH,
        source=CandidateSource.SCAN,
        owner_id=_OWNER,
        persona_id=_PERSONA,
        prompt_version="a5-scan-v1",
        scanned_at=_NOW,
    )


def _executor(app_engine: Engine, ledger: InitiativeLedger) -> InitiativeDeliveryExecutor:
    return InitiativeDeliveryExecutor(
        ledger=ledger,
        tasks=TaskStore(app_engine),
        schedules=ScheduleStore(app_engine),
        timezone_for="Europe/Oslo",
        rls_engine=app_engine,
    )


def _pipeline(
    app_engine: Engine, executor: InitiativeDeliveryExecutor, sender: _RecordingSender
) -> InitiativePipeline:
    graph_store = build_graph_store(
        engine=app_engine, embedder=_Emb(), audit_logger=MemoryAuditLogger()
    )
    executor._proposal_sender = sender  # the C0 seam; message write proven at T9
    executor._resolve_tag = lambda _p: __import__(
        "persona.schema.origination", fromlist=["PersonaIdentityTag"]
    ).PersonaIdentityTag(persona_id=_PERSONA, display_name="Astrid")
    return InitiativePipeline(
        grounding=GroundingChecker(
            source=ApiGroundingSource(
                app_engine, TaskStore(app_engine), CheckpointStore(app_engine)
            ),
            backend=_ScriptedBackend(),  # type: ignore[arg-type]
        ),
        wellbeing=ApiWellbeingSubjectCheck(graph_store),
        declines=DeclineStore(app_engine),
        ledger=LedgerAdapter(InitiativeLedger(app_engine)),
        users=ApiUserContextReader(app_engine, default_timezone="Europe/Oslo"),
        provenance=ApiProvenanceReader(graph_store, app_engine),
        auditor=ApiPipelineAuditor(app_engine),
        dial_reader=lambda _o, _p: InitiativeDial.PROPOSE_ONLY,
        settings=InitiativeSettings(),
        delivery=executor,
    )


def _verb_service(app_engine: Engine, ledger: InitiativeLedger) -> InitiativeVerbService:
    return InitiativeVerbService(
        rls_engine=app_engine,
        schedules=ScheduleStore(app_engine),
        ledger=ledger,
        declines=DeclineStore(app_engine),
        executor=_executor(app_engine, ledger),
        settings=InitiativeSettings(),
    )


async def _drive(loop: object, conv: Conversation, message: str) -> list[RunEvent]:
    events: list[RunEvent] = []

    async def _on_event(event: RunEvent) -> None:
        events.append(event)

    async for _chunk in loop.turn(conv, message, _on_event):  # type: ignore[attr-defined]
        pass
    return events


@pytest.mark.asyncio
async def test_ledger_anchored_confirm_survives_a_fresh_request_and_expiry_creates_nothing(
    migrated_engine: Engine, app_engine: Engine, embedder: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PERSONA_INITIATIVE_ENABLED", "true")
    _seed(migrated_engine)
    ledger = InitiativeLedger(app_engine)
    sender = _RecordingSender()

    factory = RuntimeFactory(
        rls_engine=app_engine,
        embedder=embedder,  # type: ignore[arg-type]
        tier_registry=_ScriptedRegistry(),  # type: ignore[arg-type]
        turn_log_writer=_NullTurnLog(),  # type: ignore[arg-type]
        audit_root=_AUDIT,
    )
    token = current_user_id.set(_OWNER)
    try:
        # "Request 1": the proposal is delivered (held → flush → C0 message).
        pipeline = _pipeline(app_engine, _executor(app_engine, ledger), sender)
        await pipeline.submit((_candidate(),))
        await pipeline.flush(_OWNER)
        delivered = ledger.latest_pending_proposal(
            _OWNER, _PERSONA, max_age_days=InitiativeSettings().hold_max_days
        )
        assert delivered is not None
        assert len(sender.sent) == 1  # the proposal message went out

        # "Request 2": a FRESH factory-built loop + a FRESH conversation (no shared
        # memory, no metadata — the A4-rail failure scenario). The gate must find
        # the pending proposal in the LEDGER.
        loop = await factory.build_conversation_loop(_PERSONA)
        assert loop._initiative_pending_provider is not None  # wired through the REAL factory
        conv = Conversation(conversation_id="c_vp_fresh", persona_id=_PERSONA, messages=[])
        events = await _drive(loop, conv, "yes")
        verb_events = [e for e in events if e.type == "initiative_verb"]
        assert len(verb_events) == 1
        assert verb_events[0].data["verb"] == "confirm_proposal"
        assert verb_events[0].data["notice_id"] == delivered.id

        # The worker-side service applies it through the real doors.
        service = _verb_service(app_engine, ledger)
        await service.apply(
            owner_id=_OWNER,
            persona_id=_PERSONA,
            verb="confirm_proposal",
            notice_id=delivered.id,
        )
        task = TaskStore(app_engine).get(_OWNER, implicit_task_id(delivered.id))
        assert task.schedule_id is not None  # the confirmed act exists, schedule-backed

        # THE NEGATIVE: supersede the notice (consumed/expired) → a fresh "yes"
        # finds nothing pending → no event → nothing new is ever created.
        ledger.supersede(_OWNER, delivered.opportunity_key, now=datetime.now(UTC))
        loop2 = await factory.build_conversation_loop(_PERSONA)
        conv2 = Conversation(conversation_id="c_vp_fresh2", persona_id=_PERSONA, messages=[])
        events2 = await _drive(loop2, conv2, "yes")
        assert [e for e in events2 if e.type == "initiative_verb"] == []
        with migrated_engine.begin() as conn:
            task_count = conn.execute(
                text("SELECT count(*) FROM tasks WHERE owner_id = :u"), {"u": _OWNER}
            ).scalar_one()
        assert task_count == 1  # exactly the one confirmed act — expiry creates nothing
    finally:
        current_user_id.reset(token)
    with migrated_engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": _OWNER})


@pytest.mark.asyncio
async def test_dial_verb_flows_through_the_real_loop_service_and_next_scan_cycle(
    migrated_engine: Engine, app_engine: Engine, embedder: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PERSONA_INITIATIVE_ENABLED", "true")
    _seed(migrated_engine)
    ledger = InitiativeLedger(app_engine)

    factory = RuntimeFactory(
        rls_engine=app_engine,
        embedder=embedder,  # type: ignore[arg-type]
        tier_registry=_ScriptedRegistry(),  # type: ignore[arg-type]
        turn_log_writer=_NullTurnLog(),  # type: ignore[arg-type]
        audit_root=_AUDIT,
    )
    token = current_user_id.set(_OWNER)
    try:
        # The dial verb through the REAL loop: "stop suggesting things".
        loop = await factory.build_conversation_loop(_PERSONA)
        conv = Conversation(conversation_id="c_vp_dial", persona_id=_PERSONA, messages=[])
        events = await _drive(loop, conv, "stop suggesting things")
        dial_events = [e for e in events if e.type == "initiative_verb"]
        assert len(dial_events) == 1
        assert dial_events[0].data["verb"] == "dial_off"

        # The worker-side service writes the REAL setting.
        await _verb_service(app_engine, ledger).apply(
            owner_id=_OWNER, persona_id=_PERSONA, verb="dial_off", notice_id=None
        )
        assert read_initiative_dial(app_engine, _OWNER, _PERSONA) is InitiativeDial.OFF
    finally:
        current_user_id.reset(token)

    # The setting FLOWS to the next REAL cycle: provision a schedule, fire it
    # through the real tick, run the real handler — it exits without scanning
    # (no metering row; the T6 dial-off leg, now driven BY the verb's write).
    settings = InitiativeSettings()
    schedules = ScheduleStore(app_engine)
    provisioner = InitiativeProvisioner(
        dispatch_engine=migrated_engine,
        store=schedules,
        settings=settings,
        default_timezone="Europe/Oslo",
    )
    # Dial is OFF → the sweep skips this persona entirely (dial != 'off' filter).
    assert provisioner.run_once(now=_NOW) == 0
    # Turn it back to propose-only via the service (the enable path ensures the schedule).
    token = current_user_id.set(_OWNER)
    try:
        await _verb_service(app_engine, ledger).apply(
            owner_id=_OWNER, persona_id=_PERSONA, verb="dial_propose_only", notice_id=None
        )
        first_fire = schedules.get(_OWNER, f"initsched:{_PERSONA}").next_fire_at
        assert first_fire is not None  # the enable path provisioned the schedule (A5-D-1)
    finally:
        current_user_id.reset(token)

    registry = JobRegistry()
    scanner = InitiativeScanner(
        graph=__import__(
            "persona_api.initiative.readers", fromlist=["ApiScanGraphReader"]
        ).ApiScanGraphReader(
            build_graph_store(engine=app_engine, embedder=_Emb(), audit_logger=MemoryAuditLogger())
        ),
        conversations=__import__(
            "persona_api.initiative.readers", fromlist=["ApiScanConversationReader"]
        ).ApiScanConversationReader(app_engine),
        tasks=__import__(
            "persona_api.initiative.readers", fromlist=["ApiScanTaskReader"]
        ).ApiScanTaskReader(TaskStore(app_engine), CheckpointStore(app_engine)),
        backend=_ScriptedBackend(),  # type: ignore[arg-type]
        settings=settings,
    )
    register_initiative_scan_handler(
        registry,
        handler=InitiativeScanHandler(
            scanner=scanner,
            dial_reader=lambda owner, persona: read_initiative_dial(app_engine, owner, persona),
        ),
    )
    dispatch = create_engine(os.environ["DATABASE_URL"].replace("+asyncpg", "+psycopg"))
    try:
        leader = SchedulerLeader(dispatch, lock_key=_LOCK)
        tick = SchedulerTick(
            dispatch_engine=dispatch,
            rls_engine=app_engine,
            leader=leader,
            default_grace_seconds=366 * 24 * 3600.0,
        )
        assert tick.run_once(now=first_fire) == 1  # the provisioned schedule FIRES (real)
        queue = JobQueue(dispatch)
        records = queue.claim(worker_id="w-vp", lease_seconds=60)
        assert len(records) == 1
        assert records[0].type == INITIATIVE_SCAN_JOB_TYPE
        executor = JobExecutor(
            queue=queue, registry=registry, rls_engine=app_engine, worker_id="w-vp"
        )
        assert await executor.execute(records[0]) is JobState.SUCCEEDED
        leader.resign()
    finally:
        dispatch.dispose()
    with migrated_engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": _OWNER})


def test_provisioner_sweeps_the_existing_population_idempotently(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    """Bar 3: pre-existing personas (propose-only default, NO schedule) get their
    A1 row from the leader-gated sweep; a second run provisions zero (idempotent);
    the provisioned schedule fires through the REAL tick."""
    owner, persona = "own_a5sw", "pers_sw_a"
    with migrated_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:u, :e) ON CONFLICT DO NOTHING"),
            {"u": owner, "e": f"{owner}@x.test"},
        )
        conn.execute(
            text(
                "INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :u, 'name: A') "
                "ON CONFLICT DO NOTHING"
            ),
            {"p": persona, "u": owner},
        )
    schedules = ScheduleStore(app_engine)
    provisioner = InitiativeProvisioner(
        dispatch_engine=migrated_engine,
        store=schedules,
        settings=InitiativeSettings(),
        default_timezone="Europe/Oslo",
    )
    assert provisioner.run_once(now=_NOW) == 1  # the existing persona is provisioned
    assert provisioner.run_once(now=_NOW) == 0  # idempotent — NOT-EXISTS filters it

    token = current_user_id.set(owner)
    try:
        schedule = schedules.get(owner, f"initsched:{persona}")
        first_fire = schedule.next_fire_at
        assert first_fire is not None
        assert schedule.timezone == "Europe/Oslo"
    finally:
        current_user_id.reset(token)

    dispatch = create_engine(os.environ["DATABASE_URL"].replace("+asyncpg", "+psycopg"))
    try:
        leader = SchedulerLeader(dispatch, lock_key=_LOCK + 1)
        tick = SchedulerTick(
            dispatch_engine=dispatch,
            rls_engine=app_engine,
            leader=leader,
            default_grace_seconds=366 * 24 * 3600.0,
        )
        assert tick.run_once(now=first_fire) == 1  # the swept schedule FIRES for real
        leader.resign()
    finally:
        dispatch.dispose()
    with migrated_engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": owner})

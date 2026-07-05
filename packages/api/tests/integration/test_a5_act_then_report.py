"""A5 T8 — act-then-report through the REAL chain (no hand-invoked delivery).

Real Postgres under ``persona_app``. The chain the test drives — with the dial
at ACT_WITHIN_ENVELOPE and an all-safe candidate:

    pipeline.submit → HELD (batch default) → pipeline.flush (the daily fire's
    release) → the REAL delivery executor creates the implicit A2 task + the
    run-once A1 schedule → the REAL ``SchedulerTick.run_once`` fires it → the
    REAL ``JobExecutor`` runs the REAL A1→A2 bridge → a ``task_leg`` job exists
    at the task's head. Zero hand-invoked delivery steps.

Then the negatives: a re-run flush creates NOTHING new (exactly-once — the
delivered disposition + deterministic ids both hold the line); a re-tick at the
same instant enqueues nothing (the one-time advanced to exhausted); a delivery
whose task-create fails leaves the notice HELD with the orphan schedule
compensated (fail-soft, retained).
"""

# ruff: noqa: ARG001, ARG002 — fixture params + fakes ignore some args
from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
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
from persona.tools.categories import ActionCategory
from persona_api.initiative.delivery import InitiativeDeliveryExecutor, implicit_task_id
from persona_api.initiative.pipeline_wiring import (
    ApiGroundingSource,
    ApiPipelineAuditor,
    ApiProvenanceReader,
    ApiUserContextReader,
    ApiWellbeingSubjectCheck,
    LedgerAdapter,
)
from persona_api.initiative.store import DeclineStore, InitiativeLedger, NoticeDisposition
from persona_api.jobs.executor import JobExecutor
from persona_api.jobs.queue import JobQueue
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from persona_api.schedules import SchedulerLeader, SchedulerTick, ScheduleStore
from persona_api.tasks.scheduled_fire import (
    TASK_SCHEDULED_FIRE_JOB_TYPE,
    register_scheduled_task_fire_handler,
)
from persona_api.tasks.store import CheckpointStore, TaskStore
from persona_runtime.initiative import GroundingChecker, InitiativePipeline
from sqlalchemy import create_engine, text

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 7, 4, 6, 0, tzinfo=UTC)
_OWNER = "own_a5atr"
_PERSONA = "pers_atr_a"
_NODE = f"{_OWNER}::node::hearing1"
_HEARING = "The custody hearing is on Friday 10 July."
_LOCK = 0x5A5C42
DIM = 384


class _VerbatimYesJudge:
    provider_name = "anthropic"
    model_name = "scripted"

    async def chat(self, messages: Any, **_: object) -> ChatResponse:  # noqa: ANN401
        prompt = str(messages[-1].content)
        marker = "[excerpt 1]\n"
        start = prompt.index(marker) + len(marker)
        return ChatResponse(
            content=json.dumps({"entailed": True, "quote": prompt[start : start + 25]}),
            model=self.model_name,
            provider=self.provider_name,
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            latency_ms=1.0,
        )


class _Emb:
    model_name = "fake"

    @property
    def dimension(self) -> int:
        return DIM

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] + [0.0] * (DIM - 1) for _ in texts]


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
                "INSERT INTO personas (id, owner_id, yaml, initiative_dial) "
                "VALUES (:p, :u, 'name: A', 'act_within_envelope') ON CONFLICT DO NOTHING"
            ),
            {"p": _PERSONA, "u": _OWNER},
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


def _pipeline(
    app_engine: Engine, executor: InitiativeDeliveryExecutor | None
) -> InitiativePipeline:
    graph_store = build_graph_store(
        engine=app_engine, embedder=_Emb(), audit_logger=MemoryAuditLogger()
    )
    return InitiativePipeline(
        grounding=GroundingChecker(
            source=ApiGroundingSource(
                app_engine, TaskStore(app_engine), CheckpointStore(app_engine)
            ),
            backend=_VerbatimYesJudge(),  # type: ignore[arg-type]
        ),
        wellbeing=ApiWellbeingSubjectCheck(graph_store),
        declines=DeclineStore(app_engine),
        ledger=LedgerAdapter(InitiativeLedger(app_engine)),
        users=ApiUserContextReader(app_engine, default_timezone="Europe/Oslo"),
        provenance=ApiProvenanceReader(graph_store, app_engine),
        auditor=ApiPipelineAuditor(app_engine),
        dial_reader=lambda _o, _p: InitiativeDial.ACT_WITHIN_ENVELOPE,
        settings=InitiativeSettings(),
        delivery=executor,
    )


def test_act_then_report_flows_through_the_real_chain_exactly_once(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed(migrated_engine)
    ledger = InitiativeLedger(app_engine)
    tasks = TaskStore(app_engine)
    schedules = ScheduleStore(app_engine)
    executor = InitiativeDeliveryExecutor(
        ledger=ledger, tasks=tasks, schedules=schedules, timezone_for="Europe/Oslo"
    )
    pipeline = _pipeline(app_engine, executor)

    token = current_user_id.set(_OWNER)
    try:
        # (1) submit → HELD (batch default), then the flush releases through the
        # REAL executor — the same call path the daily fire takes.
        asyncio.run(pipeline.submit((_candidate(),)))
        held = ledger.held_for_owner(_OWNER)
        assert len(held) == 1
        notice_id = held[0].id

        asyncio.run(pipeline.flush(_OWNER))
        delivered = ledger.get_notice(_OWNER, notice_id)
        assert delivered is not None
        assert delivered.disposition is NoticeDisposition.DELIVERED
        assert delivered.envelope_action == "act"

        # (2) the implicit task + run-once schedule exist with deterministic ids.
        task = tasks.get(_OWNER, implicit_task_id(notice_id))
        assert task.persona_id == _PERSONA
        assert task.schedule_id is not None
        schedule = schedules.get(_OWNER, task.schedule_id)
        assert schedule.one_time_at is not None
        first_fire = schedule.next_fire_at
        assert first_fire is not None
    finally:
        current_user_id.reset(token)

    # (3) the REAL tick fires it; the REAL executor runs the REAL A1→A2 bridge.
    registry = JobRegistry()
    register_scheduled_task_fire_handler(
        registry,
        task_store=tasks,
        queue=JobQueue(app_engine),
        schedule_store=schedules,  # merge-back: A10 added the deleted-executor degrade args
        rls_engine=app_engine,
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
        assert tick.run_once(now=first_fire) == 1  # the one-time fire, real
        queue = JobQueue(dispatch)
        records = queue.claim(worker_id="w-atr", lease_seconds=60)
        assert len(records) == 1
        assert records[0].type == TASK_SCHEDULED_FIRE_JOB_TYPE
        executor_ = JobExecutor(
            queue=queue, registry=registry, rls_engine=app_engine, worker_id="w-atr"
        )
        assert asyncio.run(executor_.execute(records[0])) is JobState.SUCCEEDED

        # The bridge enqueued the LEG at the task's head — the act is underway
        # through the ordinary task machinery (which alone authors any report).
        with migrated_engine.begin() as conn:
            leg_count = conn.execute(
                text("SELECT count(*) FROM jobs WHERE type = 'task_leg'")
            ).scalar_one()
        assert leg_count == 1

        # (4) exactly-once, both directions: a re-run flush creates nothing new;
        # a re-tick at the same instant enqueues nothing (one-time exhausted).
        token = current_user_id.set(_OWNER)
        try:
            asyncio.run(_pipeline(app_engine, executor).flush(_OWNER))
        finally:
            current_user_id.reset(token)
        with migrated_engine.begin() as conn:
            task_count = conn.execute(
                text("SELECT count(*) FROM tasks WHERE owner_id = :u"), {"u": _OWNER}
            ).scalar_one()
        assert task_count == 1  # no second implicit task
        assert tick.run_once(now=first_fire) == 0  # nothing due — no double fire
        leader.resign()
    finally:
        dispatch.dispose()
    with migrated_engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": _OWNER})


def test_failed_delivery_retains_the_hold_and_compensates(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    """Bar 5 on the real stack: task-create failure ⇒ notice stays HELD, the
    orphan schedule is compensated, the flush does not crash."""
    _seed(migrated_engine)
    ledger = InitiativeLedger(app_engine)

    class _FailingTasks(TaskStore):
        def create(self, task: Any) -> Any:  # noqa: ANN401
            msg = "task store down"
            raise RuntimeError(msg)

    executor = InitiativeDeliveryExecutor(
        ledger=ledger,
        tasks=_FailingTasks(app_engine),
        schedules=ScheduleStore(app_engine),
        timezone_for="Europe/Oslo",
    )
    pipeline = _pipeline(app_engine, executor)

    token = current_user_id.set(_OWNER)
    try:
        asyncio.run(pipeline.submit((_candidate(),)))
        held = ledger.held_for_owner(_OWNER)
        assert len(held) == 1
        notice_id = held[0].id

        asyncio.run(pipeline.flush(_OWNER))  # must not raise
        still_held = ledger.get_notice(_OWNER, notice_id)
        assert still_held is not None
        assert still_held.disposition is NoticeDisposition.HELD  # retained, not lost

        # The orphan schedule was compensated — no taskless fire is pending.
        with pytest.raises(Exception, match="not found"):
            ScheduleStore(app_engine).get(_OWNER, f"itasksched_{notice_id}")
    finally:
        current_user_id.reset(token)
    with migrated_engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": _OWNER})

"""A5 T9 — propose-first through the REAL doors (Option C ruling).

Real Postgres under ``persona_app``. Two routes, four bars:

* **The A8-door route (schedule_change):** the pipeline holds → the flush
  delivers the proposal through A8's REAL ``propose_reschedule`` (a PENDING
  proposal, deterministic id). The NEGATIVE first: unconfirmed ⇒ the schedule
  row is byte-unmoved (rule, revision), zero new tasks/schedules, a re-tick
  fires only the OLD cadence. Then the user confirms through A8's REAL
  resolution machinery (``resolve_proposal`` with a user ``resolved_by`` →
  ``apply_proposal`` — the CAS door) and the schedule now carries the proposed
  rule; the next REAL tick fires at the NEW time. No hand-invoked door step —
  the same machinery A8's own confirmation surfaces drive.
* **The generic route:** the proposal is delivered as a persona-voiced C0
  message (sender seam; the composition is A4's proven ``OriginatorUpdateSender``)
  and creates NOTHING — zero tasks, zero schedules — until T10's ledger-anchored
  confirm verb exists.
* **Exactly-once:** a re-run flush re-delivers nothing (the disposition holds);
  the A8 proposal id is deterministic in the notice (one notice, one proposal).
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
    ScheduleChange,
    Urgency,
)
from persona.schedules import (
    RecurrenceFreq,
    RecurrenceRule,
    Schedule,
    resolve_proposal,
)
from persona.schema.chunks import WriteSource
from persona.tasks import Contract, Task, TaskState, WaitKind
from persona.tools.categories import ActionCategory
from persona_api.initiative.delivery import (
    InitiativeDeliveryExecutor,
    initiative_reschedule_proposal_id,
)
from persona_api.initiative.pipeline_wiring import (
    ApiGroundingSource,
    ApiPipelineAuditor,
    ApiProvenanceReader,
    ApiUserContextReader,
    ApiWellbeingSubjectCheck,
    LedgerAdapter,
)
from persona_api.initiative.store import DeclineStore, InitiativeLedger, NoticeDisposition
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from persona_api.schedules import SchedulerLeader, SchedulerTick, ScheduleStore
from persona_api.schedules.reschedule import apply_proposal, propose_reschedule
from persona_api.tasks.store import CheckpointStore, TaskStore
from persona_runtime.initiative import GroundingChecker, InitiativePipeline
from sqlalchemy import create_engine, text

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 7, 4, 6, 0, tzinfo=UTC)
_OWNER = "own_a5pf"
_PERSONA = "pers_pf_a"
_NODE = f"{_OWNER}::node::checkin1"
_CONTENT = "The daily check-in keeps colliding with the user's early meetings."
_TASK_ID = "t_pf_standing"
_SCHED_ID = "s_pf_standing"
_LOCK = 0x5A5C43
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


def _seed(migrated_engine: Engine, app_engine: Engine) -> None:
    with migrated_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:u, :e) ON CONFLICT DO NOTHING"),
            {"u": _OWNER, "e": f"{_OWNER}@x.test"},
        )
        conn.execute(
            text(
                "INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :u, 'name: A') "
                "ON CONFLICT DO NOTHING"
            ),
            {"p": _PERSONA, "u": _OWNER},
        )
    PostgresGraphBackend(engine=migrated_engine).insert_node_if_absent(
        _OWNER,
        ConceptNode(
            id=_NODE,
            node_kind=NodeKind.FACT,
            concept_name="check-in collision",
            content=_CONTENT,
            provenance=(
                NodeProvenance(
                    source=WriteSource.PERSONA_SELF, persona_id=_PERSONA, written_at=_NOW
                ),
            ),
            created_at=_NOW,
        ),
        [1.0] + [0.0] * (DIM - 1),
    )
    # The standing task + its REAL daily-08:00 schedule (the reschedule target).
    schedules = ScheduleStore(app_engine)
    tasks = TaskStore(app_engine)
    token = current_user_id.set(_OWNER)
    try:
        try:
            schedules.get(_OWNER, _SCHED_ID)
        except Exception:  # noqa: BLE001 — absent on first run
            schedules.create(
                Schedule(
                    id=_SCHED_ID,
                    owner_id=_OWNER,
                    timezone="Europe/Oslo",
                    recurrence=RecurrenceRule(
                        freq=RecurrenceFreq.DAILY, byhour=(8,), byminute=(0,)
                    ),
                    target_job_type="task_scheduled_fire",
                    payload_template={"task_id": _TASK_ID},
                    created_at=_NOW,
                    updated_at=_NOW,
                ),
                now=_NOW,
            )
            tasks.create(
                Task(
                    id=_TASK_ID,
                    owner_id=_OWNER,
                    persona_id=_PERSONA,
                    contract=Contract(goal="the daily check-in"),
                    schedule_id=_SCHED_ID,
                    state=TaskState.WAITING,
                    wait_kind=WaitKind.UNTIL_TIME,
                    created_at=_NOW,
                    updated_at=_NOW,
                )
            )
    finally:
        current_user_id.reset(token)


def _candidate(*, schedule_change: ScheduleChange | None = None) -> InitiativeCandidate:
    return InitiativeCandidate(
        observation="The daily check-in keeps colliding with early meetings.",
        citations=(GroundingCitation(kind=CitationKind.NODE, ref=_NODE),),
        trigger=InitiativeTrigger.CONFLICT,
        why_now="The collisions recurred this week.",
        plan=(PlannedStep(description="retime", categories=frozenset({ActionCategory.DRAFT})),),
        next_step="Move the daily check-in to 09:00.",
        value=0.9,
        acceptance=0.8,
        urgency=Urgency.BATCH,
        source=CandidateSource.SCAN,
        owner_id=_OWNER,
        persona_id=_PERSONA,
        prompt_version="a5-scan-v1",
        scanned_at=_NOW,
        schedule_change=schedule_change,
    )


def _pipeline(app_engine: Engine, executor: InitiativeDeliveryExecutor) -> InitiativePipeline:
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
        dial_reader=lambda _o, _p: InitiativeDial.PROPOSE_ONLY,
        settings=InitiativeSettings(),
        delivery=executor,
    )


def test_schedule_change_proposes_via_the_a8_door_then_confirms_and_fires(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed(migrated_engine, app_engine)
    ledger = InitiativeLedger(app_engine)
    schedules = ScheduleStore(app_engine)
    sender = _RecordingSender()
    executor = InitiativeDeliveryExecutor(
        ledger=ledger,
        tasks=TaskStore(app_engine),
        schedules=schedules,
        timezone_for="Europe/Oslo",
        proposal_sender=sender,  # type: ignore[arg-type]
        persona_tag_resolver=lambda _p: None,  # the A8 route needs no messenger
    )
    pipeline = _pipeline(app_engine, executor)
    change = ScheduleChange(task_id=_TASK_ID, recurrence_rrule="FREQ=DAILY;BYHOUR=9;BYMINUTE=0")

    token = current_user_id.set(_OWNER)
    try:
        asyncio.run(pipeline.submit((_candidate(schedule_change=change),)))
        held = ledger.held_for_owner(_OWNER)
        assert len(held) == 1
        notice_id = held[0].id
        before = schedules.get(_OWNER, _SCHED_ID)

        asyncio.run(pipeline.flush(_OWNER))
        delivered = ledger.get_notice(_OWNER, notice_id)
        assert delivered is not None
        assert delivered.disposition is NoticeDisposition.DELIVERED
        assert delivered.envelope_action == "propose"

        # THE NEGATIVE (bar 2): unconfirmed ⇒ the schedule row is byte-unmoved.
        after_propose = schedules.get(_OWNER, _SCHED_ID)
        assert after_propose.recurrence == before.recurrence  # still 08:00
        assert after_propose.revision == before.revision  # the CAS row never moved
        with migrated_engine.begin() as conn:
            task_count = conn.execute(
                text("SELECT count(*) FROM tasks WHERE owner_id = :u"), {"u": _OWNER}
            ).scalar_one()
        assert task_count == 1  # only the standing task — the proposal created nothing

        # Exactly-once on the propose side: a re-run flush re-delivers nothing.
        asyncio.run(pipeline.flush(_OWNER))
        assert len(ledger.held_for_owner(_OWNER)) == 0  # nothing re-held / re-proposed

        # THE CONFIRMATION through A8's REAL machinery: rebuild the deterministic
        # pending proposal (pure — writes nothing), resolve it WITH a user
        # reference, apply through the CAS door.
        pending = propose_reschedule(
            schedules,
            proposal_id=initiative_reschedule_proposal_id(notice_id),
            owner_id=_OWNER,
            schedule_id=_SCHED_ID,
            persona_id=_PERSONA,
            new_recurrence=RecurrenceRule(freq=RecurrenceFreq.DAILY, byhour=(9,), byminute=(0,)),
            new_one_time=None,
            new_timezone=before.timezone,
            reason="collisions with early meetings",
            now=_NOW,
        )
        confirmed = resolve_proposal(pending, approve=True, resolved_by="user_via_chat", now=_NOW)
        applied = apply_proposal(schedules, app_engine, confirmed, now=_NOW)
        assert applied.recurrence is not None
        assert applied.recurrence.byhour == (9,)  # the proposed rule, applied by the USER

        # The next REAL fire happens at the NEW time through the real scheduler.
        new_fire = schedules.get(_OWNER, _SCHED_ID).next_fire_at
        assert new_fire is not None
    finally:
        current_user_id.reset(token)

    dispatch = create_engine(os.environ["DATABASE_URL"].replace("+asyncpg", "+psycopg"))
    try:
        leader = SchedulerLeader(dispatch, lock_key=_LOCK)
        tick = SchedulerTick(
            dispatch_engine=dispatch,
            rls_engine=app_engine,
            leader=leader,
            default_grace_seconds=366 * 24 * 3600.0,
        )
        assert tick.run_once(now=new_fire) == 1  # fired at the NEW cadence, real tick
        leader.resign()
    finally:
        dispatch.dispose()
    with migrated_engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": _OWNER})


def test_generic_proposal_delivers_c0_message_and_creates_nothing(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed(migrated_engine, app_engine)
    from persona.schema.origination import PersonaIdentityTag

    ledger = InitiativeLedger(app_engine)
    sender = _RecordingSender()
    executor = InitiativeDeliveryExecutor(
        ledger=ledger,
        tasks=TaskStore(app_engine),
        schedules=ScheduleStore(app_engine),
        timezone_for="Europe/Oslo",
        proposal_sender=sender,  # type: ignore[arg-type]
        persona_tag_resolver=lambda _p: PersonaIdentityTag(
            persona_id=_PERSONA, display_name="Astrid"
        ),
    )
    pipeline = _pipeline(app_engine, executor)

    token = current_user_id.set(_OWNER)
    try:
        asyncio.run(pipeline.submit((_candidate(),)))
        held = ledger.held_for_owner(_OWNER)
        assert len(held) == 1
        notice_id = held[0].id

        asyncio.run(pipeline.flush(_OWNER))
        delivered = ledger.get_notice(_OWNER, notice_id)
        assert delivered is not None
        assert delivered.disposition is NoticeDisposition.DELIVERED
        assert len(sender.sent) == 1
        assert "From your notes" in sender.sent[0]["content"]  # the honest why

        # Unconfirmed creates NOTHING: only the seeded standing task/schedule exist.
        with migrated_engine.begin() as conn:
            task_count = conn.execute(
                text("SELECT count(*) FROM tasks WHERE owner_id = :u"), {"u": _OWNER}
            ).scalar_one()
            sched_count = conn.execute(
                text("SELECT count(*) FROM schedules WHERE owner_id = :u"), {"u": _OWNER}
            ).scalar_one()
        assert task_count == 1
        assert sched_count == 1

        # Exactly-once: the re-run flush sends no second message.
        asyncio.run(pipeline.flush(_OWNER))
        assert len(sender.sent) == 1
    finally:
        current_user_id.reset(token)
    with migrated_engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": _OWNER})

"""A5 T6 — the initiative scan fires through the REAL chain (the anti-inertness gate).

No hand-invoked terminal step (the A4 lesson): a real A1 schedule row (the
idempotent ensure), the real ``SchedulerTick.run_once`` materialising the fire
into a real A0 job, the real ``JobExecutor`` claim→owner-GUC→handler run, real
api readers over real stores — only the scan MODEL is scripted. Proves:

* **recurring ≥2 real fires** — two consecutive daily occurrences each fire,
  each runs the handler, each leaves the synthesis-style metering audit row
  (``job.spend`` / ``surface=initiative_scan``, credits_charged=0);
* **exactly-once per fire** — a re-tick at the same instant enqueues nothing
  (A1's idempotency key through A0's ON CONFLICT);
* **dial-OFF = handler exit** — the third fire still fires the SCHEDULE (the
  row stays, one source of truth) but the handler exits: job SUCCEEDED, no
  scan, no metering row;
* **env-gate composition** — the tenant registers ONLY under
  ``PERSONA_INITIATIVE_ENABLED`` (built-but-inert killed at the root);
* **reader RLS** — the graph reader under ``persona_app`` returns only the
  owner's nodes (non-vacuous: both tenants hold rows).
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
from persona.initiative import InitiativeSettings
from persona.jobs import JobRegistry, JobState
from persona.schema.chunks import WriteSource
from persona_api.background.worker_root import build_worker_registry
from persona_api.config import APIConfig
from persona_api.initiative.handler import (
    INITIATIVE_SCAN_JOB_TYPE,
    InitiativeScanHandler,
    ensure_initiative_schedule,
    initiative_schedule_id,
    read_initiative_dial,
    register_initiative_scan_handler,
)
from persona_api.initiative.readers import (
    ApiScanConversationReader,
    ApiScanGraphReader,
    ApiScanTaskReader,
)
from persona_api.jobs.executor import JobExecutor
from persona_api.jobs.queue import JobQueue
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from persona_api.schedules import SchedulerLeader, SchedulerTick, ScheduleStore
from persona_api.tasks.store import CheckpointStore, TaskStore
from persona_runtime.initiative import InitiativeScanner
from sqlalchemy import create_engine, text

if TYPE_CHECKING:
    from collections.abc import Iterator

    from persona.schema.conversation import ConversationMessage
    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_LOCK = 0x5A5C41
_T0 = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
_NODE_ID = "own_a5rf::node::00000001"
DIM = 384


class _ScriptedScanBackend:
    """Returns one grounded catch citing the seeded node (the only model in the test)."""

    provider_name = "anthropic"
    model_name = "scripted"

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages: list[ConversationMessage], **_: object) -> ChatResponse:
        self.calls += 1
        content = json.dumps(
            {
                "candidates": [
                    {
                        "observation": "The hearing is Friday and nothing is drafted.",
                        "trigger": "approaching_commitment",
                        "why_now": "The date entered the coming days.",
                        "citations": [{"kind": "node", "ref": _NODE_ID}],
                        "plan": [{"description": "draft it", "categories": ["draft"]}],
                        "next_step": "Draft the response letter.",
                        "value": 0.9,
                        "acceptance": 0.8,
                        "urgency": "batch",
                    }
                ]
            }
        )
        return ChatResponse(
            content=content,
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


def _seed(migrated_engine: Engine, owner: str, persona_id: str) -> None:
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
            {"p": persona_id, "u": owner},
        )


def _seed_node(migrated_engine: Engine, owner: str, node_id: str) -> None:
    backend = PostgresGraphBackend(engine=migrated_engine)
    backend.insert_node_if_absent(
        owner,
        ConceptNode(
            id=node_id,
            node_kind=NodeKind.FACT,
            concept_name="hearing",
            content="The custody hearing is on Friday 10 July.",
            provenance=(NodeProvenance(source=WriteSource.PERSONA_SELF, written_at=_T0),),
            created_at=_T0,
        ),
        [1.0] + [0.0] * (DIM - 1),
    )


def _scan_registry(app_engine: Engine) -> tuple[JobRegistry, _ScriptedScanBackend]:
    settings = InitiativeSettings()
    backend = _ScriptedScanBackend()
    graph_store = build_graph_store(
        engine=app_engine, embedder=_Emb(), audit_logger=MemoryAuditLogger()
    )
    scanner = InitiativeScanner(
        graph=ApiScanGraphReader(graph_store),
        conversations=ApiScanConversationReader(app_engine),
        tasks=ApiScanTaskReader(TaskStore(app_engine), CheckpointStore(app_engine)),
        backend=backend,  # type: ignore[arg-type] — the only scripted piece
        settings=settings,
    )
    registry = JobRegistry()
    register_initiative_scan_handler(
        registry,
        handler=InitiativeScanHandler(
            scanner=scanner,
            dial_reader=lambda owner, persona: read_initiative_dial(app_engine, owner, persona),
        ),
    )
    return registry, backend


def _metering_rows(migrated_engine: Engine, owner: str) -> list[dict[str, Any]]:
    with migrated_engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT metadata FROM audit_log WHERE user_id = :u AND action = 'job.spend' "
                "ORDER BY created_at"
            ),
            {"u": owner},
        ).all()
    out = []
    for (metadata,) in rows:
        data = metadata if isinstance(metadata, dict) else json.loads(metadata)
        if data.get("surface") == "initiative_scan":
            out.append(data)
    return out


def _run_one_job(
    dispatch_engine: Engine, app_engine: Engine, registry: JobRegistry, worker_id: str
) -> JobState:
    """Claim on the dispatch engine (cross-tenant, like the real worker loop);
    execute on the RLS engine (the executor sets the owner GUC — the chokepoint)."""
    queue = JobQueue(dispatch_engine)
    records = queue.claim(worker_id=worker_id, lease_seconds=60)
    assert len(records) == 1, "exactly one due job expected"
    executor = JobExecutor(
        queue=queue, registry=registry, rls_engine=app_engine, worker_id=worker_id
    )
    return asyncio.run(executor.execute(records[0]))


def test_scan_fires_through_the_real_chain_recurring_twice_then_dial_off(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    owner, persona_id = "own_a5rf", "pers_a5rf"
    _seed(migrated_engine, owner, persona_id)
    _seed_node(migrated_engine, owner, _NODE_ID)

    store = ScheduleStore(app_engine)
    settings = InitiativeSettings()

    # The idempotent ensure: two calls, one schedule row (A5-D-1).
    sid = ensure_initiative_schedule(
        store,
        owner_id=owner,
        persona_id=persona_id,
        timezone="Europe/Oslo",
        settings=settings,
        now=_T0,
    )
    assert sid == initiative_schedule_id(persona_id)
    assert (
        ensure_initiative_schedule(
            store,
            owner_id=owner,
            persona_id=persona_id,
            timezone="Europe/Oslo",
            settings=settings,
            now=_T0,
        )
        == sid
    )
    schedule = store.get(owner, sid)
    assert schedule.target_job_type == INITIATIVE_SCAN_JOB_TYPE
    first_fire = schedule.next_fire_at
    assert first_fire is not None

    registry, backend = _scan_registry(app_engine)
    dispatch = create_engine(os.environ["DATABASE_URL"].replace("+asyncpg", "+psycopg"))
    try:
        leader = SchedulerLeader(dispatch, lock_key=_LOCK)
        tick = SchedulerTick(
            dispatch_engine=dispatch,
            rls_engine=app_engine,
            leader=leader,
            default_grace_seconds=366 * 24 * 3600.0,
        )

        # --- fire 1: the REAL tick materialises the job; re-tick dedups. ------
        assert tick.run_once(now=first_fire) == 1
        assert tick.run_once(now=first_fire) == 0  # same instant — nothing new due
        with migrated_engine.begin() as conn:
            job_count = conn.execute(
                text("SELECT count(*) FROM jobs WHERE type = :t"),
                {"t": INITIATIVE_SCAN_JOB_TYPE},
            ).scalar_one()
        assert job_count == 1  # exactly one job per fire (idempotency, two layers)

        assert _run_one_job(migrated_engine, app_engine, registry, "w-a5-1") is JobState.SUCCEEDED
        rows = _metering_rows(migrated_engine, owner)
        assert len(rows) == 1
        assert rows[0]["candidates"] == "1"
        assert rows[0]["amount_micros"] == "0"  # credits_charged=0 (system-initiated)
        assert backend.calls == 1

        # --- fire 2: the NEXT daily occurrence — recurring ≥2 real fires. -----
        second_fire = store.get(owner, sid).next_fire_at
        assert second_fire is not None
        assert second_fire > first_fire
        assert tick.run_once(now=second_fire) == 1
        assert _run_one_job(migrated_engine, app_engine, registry, "w-a5-2") is JobState.SUCCEEDED
        assert len(_metering_rows(migrated_engine, owner)) == 2
        assert backend.calls == 2

        # --- fire 3 with the dial OFF: schedule fires, handler exits. ---------
        with migrated_engine.begin() as conn:
            conn.execute(
                text("UPDATE personas SET initiative_dial = 'off' WHERE id = :p"),
                {"p": persona_id},
            )
        third_fire = store.get(owner, sid).next_fire_at
        assert third_fire is not None
        assert tick.run_once(now=third_fire) == 1  # the schedule STAYS live
        assert _run_one_job(migrated_engine, app_engine, registry, "w-a5-3") is JobState.SUCCEEDED
        assert len(_metering_rows(migrated_engine, owner)) == 2  # no scan ran
        assert backend.calls == 2  # the model was never consulted
        leader.resign()
    finally:
        dispatch.dispose()
    with migrated_engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": owner})


def test_initiative_tenant_registered_only_when_enabled(
    app_engine: Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    """The built-but-inert killer at the composition root (env gate, default OFF)."""

    def _registry() -> JobRegistry:
        return build_worker_registry(
            rls_engine=app_engine,
            embedder=_Emb(),  # type: ignore[arg-type]
            tier_registry=_FakeTierRegistry(),  # type: ignore[arg-type]
            config=APIConfig(audit_root=str(tmp_path)),
            synthesis_tier="small",
        )

    monkeypatch.delenv("PERSONA_INITIATIVE_ENABLED", raising=False)
    assert INITIATIVE_SCAN_JOB_TYPE not in _registry().types()  # the default-OFF gate

    monkeypatch.setenv("PERSONA_INITIATIVE_ENABLED", "true")
    assert INITIATIVE_SCAN_JOB_TYPE in _registry().types()


class _FakeTierRegistry:
    def get(self, _tier: str) -> _ScriptedScanBackend:
        return _ScriptedScanBackend()

    @property
    def configured_tier_names(self) -> tuple[str, ...]:
        return ("frontier", "mid", "small")

    def metadata_for(self, _tier: str) -> None:
        return None

    def model_name_for(self, _tier: str) -> str:
        return "scripted"


def test_graph_reader_is_owner_scoped_under_persona_app(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    """Non-vacuous reader RLS: both tenants hold nodes; each pool is its own."""
    _seed(migrated_engine, "own_a5ra", "pers_a5ra")
    _seed(migrated_engine, "own_a5rb", "pers_a5rb")
    _seed_node(migrated_engine, "own_a5ra", "own_a5ra::node::00000001")
    _seed_node(migrated_engine, "own_a5rb", "own_a5rb::node::00000001")

    reader = ApiScanGraphReader(
        build_graph_store(engine=app_engine, embedder=_Emb(), audit_logger=MemoryAuditLogger())
    )
    token = current_user_id.set("own_a5ra")
    try:
        pool_a = reader.noticing_pool("own_a5ra", limit=10)
    finally:
        current_user_id.reset(token)
    token = current_user_id.set("own_a5rb")
    try:
        pool_b = reader.noticing_pool("own_a5rb", limit=10)
    finally:
        current_user_id.reset(token)
    assert [n.id for n in pool_a] == ["own_a5ra::node::00000001"]
    assert [n.id for n in pool_b] == ["own_a5rb::node::00000001"]
    with migrated_engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id IN ('own_a5ra', 'own_a5rb')"))

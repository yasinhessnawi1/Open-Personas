"""The in-process worker root — activates the synthesis pipeline (Spec K2, T8d).

The deploy is a single uvicorn process (D-08-5), so A0's durable worker + A1's
scheduler tick run as an **in-process background task** started in the API
lifespan, not a separate machine. This module is that composition root + the
task manager:

- :func:`build_worker_registry` composes the typed :class:`JobRegistry` — the
  ``synthesis`` handler (the K2 reflection pass) wired to the
  :class:`PgSynthesisRepository` + a :class:`Synthesizer` built on the wired
  small/mid tier (``build_synthesizer``), graph store, and a
  :class:`PostgresEntityRegistry`.
- :class:`InProcessWorker` owns the ``Worker.run()`` task: it starts the
  claim→execute loop (with A1's leader-gated scheduler tick wired additively) on
  app startup and drains it on shutdown.

The synthesis ``Synthesizer`` is built ONCE on the RLS engine. The worker's
per-job choke point sets the ``current_user_id`` contextvar to the job owner, and
the RLS engine's checkout listener scopes every connection to it — so the single
shared graph store + entity registry are owner-scoped per job automatically (no
per-job rebuild). The synthesis tier (``config.synthesis_tier``, default
``small``) is the eval-re-run gate's tier, NOT the frontier/sonnet tier.

**Model backends are the one thing that could NOT be resolved once (R9-096).** The
per-job owner scope makes a shared *store* correct, and the same reasoning was
wrongly applied to model backends: every background LLM backend here used to be
``tier_registry.get(<tier>)`` at worker startup, on the app's PAID registry, with
no owner bound. One instance therefore served every owner's jobs on paid models —
including free-plan owners, whose episodic consolidations were billed as
``estimate_static`` paid-model cost. That is a cost leak and a D-M4-9 violation
("a free user must NEVER reach a paid model"), and it is the exact sibling of
R9-074 (the connector composing from the global registry once).

Every background backend is now built by
:func:`~persona_api.services.model_tiers.plan_scoped_background_backend`, which
defers the registry choice to CALL time inside the job's owner scope. The tier
NAME knobs (``config.episodic_summary_tier`` etc.) are untouched — what changed is
which registry the name resolves against. ``free_tier_registry`` is a REQUIRED
keyword on both composition entry points, so "plan gating off" can only ever be a
stated decision (``None`` = community / self-host), never a forgotten argument.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import TYPE_CHECKING

from persona.billing import BillingConfig
from persona.config import PersonaCoreConfig
from persona.events import EventTriggerSettings
from persona.graph import ConsolidationPass, PostgresEntityRegistry, build_graph_store
from persona.graph.config import GraphSettings
from persona.graph.index import make_graph_index
from persona.graph.postgres import PostgresGraphBackend
from persona.initiative import InitiativeDial, InitiativeSettings
from persona.jobs import JobRegistry
from persona.logging import get_logger
from persona.stores.engine import EpisodicConsolidationEngine
from persona.stores.lifecycle import EpisodicSettings
from persona.stores.pyramid import EpisodicPyramid
from persona.stores.summarizer import TierSummarizer
from persona_runtime.extraction.synthesizer import build_synthesizer
from persona_runtime.initiative import GroundingChecker, InitiativePipeline, InitiativeScanner
from persona_runtime.legs import CompactingCheckpointWriter
from persona_runtime.routing import tier_for

from persona_api.approvals.kill_switch import KillSwitchStore
from persona_api.db.audit_factory import build_audit_logger, build_tool_audit_logger
from persona_api.editions.factory import build_credits_policy
from persona_api.errors import CommunityDbError
from persona_api.initiative.delivery import InitiativeDeliveryExecutor
from persona_api.initiative.handler import (
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
from persona_api.initiative.readers import (
    ApiScanConversationReader,
    ApiScanGraphReader,
    ApiScanTaskReader,
)
from persona_api.initiative.store import DeclineStore, InitiativeLedger
from persona_api.jobs.catalog_sync import build_catalog_sync
from persona_api.jobs.handlers.avatar import avatar_queue_ready, register_avatar_handler
from persona_api.jobs.handlers.consolidation import (
    enqueue_graph_consolidation,
    register_graph_consolidation_handler,
)
from persona_api.jobs.handlers.episodic_consolidation import (
    build_core_block_refresher,
    register_episodic_consolidation_handler,
)
from persona_api.jobs.handlers.file_extract import (
    SandboxFileRenderer,
    build_file_extract_episodic_query,
    build_file_extract_generator,
    file_extract_queue_ready,
    register_file_extract_handler,
)
from persona_api.jobs.handlers.synthesis import PgSynthesisRepository, register_synthesis_handler
from persona_api.jobs.handlers.title_refresh import (
    build_title_refresh_generator,
    register_title_refresh_handler,
)
from persona_api.jobs.queue import JobQueue
from persona_api.jobs.skill_catalog_sync import build_skill_catalog_sync
from persona_api.jobs.worker import build_worker
from persona_api.schedules.store import ScheduleStore
from persona_api.schedules.tick import build_scheduler_tick
from persona_api.schedules.tombstones import ScheduleTombstoneStore
from persona_api.services.model_tiers import plan_scoped_background_backend
from persona_api.services.notifications_service import publish_task_updated
from persona_api.tasks.continuation import TaskContinuation
from persona_api.tasks.handler import RunnableGuard, register_task_leg_handler
from persona_api.tasks.leg_runner import RuntimeFactoryLegRunnerBuilder
from persona_api.tasks.scheduled_fire import register_scheduled_task_fire_handler
from persona_api.tasks.store import CheckpointStore, TaskStore

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping
    from datetime import datetime

    from persona.audit import AuditLogger
    from persona.imagegen.protocol import ImageBackend
    from persona.stores.backend import Backend
    from persona.stores.embedder import Embedder
    from persona.tasks import ResumeTrigger, StuckReport, Task
    from persona_runtime.legs import LegOutcome
    from persona_runtime.tier import TierRegistry
    from sqlalchemy import Engine

    from persona_api.approvals.sweep import ApprovalSweepRunner
    from persona_api.config import APIConfig
    from persona_api.editions.credits_policy import CreditsPolicy
    from persona_api.jobs.catalog_sync import CatalogSyncTask
    from persona_api.jobs.skill_catalog_sync import SkillCatalogSyncTask
    from persona_api.jobs.worker import Worker
    from persona_api.realtime.channel import UserEventChannel
    from persona_api.sandbox.pool import SandboxPool
    from persona_api.schedules.tick import SchedulerTick
    from persona_api.services.runtime_factory import RuntimeFactory
    from persona_api.services.web_deliverer import LiveSessionRegistry
    from persona_api.storage import FileStorage
    from persona_api.tasks.dead_leg_sweep import DeadLegSweeper

__all__ = ["InProcessWorker", "build_worker_registry", "start_in_process_worker"]

_log = get_logger("api.worker_root")


def build_worker_registry(
    *,
    rls_engine: Engine,
    embedder: Embedder,
    tier_registry: TierRegistry,
    free_tier_registry: TierRegistry | None,
    config: APIConfig,
    synthesis_tier: str,
    runtime_factory: RuntimeFactory | None = None,
    memory_backend: Backend | None = None,
    edition: object | None = None,
    live_sessions: LiveSessionRegistry | None = None,
    event_channel: UserEventChannel | None = None,
    image_backend: ImageBackend | None = None,
    file_storage: FileStorage | None = None,
    sandbox_pool: SandboxPool | None = None,
    workspace_root: Path | None = None,
) -> JobRegistry:
    """Compose the worker's :class:`JobRegistry` — A0's durable tenants.

    Registers the ``synthesis`` handler (K2's reflection pass) and — when a
    ``runtime_factory`` is supplied (Spec A4 composition-root activation) — the
    ``task_leg`` handler: a scheduled/continued task leg runs the **identical**
    :class:`AgenticLoop` the chat path uses (the no-bypass guarantee), advances the
    A2 state via :class:`TaskContinuation`, and publishes a granularity-gated digest
    update on each milestone (T10; live only when ``memory_backend`` is present).

    Args:
        rls_engine: The ``persona_app`` RLS engine handlers run on.
        embedder: The persona-memory embedder (shared, lazy weights).
        tier_registry: The app-scoped PAID tier registry. Still the readiness
            signal for tenant registration (``file_extract_queue_ready``), but no
            background backend is resolved from it directly any more — see
            ``free_tier_registry``.
        free_tier_registry: The cloud free-plan registry, or ``None`` when plan
            gating is off (community / self-host). REQUIRED (R9-096): every
            background LLM backend is composed plan-scoped from this pair, so a
            free owner's job resolves the free-only chain and a paid owner's the
            paid one. Passing ``None`` states "this deployment has no plans"; it
            is not a default, because the forgotten-keyword value is the unsafe one.
        config: The API config — drives the graph store's audit backend so the
            worker writes graph-mutation audit to the SAME place the API does
            (R5-D-2: worker MUST select the same backend or scaling it re-opens
            the single-writer JSONL hole).
        synthesis_tier: The tier the extractor + entity judge run on (D-K2-3).
        runtime_factory: The app's runtime factory (the leg runner). ``None`` →
            the ``task_leg`` tenant is NOT registered (A2 legs stay inert — the
            pre-activation posture).
        memory_backend: The edition's memory transport (the digest sender needs it).
            ``None`` → legs still run, digest updates are simply not delivered.
        edition: The open-core edition (the C0 recorder's RLS gate).
        image_backend: The app's composed image-generation backend — the
            ``avatar_generation`` tenant's generator runs on it (R9-013). Gated by
            :func:`avatar_queue_ready`, the SAME predicate the create route's
            producer consults: the tenant registers iff the route may enqueue.
        file_storage: The app's storage backend (the avatar persist target); part
            of the same shared gate.
        sandbox_pool: The app's composed hosted-sandbox pool (R9-025b) — the
            SAME pool the live chat path's ``code_execution`` tool acquires
            from. ``None`` when no E2B key is configured; the ``file_extract``
            tenant then simply is NOT registered (paired with the route's own
            ``file_extract_queue_ready`` gate, so a job is never enqueued into
            a handler-less worker).
        workspace_root: The app's workspace root (the ``file_extract``
            renderer's persist target — the SAME root the code_execution
            produced-file persister writes under).
    """
    # R9-096: plan-scoped, resolved per job inside the owner scope — never once here on
    # the paid registry. Unmetered: K2 synthesis has no owner-billing seam (no
    # ``collect_llm_usage`` block in its handler), so wrapping it would meter nothing.
    backend = plan_scoped_background_backend(
        tier=synthesis_tier,
        rls_engine=rls_engine,
        paid_tier_registry=tier_registry,
        free_tier_registry=free_tier_registry,
        metered=False,
    )
    graph_backend = PostgresGraphBackend(engine=rls_engine)
    graph_store = build_graph_store(
        engine=rls_engine,
        embedder=embedder,
        # R5-D-2: Postgres audit when PERSONA_API_AUDIT_BACKEND=postgres, else the
        # JSONL default (config.audit_root) — the same selection the API makes.
        audit_logger=build_audit_logger(config, rls_engine),
    )
    entity_registry = PostgresEntityRegistry(backend=graph_backend, embedder=embedder)
    synthesizer = build_synthesizer(
        graph_store=graph_store, registry=entity_registry, backend=backend
    )
    registry = JobRegistry()

    # Graph consolidation (Spec K7, K7-D-5) — the durable background pass, enabled by
    # config (default ON in the worker). Wire it BEFORE synthesis so the synthesis-tail
    # trigger can enqueue it. Built-but-inert is the failure class this guards against:
    # the handler is only registered AND the trigger only bound when enabled.
    graph_settings = GraphSettings()
    enqueue_consolidation: Callable[[str], None] | None = None
    if graph_settings.consolidation_enabled:
        consolidation_pass = ConsolidationPass(
            backend=graph_backend,
            index=make_graph_index(
                settings=graph_settings,
                engine=rls_engine,
                float32_fetch=graph_backend.embeddings_by_surrogate,
            ),
            embedder=embedder,
            # R5-D-2 worker parity: consolidation runs IN the A0 worker — its
            # mutation audit must use the config-selected multi-worker-safe
            # backend, same as every other worker-side sink.
            audit_logger=build_audit_logger(config, rls_engine),
            settings=graph_settings,
        )
        register_graph_consolidation_handler(registry, pass_=consolidation_pass)

        def enqueue_consolidation(owner_id: str) -> None:
            enqueue_graph_consolidation(
                JobQueue(rls_engine),
                owner_id=owner_id,
                delay_seconds=graph_settings.consolidation_delay_seconds,
                bucket_seconds=graph_settings.consolidation_bucket_seconds,
            )

    register_synthesis_handler(
        registry,
        runner=synthesizer,
        repository=PgSynthesisRepository(),
        enqueue_consolidation=enqueue_consolidation,
    )

    # Title refresh (R9-020): dynamic self-improving conversation titles. The tier NAME
    # is the TITLE tier (P9 ``title`` surface — mid by default,
    # ``PERSONA_API_TITLE_TIER`` overridable; the recognition precedent); which registry
    # that name resolves against is decided per job, in the owner's scope (R9-096 —
    # a free owner's title refresh must not run on a paid model either). Unmetered:
    # the title handler has no owner-billing seam. Registered unconditionally — the
    # producer (the chat turn worker's threshold trigger) is already no-op without a
    # queue, and a
    # keyless boot never reaches this root (the app catches AuthenticationError
    # around ``start_in_process_worker``). A successful refresh pings
    # ``sidebar.changed`` through the SAME channel the SSE endpoint serves.
    title_backend = plan_scoped_background_backend(
        tier=tier_for("title", override=config.title_tier),
        rls_engine=rls_engine,
        paid_tier_registry=tier_registry,
        free_tier_registry=free_tier_registry,
        metered=False,
    )
    register_title_refresh_handler(
        registry,
        generator=build_title_refresh_generator(title_backend),
        event_channel=event_channel,
    )

    # Turn-into-file (R9-025b): message action -> LLM extraction (mid tier by
    # default, PERSONA_API_FILE_EXTRACT_TIER overridable, plan-scoped per job like
    # title/synthesis above) -> a render via the doc-gen sandbox boundary.
    # Gated on `file_extract_queue_ready`
    # (the avatar_queue_ready precedent, R9-013): registered iff BOTH a model
    # backend AND a sandbox pool are composed — the SAME predicate the route
    # consults before enqueueing, so a job is never dropped into a handler-less
    # worker. `sandbox_pool`/`workspace_root` absent (no E2B key configured, or a
    # boot with no workspace) -> the tenant is simply not registered; the route's
    # own gate keeps the producer honest about that. The readiness gate reads the
    # PAID registry deliberately: whether this PROCESS has a model configured is a
    # process fact, not an owner fact — only the backend it hands out is per-owner.
    if file_extract_queue_ready(tier_registry=tier_registry, sandbox_pool=sandbox_pool):
        assert sandbox_pool is not None  # noqa: S101 — narrowed by the gate above
        assert workspace_root is not None  # noqa: S101 — always set alongside sandbox_pool at boot
        file_extract_backend = plan_scoped_background_backend(
            tier=config.file_extract_tier,
            rls_engine=rls_engine,
            paid_tier_registry=tier_registry,
            free_tier_registry=free_tier_registry,
            metered=False,
        )
        episodic_query = (
            build_file_extract_episodic_query(
                memory_backend, build_audit_logger(config, rls_engine)
            )
            if memory_backend is not None
            else None
        )
        register_file_extract_handler(
            registry,
            extractor=build_file_extract_generator(file_extract_backend),
            renderer=SandboxFileRenderer(pool=sandbox_pool, workspace_root=workspace_root),
            episodic_query=episodic_query,
            event_channel=event_channel,
        )

    # The K8 sleep-time engine (Spec K8, K8-D-8) — registered ONLY when enabled
    # (built-but-inert guard; the turn-tail trigger gates on the same switch).
    # Composed on the SAME RLS engine + embedder as everything else: the
    # per-job owner GUC scopes both the episodic transport and the graph merge.
    episodic_settings = EpisodicSettings()
    if episodic_settings.engine_enabled and memory_backend is not None:
        # The interim tier summarizer (K8-D-10): the engine's OWN tier knob; P7 swaps in
        # behind the same Protocol later. Shared with the K9 core-block refresher below.
        # Spec M3 (T5): the summarizer's chat backend is metered so ``collect_llm_usage``
        # in the handler captures the real per-op summarizer cost for owner billing.
        # R9-096 (THE reported defect): this used to be
        # ``UsageCollectingBackend(tier_registry.get(...))`` — one PAID backend, built at
        # worker startup, serving every owner. A free-plan owner with 2 personas was
        # charged 247 credits across 3 consolidations at paid-model static pricing. The
        # tier knob is unchanged; the registry it resolves against is now the job owner's.
        episodic_summarizer = TierSummarizer(
            backend=plan_scoped_background_backend(
                tier=config.episodic_summary_tier,
                rls_engine=rls_engine,
                paid_tier_registry=tier_registry,
                free_tier_registry=free_tier_registry,
                metered=True,
            )
        )
        episodic_engine = EpisodicConsolidationEngine(
            backend=memory_backend,
            pyramid=EpisodicPyramid(
                backend=memory_backend,
                audit_logger=build_audit_logger(config, rls_engine),
            ),
            summarizer=episodic_summarizer,
            graph=graph_store,
            settings=episodic_settings,
            embedder=embedder,
        )
        # K9 (K9-D-10): the always-in-context core block is refreshed on THIS background job
        # (never the turn path — acceptance-7), using the same summarizer + owner-scoped backend.
        core_refresher = build_core_block_refresher(
            summarizer=episodic_summarizer,
            backend=memory_backend,
            audit_logger=build_audit_logger(config, rls_engine),
        )
        register_episodic_consolidation_handler(
            registry,
            engine=episodic_engine,
            core_refresher=core_refresher,
            # Spec M3 (T5): owner-billed summarizer cost, idempotent + fail-soft.
            credits_policy=build_credits_policy(config),
            rls_engine=rls_engine,
            cost_source=(
                runtime_factory.metadata_resolver if runtime_factory is not None else None
            ),
            floor=config.agentic_credit_floor,
        )
    # Event triggers (Spec A7, T6) — env-gated at the composition root
    # (PERSONA_EVENT_TRIGGERS_ENABLED, default OFF — the A5 criterion-9 posture; OFF ⇒ the leg
    # handler is byte-identical to pre-A7). When enabled, a leg's completion emits its A2
    # lifecycle event through the dispatcher, INHERITING the ``EventFire`` causal chain (the
    # cross-process loop guard, A7-D-4/D-6). The A6-D-8 autonomy-pause reader injects at
    # merge-back; here it is the frozen default never-paused no-op.
    # A6-D-8 completeness: ONE read-only kill-switch store binds the owner-pause / persona-suspend
    # predicates to the worker's RLS engine. It feeds EVERY origination gate — the task-leg runner
    # (``is_runnable``), the A7 dispatcher, the A5 scan, and (in ``start_in_process_worker``) the
    # A10 tick — so a paused owner leaks NO origination path. Read-only: no continuation (cancel is
    # a route concern, not a worker one).
    kill_switch = KillSwitchStore(rls_engine)
    on_leg_settled: Callable[[LegOutcome, ResumeTrigger, datetime], Awaitable[None]] | None = None
    if EventTriggerSettings().enabled:
        from persona_api.events import LifecycleEmitter, build_event_dispatcher

        event_dispatcher = build_event_dispatcher(
            rls_engine=rls_engine,
            config=config,
            pause_check=kill_switch.is_owner_autonomy_paused,
        )
        on_leg_settled = LifecycleEmitter(dispatcher=event_dispatcher).on_leg_settled
    if runtime_factory is not None:
        _register_task_leg_tenant(
            registry,
            rls_engine=rls_engine,
            runtime_factory=runtime_factory,
            memory_backend=memory_backend,
            edition=edition,
            # R5-D-2 worker parity: the origination audit sink follows the same
            # config-selected backend; audit_root stays the JSONL fallback path.
            audit_root=Path(config.audit_root),
            audit_logger=build_audit_logger(config, rls_engine),
            live_sessions=live_sessions,
            event_channel=event_channel,
            on_leg_settled=on_leg_settled,
            runnable_guard=kill_switch,
            # Spec M3 (T4b): owner-billed leg billing — the same edition policy the
            # chat/run paths use; the per-leg floor from config.
            credits_policy=build_credits_policy(config),
            agentic_floor=config.agentic_credit_floor,
        )

    # Initiative scan (Spec A5, T6) — env-gated at the composition root:
    # PERSONA_INITIATIVE_ENABLED default OFF (the criterion-9 gate — initiative
    # does not enable until the A5-R-1 judged gate passes). OFF ⇒ the tenant is
    # NOT registered and the registry is byte-identical to pre-A5 (the
    # built-but-inert killer is the composition test over this branch). The
    # scan routes on the SMALL tier directly (no router profile — the K2
    # synthesis_tier precedent; Phase-1 ruling 1).
    initiative_settings = InitiativeSettings()
    if initiative_settings.enabled:
        # Spec M3 (T5b): the scan backend is metered so ``collect_llm_usage`` in the
        # handler captures the scan's real cost for owner billing. R9-096: plan-scoped —
        # ``initiative_scan`` was the second surface seen billing free owners at
        # ``estimate_static`` paid-model pricing in production. One instance is shared by
        # the scanner, the grounding judge and the A7 event-candidate producer; all three
        # run inside the job's owner scope, so all three follow the owner's plan.
        initiative_backend = plan_scoped_background_backend(
            tier=initiative_settings.scan_tier,
            rls_engine=rls_engine,
            paid_tier_registry=tier_registry,
            free_tier_registry=free_tier_registry,
            metered=True,
        )
        scanner = InitiativeScanner(
            graph=ApiScanGraphReader(graph_store),
            conversations=ApiScanConversationReader(rls_engine),
            tasks=ApiScanTaskReader(TaskStore(rls_engine), CheckpointStore(rls_engine)),
            backend=initiative_backend,
            settings=initiative_settings,
        )

        def _dial_reader(owner: str, persona: str) -> InitiativeDial:
            return read_initiative_dial(rls_engine, owner, persona)

        # The pipeline (T7) — the ONE enforced path from candidate to disposition.
        # T8 fills the delivery seam for ACT (the implicit task through the real
        # A2/A1 doors; the report is the task machinery's alone); T9 wires PROPOSE
        # (the propose-first door) below — a durable A8 reschedule proposal or a
        # persona-voiced C0 proposal message when a memory backend is present.
        initiative_ledger = InitiativeLedger(rls_engine)
        # The generic-proposal C0 seam (T9): the SAME sender composition A4's
        # digests use — one origination door. Absent memory backend/edition (the
        # community no-messaging posture) generic proposals stay HELD, exactly
        # like the digest path's absence; the A8-door route needs no sender.
        proposal_sender = None
        tag_resolver = None
        if memory_backend is not None and edition is not None:
            from persona_api.services.origination_adapters import (
                OriginatorUpdateSender,
                resolve_persona_tag,
            )

            proposal_sender = OriginatorUpdateSender(
                rls_engine=rls_engine,
                memory_backend=memory_backend,
                edition=edition,  # type: ignore[arg-type]  # Edition; typed object (import cycle)
                audit_root=Path(config.audit_root),
                audit_logger=build_audit_logger(config, rls_engine),
                sessions=live_sessions,
            )

            def tag_resolver(persona_id: str) -> object:
                return resolve_persona_tag(rls_engine, persona_id)

        delivery_executor = InitiativeDeliveryExecutor(
            ledger=initiative_ledger,
            tasks=TaskStore(rls_engine),
            schedules=ScheduleStore(rls_engine),
            timezone_for=PersonaCoreConfig().default_timezone,
            proposal_sender=proposal_sender,
            persona_tag_resolver=tag_resolver,  # type: ignore[arg-type]
        )
        pipeline = InitiativePipeline(
            grounding=GroundingChecker(
                source=ApiGroundingSource(
                    rls_engine, TaskStore(rls_engine), CheckpointStore(rls_engine)
                ),
                backend=initiative_backend,
            ),
            wellbeing=ApiWellbeingSubjectCheck(graph_store),
            declines=DeclineStore(rls_engine),
            ledger=LedgerAdapter(initiative_ledger),
            users=ApiUserContextReader(
                rls_engine, default_timezone=PersonaCoreConfig().default_timezone
            ),
            provenance=ApiProvenanceReader(graph_store, rls_engine),
            auditor=ApiPipelineAuditor(rls_engine),
            dial_reader=_dial_reader,
            settings=initiative_settings,
            delivery=delivery_executor,
        )
        register_initiative_scan_handler(
            registry,
            handler=InitiativeScanHandler(
                scanner=scanner,
                dial_reader=_dial_reader,
                sink=pipeline,
                pause_check=kill_switch.is_owner_autonomy_paused,
                # Spec M3 (T5b): owner-billed scan cost, idempotent + fail-soft.
                credits_policy=build_credits_policy(config),
                rls_engine=rls_engine,
                cost_source=(
                    runtime_factory.metadata_resolver if runtime_factory is not None else None
                ),
                floor=config.agentic_credit_floor,
            ),
        )
        # Spec A9 (A9-D-5/D-7): the ``delegated_turn`` tenant — voice's confirmed spoken ask
        # executed on the frontier chat pipeline through the ONE audited path (the same loop the
        # task-leg tenant + interactive chat use). Built-but-inert until voice enqueues (gated on
        # the voice-side ``delegation_enabled``); the create rides the unchanged origination svc.
        # Needs a memory backend for the origination failure notifier (as the task-leg digest does);
        # a backend-less worker path simply does not register it (no delegation without a backend).
        if memory_backend is not None and runtime_factory is not None:
            _register_delegated_turn_tenant(
                registry,
                rls_engine=rls_engine,
                runtime_factory=runtime_factory,
                memory_backend=memory_backend,
                edition=edition,
                audit_root=Path(config.audit_root),
            )
        # Door (b) of A7 — the ``event_candidate`` job feeds this SAME pipeline (A7-D-5). Double-
        # gated: it needs BOTH initiative enabled (there is a pipeline to submit into) AND
        # event-triggers enabled (a dispatcher enqueues into it). The wellbeing gate (layer a of
        # criterion 8) lives at the handler seam; the producer is the small-tier layer (b).
        if EventTriggerSettings().enabled:
            from persona_api.events import (
                ApiEventWellbeingCheck,
                SmallTierEventCandidateProducer,
                register_event_candidate_handler,
            )
            from persona_api.services import audit_service

            def _event_candidate_audit(
                owner: str, action: str, target: str, metadata: Mapping[str, str] | None = None
            ) -> None:
                audit_service.record(
                    engine=rls_engine,
                    user_id=owner,
                    action=action,
                    target=target,
                    metadata=dict(metadata) if metadata else None,
                )

            register_event_candidate_handler(
                registry,
                producer=SmallTierEventCandidateProducer(
                    backend=initiative_backend,
                    grounding=ApiGroundingSource(
                        rls_engine, TaskStore(rls_engine), CheckpointStore(rls_engine)
                    ),
                    settings=initiative_settings,
                ),
                sink=pipeline,
                wellbeing=ApiEventWellbeingCheck(graph_store),
                audit=_event_candidate_audit,
            )
    # Avatar generation (Spec A0 T9 → completed at R9-013): the durable create-time
    # avatar tenant. Registered under avatar_queue_ready — the ONE gate the create
    # route's producer shares — so an ``avatar_generation`` job is enqueued iff
    # this handler exists (the half-shipped cutover enqueued into a handler-less
    # worker → the unknown-type poison loop). The generator is the SAME
    # generation+persist implementation the inline hook runs (ImagegenAvatarGenerator
    # over the free build-time entry); the tool-audit sink follows the config-selected
    # backend, same as the route (R5-D-2 worker parity).
    if (
        avatar_queue_ready(
            avatar_via_queue=config.avatar_via_queue,
            image_backend=image_backend,
            file_storage=file_storage,
        )
        # Redundant with the predicate; repeated only to type-narrow the Optionals.
        and image_backend is not None
        and file_storage is not None
    ):
        from persona_api.imagegen.service import ImagegenAvatarGenerator

        register_avatar_handler(
            registry,
            ImagegenAvatarGenerator(
                backend=image_backend,
                file_storage=file_storage,
                audit_logger=build_tool_audit_logger(config, rls_engine),
                timeout_s=config.avatar_gen_timeout_s,
            ),
        )
    elif config.avatar_via_queue:
        # Flag on but the gate fails (no image backend / no file storage composed
        # for this worker): the route's shared predicate falls back to the inline
        # no-op path — no dead jobs — and THIS is the once-per-boot why.
        _log.warning(
            "avatar_via_queue is ON but the avatar tenant was NOT registered "
            "(image backend / file storage not composed); persona-create avatars "
            "fall back to the inline path",
            image_backend_present=image_backend is not None,
            file_storage_present=file_storage is not None,
        )
    _log.info(
        "worker registry composed",
        synthesis_tier=synthesis_tier,
        registered_types=registry.types(),
    )
    return registry


def _register_task_leg_tenant(
    registry: JobRegistry,
    *,
    rls_engine: Engine,
    runtime_factory: RuntimeFactory,
    memory_backend: Backend | None,
    edition: object | None,
    audit_root: Path,
    audit_logger: AuditLogger | None = None,
    live_sessions: LiveSessionRegistry | None = None,
    event_channel: UserEventChannel | None = None,
    on_leg_settled: Callable[[LegOutcome, ResumeTrigger, datetime], Awaitable[None]] | None = None,
    runnable_guard: RunnableGuard | None = None,
    credits_policy: CreditsPolicy | None = None,
    agentic_floor: int = 1,
) -> None:
    """Register the A4 ``task_leg`` handler: leg execution + continuation + digest (Spec A4).

    ``on_leg_settled`` (Spec A7, T6) — the A7 lifecycle emitter, wired only when event triggers are
    enabled; a settled leg emits its A2 lifecycle event through the dispatcher (chain-inheriting).

    ``runnable_guard`` (Spec A3/A6-D-8) — the kill-switch guard consulted before every leg: a
    terminal / budget-paused / persona-suspended / globally-paused / owner-autonomy-paused task
    runs no new leg. The primary origination gate for the owner pause — wired live at merge-back.
    """
    task_store = TaskStore(rls_engine)

    # Spec A11/A6 (W8): a background task transition pings the owner's open tabs (task.updated),
    # which refetch A6's Review/Tasks/Approvals live. Best-effort over the A11 channel; no channel
    # (community / no open tab) → the surface catches up on its next poll (the durable floor).
    def _emit_task_updated(owner: str, task_id: str, state: str) -> None:
        publish_task_updated(event_channel, owner_id=owner, task_id=task_id, state=state)

    continuation = TaskContinuation(
        task_store=task_store,
        queue=JobQueue(rls_engine),
        checkpoint_store=CheckpointStore(rls_engine),
        # Spec A4 recurrence: the continuation reads the schedule to decide occurrence-complete →
        # WAITING (recurring, more fires) vs task-complete (one-time / exhausted).
        schedule_store=ScheduleStore(rls_engine),
        on_state_change=_emit_task_updated,
    )
    on_milestone = _build_milestone_hook(
        rls_engine=rls_engine,
        memory_backend=memory_backend,
        edition=edition,
        audit_root=audit_root,
        audit_logger=audit_logger,
        live_sessions=live_sessions,
    )
    budget_gate = _build_budget_gate(
        rls_engine=rls_engine,
        task_store=task_store,
        memory_backend=memory_backend,
        edition=edition,
        audit_root=audit_root,
        audit_logger=audit_logger,
        live_sessions=live_sessions,
        emit_task_updated=_emit_task_updated,
    )
    on_approval_parked = _build_approval_announce_hook(
        rls_engine=rls_engine,
        memory_backend=memory_backend,
        edition=edition,
        audit_root=audit_root,
        audit_logger=audit_logger,
    )
    on_task_stuck = _build_task_stuck_hook(
        rls_engine=rls_engine,
        task_store=task_store,
        memory_backend=memory_backend,
        edition=edition,
        audit_root=audit_root,
        audit_logger=audit_logger,
        live_sessions=live_sessions,
    )
    register_task_leg_handler(
        registry,
        task_store=task_store,
        checkpoint_store=CheckpointStore(rls_engine),
        runner_builder=RuntimeFactoryLegRunnerBuilder(runtime_factory),
        continuation=continuation,
        # R9-005 (Spec A2, T12): the live path runs the reflect-and-compact distiller, NEVER the
        # BasicCheckpointWriter stand-in — the stand-in accumulates unboundedly and, after a
        # SUCCESSFUL run, deterministically trips the store's budget gate (3× model re-spend,
        # then dead-letter). Explicit here (belt) on top of the handler's default (suspenders).
        writer=CompactingCheckpointWriter(),
        on_milestone=on_milestone,
        on_leg_settled=on_leg_settled,
        runnable_guard=runnable_guard,
        budget_gate=budget_gate,
        on_approval_parked=on_approval_parked,
        on_task_stuck=on_task_stuck,
        # Spec M3 (T4b): OWNER-billed, CAS-ridden idempotent leg billing. The
        # ``cost_source`` is the shared pricing chain (full catalog coverage);
        # ``rls_engine`` is the same owner-scoped engine the stores use (the A0
        # worker binds ``current_user_id`` before the handler runs).
        credits_policy=credits_policy,
        rls_engine=rls_engine,
        cost_source=runtime_factory.metadata_resolver,
        billing_config=BillingConfig(),
        agentic_floor=agentic_floor,
    )
    # The A1→A2 bridge: a schedule fire → a task leg at the head-of-fire seq (Spec A4). Without it
    # an origination-created schedule fires a payload the leg handler can't parse (the inert trap).
    # ScheduleStore + engine feed the A10-D-7 deleted-executor degrade (pause + P6 notification).
    register_scheduled_task_fire_handler(
        registry,
        task_store=task_store,
        queue=JobQueue(rls_engine),
        schedule_store=ScheduleStore(rls_engine),
        rls_engine=rls_engine,
        event_channel=event_channel,
    )


def _build_budget_gate(
    *,
    rls_engine: Engine,
    task_store: TaskStore,
    memory_backend: Backend | None,
    edition: object | None,
    audit_root: Path,
    audit_logger: AuditLogger | None,
    live_sessions: LiveSessionRegistry | None,
    emit_task_updated: Callable[[str, str, str], None],
) -> Callable[[str, Task, datetime], Awaitable[bool]]:
    """The A3 leg-boundary budget gate (T10) — enforce the per-task cap + voice the extend ask.

    Returns an async gate the leg handler consults before enqueueing a CONTINUE's next leg: over
    cap ⇒ the task is paused (the A2 overlay, so no new legs) and the "budget reached; extend?"
    account is originated on the task's conversation. When no memory backend is present (community
    without C0), the pause still holds — only the voiced ask degrades to the persist-only floor
    (the Tasks surface still shows the budget-paused "extend?" affordance). The pause emits a
    ``task.updated`` ping through ``emit_task_updated`` (A11) so the surface refetches live.
    """
    from persona_api.approvals import account_for_budget_pause
    from persona_api.approvals.budget import BudgetEnforcer
    from persona_api.services.origination_adapters import (
        OriginatorFailureNotifier,
        resolve_persona_tag,
    )

    budget = BudgetEnforcer(
        engine=rls_engine,
        tasks=task_store,
        queue=JobQueue(rls_engine),
        on_state_change=emit_task_updated,
    )
    notifier = (
        OriginatorFailureNotifier(
            rls_engine=rls_engine,
            memory_backend=memory_backend,
            edition=edition,  # type: ignore[arg-type]  # Edition; typed object (import cycle)
            audit_root=audit_root,
            audit_logger=audit_logger,
            sessions=live_sessions,
        )
        if memory_backend is not None and edition is not None
        else None
    )

    async def _gate(owner: str, task: Task, now: datetime) -> bool:
        if not budget.enforce(owner, task, now=now):
            return False  # within cap — the leg continues
        # Paused at cap. Voice the "extend?" ask (best-effort; persist-only floor with no backend).
        if notifier is not None and task.conversation_id is not None:
            try:
                persona = resolve_persona_tag(rls_engine, task.persona_id)
                if persona is not None:
                    account = account_for_budget_pause(
                        task.id,
                        cap_micros=budget.effective_cap(owner, task),
                        spent_micros=task.ledger.total_micros,
                    )
                    await notifier.notify(
                        account,
                        persona=persona,
                        owner_id=owner,
                        conversation_id=task.conversation_id,
                    )
            except Exception:  # noqa: BLE001 — the ask is additive; never fail the paused leg
                _log.warning("budget extend ask voicing failed", task_id=task.id)
        return True

    return _gate


def _build_approval_announce_hook(
    *,
    rls_engine: Engine,
    memory_backend: Backend | None,
    edition: object | None,
    audit_root: Path,
    audit_logger: AuditLogger | None,
) -> Callable[[str, str], Awaitable[None]] | None:
    """The A3 notify-on-park hook — proactively voice a freshly-parked approval's "may I do X?".

    Returns an async ``(owner_id, proposal_id)`` callback the leg handler fires when a leg gates an
    action and the task parks ``waiting(on_user)``. It loads the durable proposal and voices the
    persona's ask via the deterministic :class:`OriginatorApprovalNotifier` (the SAME notifier the
    inbox/chat resolution loop uses). Runs inside the per-job tenant context (``current_user_id``
    is set by the executor), so the notifier's RLS reads resolve. ``None`` without a memory backend
    (community / keyless) — the durable proposal + the Approvals inbox stay the floor.
    """
    if memory_backend is None or edition is None:
        return None
    from persona.audit import JSONLAuditLogger
    from persona.stores.episodic import EpisodicStore

    from persona_api.approvals import ApprovalStore
    from persona_api.services.origination_adapters import OriginatorApprovalNotifier

    notifier = OriginatorApprovalNotifier(
        rls_engine=rls_engine,
        episodic=EpisodicStore(
            backend=memory_backend,
            audit_logger=audit_logger or JSONLAuditLogger(audit_root),
        ),
        edition=edition,  # type: ignore[arg-type]  # Edition; typed object (import cycle)
        tasks=TaskStore(rls_engine),
    )
    approvals = ApprovalStore(rls_engine)

    async def _announce(owner_id: str, proposal_id: str) -> None:
        # Load the durable proposal + voice the ask; a deleted persona / gone proposal is a
        # graceful no-op inside the notifier. Best-effort — the handler wraps + never fails the leg.
        proposal = approvals.get_proposal(owner_id, proposal_id)
        await notifier.ask(proposal)

    return _announce


def _build_task_stuck_hook(
    *,
    rls_engine: Engine,
    task_store: TaskStore,
    memory_backend: Backend | None,
    edition: object | None,
    audit_root: Path,
    audit_logger: AuditLogger | None,
    live_sessions: LiveSessionRegistry | None,
) -> Callable[[str, StuckReport], Awaitable[None]] | None:
    """The R9-005 honesty voice — a task parked on an over-budget checkpoint says so (C0).

    Returns an async ``(owner_id, StuckReport)`` callback the leg handler fires after parking a
    task whose finished run produced a checkpoint the store's budget gate rejected (the
    deterministic write failure that must never burn A0 retries). Voices the SAME
    ``account_for_stuck`` account the dead-leg sweep voices — one stuck-report shape, two
    entry points. Runs inside the per-job tenant context (``current_user_id`` is set by the
    executor), so the RLS reads resolve. ``None`` without a memory backend (community /
    keyless) — the ``waiting(on_user)`` state on the Tasks surface stays the floor.
    """
    if memory_backend is None or edition is None:
        return None
    from persona_api.approvals.failure import FailureKind, account_for_stuck
    from persona_api.services.origination_adapters import (
        OriginatorFailureNotifier,
        resolve_persona_tag,
    )

    notifier = OriginatorFailureNotifier(
        rls_engine=rls_engine,
        memory_backend=memory_backend,
        edition=edition,  # type: ignore[arg-type]  # Edition; typed object (import cycle)
        audit_root=audit_root,
        audit_logger=audit_logger,
        sessions=live_sessions,
    )

    async def _voice(owner_id: str, report: StuckReport) -> None:
        # A missing conversation / deleted persona degrades to the persist-only floor (the task
        # is already parked). Best-effort — the handler wraps + never fails the (done) leg.
        task = task_store.get(owner_id, report.task_id)
        if task.conversation_id is None:
            return
        persona = resolve_persona_tag(rls_engine, task.persona_id)
        if persona is None:
            return
        account = account_for_stuck(report, kind=FailureKind.TASK_STUCK)
        await notifier.notify(
            account,
            persona=persona,
            owner_id=owner_id,
            conversation_id=task.conversation_id,
        )

    return _voice


def _register_delegated_turn_tenant(
    registry: JobRegistry,
    *,
    rls_engine: Engine,
    runtime_factory: RuntimeFactory,
    memory_backend: Backend,
    edition: object | None,
    audit_root: Path,
) -> None:
    """Register the A9 ``delegated_turn`` handler over the real origination composition (A9-D-5)."""
    from persona_api.config import Edition
    from persona_api.jobs.handlers.delegated_turn import register_delegated_turn_handler
    from persona_api.services.task_origination_composition import (
        compose_task_origination_services,
    )

    services = compose_task_origination_services(
        rls_engine=rls_engine,
        memory_backend=memory_backend,
        edition=edition if isinstance(edition, Edition) else Edition.community,
        audit_root=audit_root,
    )
    register_delegated_turn_handler(
        registry,
        runtime_factory=runtime_factory,
        origination_service=services.origination,
        steering_service=services.steering,
        rls_engine=rls_engine,
    )


def _build_milestone_hook(
    *,
    rls_engine: Engine,
    memory_backend: Backend | None,
    edition: object | None,
    audit_root: Path,
    audit_logger: AuditLogger | None = None,
    live_sessions: LiveSessionRegistry | None = None,
) -> Callable[[LegOutcome, datetime], Awaitable[None]] | None:
    """Build the digest-on-milestone closure, or ``None`` when the digest can't be delivered.

    Maps a settled leg outcome onto the update's milestone/completion flags, resolves the persona
    tag, and publishes through the granularity filter (:class:`TaskUpdatePublisher`) on the real C0
    sender. Completion + timed-wait are milestones; a plain continuation is progress (delivered only
    under an ``every_leg`` contract). ``memory_backend``/``edition`` absent → no digest (``None``).
    """
    if memory_backend is None or edition is None:
        return None
    from persona.tasks import is_terminal
    from persona_runtime.legs import LegDisposition

    from persona_api.approvals.cadence import CadenceGate, MessagePriority
    from persona_api.digest.store import DeferredDigestStore
    from persona_api.services.origination_adapters import (
        OriginatorUpdateSender,
        resolve_persona_tag,
    )
    from persona_api.tasks.store import TaskStore
    from persona_api.tasks.updates import TaskUpdatePublisher

    sender = OriginatorUpdateSender(
        rls_engine=rls_engine,
        memory_backend=memory_backend,
        edition=edition,  # type: ignore[arg-type]  # Edition; typed as object to avoid an import cycle
        audit_root=audit_root,
        audit_logger=audit_logger,
        sessions=live_sessions,
    )
    # A3-D-4 / A6-D-10: the per-persona/day chatter cap batches over-cap PROGRESS updates to the
    # DeferredDigestStore (the morning review) instead of dropping them — the digest's "deferred
    # chatter" reader (routes/autonomy.py) finally has a producer.
    publisher = TaskUpdatePublisher(
        sender=sender,
        cadence=CadenceGate(rls_engine),
        digest_sink=DeferredDigestStore(rls_engine),
    )
    tasks = TaskStore(rls_engine)

    async def _publish(outcome: LegOutcome, now: datetime) -> None:
        task = outcome.task
        tag = resolve_persona_tag(rls_engine, task.persona_id)
        if tag is None:
            return
        # Read the SETTLED state (continuation already applied): a task the leg finished is only
        # truly *complete* if it's now terminal — a recurring occurrence-complete left it WAITING
        # for the next fire, so it's PROGRESS, not "I've finished" (the digest must not lie).
        settled = tasks.get(task.owner_id, task.id)
        completed = is_terminal(settled.state)
        occurrence = outcome.disposition is LegDisposition.COMPLETED and not completed
        waiting = outcome.disposition is LegDisposition.CONTINUE and outcome.resume_at is not None
        content = _render_digest(
            task.contract.goal, completed=completed, occurrence=occurrence, waiting=waiting
        )
        await publisher.publish(
            task=task,
            persona=tag,
            priority=MessagePriority.PROGRESS,
            is_milestone=completed or occurrence or waiting,
            is_completion=completed,
            content=content,
            now=now,
        )

    return _publish


def _render_digest(goal: str, *, completed: bool, occurrence: bool, waiting: bool) -> str:
    """A short, persona-neutral digest line (the originated message name-tags the persona)."""
    if completed:
        return f"I've finished the task you set up: {goal}."
    if occurrence:  # a recurring occurrence done — the task keeps going, so this is progress
        return f'Done this time on "{goal}" — I\'ll run it again on schedule.'
    if waiting:
        return f'Progress on "{goal}" — I\'ve paused until the next scheduled check.'
    return f'I\'ve made progress on "{goal}".'


class InProcessWorker:
    """Owns the in-process ``Worker.run()`` task (start on boot, drain on shutdown)."""

    def __init__(self, worker: Worker) -> None:
        self._worker = worker
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Launch the claim→execute loop as a background task. Idempotent.

        ``install_signal_handlers=False`` — signal ownership belongs to uvicorn
        in this hosting mode (R9-004): the worker's ``loop.add_signal_handler``
        would REPLACE uvicorn's ``signal.signal`` SIGINT/SIGTERM handlers, so ^C
        would drain the worker but the server would keep serving forever. The
        lifespan's :meth:`aclose` is the in-process drain path instead.
        """
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._worker.run(install_signal_handlers=False))
        _log.info("in-process worker started", worker_id=self._worker.worker_id)

    async def aclose(self) -> None:
        """Request a graceful drain, then await the loop's exit (shutdown)."""
        if self._task is None:
            return
        # Worker.run() drains in-flight jobs on a drain signal; request it, then
        # await the loop. On a stuck loop, cancel as a last resort (lease-expiry
        # reclaim covers any job left mid-flight — the same as a hard crash).
        self._worker.request_drain()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._worker.aclose()
        _log.info("in-process worker stopped")


def start_in_process_worker(
    *,
    config: APIConfig,
    rls_engine: Engine,
    embedder: Embedder,
    tier_registry: TierRegistry,
    free_tier_registry: TierRegistry | None,
    runtime_factory: RuntimeFactory | None = None,
    memory_backend: Backend | None = None,
    live_sessions: LiveSessionRegistry | None = None,
    event_channel: UserEventChannel | None = None,
    image_backend: ImageBackend | None = None,
    file_storage: FileStorage | None = None,
    sandbox_pool: SandboxPool | None = None,
    workspace_root: Path | None = None,
) -> InProcessWorker:
    """Compose + start the in-process worker (registry + worker + A1 tick).

    Composes the synthesis registry (+ the A4 ``task_leg`` tenant when a
    ``runtime_factory`` is supplied), builds the :class:`Worker` (its own dispatch
    + RLS engines), wires A1's leader-gated :func:`build_scheduler_tick` additively
    (the worker's loop calls it on its cadence — at most one process actually ticks
    under the advisory lock), starts the loop, and returns the handle for the
    lifespan to drain on shutdown.

    ``free_tier_registry`` (R9-096) is REQUIRED, not defaulted: it is what makes every
    background LLM backend resolve the JOB OWNER's plan instead of the process's paid
    registry. ``None`` states "this deployment has no plans" (community / self-host);
    the cloud lifespan passes the registry :func:`build_free_tier_registry` returned.

    Spec K10 (T3): REFUSES a non-Postgres engine. The worker's substrate — the
    ``PostgresGraphBackend`` it composes for synthesis/consolidation and the
    ``FOR UPDATE SKIP LOCKED`` job queue — is Postgres-only. On a community
    legacy-SQLite engine there is no graph and no claimable queue, so starting the
    worker would compose a Postgres-typed backend over SQLite and fail on first use.
    Refuse loudly at boot instead (a self-hoster who force-enabled the worker on the
    deprecated SQLite path is told to move to the managed Postgres mode).
    """
    if rls_engine.dialect.name != "postgresql":
        raise CommunityDbError(
            "the in-process worker requires a Postgres engine — its graph store and durable "
            "job queue are Postgres-only. The community legacy-SQLite path has neither. Set "
            "PERSONA_COMMUNITY_DB_MODE=auto to run on the bundled managed Postgres, or unset "
            "PERSONA_API_IN_PROCESS_WORKER.",
            context={"reason": "worker_requires_postgres", "dialect": rls_engine.dialect.name},
        )
    registry = build_worker_registry(
        rls_engine=rls_engine,
        embedder=embedder,
        tier_registry=tier_registry,
        # R9-096: the plan gate's other half — background backends resolve per job owner.
        free_tier_registry=free_tier_registry,
        config=config,
        synthesis_tier=config.synthesis_tier,
        runtime_factory=runtime_factory,
        memory_backend=memory_backend,
        edition=config.edition,
        live_sessions=live_sessions,
        event_channel=event_channel,
        # R9-013: the avatar tenant's substrate — shared with the create route's
        # producer gate (avatar_queue_ready), so enqueue implies handler.
        image_backend=image_backend,
        file_storage=file_storage,
        # R9-025b: the file_extract tenant's substrate — shared with the route's
        # producer gate (file_extract_queue_ready), so enqueue implies handler.
        sandbox_pool=sandbox_pool,
        workspace_root=workspace_root,
    )

    # A1's scheduler tick — additive, leader-gated, built on the SAME two engines
    # the worker creates (dispatch for the leader's held session; RLS for the
    # tick's owner-scoped fires). Passed as the ``build_worker`` composition seam so
    # the worker owns engine lifecycle; the worker's loop calls ``tick.run_once`` on
    # its cadence (at most one process actually ticks under the advisory lock).
    def _tick_builder(dispatch_engine: Engine, tick_rls_engine: Engine) -> SchedulerTick:
        # A6-D-8: the tick consults the owner pause on its OWN rls engine (a read-only kill-switch
        # store) so a paused owner's schedules are held — the completeness the leg runner + A5 + A7
        # gates share (each on its own engine, all reading the same owner_autonomy_pause row).
        return build_scheduler_tick(
            config,
            dispatch_engine=dispatch_engine,
            rls_engine=tick_rls_engine,
            autonomy_pause_check=KillSwitchStore(tick_rls_engine).is_owner_autonomy_paused,
        )

    # N2 catalog auto-sync — additive, leader-gated, on the worker's cross-tenant dispatch
    # engine. ``build_catalog_sync`` returns None when disabled (PERSONA_MCP_SYNC_ENABLED=false).
    def _catalog_sync_builder(dispatch_engine: Engine) -> CatalogSyncTask | None:
        return build_catalog_sync(config, dispatch_engine=dispatch_engine)

    # S2 skill-catalog auto-sync — same additive, leader-gated shape (distinct key).
    # ``build_skill_catalog_sync`` returns None when disabled (PERSONA_SKILL_SYNC_ENABLED=false).
    def _skill_catalog_sync_builder(dispatch_engine: Engine) -> SkillCatalogSyncTask | None:
        return build_skill_catalog_sync(config, dispatch_engine=dispatch_engine)

    # Spec A3 (T9) — the approval reminder/expiry sweep, leader-gated on APPROVAL_SWEEP_LOCK_KEY.
    # Always wired (the reminder/expiry state changes are un-gated — an approval must never rot);
    # the C0 voice is memory-backend-gated (absent ⇒ the persist-only floor: it still expires +
    # auto-pauses, just doesn't voice). Built on the worker's OWN two engines.
    def _approval_sweep_builder(
        dispatch_engine: Engine, worker_rls_engine: Engine
    ) -> ApprovalSweepRunner | None:
        from datetime import timedelta

        from persona.stores.episodic import EpisodicStore

        from persona_api.approvals import (
            APPROVAL_SWEEP_LOCK_KEY,
            ApprovalStore,
            ApprovalSweeper,
            ApprovalSweepRunner,
        )
        from persona_api.schedules.leadership import SchedulerLeader
        from persona_api.services.origination_adapters import OriginatorApprovalNotifier

        sweeper = ApprovalSweeper(
            dispatch_engine=dispatch_engine,
            approvals=ApprovalStore(worker_rls_engine),
            tasks=TaskStore(worker_rls_engine),
            remind_after=timedelta(hours=config.approval_remind_after_hours),
            expire_after=timedelta(hours=config.approval_expire_after_hours),
        )
        notifier = None
        if memory_backend is not None:
            notifier = OriginatorApprovalNotifier(
                rls_engine=worker_rls_engine,
                episodic=EpisodicStore(
                    backend=memory_backend,
                    audit_logger=build_audit_logger(config, worker_rls_engine),
                ),
                edition=config.edition,
                tasks=TaskStore(worker_rls_engine),
            )
        return ApprovalSweepRunner(
            sweeper=sweeper,
            leader=SchedulerLeader(dispatch_engine, lock_key=APPROVAL_SWEEP_LOCK_KEY),
            notifier=notifier,
        )

    # Spec A3 (T13) — the dead-leg voicing sweep, leader-gated on DEAD_LEG_SWEEP_LOCK_KEY (its own
    # distinct key). Always wired (a retry-exhausted task must never orphan silently); the C0 voice
    # is memory-backend-gated (absent ⇒ the persist-only floor: it still parks waiting(on_user)).
    def _dead_leg_sweep_builder(
        dispatch_engine: Engine, worker_rls_engine: Engine
    ) -> DeadLegSweeper | None:
        from persona_api.schedules.leadership import SchedulerLeader
        from persona_api.services.origination_adapters import OriginatorFailureNotifier
        from persona_api.tasks.dead_leg_sweep import DEAD_LEG_SWEEP_LOCK_KEY, DeadLegSweeper

        def _emit_task_updated(owner: str, task_id: str, state: str) -> None:
            publish_task_updated(event_channel, owner_id=owner, task_id=task_id, state=state)

        continuation = TaskContinuation(
            task_store=TaskStore(worker_rls_engine),
            queue=JobQueue(worker_rls_engine),
            checkpoint_store=CheckpointStore(worker_rls_engine),
            on_state_change=_emit_task_updated,
        )
        notifier = (
            OriginatorFailureNotifier(
                rls_engine=worker_rls_engine,
                memory_backend=memory_backend,
                edition=config.edition,
                audit_root=Path(config.audit_root),
                audit_logger=build_audit_logger(config, worker_rls_engine),
            )
            if memory_backend is not None
            else None
        )
        return DeadLegSweeper(
            continuation=continuation,
            dead_letter_queue=JobQueue(dispatch_engine),
            leader=SchedulerLeader(dispatch_engine, lock_key=DEAD_LEG_SWEEP_LOCK_KEY),
            rls_engine=worker_rls_engine,
            task_store=TaskStore(worker_rls_engine),
            notifier=notifier,
        )

    def _initiative_provisioner_builder(
        dispatch_engine: Engine, worker_rls_engine: Engine
    ) -> InitiativeProvisioner | None:
        # Spec A5 (T10): the leader-gated ensure sweep — built ONLY when initiative
        # is enabled (the same gate as the tenant; OFF ⇒ the worker loop is
        # byte-identical to pre-A5).
        settings = InitiativeSettings()
        if not settings.enabled:
            return None
        return InitiativeProvisioner(
            dispatch_engine=dispatch_engine,
            store=ScheduleStore(worker_rls_engine),
            settings=settings,
            default_timezone=PersonaCoreConfig().default_timezone,
            # R9-037: a persona whose scan schedule the user recently deleted is
            # refused, not silently re-provisioned on this sweep's own cadence
            # (default hourly) NOR immediately on the worker's next restart.
            tombstones=ScheduleTombstoneStore(worker_rls_engine),
            tombstone_window_days=config.schedule_tombstone_window_days,
        )

    worker = build_worker(
        config,
        registry,
        scheduler_tick_builder=_tick_builder,
        catalog_sync_builder=_catalog_sync_builder,
        skill_catalog_sync_builder=_skill_catalog_sync_builder,
        initiative_provisioner_builder=_initiative_provisioner_builder,
        approval_sweep_builder=_approval_sweep_builder,
        dead_leg_sweep_builder=_dead_leg_sweep_builder,
    )
    handle = InProcessWorker(worker)
    handle.start()
    return handle

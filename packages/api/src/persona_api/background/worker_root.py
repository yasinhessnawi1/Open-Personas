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
import time
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
from persona.tasks import LegBox
from persona_runtime.extraction.synthesizer import build_synthesizer
from persona_runtime.initiative import GroundingChecker, InitiativePipeline, InitiativeScanner
from persona_runtime.legs import CompactingCheckpointWriter, MilestoneRecorder
from persona_runtime.legs.acceptance import AcceptanceAssessor
from persona_runtime.legs.semantic_distiller import SemanticCheckpointWriter
from persona_runtime.routing import tier_for

from persona_api.approvals import ApprovalStore
from persona_api.approvals.kill_switch import KillSwitchStore
from persona_api.db.audit_factory import build_audit_logger, build_tool_audit_logger
from persona_api.editions.factory import build_credits_policy, build_stripe_gateway
from persona_api.errors import CommunityDbError
from persona_api.initiative.delivery import InitiativeDeliveryExecutor
from persona_api.initiative.handler import (
    InitiativeScanHandler,
    read_initiative_dial,
    register_initiative_scan_handler,
)
from persona_api.initiative.ignored_sweep import IgnoredProposalSweeper
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
from persona_api.tasks.drain import LegDrainSignal
from persona_api.tasks.handler import (
    DEFAULT_RECENT_LEG_SUMMARIES,
    RunnableGuard,
    register_task_leg_handler,
)
from persona_api.tasks.leg_retrieval import LegRetrieval
from persona_api.tasks.leg_runner import RuntimeFactoryLegRunnerBuilder
from persona_api.tasks.scheduled_fire import register_scheduled_task_fire_handler
from persona_api.tasks.store import CheckpointStore, TaskStore

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping
    from datetime import datetime

    from persona.audit import AuditLogger
    from persona.backends import ChatBackend
    from persona.imagegen.protocol import ImageBackend
    from persona.stores.backend import Backend
    from persona.stores.embedder import Embedder
    from persona.tasks import ResumeTrigger, StuckReport, Task
    from persona_runtime.legs import CheckpointWriter, LegOutcome
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
    from persona_api.services.origination_delivery import ChannelDeliverers
    from persona_api.services.runtime_factory import RuntimeFactory
    from persona_api.services.title_backfill import UntitledConversationBackfill
    from persona_api.services.web_deliverer import LiveSessionRegistry
    from persona_api.storage import FileStorage
    from persona_api.tasks.dead_leg_sweep import DeadLegSweeper
    from persona_api.tasks.revival_sweep import RevivalSweeper

__all__ = [
    "InProcessWorker",
    "build_leg_box",
    "build_worker_registry",
    "start_in_process_worker",
]

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
    channels: ChannelDeliverers | None = None,
    event_channel: UserEventChannel | None = None,
    drain: LegDrainSignal | None = None,
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
    # the paid registry. Metered (Spec M3, T5): the handler now has its owner-billing seam.
    # Until 2026-09-19 this read "Unmetered: K2 synthesis has no owner-billing seam ... so
    # wrapping it would meter nothing", while the handler's own meter called itself
    # "attribution-not-deduct ... a refinement once the extractor surfaces per-call usage".
    # Each half pointed at the other as the reason it could not be done, and the extractor
    # ran on the owner's behalf for free.
    backend = plan_scoped_background_backend(
        tier=synthesis_tier,
        rls_engine=rls_engine,
        paid_tier_registry=tier_registry,
        free_tier_registry=free_tier_registry,
        metered=True,
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
        # Spec M3 (T5): owner-billed extractor cost, idempotent + fail-soft.
        credits_policy=build_credits_policy(config),
        rls_engine=rls_engine,
        cost_source=(runtime_factory.metadata_resolver if runtime_factory is not None else None),
        floor=config.agentic_credit_floor,
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
        # Spec M3 (T5): metered so the refresh's model call records its usage. It was
        # ``False``, which is half of why this surface was billed to nobody: even had the
        # handler asked to bill, there was no usage to bill from.
        metered=True,
    )
    register_title_refresh_handler(
        registry,
        generator=build_title_refresh_generator(title_backend),
        event_channel=event_channel,
        # Spec M3 (T5): owner-billed title model call, idempotent + fail-soft.
        credits_policy=build_credits_policy(config),
        rls_engine=rls_engine,
        cost_source=(runtime_factory.metadata_resolver if runtime_factory is not None else None),
        floor=config.agentic_credit_floor,
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
            # Spec M3 (T5): metered so the extraction's model call records its usage.
            # It was ``False``, which is half of why this surface was billed to nobody:
            # even had the handler asked to bill, there was no usage to bill from and
            # every extraction would have cost the one-credit floor regardless of the
            # real price.
            metered=True,
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
            # Spec M3 (T5): owner-billed extraction model call AND sandbox render,
            # idempotent + fail-soft.
            credits_policy=build_credits_policy(config),
            rls_engine=rls_engine,
            cost_source=(
                runtime_factory.metadata_resolver if runtime_factory is not None else None
            ),
            floor=config.agentic_credit_floor,
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
            drain=drain,
            rls_engine=rls_engine,
            runtime_factory=runtime_factory,
            memory_backend=memory_backend,
            edition=edition,
            # R5-D-2 worker parity: the origination audit sink follows the same
            # config-selected backend; audit_root stays the JSONL fallback path.
            audit_root=Path(config.audit_root),
            # Findings F+K (completion sweep, part 2): the leg bounds + the checkpoint token
            # budget are read off the config here, where every other leg knob already is.
            config=config,
            audit_logger=build_audit_logger(config, rls_engine),
            live_sessions=live_sessions,
            channels=channels,
            event_channel=event_channel,
            on_leg_settled=on_leg_settled,
            runnable_guard=kill_switch,
            # Spec M3 (T4b): owner-billed leg billing — the same edition policy the
            # chat/run paths use; the per-leg floor from config.
            credits_policy=build_credits_policy(config),
            agentic_floor=config.agentic_credit_floor,
            # Spec W1 (T12): the continuity window, from the knob .env.example has
            # documented since A2 and nothing read until now.
            recent_leg_summaries=config.task_recent_leg_summaries,
            # Spec W1 (T14, D-W1-18): the distiller rides its own flag, default OFF. The
            # backend resolves per job inside the owner scope (R9-096), never once here.
            writer=_checkpoint_writer(
                config,
                rls_engine=rls_engine,
                tier_registry=tier_registry,
                free_tier_registry=free_tier_registry,
            ),
            # R9-164: the acceptance assessor, on the same per-job owner-scoped backend
            # resolution the distiller uses. Separate from the writer on purpose: a bad
            # summary is a poor next leg, a bad acceptance claim tells the user their work
            # is finished when it is not, so it gets its own flag and its own gate.
            acceptance=_acceptance_assessor(
                config,
                rls_engine=rls_engine,
                tier_registry=tier_registry,
                free_tier_registry=free_tier_registry,
            ),
        )

    # Initiative scan (Spec A5, T6) — env-gated at the composition root:
    # PERSONA_INITIATIVE_ENABLED default OFF (the criterion-9 gate — initiative
    # does not enable until the A5-R-1 judged gate passes). OFF ⇒ the tenant is
    # NOT registered and the registry is byte-identical to pre-A5 (the
    # built-but-inert killer is the composition test over this branch). The
    # scan routes on the SMALL tier directly (no router profile — the K2
    # synthesis_tier precedent; Phase-1 ruling 1).
    # Spec M5 (B5, D-M5-24) — the auto-top-up tenant. Voice observes a balance crossing
    # and enqueues a durable trigger; this worker holds the Stripe gateway, so the charge
    # decision runs HERE (D-M5-15). Registered ONLY when billing is active:
    # ``build_stripe_gateway`` returns None on community / flag-off, so the tenant is
    # simply absent, the ``stripe`` SDK is never imported, and the registry stays
    # byte-identical to a pre-M5 worker. Same dependency-conditional shape the
    # delegated-turn + avatar tenants use.
    #
    # At FUNCTION scope on purpose: this block first landed inside the
    # ``initiative_settings.enabled`` branch, which is a different feature's flag and
    # defaults OFF — so a correctly-configured billing deployment with initiative off
    # would have registered no tenant, and every voice top-up would have dead-lettered.
    # Billing must be gated by billing alone.
    _stripe_gateway = build_stripe_gateway(config)
    if _stripe_gateway is not None:
        from persona_api.jobs.handlers.auto_topup import (  # noqa: PLC0415 — active path only
            register_auto_topup_handler,
        )

        register_auto_topup_handler(registry, rls_engine=rls_engine, gateway=_stripe_gateway)

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
                channels=channels,
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
                # R9-183: flush-on-next-scan (Phase-1 ruling 4), the daily fire is what
                # releases (or expires) the owner's held batch. Same object as the sink;
                # a distinct seam because A7's event door gets the sink and not this.
                held_batch=pipeline,
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
    # Avatar generation (Spec A0 T9 → completed at R9-013, default path since the
    # cutover-flag retirement): the durable avatar tenant, create and regenerate.
    # Registered whenever this worker CAN generate (image backend + file storage
    # composed); the create/regenerate routes enqueue iff the started worker's
    # registered types include it (``avatar_queue_available``), so "enqueue implies
    # handler" is read off the registry that runs the job rather than re-derived
    # (the half-shipped cutover enqueued into a handler-less worker → the
    # unknown-type poison loop). The generator is the SAME generation + persist +
    # owner-billing implementation the inline hook runs (ImagegenAvatarGenerator
    # over the build-time entry and the M3 ``bill_avatar_owner`` seam); the
    # tool-audit sink follows the config-selected backend, same as the route
    # (R5-D-2 worker parity).
    if (
        avatar_queue_ready(image_backend=image_backend, file_storage=file_storage)
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
                credits_policy=build_credits_policy(config),
                rls_engine=rls_engine,
                cost_source=(
                    runtime_factory.metadata_resolver if runtime_factory is not None else None
                ),
                image_credit_floor=config.image_credit_floor,
            ),
        )
    else:
        # No image backend / no file storage composed for this worker: the routes
        # see no avatar handler and keep the inline path (which no-ops fail-soft
        # and audits). Stated once per boot so a missing avatar has a named why.
        _log.info(
            "avatar tenant not registered (image backend / file storage not composed); "
            "persona avatars stay on the in-request path",
            image_backend_present=image_backend is not None,
            file_storage_present=file_storage is not None,
        )
    _log.info(
        "worker registry composed",
        synthesis_tier=synthesis_tier,
        registered_types=registry.types(),
    )
    return registry


def _checkpoint_writer(
    config: APIConfig,
    *,
    rls_engine: Engine,
    tier_registry: TierRegistry,
    free_tier_registry: TierRegistry | None,
) -> CheckpointWriter:
    """The leg's checkpoint writer, chosen by the flag (Spec W1, T14; D-W1-17, D-W1-18).

    Off (the default) is the deterministic writer, which is what every leg has used in
    production. On is the semantic distiller over the configured tier, with that same
    deterministic writer as its fallback, so the flag can only ever ADD an attempt to think:
    every failure path inside the distiller ends in the floor.

    The backend is resolved per write through ``plan_scoped_background_backend``, inside the
    owner scope the worker binds per job (R9-096), never once at composition time.
    """
    floor = CompactingCheckpointWriter()
    if not config.task_semantic_distiller_enabled:
        return floor

    def _backend() -> ChatBackend:
        return plan_scoped_background_backend(
            tier=config.task_semantic_distiller_tier,
            rls_engine=rls_engine,
            paid_tier_registry=tier_registry,
            free_tier_registry=free_tier_registry,
            # Metered (D-W1-44): the call happens because this leg ran, so it is billed
            # with the leg. The wrapper records into the handler's ``collect_llm_usage``
            # sink, which folds the totals into the leg's own accumulator, so it rides the
            # SAME per-leg deduct rather than becoming a second charge.
            metered=True,
        )

    from datetime import timedelta

    return SemanticCheckpointWriter(
        backend_provider=_backend,
        fallback=floor,
        timeout_s=config.task_semantic_distiller_timeout_seconds,
        max_idle=timedelta(days=config.task_leg_max_idle_days),
    )


def _acceptance_assessor(
    config: APIConfig,
    *,
    rls_engine: Engine,
    tier_registry: TierRegistry,
    free_tier_registry: TierRegistry | None,
) -> AcceptanceAssessor | None:
    """The leg's acceptance assessor, or ``None`` when the operator turned it off (R9-164).

    ``None`` is the pre-R9-164 behaviour exactly: criteria stay pending forever. On, a leg's
    work is read once on the small tier and its claims go through the core gate, which is
    where the safety lives — not here.
    """
    if not config.task_acceptance_assessor_enabled:
        return None

    def _backend() -> ChatBackend:
        return plan_scoped_background_backend(
            tier=config.task_acceptance_assessor_tier,
            rls_engine=rls_engine,
            paid_tier_registry=tier_registry,
            free_tier_registry=free_tier_registry,
            # Metered: the call happens because this leg ran, so it rides the leg's own
            # per-leg deduct rather than becoming a charge of its own (D-W1-44's rule).
            metered=True,
        )

    return AcceptanceAssessor(
        backend_provider=_backend,
        timeout_s=config.task_acceptance_assessor_timeout_seconds,
    )


def build_leg_box(config: APIConfig) -> LegBox:
    """The per-leg bounds an operator asked for (completion sweep part 2, Finding F).

    The ONE place a production ``LegBox`` is built, so no second construction site can drift
    from it and so a test can cross each bound through the same function the worker calls.
    That matters more than it looks: the bounds were dark precisely because every production
    path fell through to ``LegBox()`` with no arguments, and every test of the mechanism
    passed throughout.

    ``budget_micros`` is a ceiling rather than a grant. The handler still narrows it to the
    task's remaining budget when that is smaller (:meth:`TaskLegHandler._leg_box`).

    Args:
        config: The process configuration carrying the three ``PERSONA_TASK_LEG_*`` knobs.

    Returns:
        The box every leg this worker runs is bounded by.
    """
    return LegBox(
        max_steps=config.task_leg_max_steps,
        wall_clock_seconds=config.task_leg_wallclock_seconds,
        budget_micros=config.task_leg_budget_micros,
    )


def _register_task_leg_tenant(
    registry: JobRegistry,
    *,
    rls_engine: Engine,
    runtime_factory: RuntimeFactory,
    memory_backend: Backend | None,
    edition: object | None,
    audit_root: Path,
    config: APIConfig,
    audit_logger: AuditLogger | None = None,
    live_sessions: LiveSessionRegistry | None = None,
    channels: ChannelDeliverers | None = None,
    event_channel: UserEventChannel | None = None,
    on_leg_settled: Callable[[LegOutcome, ResumeTrigger, datetime], Awaitable[None]] | None = None,
    runnable_guard: RunnableGuard | None = None,
    credits_policy: CreditsPolicy | None = None,
    agentic_floor: int = 1,
    recent_leg_summaries: int = DEFAULT_RECENT_LEG_SUMMARIES,
    writer: CheckpointWriter | None = None,
    acceptance: AcceptanceAssessor | None = None,
    drain: LegDrainSignal | None = None,
) -> None:
    """Register the A4 ``task_leg`` handler: leg execution + continuation + digest (Spec A4).

    ``on_leg_settled`` (Spec A7, T6) — the A7 lifecycle emitter, wired only when event triggers are
    enabled; a settled leg emits its A2 lifecycle event through the dispatcher (chain-inheriting).

    ``runnable_guard`` (Spec A3/A6-D-8) — the kill-switch guard consulted before every leg: a
    terminal / budget-paused / persona-suspended / globally-paused / owner-autonomy-paused task
    runs no new leg. The primary origination gate for the owner pause — wired live at merge-back.

    ``config`` (completion sweep part 2, Findings F+K) — the per-leg bounds and the checkpoint
    token budget. .env.example documented all four as "injected by the worker composition" since
    Spec A2 and this is the composition that finally does it, so an operator can bound a leg.
    """
    task_store = TaskStore(rls_engine)
    # Findings F+K: every checkpoint store on the leg path enforces the CONFIGURED core budget.
    # The continuation's is the one that actually writes (``CheckpointStore.append`` runs the
    # gate); the handler's reads. Both take the number so the two can never disagree about
    # what a leg is allowed to carry forward.
    checkpoint_token_budget = config.task_checkpoint_token_budget

    # Spec A2 (T10, D-A2-4): the task's own memory. The recorder writes into the SAME episodic
    # store the persona reads from in chat and the same one a leg's retrieval queries
    # (``runtime_factory.build_task_episodic_store`` composes both), so a milestone lands
    # where the persona will actually look for it. One instance for every leg this worker
    # runs: the store takes the persona per call and the worker binds the job owner's RLS
    # scope before the handler runs, exactly as the shared graph store relies on.
    #
    # Wired at BOTH emitters: the handler records what a leg achieved, the continuation
    # records the wait, and they share one recorder so a task's memory has one voice.
    milestones = MilestoneRecorder(runtime_factory.build_task_episodic_store())

    # Spec A11/A6 (W8): a background task transition pings the owner's open tabs (task.updated),
    # which refetch A6's Review/Tasks/Approvals live. Best-effort over the A11 channel; no channel
    # (community / no open tab) → the surface catches up on its next poll (the durable floor).
    def _emit_task_updated(owner: str, task_id: str, state: str) -> None:
        publish_task_updated(event_channel, owner_id=owner, task_id=task_id, state=state)

    continuation = TaskContinuation(
        task_store=task_store,
        queue=JobQueue(rls_engine),
        checkpoint_store=CheckpointStore(rls_engine, token_budget=checkpoint_token_budget),
        # Spec A4 recurrence: the continuation reads the schedule to decide occurrence-complete →
        # WAITING (recurring, more fires) vs task-complete (one-time / exhausted).
        schedule_store=ScheduleStore(rls_engine),
        on_state_change=_emit_task_updated,
        milestones=milestones,
    )
    on_milestone = _build_milestone_hook(
        rls_engine=rls_engine,
        memory_backend=memory_backend,
        edition=edition,
        audit_root=audit_root,
        audit_logger=audit_logger,
        live_sessions=live_sessions,
        channels=channels,
    )
    budget_gate, leg_budget_micros = _build_budget_gate(
        rls_engine=rls_engine,
        task_store=task_store,
        memory_backend=memory_backend,
        edition=edition,
        audit_root=audit_root,
        audit_logger=audit_logger,
        live_sessions=live_sessions,
        channels=channels,
        emit_task_updated=_emit_task_updated,
    )
    on_approval_parked = _build_approval_announce_hook(
        rls_engine=rls_engine,
        memory_backend=memory_backend,
        edition=edition,
        audit_root=audit_root,
        audit_logger=audit_logger,
        channels=channels,
    )
    on_task_stuck = _build_task_stuck_hook(
        rls_engine=rls_engine,
        task_store=task_store,
        memory_backend=memory_backend,
        edition=edition,
        audit_root=audit_root,
        audit_logger=audit_logger,
        live_sessions=live_sessions,
        channels=channels,
    )
    register_task_leg_handler(
        registry,
        # R9-129: the deploy drain reaches a leg that is already running, so a redeploy
        # checkpoints it at its next step boundary instead of killing it mid-step and
        # paying for the whole leg again when the lease expires and it is reclaimed.
        drain=drain,
        task_store=task_store,
        checkpoint_store=CheckpointStore(rls_engine, token_budget=checkpoint_token_budget),
        # Finding F (completion sweep, part 2): the per-leg bounds an operator configured.
        # Until now both construction sites built ``LegBox()`` with no arguments, so the
        # four documented knobs bounded nothing and the shipped numbers were the only
        # numbers. The budget here is a ceiling; the handler still narrows it to the task's
        # remaining budget when that is smaller.
        box=build_leg_box(config),
        # Spec W1 (D-W1-1): the leg runner builds the loop over A3's policy-gated toolbox,
        # recording gated proposals in the ApprovalStore. Until W1 no production module
        # constructed the gate, so every leg ran ungated; this is the wiring A3 named.
        runner_builder=RuntimeFactoryLegRunnerBuilder(
            runtime_factory, recorder=ApprovalStore(rls_engine)
        ),
        continuation=continuation,
        # R9-005 (Spec A2, T12): the live path runs the reflect-and-compact distiller, NEVER the
        # BasicCheckpointWriter stand-in — the stand-in accumulates unboundedly and, after a
        # SUCCESSFUL run, deterministically trips the store's budget gate (3× model re-spend,
        # then dead-letter). Explicit here (belt) on top of the handler's default (suspenders).
        # Spec W1 (T14): the semantic distiller when the flag is on, the deterministic
        # writer otherwise. The choice is made here, once, so no other caller can end up
        # with a writer the operator did not ask for.
        writer=writer if writer is not None else CompactingCheckpointWriter(),
        # Spec W1 (T12): the two reconstruction slots A2 left empty. The window comes from
        # the checkpoint store; the memory comes from the same recall the chat loop uses,
        # fetched off-loop and skipped on timeout so a slow recall never delays a leg.
        recent_leg_summaries=recent_leg_summaries,
        retrieval=LegRetrieval(recall_for=runtime_factory.build_task_recall),
        # R9-164: the acceptance assessor. ``None`` (the flag off, or no tier) leaves every
        # criterion pending exactly as before; wired, a leg's work can tick the checklist the
        # user agreed, through the core gate that refuses an unevidenced claim.
        acceptance=acceptance,
        on_milestone=on_milestone,
        # Spec A2 (T10): the EPISODIC half of a milestone. ``on_milestone`` above delivers the
        # digest the user reads; this is what the persona itself remembers afterwards. Two
        # different audiences, so two hooks, off one gate.
        milestones=milestones,
        on_leg_settled=on_leg_settled,
        runnable_guard=runnable_guard,
        budget_gate=budget_gate,
        leg_budget_micros=leg_budget_micros,
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
    channels: ChannelDeliverers | None,
    emit_task_updated: Callable[[str, str, str], None],
) -> tuple[Callable[[str, Task, datetime], Awaitable[bool]], Callable[[str, Task], int]]:
    """The A3 leg-boundary budget gate (T10) — enforce the per-task cap + voice the extend ask.

    Returns an async gate the leg handler consults before enqueueing a CONTINUE's next leg: over
    cap ⇒ the task is paused (the A2 overlay, so no new legs) and the "budget reached; extend?"
    account is originated on the task's conversation. When no memory backend is present (community
    without C0), the pause still holds — only the voiced ask degrades to the persist-only floor
    (the Tasks surface still shows the budget-paused "extend?" affordance). The pause emits a
    ``task.updated`` ping through ``emit_task_updated`` (A11) so the surface refetches live.

    Returns the gate AND a reader for the task's REMAINING budget, because the same enforcer
    answers both and only it knows the effective cap (the contract's bound plus every granted
    extension, which lives in audit rows rather than on the task). The remaining figure is what
    finally gives the per-leg spend box a number to hold: it had never fired, because both
    ``LegBox`` construction sites passed no budget at all (R9-176).
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
            channels=channels,
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

    def _remaining(owner: str, task: Task) -> int:
        """What this task may still spend, in ledger micros; never negative."""
        return max(0, budget.effective_cap(owner, task) - task.ledger.total_micros)

    return _gate, _remaining


def _build_approval_announce_hook(
    *,
    rls_engine: Engine,
    memory_backend: Backend | None,
    edition: object | None,
    audit_root: Path,
    audit_logger: AuditLogger | None,
    channels: ChannelDeliverers | None,
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

    from persona_api.approvals import ApprovalStore, announce_parked_proposal
    from persona_api.services.origination_adapters import OriginatorApprovalNotifier

    notifier = OriginatorApprovalNotifier(
        rls_engine=rls_engine,
        episodic=EpisodicStore(
            backend=memory_backend,
            audit_logger=audit_logger or JSONLAuditLogger(audit_root),
        ),
        edition=edition,  # type: ignore[arg-type]  # Edition; typed object (import cycle)
        tasks=TaskStore(rls_engine),
        channels=channels,
    )
    approvals = ApprovalStore(rls_engine)

    async def _announce(owner_id: str, proposal_id: str) -> None:
        # Load the durable proposal + voice the ask; a deleted persona / gone proposal is a
        # graceful no-op inside the notifier. Best-effort — the handler wraps + never fails the leg.
        #
        # part1 F6: this was a near-copy of ``ApprovalResolver.announce`` that dropped its
        # ``PENDING`` guard, so a re-delivered leg job could ask "may I do X?" about something
        # the user had already answered. The shared function carries the guard; the sweep had
        # this recorded as an unwired feature, and it was really a wired duplicate of one.
        await announce_parked_proposal(
            approvals, notifier, owner_id=owner_id, proposal_id=proposal_id
        )

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
    channels: ChannelDeliverers | None,
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
        channels=channels,
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
    channels: ChannelDeliverers | None = None,
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
        channels=channels,
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
        return f'Done this time on "{goal}". I\'ll run it again on schedule.'
    if waiting:
        return f'Progress on "{goal}". I\'ve paused until the next scheduled check.'
    return f'I\'ve made progress on "{goal}".'


# R9-093: a crashed worker loop is RESTARTED, not left dead. Mirrors the connector
# runners' supervision (R9-073c) deliberately — same failure class, so the same
# constants and the same shape, rather than a second dialect of "heal a dead loop".
_WORKER_RESTART_BACKOFF_INITIAL_SECONDS = 1.0
_WORKER_RESTART_BACKOFF_MAX_SECONDS = 60.0
#: A loop that stayed up at least this long before crashing was genuinely healthy;
#: its fault resets the failure count + backoff rather than counting toward the ceiling.
_WORKER_RESTART_HEALTHY_UPTIME_SECONDS = 120.0
#: After this many crashes IN QUICK SUCCESSION the loop is left down — a persistently
#: broken worker must not hot-spin forever — but it IS given a real chance first.
_WORKER_RESTART_MAX_CONSECUTIVE_FAILURES = 10


class InProcessWorker:
    """Owns the in-process ``Worker.run()`` task (start on boot, drain on shutdown)."""

    def __init__(self, worker: Worker, *, job_types: frozenset[str] = frozenset()) -> None:
        self._worker = worker
        self._job_types = job_types
        self._task: asyncio.Task[None] | None = None

    @property
    def job_types(self) -> frozenset[str]:
        """The job types this process's worker will consume (its composed registry).

        The producer side of "enqueue implies handler": a route enqueues a job
        type iff it is in here (``avatar_queue_available``), so a job is never
        written for a handler this process does not carry.
        """
        return self._job_types

    @property
    def last_beat_at(self) -> datetime | None:
        """When the wrapped loop last completed an iteration (R9-093 observability).

        Read by :func:`~persona_api.routes.health.healthz`. Supervision restarts a
        CRASHED loop; a frozen beat is how the OTHER shape — alive but wedged on a
        hung await, where nothing ever raises — becomes visible instead of silent.
        """
        return self._worker.last_beat_at

    def start(self) -> None:
        """Launch the supervised claim→execute loop as a background task. Idempotent.

        ``install_signal_handlers=False`` — signal ownership belongs to uvicorn
        in this hosting mode (R9-004): the worker's ``loop.add_signal_handler``
        would REPLACE uvicorn's ``signal.signal`` SIGINT/SIGTERM handlers, so ^C
        would drain the worker but the server would keep serving forever. The
        lifespan's :meth:`aclose` is the in-process drain path instead.

        The loop runs under :meth:`_supervise` (R9-093). Previously this was a bare
        ``asyncio.create_task(self._worker.run(...))`` with no done-callback and
        nothing awaiting it until shutdown, so a raise inside ``run()`` killed the
        task and the exception sat UNRETRIEVED in the task object: no log, no
        health-check change, and every background surface — schedules, synthesis,
        consolidation — stopped at once. Production showed a 65-minute window in
        which no job of any type was created or processed, spanning a scheduled
        fire that never happened, with ``/livez`` green throughout; the exception
        would only have surfaced at ``aclose()``, i.e. at shutdown, hours later.
        """
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._supervise())
        _log.info("in-process worker started", worker_id=self._worker.worker_id)

    async def _supervise(self) -> None:
        """Run the loop; restart a CRASH with backoff, honour a graceful exit.

        A normal return means the loop drained and is done — it must NOT be
        respawned, or ``aclose()`` could never complete. ``CancelledError``
        propagates untouched (that is shutdown, not a fault).
        """
        backoff = _WORKER_RESTART_BACKOFF_INITIAL_SECONDS
        failures = 0
        while True:
            started = time.monotonic()
            try:
                await self._worker.run(install_signal_handlers=False)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — the whole point is to survive it
                uptime = time.monotonic() - started
                if uptime >= _WORKER_RESTART_HEALTHY_UPTIME_SECONDS:
                    # It had proved itself healthy; treat this as a fresh first fault.
                    backoff = _WORKER_RESTART_BACKOFF_INITIAL_SECONDS
                    failures = 0
                failures += 1
                if failures >= _WORKER_RESTART_MAX_CONSECUTIVE_FAILURES:
                    _log.error(
                        "in-process worker crashed {count} times in a row ({error}) — "
                        "leaving it DOWN; background jobs are stopped until restart",
                        count=failures,
                        error=str(exc),
                        worker_id=self._worker.worker_id,
                    )
                    return
                _log.exception(
                    "in-process worker loop crashed ({error}) — restarting in {delay}s "
                    "[failure {count}]",
                    error=str(exc),
                    delay=backoff,
                    count=failures,
                    worker_id=self._worker.worker_id,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _WORKER_RESTART_BACKOFF_MAX_SECONDS)
                continue
            # Graceful exit (drain requested) — done, never respawn.
            return

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
    channels: ChannelDeliverers | None = None,
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
            "the in-process worker requires a Postgres engine: its graph store and durable "
            "job queue are Postgres-only. The community legacy-SQLite path has neither. Set "
            "PERSONA_COMMUNITY_DB_MODE=auto to run on the bundled managed Postgres, or unset "
            "PERSONA_API_IN_PROCESS_WORKER.",
            context={"reason": "worker_requires_postgres", "dialect": rls_engine.dialect.name},
        )
    # R9-129: ONE signal per worker process, held by the leg handler (which takes a token
    # per running leg) and tripped by the worker's own drain. Built here because this is
    # where the registry and the worker meet; neither half can reach the other.
    leg_drain = LegDrainSignal()
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
        channels=channels,
        event_channel=event_channel,
        drain=leg_drain,
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
    # R9-165: the post-sync skill summariser rides the app's background tier as ownerless
    # system work (no owner to plan-gate or bill) and shares the runtime factory's cache
    # when one is present, so a summary made after a sync serves the very next turn.
    def _skill_catalog_sync_builder(dispatch_engine: Engine) -> SkillCatalogSyncTask | None:
        from persona_api.services.model_tiers import ownerless_background_backend

        return build_skill_catalog_sync(
            config,
            dispatch_engine=dispatch_engine,
            summary_backend=ownerless_background_backend(
                tier_registry, surface="skill mirror summariser"
            ),
            summary_cache=(
                runtime_factory.skill_summaries if runtime_factory is not None else None
            ),
        )

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

    # Spec W1 (T8) — the revival sweep, leader-gated on REVIVAL_SWEEP_LOCK_KEY (its own key).
    # Always wired: the shapes it fixes (a leg consumed without running, a transient failure
    # nobody picked up) leave a task alive with nothing running, and no per-control code
    # rescues them. It needs no memory backend: it moves work, it does not voice.
    def _revival_sweep_builder(
        dispatch_engine: Engine, worker_rls_engine: Engine
    ) -> RevivalSweeper | None:
        from persona_api.approvals.kill_switch import KillSwitchStore
        from persona_api.schedules.leadership import SchedulerLeader
        from persona_api.tasks.revival_sweep import REVIVAL_SWEEP_LOCK_KEY, RevivalSweeper

        def _emit_task_updated(owner: str, task_id: str, state: str) -> None:
            publish_task_updated(event_channel, owner_id=owner, task_id=task_id, state=state)

        return RevivalSweeper(
            continuation=TaskContinuation(
                task_store=TaskStore(worker_rls_engine),
                queue=JobQueue(worker_rls_engine),
                checkpoint_store=CheckpointStore(worker_rls_engine),
                on_state_change=_emit_task_updated,
            ),
            dispatch_engine=dispatch_engine,
            rls_engine=worker_rls_engine,
            task_store=TaskStore(worker_rls_engine),
            kill_switch=KillSwitchStore(worker_rls_engine),
            leader=SchedulerLeader(dispatch_engine, lock_key=REVIVAL_SWEEP_LOCK_KEY),
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

    def _ignored_proposal_sweep_builder(
        dispatch_engine: Engine, worker_rls_engine: Engine
    ) -> IgnoredProposalSweeper | None:
        # Spec A5 (T3): the leader-gated ignored-proposal expiry sweep — the third
        # decline source, so restraint learns from silence and not only from the
        # users who reply. Built ONLY when initiative is enabled (the same gate as
        # the tenant and the provisioner; OFF ⇒ the worker loop is unchanged).
        settings = InitiativeSettings()
        if not settings.enabled:
            return None
        return IgnoredProposalSweeper(
            dispatch_engine=dispatch_engine,
            declines=DeclineStore(worker_rls_engine),
            ledger=InitiativeLedger(worker_rls_engine),
            settings=settings,
        )

    def _title_backfill_builder(dispatch_engine: Engine) -> UntitledConversationBackfill:
        # Issue #8: connector-born and call-born conversations used to reach the message
        # count the title trigger wanted only rarely, and the web route's first-turn hook
        # never ran for them, so they sat unnamed. The trigger floor now names them at their
        # first exchange; this sweep is what names the ones that were ALREADY sitting there,
        # plus any conversation whose write path never reached a trigger at all. It produces
        # nothing new, just the same title_refresh job on the same key.
        from persona_api.schedules.leadership import SchedulerLeader
        from persona_api.services.title_backfill import (
            TITLE_BACKFILL_LOCK_KEY,
            UntitledConversationBackfill,
        )

        return UntitledConversationBackfill(
            dispatch_engine=dispatch_engine,
            queue=JobQueue(dispatch_engine),
            leader=SchedulerLeader(dispatch_engine, lock_key=TITLE_BACKFILL_LOCK_KEY),
        )

    worker = build_worker(
        config,
        registry,
        scheduler_tick_builder=_tick_builder,
        catalog_sync_builder=_catalog_sync_builder,
        skill_catalog_sync_builder=_skill_catalog_sync_builder,
        initiative_provisioner_builder=_initiative_provisioner_builder,
        ignored_proposal_sweep_builder=_ignored_proposal_sweep_builder,
        approval_sweep_builder=_approval_sweep_builder,
        dead_leg_sweep_builder=_dead_leg_sweep_builder,
        revival_sweep_builder=_revival_sweep_builder,
        title_backfill_builder=_title_backfill_builder,
        on_drain=leg_drain.request_drain,
    )
    handle = InProcessWorker(worker, job_types=frozenset(registry.types()))
    handle.start()
    return handle

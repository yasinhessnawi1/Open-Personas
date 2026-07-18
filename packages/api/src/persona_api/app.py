"""FastAPI application factory (spec 08, T01).

``create_app()`` is the composition root's entry point. It assembles the app,
registers exception handlers (T02), routers (T07+), middleware, and a lifespan
context that owns the long-lived collaborators — the database engine(s), the
``TierRegistry``, and the toolbox/MCP clients (T10 fills the body; on shutdown it
calls ``await tier_registry.aclose()`` + ``await client.disconnect()`` per the
spec-05/06 lifecycle handoff).

The app is stateless (twelve-factor): all persistent state lives in Postgres.
Single uvicorn worker for v0.1 (S08-4 / D-08-5) — the in-memory run event bus
requires it.
"""

from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from persona.backends.errors import AuthenticationError
from persona.billing import BillingConfig
from persona.errors import PersonaError
from persona.imagegen import (
    ImageBackend,
    load_image_backend_from_env,
)
from persona.logging import get_logger
from persona.stores.chroma import ChromaBackend
from persona.stores.document_store import DocumentStore
from persona.stores.postgres import PostgresBackend
from persona_runtime.errors import TierNotConfiguredError
from persona_runtime.openrouter_subscription import resolve_openrouter_subscription
from persona_runtime.tier import tier_registry_from_env

from persona_api.background.chat_turn_worker import ChatTurnRegistry
from persona_api.background.restart_sweep import reconcile_in_flight_on_startup
from persona_api.background.run_worker import RunRegistry
from persona_api.config import APIConfig, Edition
from persona_api.db.audit_factory import build_audit_logger, build_tool_audit_logger
from persona_api.db.community import (
    create_community_schema,
    ensure_owner,
    make_community_engine,
)
from persona_api.db.community_import import maybe_run_community_import
from persona_api.db.community_managed import (
    CommunityDbManager,
    CommunityDbMode,
    resolve_managed_external_url,
    run_migrations,
)
from persona_api.db.engine import create_db_engine
from persona_api.editions import (
    build_credits_policy,
    build_owner_resolver,
    check_cloud_config_guard,
    check_gateway_edition_posture,
    check_per_tenant_mcp_posture,
    check_public_noauth_guard,
)
from persona_api.errors import register_exception_handlers
from persona_api.jobs import JobQueue
from persona_api.mcp.fly import HttpxFlyMachinesClient
from persona_api.mcp.fly_runtime import FlyPerTenantMCPRuntime
from persona_api.mcp.runtime_config import FlyRuntimeConfig
from persona_api.middleware.rate_limit import (
    InMemoryRateLimitStore,
    PostgresRateLimitStore,
    RateLimiter,
    RateLimitStore,
)
from persona_api.middleware.request_telemetry import RequestTelemetryMiddleware, TelemetryBuffer
from persona_api.middleware.rls_context import make_rls_engine
from persona_api.realtime.channel import UserEventChannel
from persona_api.realtime.live_sessions import ChannelLiveSessions
from persona_api.routes import (
    approvals,
    artifacts,
    autonomy,
    calls,
    connectors,
    conversations,
    documents,
    health,
    imagegen,
    mcp_servers,
    me,
    memory,
    models,
    personas,
    runs,
    tasks,
    tools,
    uploads,
    voice,
)
from persona_api.sandbox import (
    HostedSandbox,
    SandboxPool,
    SandboxPoolConfig,
    SandboxTemplateConfig,
)
from persona_api.services import persona_service
from persona_api.services.chat_turn_sink import MessagesTurnSink
from persona_api.services.runtime_factory import RuntimeFactory
from persona_api.services.turn_log_writer import PostgresTurnLogWriter
from persona_api.storage import build_file_storage

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from persona.backends.openrouter_catalog import OpenRouterSubscriptionMode
    from persona.stores.backend import Backend
    from sqlalchemy import Engine

__all__ = ["create_app"]


_LOG = get_logger("api.app")


def _resolve_openrouter_subscription_mode() -> OpenRouterSubscriptionMode | None:
    """Resolve the OpenRouter free/paid mode at startup (Spec 22 T13 + T15).

    Probes ``GET /api/v1/key`` once (or honours the
    ``PERSONA_OPENROUTER_SUBSCRIPTION_MODE`` override) so the resolved mode can
    be threaded into both the chat :class:`TierRegistry` (D-22-2 free-mode
    filter) and the image-gen factory (D-22-20 drop). Returns ``None`` when
    OpenRouter is not configured (no key) — the zero-touch opt-in path.

    Composition-root degradation: an :class:`AuthenticationError` (the
    resolver's D-22-9 fail-loud signal for an invalid key) is logged at ERROR
    and swallowed here so one optional provider's bad key does NOT block API
    startup — consistent with the graceful-absence pattern used for the
    image backend and the E2B-less sandbox pool above. The misconfigured
    OpenRouter entries then surface their 401 at call time. A transient probe
    failure already degrades to free-mode inside the resolver (D-22-3).
    """
    try:
        state = resolve_openrouter_subscription()
    except AuthenticationError as exc:
        _LOG.error(
            "OpenRouter API key rejected at startup; OpenRouter free-mode "
            "filtering disabled (reason={reason})",
            reason=str(exc),
        )
        return None
    if state is None:
        return None
    _LOG.info(
        "OpenRouter subscription mode resolved mode={mode} probe_failed={probe_failed}",
        mode=state.mode,
        probe_failed=state.probe_failed,
    )
    return state.mode


def _compose_image_backend(
    openrouter_subscription_mode: OpenRouterSubscriptionMode | None = None,
) -> ImageBackend | None:
    """Build the image-generation backend at startup (spec 15 T16; spec 25 §2.7).

    Spec 25 T17 fix: delegate to
    :func:`persona.imagegen.load_image_backend_from_env`, which applies the
    D-20-17 four-case precedence — ``PERSONA_IMAGEGEN_MODELS`` (cross-provider
    list, wrapped in a ``MultiModelImageBackend`` for N≥2) wins over the legacy
    ``PERSONA_IMAGEGEN_PROVIDER/MODEL/API_KEY`` triplet, with an INFO log when
    both are set. The pre-fix code read ONLY the triplet, so a MODELS-list
    Setup C config silently fell back to the hard-coded ``openai/gpt-image-1``
    default and 503'd (§2.7).

    When NEITHER form is configured we return ``None`` with a clear
    "not configured" warning (no silent hard-coded default); the route then
    raises :class:`persona.imagegen.ImageGenUnavailableError` → 503 on a
    request that needs the backend (same fail-loud pattern as the Spec 12
    sandbox-pool absence, D-12-5). Any construction-time
    :class:`persona.errors.PersonaError` (missing key, all-slots-unresolved,
    malformed MODELS, unknown provider) is caught so the deployment still
    boots cleanly and chat / runs / authoring routes stay available.
    """
    import os

    models_set = bool(os.environ.get("PERSONA_IMAGEGEN_MODELS", "").strip())
    triplet_key_set = bool(os.environ.get("PERSONA_IMAGEGEN_API_KEY", "").strip())
    if not models_set and not triplet_key_set:
        _LOG.warning(
            "image generation not configured — set PERSONA_IMAGEGEN_MODELS "
            "(cross-provider list) OR the PERSONA_IMAGEGEN_PROVIDER/MODEL/API_KEY "
            "triplet; image generation will return 503 until then"
        )
        return None
    try:
        return load_image_backend_from_env(
            openrouter_subscription_mode=openrouter_subscription_mode
        )
    except PersonaError as exc:
        _LOG.warning(
            "image backend construction failed; image generation will return 503 (reason={reason})",
            reason=str(exc),
        )
        return None


def _e2b_api_key_present() -> bool:
    """Whether ``E2B_API_KEY`` is set in the environment.

    The pool is wired only when E2B is reachable (D-12-12 substrate). Dev
    environments without an E2B account boot cleanly without a hosted pool;
    the ``code_execution`` tool surfaces ``SandboxUnavailableError`` to the
    model (D-12-5 no degraded fallback).
    """
    import os

    return bool(os.environ.get("E2B_API_KEY", "").strip())


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup/shutdown for the long-lived collaborators.

    T05 builds the **RLS engine** here — the per-request engine whose pool
    listener structurally scopes every connection (D-08-1); it is the spine the
    auth/route DB access composes through. T10 extends this with the embedder,
    typed stores, and the ``TierRegistry`` (and tears them down — calling
    ``await tier_registry.aclose()`` + ``await client.disconnect()`` per MCP
    client, D-05-4 / spec-06 handoff).
    """
    config: APIConfig = app.state.config
    # The embedder for persona memory population (D-08-8). Lazy: weights load on
    # first encode, not at startup. Shared (thread-safe read path). Built early so
    # the community Chroma memory backend can compose it.
    app.state.embedder = persona_service.default_embedder(config.embedder_model)

    # Spec 33 (Cluster B) + Spec K10 (T2): the persistence backends are
    # edition-selected; within community, ``PERSONA_COMMUNITY_DB_MODE`` selects the
    # legacy SQLite path or the K10 managed-Postgres substrate.
    rls_engine: Engine | None = None
    admin_engine: Engine | None = None
    memory_backend: Backend | None = None
    community_db_manager: CommunityDbManager | None = None
    if config.edition is Edition.community:
        db_mode = CommunityDbMode(config.community_db_mode)
        if db_mode is CommunityDbMode.legacy_sqlite:
            # Zero-infra legacy path (Spec 33): a single SQLite file (no RLS —
            # single owner) + Chroma for typed-memory vectors. No superuser/admin
            # engine; the fixed owner is seeded so the app-table FKs hold (D-33-7 /
            # D-33-8 / D-33-X-owner-seed). DEPRECATED (Spec K10 D-K10-5): retained
            # one release as the auto-import source + rollback target — warn on use.
            _LOG.warning(
                "PERSONA_COMMUNITY_DB_MODE=legacy-sqlite is DEPRECATED (Spec K10 D-K10-5): "
                "the community edition now runs an invisible managed Postgres. This path is "
                "retained one release as the migration source; set PERSONA_COMMUNITY_DB_MODE="
                "auto to move onto managed Postgres."
            )
            rls_engine = make_community_engine(config.community_db_path)
            create_community_schema(rls_engine)
            ensure_owner(
                rls_engine, owner_id=config.community_owner_id, email=config.community_owner_email
            )
            community_memory_dir = Path(config.community_memory_path)
            community_memory_dir.mkdir(parents=True, exist_ok=True)
            memory_backend = ChromaBackend(
                persist_path=community_memory_dir, embedder=app.state.embedder
            )
        else:
            # Spec K10 (D-K10-1/-2/-3/-6/-8): the invisible product-managed Postgres.
            # Resolution order external DATABASE_URL → embedded (D-K10-1); connect as
            # the instance superuser (D-K10-2 — RLS inert, single-owner correctness by
            # the owner predicate); reuse ``make_rls_engine`` (D-K10-6 — the GUC
            # listener is harmless under superuser); run the SAME Alembic chain every
            # boot (D-K10-8); typed memory on Postgres/``memory_chunks`` (D-K10-3).
            external_url = resolve_managed_external_url(db_mode, config.database_url)
            community_db_manager = CommunityDbManager(
                external_url=external_url, base_dir=config.community_managed_db_dir
            )
            managed_url = community_db_manager.start()
            run_migrations(managed_url)
            rls_engine = make_rls_engine(managed_url, pool_size=config.db_pool_size)
            # Spec K10 (T8, D-K10-4): auto-import a legacy SQLite + Chroma install on
            # the first managed boot — the legacy-data branching (data present →
            # import-first, then boot managed; absent → fresh embedded). A no-op when
            # no legacy store is found; crash-safe + resumable (D-K10-11). Runs AFTER
            # the schema is migrated (targets exist) and BEFORE ensure_owner (which is
            # idempotent, so an imported owner row is fine).
            maybe_run_community_import(
                sqlite_path=config.community_db_path,
                chroma_path=config.community_memory_path,
                target_engine=rls_engine,
                embedder=app.state.embedder,
            )
            ensure_owner(
                rls_engine, owner_id=config.community_owner_id, email=config.community_owner_email
            )
            memory_backend = PostgresBackend(engine=rls_engine, embedder=app.state.embedder)
    else:
        # Cloud: Postgres + RLS (today's behavior, unchanged).
        if config.effective_app_database_url:
            rls_engine = make_rls_engine(
                config.effective_app_database_url, pool_size=config.db_pool_size
            )
            # Spec R2 R2-D-1 (defense-in-depth): now that the rls_engine exists, probe
            # its role — refuse to serve if it is a PostgreSQL superuser (RLS-bypassing),
            # even if the DSN strings differ from ``database_url`` (F-06). Runs before the
            # app accepts a request; the cheap config asserts already ran at ``create_app``.
            check_cloud_config_guard(config, probe_engine=rls_engine)
        # Superuser engine for JIT user provisioning (spec-09 integration): a
        # freshly authenticated Clerk user has no `users` row (webhook mirroring
        # deferred, spec 08), yet everything FKs users.id. The auth dep upserts it
        # via this RLS-bypassing engine. None when no superuser DSN is set.
        admin_engine = create_db_engine(config.database_url) if config.database_url else None
        if rls_engine is not None:
            memory_backend = PostgresBackend(engine=rls_engine, embedder=app.state.embedder)
    app.state.rls_engine = rls_engine
    app.state.admin_engine = admin_engine
    # Spec K10 (D-K10-9): the managed-Postgres lifecycle owner (None unless the
    # community managed path is active). The finally block stops an embedded
    # postmaster on shutdown; external mode makes it a no-op.
    app.state.community_db_manager = community_db_manager
    # Spec 33 (D-33-X-memory-chroma-community): expose the edition's typed-memory
    # backend so the persona-create/update service composes its four typed stores
    # over the edition-appropriate transport (Chroma for community, Postgres for
    # cloud) — never a hardcoded PostgresBackend, which has no ``memory_chunks``
    # table on the community SQLite path.
    app.state.memory_backend = memory_backend
    # A0 (T9): the durable enqueue surface. A JobQueue on the cross-tenant dispatch
    # engine (admin/superuser for v0.1; a job_dispatcher role to harden). Present
    # only when a dispatch engine exists; the create path uses it iff
    # ``avatar_via_queue`` is on (the orchestrator's cutover flag).
    _dispatch_engine = admin_engine if admin_engine is not None else rls_engine
    app.state.job_queue = JobQueue(_dispatch_engine) if _dispatch_engine is not None else None
    app.state.audit_root = Path(config.audit_root)
    # Audit loggers (R5-D-2). Postgres-backed (multi-worker-safe
    # store_audit_events / tool_audit_events) when PERSONA_API_AUDIT_BACKEND=postgres
    # + an engine exists; the JSONL default otherwise (community / single-node
    # byte-unchanged). Built ONCE here and threaded through app.state to every
    # store-mutation / tool-audit site so N workers don't each open the
    # single-writer JSONL hole (S08-4). The background worker selects the SAME
    # backend independently in worker_root (R5-D-2). Mirrors _build_rate_limiter:
    # the tables are non-RLS, so the plain rls_engine (no GUC dependence) is fine.
    app.state.audit_logger = build_audit_logger(config, rls_engine)
    app.state.tool_audit_logger = build_tool_audit_logger(config, rls_engine)
    # Spec 13 D-13-4: workspace root for image uploads + (later) per-persona
    # tool artefacts. Resolved up front so routes/services can rely on it.
    app.state.workspace_root = Path(config.workspace_root)
    app.state.workspace_root.mkdir(parents=True, exist_ok=True)
    # External file storage (R5-D-4). Local (byte-unchanged) by default; S3-generic
    # (Tigris / R2 / MinIO / AWS) when PERSONA_API_STORAGE_BACKEND=s3, lifting the
    # Fly-volume single-Machine pin so artifacts/uploads/images aren't host-pinned.
    # Keyed by the workspace-relative logical key ({owner}/{persona}/{relative}).
    app.state.file_storage = build_file_storage(config, app.state.workspace_root)
    # Spec 29 D-29-3: the wall-clock bound the create hook applies to
    # build-time avatar generation (env: PERSONA_API_AVATAR_GEN_TIMEOUT_S).
    app.state.avatar_gen_timeout_s = config.avatar_gen_timeout_s
    # Rate limiter (§6, D-08-5). Postgres-backed when a non-RLS engine is
    # available; in-memory otherwise. The buckets table is NOT under RLS, so the
    # Postgres store uses a plain (non-listener) engine.
    app.state.rate_limiter = _build_rate_limiter(config, rls_engine)

    # Request telemetry (R5-D-3, §6.3). The buffer + its background flush are built
    # here (the middleware, added in create_app, records into it). NON-RLS table
    # written by a plain platform engine (admin_engine when present, else the
    # rls_engine — the GUC is irrelevant for a non-RLS table), mirroring the sweep
    # / dispatch engine choice. None ⇒ the middleware no-ops (no-DB / disabled).
    app.state.telemetry_buffer = None
    if config.telemetry_enabled:
        _telemetry_engine = admin_engine if admin_engine is not None else rls_engine
        if _telemetry_engine is not None:
            telemetry_buffer = TelemetryBuffer(_telemetry_engine)
            telemetry_buffer.start()
            app.state.telemetry_buffer = telemetry_buffer

    # F3 — DocumentStore builder (Spec 14 T17/T18). Per-request callable so
    # routes/uploads.py + routes/documents.py + routes/conversations.py
    # (cascade-delete) can construct a DocumentStore bound to the RLS-scoped
    # engine. Cross-tenant access is structurally blocked by D-08-1's pool
    # listener; the builder is just the composition wiring. None when no DB
    # engine is configured (test paths override `app.state.build_document_store`
    # with an in-memory fake; see routes/documents.py).
    if memory_backend is not None:
        _document_backend = memory_backend
        app.state.build_document_store = lambda: DocumentStore(backend=_document_backend)
    # Sibling alias for routes/documents.py which reads `sandbox_root` (legacy
    # name from the spec-03 sandbox-path resolver). Same value as
    # `workspace_root`; keeping the alias avoids a route rename ripple.
    app.state.sandbox_root = app.state.workspace_root
    # The agentic-run registry (T11, D-08-5): in-process event bus + task tracker.
    # Single worker (S08-4). Cancelled on shutdown.
    #
    # Within-runtime origination (Spec C0, T7): when enabled, a completed run
    # originates its conclusion as a delivered message (criterion 7). Default OFF
    # (config.within_runtime_origination) → byte-unchanged run behaviour
    # (criterion 10); injected only when explicitly enabled and an RLS engine is
    # present (cloud). The gateway reuses the app's engine + embedder + edition.
    within_runtime_originator = None
    if rls_engine is not None and memory_backend is not None and config.within_runtime_origination:
        from persona_api.services.within_runtime_origination import (
            WithinRuntimeOriginator,
        )

        within_runtime_originator = WithinRuntimeOriginator(
            rls_engine=rls_engine,
            memory_backend=memory_backend,
            edition=config.edition,
            audit_root=app.state.audit_root,
            # R5-D-2: the app-selected audit backend (Postgres when multi-worker).
            audit_logger=app.state.audit_logger,
        )
    # Spec A11 (A11-D-1) — the persistent user-level SSE channel's in-process bus.
    # One per process; its fresh ``epoch`` makes a stale cross-restart Last-Event-ID
    # detectable (A11-D-3). Both editions run in-process today (community SQLite;
    # cloud single Fly Machine), so this bus is the whole fan-out mechanism; a future
    # api/worker split adds a cross-process transport behind the same publish seam.
    # Built HERE (before the run/A4/worker composition) so a background delivery fans out
    # through the channel-backed ``LiveSessionRegistry`` (message.delivered) and a
    # committed notification pings the bell (notification.created) — both live.
    event_channel = UserEventChannel()
    app.state.event_channel = event_channel
    live_sessions = ChannelLiveSessions(event_channel)

    # Spec K2 (T8d): thread the durable ``job_queue`` so a completed agentic run
    # enqueues synthesis (the producer was inert — ``job_queue=None`` — until now).
    # ``None`` queue keeps the producer a no-op (the community / no-dispatch path).
    run_registry = (
        RunRegistry(
            rls_engine,
            job_queue=app.state.job_queue,
            origination=within_runtime_originator,
            # Spec A11: a completed run's run_terminal notification pings the bell live.
            event_channel=event_channel,
            # Spec M3 (T4a): incremental caller-paid billing + exhaustion cutoff.
            # ``credits_policy`` is set at factory time (create_app), available here;
            # ``cost_source`` uses the static default (the shared resolver is wired
            # later in the lifespan) — OpenRouter actuals still price exactly.
            credits_policy=app.state.credits_policy,
            billing_config=BillingConfig(),
            agentic_floor=config.agentic_credit_floor,
        )
        if rls_engine is not None
        else None
    )
    app.state.run_registry = run_registry

    # Detached chat-turn registry (Spec P1 — D-P1-detached-execution): a chat turn
    # now runs in a background task (survives navigation/reload) and is billed on
    # clean completion regardless of client presence (the D-08-6 revision,
    # D-P1-billing-contract). The ``MessagesTurnSink`` persists at-start +
    # checkpoint + finalize; the registry owns the credits deduct. ``None`` when
    # there is no DB engine (the chat path needs one anyway). Cancelled on
    # shutdown (S08-4 single worker; the startup sweep reconciles orphaned
    # ``running`` rows on next boot — T3, D-P1-restart-sweep).
    chat_turn_sink = MessagesTurnSink(rls_engine) if rls_engine is not None else None
    # Spec A4 (composition-root activation): the WORKER side of the contract flow. Built together
    # with the loop-side interpreters (runtime_factory) so a confirmed contract that emits
    # ``task_originated`` is actually created (idempotent + failure-visible) and steering actually
    # steers (cancel-failure surfaces an un-suppressible account). Gated on a real engine +
    # memory backend (the C0 originator needs both); ``None`` in unit/community-keyless paths →
    # the loop's A4 gates run but the worker no-ops, exactly the pre-activation posture.
    origination_service = None
    task_steering_service = None
    if rls_engine is not None and memory_backend is not None:
        from persona_api.services.task_origination_composition import (
            compose_task_origination_services,
        )

        _a4_services = compose_task_origination_services(
            rls_engine=rls_engine,
            memory_backend=memory_backend,
            edition=config.edition,
            audit_root=app.state.audit_root,
            live_sessions=live_sessions,
        )
        origination_service = _a4_services.origination
        task_steering_service = _a4_services.steering
    # Spec A8 (T6): the WORKER side of the conversational reschedule verb — applies a user-confirmed
    # reschedule through the one CAS-guarded door (owner-scoped via the worker's injected owner_id).
    task_reschedule_service = None
    if rls_engine is not None:
        from persona_api.schedules.store import ScheduleStore
        from persona_api.services.task_reschedule_service import TaskRescheduleService
        from persona_api.tasks.store import TaskStore

        task_reschedule_service = TaskRescheduleService(
            task_reader=TaskStore(rls_engine),
            schedule_store=ScheduleStore(rls_engine),
            engine=rls_engine,
        )
    # Spec A5 (T10): the worker side of the initiative-verb family — dial writes
    # (+ the lazy schedule ensure) and the LEDGER-anchored confirm/decline. Built
    # only when initiative is enabled (PERSONA_INITIATIVE_ENABLED, default OFF).
    initiative_verb_service = None
    if rls_engine is not None:
        from persona.initiative import InitiativeSettings as _InitiativeSettings

        _initiative_settings = _InitiativeSettings()
        if _initiative_settings.enabled:
            from persona.config import PersonaCoreConfig

            from persona_api.initiative.delivery import InitiativeDeliveryExecutor
            from persona_api.initiative.store import DeclineStore, InitiativeLedger
            from persona_api.initiative.verb_service import InitiativeVerbService
            from persona_api.schedules.store import ScheduleStore as _A5ScheduleStore
            from persona_api.schedules.tombstones import ScheduleTombstoneStore as _A5TombstoneStore
            from persona_api.tasks.store import TaskStore as _A5TaskStore

            _a5_ledger = InitiativeLedger(rls_engine)
            initiative_verb_service = InitiativeVerbService(
                rls_engine=rls_engine,
                schedules=_A5ScheduleStore(rls_engine),
                ledger=_a5_ledger,
                declines=DeclineStore(rls_engine),
                executor=InitiativeDeliveryExecutor(
                    ledger=_a5_ledger,
                    tasks=_A5TaskStore(rls_engine),
                    schedules=_A5ScheduleStore(rls_engine),
                    timezone_for=PersonaCoreConfig().default_timezone,
                    rls_engine=rls_engine,
                ),
                settings=_initiative_settings,
                # R9-037: the dial verb's own lazy ensure refuses a recently
                # user-deleted scan schedule too (the same shared function the
                # provisioner sweep gates).
                tombstones=_A5TombstoneStore(rls_engine),
                tombstone_window_days=config.schedule_tombstone_window_days,
            )
    chat_turn_registry = (
        ChatTurnRegistry(
            sink=chat_turn_sink,
            rls_engine=rls_engine,
            credits_policy=app.state.credits_policy,
            credits_per_turn=config.credits_per_turn,
            # Spec M2 (D-M2-5): proportional chat-turn billing (floor above);
            # PERSONA_API_PROPORTIONAL_CREDITS=false is the rollback hatch.
            proportional_credits=config.proportional_credits,
            # Spec M2 review (reviewer defense-in-depth, TAKE): the per-turn
            # charge sanity ceiling (PERSONA_API_MAX_TURN_CREDITS).
            max_turn_credits=config.max_turn_credits,
            # Spec M3 (T1b): the shared credit formula config (PERSONA_CREDIT_MARKUP,
            # default 1.0 → byte-identical charge; chat carries no per-call infra).
            billing_config=BillingConfig(),
            job_queue=app.state.job_queue,
            origination_service=origination_service,
            task_steering_service=task_steering_service,
            task_reschedule_service=task_reschedule_service,
            initiative_verb_service=initiative_verb_service,
        )
        if chat_turn_sink is not None and rls_engine is not None
        else None
    )
    app.state.chat_turn_sink = chat_turn_sink
    app.state.chat_turn_registry = chat_turn_registry

    # Hosted sandbox + pool (spec 12 T08/T09; D-12-12/D-12-17). Built BEFORE
    # the runtime factory so the factory can compose the ``code_execution``
    # tool when the pool is present. Gated on E2B_API_KEY — dev environments
    # without an E2B account boot cleanly; the tool is absent in that case
    # and the model would surface ``SandboxUnavailableError`` if it tried
    # (D-12-5 no degraded fallback).
    # Spec P5: the hosted sandbox template selection (P5-D-3). ``template`` names the
    # owner's custom doc-gen template alias (reportlab + python-pptx baked in, egress
    # still off); ``None`` ⇒ the SDK default ``code-interpreter-v1`` (community/OSS).
    # ``docgen_full_fidelity`` (P5-D-4) is derived from the SAME value and threaded to
    # the runtime factory below — persona-core never reads ``PERSONA_SANDBOX_*``.
    sandbox_template_cfg = SandboxTemplateConfig()
    sandbox_pool: SandboxPool | None = None
    if _e2b_api_key_present():
        pool_cfg = SandboxPoolConfig()
        sandbox_pool = SandboxPool(
            # SDK reads E2B_API_KEY from env (D-12-12); ``template`` selects the
            # custom image (P5) — None keeps the SDK default. Egress stays off
            # regardless (D-12-4; ``allow_internet_access=False`` in _create_sandbox).
            sandbox=HostedSandbox(template=sandbox_template_cfg.template),
            max_per_user=pool_cfg.max_per_user,
            idle_timeout_s=pool_cfg.idle_timeout_s,
            reap_interval_s=pool_cfg.reap_interval_s,
        )
        await sandbox_pool.start()  # spawns the pool-owned background reaper
    app.state.sandbox_pool = sandbox_pool

    # Spec N6 (N6-D-1/3/5): the per-tenant image-MCP runtime (a Fly Machine per
    # (tenant, server)). Built ONLY in cloud, with a Fly app+token configured, AND the
    # operator ack — the startup guard (N6-D-5) refuses cloud+configured without the ack.
    # Community / CLI / unconfigured ⇒ ``None`` (image-runtime servers report not-connected,
    # T6). The reaper sweeps CROSS-TENANT under the admin (RLS-bypassing) engine (N6-D-7a).
    fly_runtime_cfg = FlyRuntimeConfig()
    check_per_tenant_mcp_posture(config, runtime_configured=fly_runtime_cfg.configured)
    app.state.mcp_runtime_max_per_tenant = fly_runtime_cfg.max_per_tenant
    mcp_runtime: FlyPerTenantMCPRuntime | None = None
    if (
        fly_runtime_cfg.configured
        and config.edition is Edition.cloud
        and config.allow_per_tenant_mcp
        and rls_engine is not None
    ):
        import httpx
        from persona.tools.mcp.catalog import MCPCatalog

        from persona_api.mcp import run_policy
        from persona_api.services import catalog_service

        def _is_runnable_image(image: str) -> bool:
            catalog = MCPCatalog(servers={e.name: e for e in catalog_service.merged_mcp_catalog()})
            return image in run_policy.runnable_images(
                edition=config.edition, vetted=config.mcp_run_vetted_list, catalog=catalog
            )

        fly_client = HttpxFlyMachinesClient(
            app=fly_runtime_cfg.fly_app,
            token=fly_runtime_cfg.fly_token.get_secret_value(),
            client=httpx.AsyncClient(),
        )
        mcp_runtime = FlyPerTenantMCPRuntime(
            rls_engine=rls_engine,
            # CROSS-TENANT reaper needs the RLS-bypassing engine (admin), never persona_app.
            bypass_engine=admin_engine if admin_engine is not None else rls_engine,
            fly=fly_client,
            app=fly_runtime_cfg.fly_app,
            port=fly_runtime_cfg.port,
            is_runnable_image=_is_runnable_image,
            idle_timeout_s=fly_runtime_cfg.idle_timeout_s,
            reap_interval_s=fly_runtime_cfg.reap_interval_s,
        )
        await mcp_runtime.start()  # spawns the cross-tenant idle-reaper
    app.state.mcp_runtime = mcp_runtime

    # Spec N7 (D-N7-5): ONE structured startup summary of which MCP mechanisms
    # THIS DEPLOYMENT can actually exercise — an honest, operator-visible
    # complement to the per-request `/v1/mcp-catalog` capabilities block (T2),
    # readable at boot without querying an endpoint. Structured fields only,
    # no secrets: the gateway URL and mirror path are operator deployment
    # config (same trust tier as DATABASE_URL) and fine to log; a gateway
    # bearer token never rides this line.
    from persona.config import PersonaCoreConfig

    from persona_api.mcp.oauth.providers import provider_registry

    _core_config = PersonaCoreConfig()
    if mcp_runtime is not None:
        _runtime_summary = "on"
    elif not fly_runtime_cfg.configured:
        _runtime_summary = "off (reason: unconfigured)"
    elif config.edition is not Edition.cloud:
        _runtime_summary = "off (reason: edition)"
    elif not config.allow_per_tenant_mcp:
        _runtime_summary = "off (reason: ack)"
    else:
        # Configured + cloud + acked but still off (e.g. no RLS engine) — no
        # single-word reason in the D-N7-5 vocabulary (edition|ack|unconfigured)
        # fits; fold into "unconfigured" rather than invent a fourth reason.
        _runtime_summary = "off (reason: unconfigured)"
    _oauth_provider_names = sorted(provider_registry(config))
    _LOG.info(
        "MCP mechanisms: builtin-launcher={builtin}, gateway={gateway}, "
        "per-tenant-runtime={runtime}, byo=on, oauth-providers={oauth}, "
        "mirror={mirror}",
        builtin=",".join(_core_config.mcp_builtin_enabled_parsed) or "none",
        gateway="on" if _core_config.docker_mcp_gateway_url else "off",
        runtime=_runtime_summary,
        oauth=",".join(_oauth_provider_names) or "none",
        mirror=(
            "bundled"
            if _core_config.mcp_mirror_path is None
            else f"volume:{_core_config.mcp_mirror_path}"
        ),
    )

    # Image-generation backend (spec 15 T16). Composed at startup so the
    # route layer can dispatch through ``app.state.image_backend`` without
    # re-reading env vars per request. ``None`` when no provider is
    # configured — the route raises ``ImageGenUnavailableError`` → 503
    # in that case (same dev-friendly graceful-absence shape as the
    # E2B-less sandbox pool).
    # Spec 22 T13/T15: resolve OpenRouter free/paid mode once (probe or env
    # override), then thread it into both the image backend (D-22-20 drop) and
    # the chat TierRegistry (D-22-2 filter). ``None`` when OpenRouter is unused.
    openrouter_mode = _resolve_openrouter_subscription_mode()
    app.state.image_backend = _compose_image_backend(openrouter_mode)
    if config.avatar_via_queue and app.state.image_backend is None:
        # R9-013 once-per-boot honesty: the cutover flag is on but the shared
        # avatar_queue_ready gate can never pass without an image backend, so
        # persona-create falls back to the inline no-op avatar path (no queue
        # job is ever enqueued — a dead job would poison-loop a handler-less
        # worker). Configure PERSONA_IMAGEGEN_* to activate the queue cutover.
        _LOG.warning(
            "PERSONA_API_AVATAR_VIA_QUEUE is on but image generation is not "
            "configured; persona-create avatars fall back to the inline no-op path"
        )

    # Runtime composition root (T10): the TierRegistry (app-scoped) + the
    # per-request loop builders. Built only when a model backend is configured
    # AND a DB engine exists; tests override app.state.build_conversation_loop
    # with a scripted loop, so a missing registry doesn't block them.
    runtime_factory: RuntimeFactory | None = None
    if rls_engine is not None:
        try:
            tier_registry = tier_registry_from_env(openrouter_subscription_mode=openrouter_mode)
        except TierNotConfiguredError:
            tier_registry = None
        if tier_registry is not None:
            from persona_runtime.crisis_encoder import (
                build_crisis_encoder,
                start_crisis_encoder_warmup,
            )
            from persona_runtime.safety_intercept import SafetyInterceptSettings

            # R6 (T8): ONE app-scoped crisis encoder, injected into every chat + agentic
            # loop the factory builds — so all request paths share the SAME composed
            # ``classify_user_message`` (lexical ∪ encoder). Built (lazy) only when the
            # encoder is enabled (an edition may disable it → V11 lexical-only); the model
            # loads at the off-loop warm-up below, never on a user's first turn.
            crisis_encoder = (
                build_crisis_encoder() if SafetyInterceptSettings().encoder_enabled else None
            )
            runtime_factory = RuntimeFactory(
                rls_engine=rls_engine,
                embedder=app.state.embedder,
                crisis_encoder=crisis_encoder,
                tier_registry=tier_registry,
                # Postgres turn_logs (D-08-7); RLS-scoped via conversations.
                turn_log_writer=PostgresTurnLogWriter(rls_engine),
                audit_root=Path(config.audit_root),
                # R5-D-2: the app-selected audit backend (Postgres when
                # multi-worker). audit_root stays the JSONL fallback.
                audit_logger=app.state.audit_logger,
                # R5-D-4: the storage backend the chat-path workspace persister
                # writes produced artifacts through (local/S3).
                file_storage=app.state.file_storage,
                # Spec 12 T10: pass the hosted sandbox pool (may be None when
                # E2B_API_KEY is unset; factory absents code_execution in that case).
                sandbox_pool=sandbox_pool,
                # Spec 17 D-17-X-bytes-persistence: thread the workspace root
                # so the code_execution tool persists produced files into the
                # served persona workspace + stages intermediate/* cross-turn.
                workspace_root=app.state.workspace_root,
                # Spec 15 T16 + Spec 25 §2.9: thread the image backend so the
                # factory composes ``generate_image`` into the persona's
                # toolbox (the wiring gap diagnosed in Spec 25 §2.9 — the
                # factory + HTTP endpoint had the backend; the per-request
                # toolbox never did). ``None`` ⇒ tool absent (same graceful
                # shape as sandbox_pool).
                image_backend=app.state.image_backend,
                # Spec 30 (D-30-4/6): the credential cipher key for bring-your-own
                # MCP — the factory resolves a persona's assigned BYO servers and
                # connects them SSRF-pinned with the decrypted auth header.
                api_config=config,
                # Spec 33 (D-33-X-creditspolicy-di): the edition's credits policy
                # (metered for cloud, unlimited no-op for community) drives the
                # code_execution deduction.
                credits_policy=app.state.credits_policy,
                # Spec 33 (D-33-X-memory-chroma-community): the edition's typed-
                # memory backend (Chroma for community, Postgres for cloud).
                memory_backend=memory_backend,
                # Spec P5 (P5-D-4): whether the hosted sandbox runs the custom
                # doc-gen template (reportlab + python-pptx present). Computed here
                # from PERSONA_SANDBOX_TEMPLATE; the factory hands it to the
                # document_generation skill assembly so it teaches full pdf/pptx
                # fidelity vs the offline-degrade fallback. Core never reads the env.
                docgen_full_fidelity=sandbox_template_cfg.docgen_full_fidelity,
                # Spec N6 (N6-D-1): the per-tenant image-MCP runtime (None unless cloud +
                # Fly configured + ack). The factory resolves a persona's assigned
                # image-runtime servers through it to per-tenant /mcp URLs (acceptance #4).
                mcp_runtime=mcp_runtime,
            )
            app.state.tier_registry = tier_registry
            app.state.authoring_tier = config.authoring_tier
            app.state.build_conversation_loop = runtime_factory.build_conversation_loop
            app.state.build_agentic_loop = runtime_factory.build_agentic_loop
            app.state.title_builder = runtime_factory.build_title  # auto-title (title tier, R9-020)
            # Spec K2 (T8d): expose the graph store on the factory so the
            # ``record_user_fact`` direct-write tool can merge into the user's
            # graph per request (the request-path graph wiring). Built once on the
            # RLS engine; owner-scoped per request via the checkout listener.
            # ``enable_graph_writes`` self-guards on the ENGINE dialect (K5 +
            # R4-C1-7): on a non-Postgres engine (community-on-SQLite) it leaves the
            # store None instead of composing a Postgres-typed store that 500s on
            # first use — so this call is unconditional and the factory decides.
            # A self-hosted community-on-Postgres deploy still gets the full graph.
            runtime_factory.enable_graph_writes(audit_root=app.state.audit_root)
            # Spec K5: expose that same owner-scoped store to the Memory read/edit
            # routes (one instance, one RLS scope per request). ``None`` when the
            # graph isn't composed (no-Postgres engine) ⇒ the Memory area reads as
            # empty + the nav row gates (the routes fall back gracefully).
            app.state.graph_store = runtime_factory.graph_store
            # Spec M3 (T3a): the shared cost-pricing chain (static + OpenRouter
            # catalog) — the image-gen path reads it as its ``cost_source`` to price
            # the token-metered OpenRouter image model (estimate_catalog fallback
            # when the response carries no usage.cost). Absent on an unwired runtime
            # (community) → the image path uses compute_turn_cost's static default.
            app.state.metadata_resolver = runtime_factory.metadata_resolver
            # Spec A6 (T-seam): the shared approval-resolution service — the ONE live path that
            # completes A3's loop (a parked proposal → floor → verbatim replay → resume). Both the
            # approvals inbox and the chat reply path consume it (one floor, one CAS, one durable
            # record → dual-resolution has exactly one winner). It needs the C0 episodic recorder,
            # so it rides ``memory_backend`` (absent on a keyless boot ⇒ the loop stays inert, like
            # within_runtime_origination). Built once + app-scoped; RLS is per-request via the
            # engine's checkout listener, so the closure matches the build_*_loop idiom.
            if memory_backend is not None:
                from persona_api.services.approval_resolution_service import (
                    ApprovalResolutionService,
                )

                _approval_resolution_service = ApprovalResolutionService(
                    engine=rls_engine,
                    factory=runtime_factory,
                    edition=config.edition,
                    memory_backend=memory_backend,
                    audit_root=app.state.audit_root,
                    audit_logger=app.state.audit_logger,
                )

                def _build_approval_resolver(
                    _svc: ApprovalResolutionService = _approval_resolution_service,
                ) -> ApprovalResolutionService:
                    return _svc

                app.state.build_approval_resolver = _build_approval_resolver
            # R6 (T8): pay the crisis-encoder cold load (~40 s) OFF the event loop at
            # boot, so the first user never pays it (the built-but-inert failure class).
            # Non-blocking: the warm window is fail-soft (encoder score times out → the
            # turn runs lexical-only). The task is held on app.state so it is not GC'd.
            # R9-027 posture: the load rides a dedicated DAEMON thread under a hard
            # deadline (PERSONA_CRISIS_WARMUP_DEADLINE_S, default 120 s) — on deadline
            # the boot serves lexical-only and the load keeps going in the background.
            # /livez and /healthz never consult this task (readiness/liveness never
            # block on warm-up), and the task is cancelled in the finally below so
            # shutdown neither waits for nor joins a stuck load.
            if crisis_encoder is not None:
                app.state.crisis_encoder_warmup = start_crisis_encoder_warmup(crisis_encoder)

    # Spec K2 (T8d): the CONSUMER. Single-process deploy (D-08-5) ⇒ the durable A0
    # worker + A1 scheduler tick run as an in-process background task. Composes the
    # synthesis handler on the wired ``synthesis_tier`` (the eval-re-run gate's
    # tier, NOT frontier) + A1's leader-gated tick, then runs the claim→execute
    # loop alongside the API. Spec K10 (D-K10-7): the switch is now edition-derived —
    # ON by default for community-on-managed-Postgres (so K2 synthesis / K7
    # consolidation / K8 gist actually RUN — real parity), an explicit env flag still
    # wins. The keyless fail-safe is UNTOUCHED: this also requires an RLS engine +
    # tier registry, so a keyless / legacy-sqlite boot keeps the producers' enqueues
    # no-op-consumed. (``start_in_process_worker`` additionally REFUSES a non-Postgres
    # engine — the worker's graph/jobs substrate is Postgres-only.)
    in_process_worker = None
    _worker_tier_registry = getattr(app.state, "tier_registry", None)
    _worker_enabled = config.effective_in_process_worker(
        community_managed=community_db_manager is not None
    )
    if (
        _worker_enabled
        and rls_engine is not None
        and runtime_factory is not None
        and _worker_tier_registry is not None
    ):
        from persona.backends.errors import AuthenticationError

        from persona_api.background.worker_root import start_in_process_worker

        try:
            in_process_worker = start_in_process_worker(
                config=config,
                rls_engine=rls_engine,
                embedder=app.state.embedder,
                tier_registry=_worker_tier_registry,
                # Spec A4 (composition-root activation): the task-leg tenant + digest hook. The leg
                # runner is the SAME AgenticLoop the chat path uses (the no-bypass guarantee); the
                # digest publisher rides the real C0 sender. ``memory_backend`` present → digest is
                # live; absent → the leg still runs, updates are simply not delivered.
                # (R5: audit_root param removed — the worker selects its audit backend from config.)
                runtime_factory=runtime_factory,
                memory_backend=memory_backend,
                # Spec A11: the in-process worker's background deliveries fan out through the
                # SAME channel the SSE endpoint serves (shared process) → an open tab gets
                # message.delivered live; the raw channel also pings the bell live on a
                # committed executor-missing notification (notification.created). A future
                # api/worker split swaps these for the cross-process transport behind the
                # unchanged seams.
                live_sessions=live_sessions,
                event_channel=event_channel,
                # R9-013: the avatar_generation tenant's substrate. Threading the
                # app's composed image backend + file storage lets the worker
                # register the avatar handler under the SAME avatar_queue_ready
                # gate the create route's producer consults — enqueue iff handler.
                image_backend=app.state.image_backend,
                file_storage=app.state.file_storage,
                # R9-025b: the file_extract tenant's substrate — the SAME hosted
                # sandbox pool the live chat path's code_execution tool acquires
                # from (None when no E2B key is configured; the tenant is then
                # simply not registered, paired with the route's own
                # file_extract_queue_ready gate). workspace_root is always set.
                sandbox_pool=sandbox_pool,
                workspace_root=app.state.workspace_root,
            )
        except AuthenticationError:
            # Keyless boot (D-K10-7 auto-off): a default tier registry is ALWAYS built,
            # so the presence guard above can't distinguish "has a usable key" — the
            # synthesis/consolidation backends only fail to authenticate here. Boot
            # worker-less rather than crash; producers' enqueues are no-op-consumed
            # until a model key is configured (a wrong/expired key degrades the same
            # honest way, surfaced by this warning).
            _LOG.warning(
                "in-process worker disabled: no usable model API key (keyless boot); "
                "background synthesis/consolidation/gist will not run until a key is set"
            )
            in_process_worker = None
    app.state.in_process_worker = in_process_worker

    # Spec M2 (D-M2-6 + the F5 closure): warm the OpenRouter catalog + the
    # metadata index OFF the event loop at boot, then keep them TTL-fresh.
    # The turn path NEVER fetches (compute_turn_cost resolves with
    # allow_fetch=False) — this task is what makes the catalog arm of the
    # cost estimator actually serve. No key → no client → no task
    # (static-only chain, nothing to warm). Held on app.state so it is not
    # GC'd (the crisis-warmup precedent); cancelled at shutdown below.
    catalog_refresh_task: asyncio.Task[None] | None = None
    if runtime_factory is not None and runtime_factory.catalog_client is not None:
        from persona_api.services.catalog_freshness import run_catalog_refresh_loop

        catalog_refresh_task = asyncio.create_task(
            run_catalog_refresh_loop(
                runtime_factory.catalog_client,
                runtime_factory.openrouter_resolver,
            ),
            name="m2-catalog-freshness",
        )
        app.state.catalog_refresh_task = catalog_refresh_task

    # Restart sweep (Spec P1, D-P1-restart-sweep): BEFORE serving, reconcile any
    # chat turn / run left non-terminal by a previous process's death (their
    # in-process tasks didn't survive — D-08-5 single worker). Runs on the
    # RLS-bypassing engine (admin on cloud / the community engine on sqlite) so it
    # sees every tenant's rows; idempotent. Makes "viewable, not resumable" honest
    # and stops a reattach from spinning on a dead turn/run. R9-022: this is the
    # between-PROCESS half of orphaned-turn recovery (once, at boot); the
    # between-restarts half is the lazy self-heal in chat_service.start_chat_turn.
    _sweep_engine = admin_engine if admin_engine is not None else rls_engine
    if _sweep_engine is not None:
        reconcile_in_flight_on_startup(engine=_sweep_engine)

    # Spec V14 (D-V14-13 trigger, T5a): the AUTO-REMAP reconciliation the T4b/T4c
    # batch report flagged as unwired — an API-startup pass that re-picks (or,
    # since T5b, RESTORES from per-provider memory) every persona whose stored
    # voice no longer matches the active TTS provider, in EITHER direction
    # (Cartesia → ElevenLabs or the flip back). GUARDED like the
    # crisis-encoder/catalog-freshness warm tasks above: a plain
    # ``asyncio.create_task`` (NEVER awaited here) so it can never delay
    # serving OR shutdown (the R9-027 lesson — no readiness/request path
    # consults it), held on ``app.state`` so it is not GC'd, and cancelled at
    # shutdown below. ``reconcile_voice_assignments`` does one cheap local
    # persona listing every boot (no network) and pays the catalogue-fetch +
    # (memory-restore or model-pick) cost ONLY for a persona actually stale
    # against the active provider — a boot where every persona already
    # matches does zero catalogue fetches, in either direction (see its
    # docstring; the T5a-era provider-name-based skip was removed in T5b
    # because it made rollback lossy — review finding I1).
    voice_remap_task: asyncio.Task[None] | None = None
    if _sweep_engine is not None and rls_engine is not None:
        from persona_api.services.voice_assignment_service import reconcile_voice_assignments

        async def _run_voice_remap_reconciliation() -> None:
            try:
                await reconcile_voice_assignments(
                    config=config,
                    registry=getattr(app.state, "tier_registry", None),
                    sweep_engine=_sweep_engine,
                    rls_engine=rls_engine,
                )
            except Exception:  # noqa: BLE001 — a boot task must never surface/crash
                _LOG.warning("voice remap reconciliation task failed unexpectedly")

        voice_remap_task = asyncio.create_task(
            _run_voice_remap_reconciliation(), name="v14-voice-remap-reconcile"
        )
        app.state.voice_remap_task = voice_remap_task

    try:
        yield
    finally:
        # R9-027: stop AWAITING the crisis-encoder warm-up first. Cancelling the
        # task ends the awaiting side promptly; the load itself (if still running)
        # continues on its daemon thread and is deliberately NEVER joined — neither
        # here nor by the loop's executor teardown — so a stuck HF download can no
        # longer hang TestClient.__exit__ or a real SIGTERM (the 2026-07-11 CI hang).
        crisis_warmup_task = getattr(app.state, "crisis_encoder_warmup", None)
        if crisis_warmup_task is not None:
            crisis_warmup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await crisis_warmup_task
        # Spec M2 (D-M2-6): stop the catalog-freshness poller before the
        # engines/factory close (it only sleeps or runs a to_thread fetch;
        # cancellation is clean at either point).
        if catalog_refresh_task is not None:
            catalog_refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await catalog_refresh_task
        # Spec V14 (T5a): cancel the (likely already-finished, one-shot) voice
        # remap reconciliation pass — cleanly, before the engines it reads/writes
        # through close.
        if voice_remap_task is not None:
            voice_remap_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await voice_remap_task
        # Flush + stop telemetry FIRST so a final drain lands before engines close
        # (R5-D-3; best-effort — never blocks shutdown on a telemetry write).
        if app.state.telemetry_buffer is not None:
            await app.state.telemetry_buffer.aclose()
        if in_process_worker is not None:
            await in_process_worker.aclose()  # drain the synthesis/job loop (K2 T8d)
        if run_registry is not None:
            await run_registry.aclose()  # cancel in-flight run tasks (S08-2)
        if chat_turn_registry is not None:
            await chat_turn_registry.aclose()  # cancel in-flight chat turns (D-P1-restart-sweep)
        if runtime_factory is not None:
            await runtime_factory.aclose()  # tier_registry.aclose() + MCP disconnect (D-05-4)
        if sandbox_pool is not None:
            # Cancels the reaper, drains sessions, closes substrate
            # (D-12-12 Gate 4 mid-exec-kill cleanliness inherits).
            await sandbox_pool.aclose()
        if mcp_runtime is not None:
            await mcp_runtime.aclose()  # Spec N6: cancel the cross-tenant idle-reaper
        if admin_engine is not None:
            admin_engine.dispose()
        if rls_engine is not None:
            rls_engine.dispose()
        # Spec K10 (D-K10-9): stop an embedded managed Postgres AFTER the engine
        # pools are disposed (connections closed first). Best-effort + a no-op in
        # external mode; a teardown failure never crashes shutdown.
        if community_db_manager is not None:
            community_db_manager.stop()


def create_app(config: APIConfig | None = None) -> FastAPI:
    """Build and return the FastAPI application.

    Args:
        config: An :class:`APIConfig`. Defaults to one loaded from the
            environment. Injected in tests to override DSNs/auth knobs.
    """
    config = config or APIConfig()

    # Spec 33 D-33-4: refuse to start a community/no-auth process on a public
    # bind unless explicitly opted in — fail-safe before any collaborator wiring.
    check_public_noauth_guard(config)
    # Spec R2 R2-D-1: refuse to start a misconfigured cloud deploy (authless / superuser
    # request path / no JWT audience). The pure-config asserts run here, before any engine
    # exists; the is_superuser probe leg runs in ``_lifespan`` once the rls_engine is built.
    check_cloud_config_guard(config)
    # Spec N1 D-N1-7: refuse to start cloud with a Docker MCP Gateway URL unless the
    # operator acknowledges the vetted-shared-across-tenants posture (community: no gate).
    check_gateway_edition_posture(config)

    app = FastAPI(
        title="Persona API",
        version="0.8.0",
        summary="Hosted service for building and running typed-memory AI personas.",
        lifespan=_lifespan,
    )
    app.state.config = config

    # Spec 33 (D-33-1): the edition seams, selected once here. Stateless, so set
    # at factory time (available even when the lifespan hasn't run, e.g. unit
    # tests that hit the app without TestClient's lifespan).
    app.state.owner_resolver = build_owner_resolver(config)
    app.state.credits_policy = build_credits_policy(config)
    # R5-D-4: the file-storage backend, set at factory time (stateless, like the
    # edition seams) so routes that read ``app.state.file_storage`` work even when
    # the lifespan hasn't run (tests hitting the app without TestClient's lifespan).
    # The lifespan re-affirms it from ``app.state.workspace_root`` (same value).
    app.state.file_storage = build_file_storage(config, Path(config.workspace_root))

    # Spec R7 (R7-D-4/6): the per-user concurrency caps ride the same edition seam —
    # ``cloud`` enforces the configured caps, ``community`` no-ops (0 = unlimited; the
    # helper takes no lock / writes no row, so the single-owner SQLite self-host path
    # is untouched). Effective values are read from ``app.state`` by the entry points
    # (chat/agentic long-op admission; imagegen/voice bounded slots).
    _cloud = config.edition is Edition.cloud
    app.state.max_concurrent_long_ops = config.max_concurrent_long_ops_per_user if _cloud else 0
    app.state.max_concurrent_bounded_ops = (
        config.max_concurrent_bounded_ops_per_user if _cloud else 0
    )

    register_exception_handlers(app)

    # CORS for the spec-09 web app (browser → API is cross-origin). Bearer auth
    # (no cookies) → allow_credentials=False; expose the rate-limit headers so the
    # browser client can read them. Origins from PERSONA_API_CORS_ORIGINS.
    if config.cors_origins_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.cors_origins_list,
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=[
                "X-RateLimit-Limit",
                "X-RateLimit-Remaining",
                "X-RateLimit-Reset",
                "Retry-After",
            ],
        )

    # Request telemetry (R5-D-3, §6.3). Records one buffered, fail-soft row per
    # request into request_telemetry — off the hot path. Added here; the buffer it
    # writes to is built in the lifespan (present only when a platform engine
    # exists), so the middleware no-ops until then + when telemetry is disabled.
    if config.telemetry_enabled:
        app.add_middleware(RequestTelemetryMiddleware)

    # Routers are registered as they land (T07 personas, T08 conversations,
    # T11 runs, T12 me/health, T13 tools). Kept as an explicit include list so
    # the surface is visible in one place.
    _register_routers(app)

    return app


def _build_rate_limiter(config: APIConfig, rls_engine: Engine | None) -> RateLimiter:
    """Build the §6 limiter: Postgres-backed when an engine exists + configured,
    else in-memory (dev/tests)."""
    per_endpoint = {
        "messages": config.rate_limit_messages,
        "runs": config.rate_limit_runs,
        "author": config.rate_limit_author,
    }
    store: RateLimitStore
    if config.rate_limit_backend == "postgres" and rls_engine is not None:
        store = PostgresRateLimitStore(rls_engine)
    else:
        store = InMemoryRateLimitStore()
    return RateLimiter(store, default_limit=config.rate_limit_default, per_endpoint=per_endpoint)


def _register_routers(app: FastAPI) -> None:
    """Include all route modules. Extended task-by-task in Phase 5."""
    app.include_router(personas.router)
    app.include_router(conversations.router)
    app.include_router(calls.router)  # spec V9: voice-call history
    app.include_router(runs.router)
    app.include_router(me.router)
    app.include_router(health.router)
    app.include_router(tools.router)
    app.include_router(documents.router)
    app.include_router(uploads.router)
    app.include_router(imagegen.router)
    app.include_router(artifacts.router)
    app.include_router(mcp_servers.router)  # spec 30: bring-your-own MCP
    app.include_router(memory.router)  # spec K5: the knowledge-graph UI ("Memory")
    app.include_router(connectors.router)  # spec C6: connector management front-door
    app.include_router(approvals.router)  # spec A6: the approvals inbox (dual-resolution twin)
    app.include_router(autonomy.router)  # spec A6: the morning review (the shared digest)
    app.include_router(tasks.router)  # spec A6: the tasks read surface (list / detail / audit)
    app.include_router(models.router)  # spec M1: model catalog — curated shortlist + browse-all
    app.include_router(voice.router)  # R9-025a: tts/stt proxy — read-aloud + mic dictation

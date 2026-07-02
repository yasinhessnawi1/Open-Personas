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
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from persona.audit import JSONLAuditLogger
from persona.graph import PostgresEntityRegistry, build_graph_store
from persona.graph.postgres import PostgresGraphBackend
from persona.jobs import JobRegistry
from persona.logging import get_logger
from persona_runtime.extraction.synthesizer import build_synthesizer

from persona_api.jobs.catalog_sync import build_catalog_sync
from persona_api.jobs.handlers.synthesis import PgSynthesisRepository, register_synthesis_handler
from persona_api.jobs.queue import JobQueue
from persona_api.jobs.skill_catalog_sync import build_skill_catalog_sync
from persona_api.jobs.worker import build_worker
from persona_api.schedules.store import ScheduleStore
from persona_api.schedules.tick import build_scheduler_tick
from persona_api.tasks.continuation import TaskContinuation
from persona_api.tasks.handler import register_task_leg_handler
from persona_api.tasks.leg_runner import RuntimeFactoryLegRunnerBuilder
from persona_api.tasks.scheduled_fire import register_scheduled_task_fire_handler
from persona_api.tasks.store import CheckpointStore, TaskStore

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import datetime
    from pathlib import Path

    from persona.stores.backend import Backend
    from persona.stores.embedder import Embedder
    from persona_runtime.legs import LegOutcome
    from persona_runtime.tier import TierRegistry
    from sqlalchemy import Engine

    from persona_api.config import APIConfig
    from persona_api.jobs.catalog_sync import CatalogSyncTask
    from persona_api.jobs.skill_catalog_sync import SkillCatalogSyncTask
    from persona_api.jobs.worker import Worker
    from persona_api.schedules.tick import SchedulerTick
    from persona_api.services.runtime_factory import RuntimeFactory

__all__ = ["InProcessWorker", "build_worker_registry", "start_in_process_worker"]

_log = get_logger("api.worker_root")


def build_worker_registry(
    *,
    rls_engine: Engine,
    embedder: Embedder,
    tier_registry: TierRegistry,
    audit_root: Path,
    synthesis_tier: str,
    runtime_factory: RuntimeFactory | None = None,
    memory_backend: Backend | None = None,
    edition: object | None = None,
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
        tier_registry: The app-scoped tier registry; ``get(synthesis_tier)``
            resolves the synthesis backend (fallback ``small → mid → frontier``).
        audit_root: The JSONL audit root the graph store's audit logger writes to.
        synthesis_tier: The tier the extractor + entity judge run on (D-K2-3).
        runtime_factory: The app's runtime factory (the leg runner). ``None`` →
            the ``task_leg`` tenant is NOT registered (A2 legs stay inert — the
            pre-activation posture).
        memory_backend: The edition's memory transport (the digest sender needs it).
            ``None`` → legs still run, digest updates are simply not delivered.
        edition: The open-core edition (the C0 recorder's RLS gate).
    """
    backend = tier_registry.get(synthesis_tier)
    graph_backend = PostgresGraphBackend(engine=rls_engine)
    graph_store = build_graph_store(
        engine=rls_engine,
        embedder=embedder,
        audit_logger=JSONLAuditLogger(audit_root),
    )
    entity_registry = PostgresEntityRegistry(backend=graph_backend, embedder=embedder)
    synthesizer = build_synthesizer(
        graph_store=graph_store, registry=entity_registry, backend=backend
    )
    registry = JobRegistry()
    register_synthesis_handler(registry, runner=synthesizer, repository=PgSynthesisRepository())
    if runtime_factory is not None:
        _register_task_leg_tenant(
            registry,
            rls_engine=rls_engine,
            runtime_factory=runtime_factory,
            memory_backend=memory_backend,
            edition=edition,
            audit_root=audit_root,
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
) -> None:
    """Register the A4 ``task_leg`` handler: leg execution + continuation + digest (Spec A4)."""
    task_store = TaskStore(rls_engine)
    continuation = TaskContinuation(
        task_store=task_store,
        queue=JobQueue(rls_engine),
        checkpoint_store=CheckpointStore(rls_engine),
        # Spec A4 recurrence: the continuation reads the schedule to decide occurrence-complete →
        # WAITING (recurring, more fires) vs task-complete (one-time / exhausted).
        schedule_store=ScheduleStore(rls_engine),
    )
    on_milestone = _build_milestone_hook(
        rls_engine=rls_engine,
        memory_backend=memory_backend,
        edition=edition,
        audit_root=audit_root,
    )
    register_task_leg_handler(
        registry,
        task_store=task_store,
        checkpoint_store=CheckpointStore(rls_engine),
        runner_builder=RuntimeFactoryLegRunnerBuilder(runtime_factory),
        continuation=continuation,
        on_milestone=on_milestone,
    )
    # The A1→A2 bridge: a schedule fire → a task leg at the head-of-fire seq (Spec A4). Without it
    # an origination-created schedule fires a payload the leg handler can't parse (the inert trap).
    register_scheduled_task_fire_handler(
        registry, task_store=task_store, queue=JobQueue(rls_engine)
    )


def _build_milestone_hook(
    *,
    rls_engine: Engine,
    memory_backend: Backend | None,
    edition: object | None,
    audit_root: Path,
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

    from persona_api.approvals.cadence import MessagePriority
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
    )
    publisher = TaskUpdatePublisher(sender=sender)
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
        """Launch the claim→execute loop as a background task. Idempotent."""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._worker.run())
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
    audit_root: Path,
    runtime_factory: RuntimeFactory | None = None,
    memory_backend: Backend | None = None,
) -> InProcessWorker:
    """Compose + start the in-process worker (registry + worker + A1 tick).

    Composes the synthesis registry (+ the A4 ``task_leg`` tenant when a
    ``runtime_factory`` is supplied), builds the :class:`Worker` (its own dispatch
    + RLS engines), wires A1's leader-gated :func:`build_scheduler_tick` additively
    (the worker's loop calls it on its cadence — at most one process actually ticks
    under the advisory lock), starts the loop, and returns the handle for the
    lifespan to drain on shutdown.
    """
    registry = build_worker_registry(
        rls_engine=rls_engine,
        embedder=embedder,
        tier_registry=tier_registry,
        audit_root=audit_root,
        synthesis_tier=config.synthesis_tier,
        runtime_factory=runtime_factory,
        memory_backend=memory_backend,
        edition=config.edition,
    )

    # A1's scheduler tick — additive, leader-gated, built on the SAME two engines
    # the worker creates (dispatch for the leader's held session; RLS for the
    # tick's owner-scoped fires). Passed as the ``build_worker`` composition seam so
    # the worker owns engine lifecycle; the worker's loop calls ``tick.run_once`` on
    # its cadence (at most one process actually ticks under the advisory lock).
    def _tick_builder(dispatch_engine: Engine, tick_rls_engine: Engine) -> SchedulerTick:
        return build_scheduler_tick(
            config, dispatch_engine=dispatch_engine, rls_engine=tick_rls_engine
        )

    # N2 catalog auto-sync — additive, leader-gated, on the worker's cross-tenant dispatch
    # engine. ``build_catalog_sync`` returns None when disabled (PERSONA_MCP_SYNC_ENABLED=false).
    def _catalog_sync_builder(dispatch_engine: Engine) -> CatalogSyncTask | None:
        return build_catalog_sync(config, dispatch_engine=dispatch_engine)

    # S2 skill-catalog auto-sync — same additive, leader-gated shape (distinct key).
    # ``build_skill_catalog_sync`` returns None when disabled (PERSONA_SKILL_SYNC_ENABLED=false).
    def _skill_catalog_sync_builder(dispatch_engine: Engine) -> SkillCatalogSyncTask | None:
        return build_skill_catalog_sync(config, dispatch_engine=dispatch_engine)

    worker = build_worker(
        config,
        registry,
        scheduler_tick_builder=_tick_builder,
        catalog_sync_builder=_catalog_sync_builder,
        skill_catalog_sync_builder=_skill_catalog_sync_builder,
    )
    handle = InProcessWorker(worker)
    handle.start()
    return handle

"""Runtime composition root (spec 08, T10).

Builds the **real** ``ConversationLoop`` / ``AgenticLoop`` per request, wiring
every collaborator (the keystones T08/T11 consume a ``build_*`` closure from
here). The app-scoped ``TierRegistry`` + MCP clients are owned by the lifespan
(T10 startup/shutdown): ``await tier_registry.aclose()`` + ``await
client.disconnect()`` on shutdown (D-05-4 / spec-06 handoff). The loops never
close the registry.

Per request, the four typed stores compose ``PostgresBackend`` over the
**RLS engine** (the checkout listener scopes every store connection to the
tenant — D-08-1), so the loop's memory reads/writes are tenant-isolated.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from persona.audit import AuditLogger, JSONLAuditLogger
from persona.backends.errors import ProviderError, TierNotConfiguredError
from persona.backends.metadata import (
    ChainedModelMetadataResolver,
    OpenRouterModelMetadataResolver,
    StaticModelMetadataResolver,
)
from persona.backends.openrouter_catalog import OpenRouterCatalogClient, catalog_ttl_from_env
from persona.backends.openrouter_passthrough import build_openrouter_passthrough
from persona.config import PersonaCoreConfig
from persona.errors import PersonaNotFoundError
from persona.history import ConversationHistoryManager
from persona.imagegen import make_generate_image_tool
from persona.logging import get_logger
from persona.schema.persona import Persona
from persona.skills import BUILTIN_ROOT, SkillInjector, SkillScanner, make_use_skill_tool
from persona.skills.document_generation import apply_docgen_fidelity
from persona.stores import (
    EpisodicStore,
    IdentityStore,
    SelfFactsStore,
    WorldviewStore,
)
from persona.stores.postgres import PostgresBackend
from persona.tools import (
    build_default_toolbox,
    make_render_diagram_tool,
    make_text_summarize_tool,
)
from persona.tools.mcp.mirror import load_mirror_catalog
from persona_runtime.agentic.loop import AgenticLoop
from persona_runtime.errors import TierNotConfiguredError as RegistryTierNotConfiguredError
from persona_runtime.loop import ConversationLoop
from persona_runtime.prompt import PromptBuilder
from persona_runtime.routing import (
    FirstTokenLatencyTracker,
    IntelligentRouter,
    PolicyRouter,
    tier_for,
)
from sqlalchemy import select, text

from persona_api.approvals.action_executor import ToolboxActionExecutor
from persona_api.db.models import personas as personas_t
from persona_api.editions import MeteredCreditsPolicy
from persona_api.mcp import BuiltinMCPSupervisor
from persona_api.mcp.adoption_policy import vetted_catalog_for_search
from persona_api.sandbox import make_pool_code_execution_tool
from persona_api.services.skill_consent_service import PostgresSkillConsentStore
from persona_api.services.workspace_persister import WorkspaceDirPersister

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from pathlib import Path

    from persona.backends import ChatBackend
    from persona.graph.fusion import HybridResult
    from persona.graph.protocol import GraphStore
    from persona.imagegen import ImageBackend
    from persona.sandbox.result import SandboxFile
    from persona.schedules import QuietHours
    from persona.schedules.reader import ScheduleReader
    from persona.schema.chunks import PersonaChunk
    from persona.stores.backend import Backend
    from persona.stores.core_memory import CoreMemoryStore
    from persona.stores.embedder import Embedder
    from persona.stores.protocol import MemoryStore
    from persona.tasks.reader import TaskStateReader
    from persona.tools.mcp.catalog import MCPCatalog, MCPServerCatalogEntry
    from persona.tools.mcp.client import MCPClient
    from persona.tools.toolbox import Toolbox
    from persona_runtime.crisis_encoder import CrisisScorer
    from persona_runtime.graph_selection import GatingContext
    from persona_runtime.initiative.verbs import InitiativeVerbInterpreter
    from persona_runtime.logging import TurnLogWriter
    from persona_runtime.prompt import GraphContext
    from persona_runtime.task_origination import (
        AmendmentInterpreter,
        RescheduleInterpreter,
        StandingIntentRecognizer,
        SteeringInterpreter,
    )
    from persona_runtime.tier import TierRegistry
    from persona_runtime.unified_recall import UnifiedProjection
    from sqlalchemy import Engine

    from persona_api.config import APIConfig
    from persona_api.editions import CreditsPolicy
    from persona_api.mcp.runtime import PerTenantMCPRuntime
    from persona_api.sandbox.pool import SandboxPool
    from persona_api.storage import FileStorage

__all__ = ["RuntimeFactory"]

_logger = get_logger("api.runtime_factory")


class RuntimeFactory:
    """Composes per-request loops; owns the app-scoped registry + MCP clients.

    One instance lives on ``app.state`` for the process. ``build_conversation_loop``
    / ``build_agentic_loop`` are the closures the routes call per request (the
    persona is loaded + stores built under the active RLS scope).
    """

    def __init__(
        self,
        *,
        rls_engine: Engine,
        embedder: Embedder,
        tier_registry: TierRegistry,
        free_tier_registry: TierRegistry | None = None,
        turn_log_writer: TurnLogWriter,
        audit_root: Path,
        audit_logger: AuditLogger | None = None,
        file_storage: FileStorage | None = None,
        core_config: PersonaCoreConfig | None = None,
        sandbox_pool: SandboxPool | None = None,
        workspace_root: Path | None = None,
        image_backend: ImageBackend | None = None,
        api_config: APIConfig | None = None,
        credits_policy: CreditsPolicy | None = None,
        memory_backend: Backend | None = None,
        docgen_full_fidelity: bool = False,
        crisis_encoder: CrisisScorer | None = None,
        mcp_runtime: PerTenantMCPRuntime | None = None,
    ) -> None:
        """Composition root for per-request loops.

        Args:
            rls_engine: The RLS-scoped SQLAlchemy engine (D-08-1).
            embedder: Persona-memory embedder (D-08-8).
            tier_registry: App-scoped tier registry; closed on shutdown.
            turn_log_writer: Per-turn log sink (D-08-7).
            audit_root: Root directory for JSONL audit files (CLI / fallback).
            core_config: Persona-core runtime config; defaults to env-derived.
            sandbox_pool: Hosted code-execution pool (Spec 12). ``None`` when
                ``E2B_API_KEY`` is unset; the ``code_execution`` tool is
                then absent from the toolbox.
            workspace_root: Per-persona workspace root (Spec 17
                D-17-X-bytes-persistence). ``None`` disables produced-file
                persistence + ``intermediate/*`` cross-turn staging.
            image_backend: Image-generation backend (Spec 15 T16, Spec 25
                §2.9 wiring). ``None`` when ``PERSONA_IMAGEGEN_API_KEY`` is
                unset OR construction failed; the ``generate_image`` tool is
                then absent from the toolbox (mirrors the sandbox_pool
                graceful-absence shape — D-12-5 / D-15-X). When non-None,
                ``_build_toolbox`` composes ``make_generate_image_tool`` so
                the persona's runtime can dispatch image generation.
        """
        self._engine = rls_engine
        self._embedder = embedder
        # R6 (R6-D-3/5): the app-scoped crisis encoder, injected into every chat/agentic
        # loop this factory builds so ALL request paths route through the SAME composed
        # ``classify_user_message`` (lexical ∪ encoder). ``None`` ⇒ lexical-only (V11) —
        # e.g. an edition with the encoder disabled. Warmed off-loop at boot (app lifespan).
        self._crisis_encoder = crisis_encoder
        # Spec 33 (D-33-X-creditspolicy-di): the code_execution credit deduction
        # flows through the injected policy. Defaults to the metered policy so a
        # RuntimeFactory built without an explicit policy keeps today's behavior.
        self._credits_policy: CreditsPolicy = credits_policy or MeteredCreditsPolicy()
        # Spec 33 (D-33-X-memory-chroma-community): the edition's typed-memory
        # transport. None ⇒ PostgresBackend built per-request (today's behavior).
        self._memory_backend = memory_backend
        self._tier_registry = tier_registry
        # Spec M4 (T5a): the free plan's dedicated free-only tier registry (D-M4-4). None
        # ⇒ plan gating is OFF (community / no free set configured) → every user resolves
        # the paid ``_tier_registry`` + the preferred_model passthrough, byte-identical to
        # pre-M4. When present (cloud), a FREE user resolves THIS registry (whose whole
        # fallback chain is free-only) + NO preferred override — a free user can never
        # reach a paid model. See :meth:`_plan_tier_selection`.
        self._free_tier_registry = free_tier_registry
        self._turn_log_writer = turn_log_writer
        self._audit_root = audit_root
        # R5-D-2: the app-selected audit backend (Postgres when multi-worker,
        # else JSONL). None ⇒ CLI / legacy callers get the byte-unchanged JSONL
        # default via ``_resolve_audit_logger`` — no behaviour change for them.
        self._audit_logger = audit_logger
        # R5-D-4: the storage backend the chat-path workspace persister writes
        # produced artifacts through. None ⇒ persistence disabled (CLI / test).
        self._file_storage = file_storage
        self._core_config = core_config or PersonaCoreConfig()
        # Spec 12 T10 — hosted sandbox pool. None when E2B_API_KEY is unset
        # (dev environments without an account boot cleanly); the
        # ``code_execution`` tool is absent from the toolbox in that case
        # and surfaces ``SandboxUnavailableError`` if the model still calls it.
        self._sandbox_pool = sandbox_pool
        # Spec P5 (P5-D-4) — whether the hosted sandbox runs the custom doc-gen
        # template (reportlab + python-pptx baked in). Computed by the composition
        # root from PERSONA_SANDBOX_TEMPLATE and passed in; persona-core never reads
        # the env. Consumed when composing the ``document_generation`` skill so it
        # teaches full pdf/pptx fidelity vs the spec-A offline-degrade fallback.
        # Default False ⇒ the safe degrade path (community/default, or no template).
        self._docgen_full_fidelity = docgen_full_fidelity
        # Spec 17 D-17-X-bytes-persistence — workspace root for produced-file
        # persist + intermediate/* cross-turn staging. Threaded through to
        # ``make_pool_code_execution_tool``. None (test / CLI path) ⇒ no
        # persistence + no staging; tool dispatches as before.
        self._workspace_root = workspace_root
        # Spec 15 T16 + Spec 25 §2.9 — image-generation backend. None when no
        # provider is configured OR construction failed; the
        # ``generate_image`` tool is absent in that case (same graceful-
        # absence pattern as ``sandbox_pool``). Composed into the toolbox
        # in ``_build_toolbox`` so the persona's chat runtime can dispatch
        # ``generate_image`` calls — closes the wiring gap diagnosed in
        # Spec 25 §2.9 where ``make_generate_image_tool`` existed in core
        # but was never called from the API composition root.
        self._image_backend = image_backend
        # Spec 30 (D-30-4/6) — APIConfig for the bring-your-own MCP credential
        # cipher. ``None`` (CLI / tests) ⇒ BYO servers are not wired (no key to
        # decrypt their credentials). The runtime resolves a persona's ASSIGNED
        # BYO servers (D-30-6), connects them SSRF-pinned (enforce_ssrf=True),
        # and merges their tools into the toolbox.
        self._api_config = api_config
        # Spec N6 (N6-D-1): the per-tenant image-MCP runtime (a Fly Machine per
        # (tenant, server)). ``None`` on the CLI / community / when unconfigured ⇒
        # image-runtime servers are simply not connected (the not-connected signal,
        # T6, reports them). When present, an assigned image-runtime server is
        # resolved THROUGH it to a per-tenant ``/mcp`` URL and connected via the
        # existing N4 client path UNCHANGED (acceptance #4).
        self._mcp_runtime = mcp_runtime
        # MCP clients accumulated across requests, closed on shutdown.
        self._mcp_clients: list[MCPClient] = []
        # Spec 27 (D-27-3) — app-scoped lazy supervisor for built-in MCP servers.
        # Construction spawns NOTHING; a server boots on first resolution of an
        # ``mcp:<server>:`` tool in ``_build_toolbox`` and is reaped at shutdown.
        self._builtin_mcp = BuiltinMCPSupervisor(
            self._core_config.mcp_builtin_enabled_parsed,
            child_uid=self._core_config.mcp_builtin_uid,
        )
        # Spec 23 T13: app-scoped intelligent-routing collaborators, wired into
        # every per-request loop. Both are stateless w.r.t. persona; the
        # FirstTokenLatencyTracker is deliberately app-scoped so its per-model
        # EWMA persists across requests (D-18-X-first-token-measurement-impl /
        # D-23-6). Per-persona ``routing.intelligent.enabled`` gates whether the
        # loop actually consults the router — existing personas (default off)
        # route byte-identically (criterion 11).
        self._latency_tracker = FirstTokenLatencyTracker()
        # Spec M2 (D-M2-1): the metadata resolver chain is built UNCONDITIONALLY
        # — hoisted out of ``_build_intelligent_router`` because turn-cost
        # estimation reads it on EVERY turn, while the routing flag below only
        # gates the router CONSUMER. One shared instance serves both the
        # IntelligentRouter and every loop's ``cost_source``. The catalog client
        # handle is kept for the D-M2-6 TTL/off-loop-warm path (``None`` when no
        # ``PERSONA_OPENROUTER_API_KEY`` — static-only chain, zero network).
        self._catalog_client, self._openrouter_resolver, self._metadata_resolver = (
            self._build_metadata_resolver()
        )
        # Spec P9 (P9-D-4/D-7): the Spec-23 scorer is DORMANT by default — the
        # global gate (``PERSONA_ROUTING_INTELLIGENT_ENABLED``, default off) is
        # consulted BEFORE the persona flag ever is. The stored per-persona
        # ``intelligent.enabled: true`` is a web-form artifact (written
        # unset-as-enabled), not a deliberate choice — it only means something
        # when an operator turns the global gate on. NB re-enable prerequisite:
        # repopulate the model-metadata tables first, or every pick silently
        # degrades to rule-based slot-0 (metadata coverage: M2-T1 added the
        # deployed anthropic/groq rows; re-verify before flipping the gate).
        self._intelligent_router = (
            self._build_intelligent_router(
                tier_registry, self._latency_tracker, self._metadata_resolver
            )
            if api_config is not None and getattr(api_config, "routing_intelligent_enabled", False)
            else None
        )
        # Spec K2 (T8d) — the user-scoped graph store for the ``record_user_fact``
        # direct-write tool (D-K2-1). ``None`` until ``enable_graph_writes`` composes
        # it (the lifespan calls it once an RLS engine is present). Built once on the
        # RLS engine; owner-scoped per request via the checkout listener. ``None`` ⇒
        # the tool is absent (graceful-absence shape — like sandbox_pool / image).
        self._graph_store: GraphStore | None = None
        # Whether to compose the on-by-default ``record_user_fact`` tool when a
        # graph store is present (the per-persona allow-list is still the final gate).
        self._record_user_fact_enabled = (
            getattr(api_config, "record_user_fact_enabled", True)
            if api_config is not None
            else True
        )

    def _resolve_audit_logger(self) -> AuditLogger:
        """The app-selected audit backend (R5-D-2), or the JSONL default.

        Returns the injected logger (Postgres in a multi-worker deploy) when the
        composition root supplied one, else a fresh ``JSONLAuditLogger`` on the
        audit root — the historical, byte-unchanged default for CLI / tests.
        """
        return self._audit_logger or JSONLAuditLogger(self._audit_root)

    def enable_graph_writes(self, *, audit_root: Path) -> None:
        """Compose the user-scoped graph store for ``record_user_fact`` (Spec K2 T8d).

        Built once on the RLS engine: the checkout listener scopes every connection
        to the request owner via the ``current_user_id`` contextvar, so the single
        shared store is owner-correct per request without a per-request rebuild. The
        ``record_user_fact`` tool's ``owner_provider`` reads the same contextvar at
        DISPATCH time, so an unbound (non-request) call fails closed. Idempotent.
        """
        if self._graph_store is not None:
            return
        # The K0 graph schema (``graph/_schema.py``) is Postgres-only — JSONB,
        # pgvector ``Vector``, ``TSVECTOR`` + HNSW indexes have no SQLite equivalent
        # (``create_all`` won't even compile the DDL on SQLite). On a non-Postgres
        # engine (community-on-SQLite, or any no-graph config) there is no usable
        # graph store, so leave ``_graph_store`` None — the documented
        # graceful-absence shape: ``record_user_fact`` stays absent (K2) and the
        # Memory routes read empty / the nav is gated (K5), instead of composing a
        # Postgres-typed store over SQLite that 500s ("no such table: graph_nodes")
        # on first use. Gates on the ENGINE, not the edition (supersedes the R4-C1-7
        # edition gate — self-hosted community-on-Postgres still gets the full graph);
        # graph RETRIEVAL is separately fail-soft-to-memoryless (R4-C1-7).
        if self._engine.dialect.name != "postgresql":
            _logger.info(
                "graph store unavailable on a %s engine — record_user_fact absent + "
                "Memory reads empty (the K0 graph is Postgres-only)",
                self._engine.dialect.name,
            )
            return
        from persona.audit import JSONLAuditLogger
        from persona.graph import build_graph_store

        # R5-D-2: prefer the app-selected backend; ``audit_root`` remains the
        # JSONL fallback for legacy callers (see ``_resolve_audit_logger``).
        self._graph_store = build_graph_store(
            engine=self._engine,
            embedder=self._embedder,
            audit_logger=self._audit_logger or JSONLAuditLogger(audit_root),
        )
        _logger.info("graph writes enabled (record_user_fact composed into toolboxes)")

    @property
    def graph_store(self) -> GraphStore | None:
        """The composed user-scoped graph store (``None`` until ``enable_graph_writes``).

        Exposed read-only so the Memory HTTP routes (Spec K5) read and edit through the
        same owner-scoped store the runtime writes/retrieves with — one instance, one
        scope (RLS + the ``current_user_id`` contextvar per request).
        """
        return self._graph_store

    def _build_graph_allowlist_provider(
        self, store: GraphStore
    ) -> Callable[[GatingContext], set[str] | None]:
        """The K4 allowlist provider over a graph store (K4-D-2) — shared by K3 + K9 paths.

        The gate-eligible flagged nodes the provider gates over: the owner's wellbeing-tagged
        nodes narrowed to the gate-eligible categories, each with its recency band. Most owners
        have none → the provider returns ``None`` (no subtraction) → the hot path stays free.
        Single-sourced so the chat graph path (K3) and the unified recall (K9) never fork the gate.
        """
        from datetime import UTC, datetime

        from persona.wellbeing_policy import is_gate_eligible, parse_category
        from persona_runtime.graph_selection import recency_bucket
        from persona_runtime.wellbeing import FlaggedNode, make_allowlist_provider, recency_band

        def flagged(owner_id: str) -> list[FlaggedNode]:
            now = datetime.now(UTC)
            out: list[FlaggedNode] = []
            for node in store.flagged_nodes(owner_id):
                category = parse_category(node.wellbeing_category)
                if category is None or not is_gate_eligible(category):
                    continue
                out.append(
                    FlaggedNode(
                        node_id=node.id,
                        category=category,
                        recency=recency_band(recency_bucket(node, now)),
                        text=f"{node.concept_name} {node.content}",
                    )
                )
            return out

        return make_allowlist_provider(
            flagged_nodes=flagged,
            owner_node_ids=lambda owner_id: set(store.node_ids_for_owner(owner_id)),
        )

    def _build_graph_retrieval(self) -> Callable[[str], GraphContext] | None:
        """The owner-scoped graph-knowledge retrieval for the chat loop (K3).

        Reuses the K2 graph store + the ``current_user_id`` owner provider (the
        same one ``record_user_fact`` writes through), so reads and writes share
        one owner scope. Graph reads are on whenever the store is composed — the
        shared-graph thesis, mirroring writes; ``None`` (no store) ⇒ the loop runs
        zero-graph (additive, byte-identical). The owner is resolved per turn at
        dispatch, so a non-request call fails closed.
        """
        if self._graph_store is None:
            return None
        from persona.graph.config import GraphSettings
        from persona.graph.retrieval import HybridRetriever
        from persona_runtime.graph_selection import make_graph_retrieval
        from persona_runtime.graph_window import get_recent_window
        from persona_runtime.prompt import GraphContext

        from persona_api.middleware.rls_context import current_user_id

        settings = GraphSettings()
        retriever = HybridRetriever(store=self._graph_store, settings=settings)
        allowlist_provider = self._build_graph_allowlist_provider(self._graph_store)
        # The recent-window source: the per-turn ContextVar every conversational loop sets
        # before retrieval (K4-D-X-gating-signal-seam) — so the gate reads the conversation,
        # not the bare query (no uncanny re-closing). Unset ⇒ empty ⇒ query-only (fail-safe).
        inner = make_graph_retrieval(
            retriever=retriever,
            owner_provider=current_user_id.get,
            settings=settings,
            allowlist_provider=allowlist_provider,
            recent_window_provider=get_recent_window,
        )

        # R4-C1-7 fail-soft: a graph-read failure must degrade the turn to
        # MEMORYLESS, never kill it — zero-graph is the loop's designed additive
        # path, so an empty context is always safe. Before this wrap, any store
        # error (a Postgres blip in cloud; the missing tables a misconfigured
        # community build hits) propagated out of the detached worker and failed
        # the whole turn. Mirrors V13's timeout⇒memoryless posture in voice.
        def safe_retrieval(query: str) -> GraphContext:
            try:
                return inner(query)
            except Exception:  # noqa: BLE001 — memoryless beats turn-fatal
                _logger.opt(exception=True).warning(
                    "graph retrieval failed; turn degrades to zero-graph"
                )
                return GraphContext()

        return safe_retrieval

    def _memory_backend_for(self) -> Backend:
        """The edition's memory backend (Postgres cloud / Chroma community) — shared builder."""
        return self._memory_backend or PostgresBackend(engine=self._engine, embedder=self._embedder)

    def _build_core_store(self) -> CoreMemoryStore:
        """The K9 core-memory store over the edition backend (K9-D-10)."""
        from persona.stores.core_memory import CoreMemoryStore

        return CoreMemoryStore(
            backend=self._memory_backend_for(), audit_logger=self._resolve_audit_logger()
        )

    def _build_core_block_provider(self, persona_id: str) -> Callable[[], str | None] | None:
        """The turn-path READER of the always-in-context core block (K9-D-10; acceptance-7).

        Reads the persona's current block (background-refreshed elsewhere — never built here).
        Gated by ``core_enabled``; fail-soft (any store error ⇒ no block, byte-identical).
        """
        from persona.recall.config import RecallSettings
        from persona.recall.core_memory import read_core_block

        if not RecallSettings().core_enabled:
            return None
        store = self._build_core_store()

        def provider() -> str | None:
            try:
                block = read_core_block(store, persona_id)
            except Exception:  # noqa: BLE001 — a nicety; never break a turn
                _logger.opt(exception=True).warning("core-block read failed; omitted")
                return None
            return block.text if block is not None else None

        return provider

    def _build_unified_recall(self, persona_id: str) -> Callable[[str], UnifiedProjection] | None:
        """The K9 unified recall for the chat/voice loop (K9-D-1/D-11), env-gated OFF by default.

        Fuses the K8 pyramid AND the K7 graph into one reranked+gated path, projected into the
        existing ``episodic`` / ``graph`` seam. Returns ``None`` (today's two-path recall) unless
        ``unified_enabled`` is flipped. The reranker is the fail-soft shell over the CPU
        cross-encoder scorer (``rerank_enabled``-gated; absent/failed ⇒ fused order, the D-12
        stub-safe path — P7 later swaps a stronger model behind the same seam); K4 gating is the
        SAME allowlist the graph path uses (single-sourced). Owner is resolved per turn
        (fail-closed graph scope).
        """
        from persona.graph.config import GraphSettings
        from persona.graph.retrieval import HybridRetriever
        from persona.recall.config import RecallSettings
        from persona.recall.rerank import build_reranker
        from persona.recall.scorer import build_scorer
        from persona.stores.lifecycle import EpisodicSettings
        from persona_runtime.graph_window import get_recent_window
        from persona_runtime.recall_adapters import GraphNeighbourProvider, PyramidEpisodeProvider
        from persona_runtime.unified_recall import make_unified_recall

        from persona_api.middleware.rls_context import current_user_id

        settings = RecallSettings()
        if not settings.unified_enabled:
            return None

        backend = self._memory_backend_for()
        episodic = EpisodicStore(backend=backend, audit_logger=self._resolve_audit_logger())

        def episodic_query(q: str, k: int) -> list[PersonaChunk]:
            return episodic.query(persona_id, q, k)

        def gist_query(q: str, k: int) -> list[PersonaChunk]:
            return episodic.pyramid.query(persona_id, q, k)

        graph_retrieve: Callable[[str], list[HybridResult]] | None = None
        neighbour_provider: GraphNeighbourProvider | None = None
        allowlist_provider: Callable[[GatingContext], set[str] | None] | None = None
        if self._graph_store is not None:
            retriever = HybridRetriever(store=self._graph_store, settings=GraphSettings())
            allowlist_provider = self._build_graph_allowlist_provider(self._graph_store)
            neighbour_provider = GraphNeighbourProvider(self._graph_store, current_user_id.get)

            def graph_retrieve(q: str) -> list[HybridResult]:
                owner = current_user_id.get()
                return retriever.retrieve(owner, q) if owner else []

        # The CPU cross-encoder scorer (persona.recall.scorer) behind the chat sync
        # deadline: process-shared, lazy-loaded on the first rerank inside the
        # DeadlineReranker's worker thread. ``rerank_enabled`` off or construction
        # failure ⇒ ``None`` ⇒ the fused-order fail-soft shell (byte-identical to
        # the D-12 stub path).
        reranker = build_reranker(
            scorer=build_scorer(settings),
            settings=settings,
            timeout_s=settings.rerank_timeout_ms_chat / 1000.0,
        )
        return make_unified_recall(
            reranker=reranker,
            settings=settings,
            episodic_settings=EpisodicSettings(),
            persona_id=persona_id,
            owner_provider=current_user_id.get,
            episodic_query=episodic_query,
            resolve_display=lambda chunks: episodic.resolve_display(persona_id, list(chunks)),
            gist_query=gist_query,
            graph_retrieve=graph_retrieve,
            episode_provider=PyramidEpisodeProvider(episodic.pyramid, persona_id),
            neighbour_provider=neighbour_provider,
            allowlist_provider=allowlist_provider,
            recent_window_provider=get_recent_window,
            rerank_top_k=settings.rerank_top_k_chat,
        )

    def _build_user_name_provider(self) -> Callable[[], str | None]:
        """The per-turn display-name resolver for the chat loop (Spec K6, K6-D-6).

        Reads OUR ``users`` table (DB the source of truth; Clerk auth-only, never read
        per-request) via the ``current_user_id`` contextvar — the same owner scope as
        graph retrieval. Fail-soft: no owner (non-request call), no row, or any DB
        error ⇒ ``None`` ⇒ the prompt omits the name line (byte-identical, null-safe).
        The name is a nicety and must never break a turn.
        """
        from persona_api.middleware.rls_context import current_user_id
        from persona_api.services import user_service

        engine = self._engine

        def provider() -> str | None:
            owner_id = current_user_id.get()
            if not owner_id:
                return None
            try:
                profile = user_service.get_user_profile(engine, user_id=owner_id)
            except Exception:  # noqa: BLE001 — the name is a nicety; never fail a turn
                _logger.opt(exception=True).warning("user-name resolution failed (non-fatal)")
                return None
            if profile is None:
                return None
            return user_service.compose_display_name(
                cast("str | None", profile.get("first_name")),
                cast("str | None", profile.get("last_name")),
            )

        return provider

    def _build_self_node_sync(self) -> Callable[[str | None], None] | None:
        """The lazy SELF-node create/sync trigger for the chat loop (Spec K6, K6-D-4).

        The runtime prompt path is the SELF node's creation trigger (the K6-D-4
        addendum): once the user has a name we ensure their central ``SELF`` node
        exists and tracks that name (idempotent, race-safe). ``None`` when no graph
        store is composed (byte-identical). Skips a nameless turn — the anchor is
        materialised only once a name exists, so it is born correctly named rather
        than generic-then-renamed. Fail-soft: a sync failure never perturbs the turn.
        """
        if self._graph_store is None:
            return None
        from persona_api.middleware.rls_context import current_user_id

        store = self._graph_store

        def sync(display_name: str | None) -> None:
            if not display_name:
                return  # materialise the self node only once a name exists
            owner_id = current_user_id.get()
            if not owner_id:
                return
            try:
                store.get_or_create_self_node(owner_id, display_name=display_name)
            except Exception:  # noqa: BLE001 — foundation upkeep; never fail a turn
                _logger.opt(exception=True).warning("self-node sync failed (non-fatal)")

        return sync

    @property
    def catalog_client(self) -> OpenRouterCatalogClient | None:
        """The shared OpenRouter catalog client (Spec M2, D-M2-6), or ``None``.

        Read by the api lifespan's catalog-freshness task (warm at boot +
        periodic ``refresh_if_stale`` off the event loop). ``None`` when no
        ``PERSONA_OPENROUTER_API_KEY`` is configured — static-only chain,
        nothing to warm or refresh.
        """
        return self._catalog_client

    @property
    def openrouter_resolver(self) -> OpenRouterModelMetadataResolver | None:
        """The chain's OpenRouter link (Spec M2, D-M2-6), or ``None``.

        Read by the api lifespan's catalog-freshness task so a refreshed
        catalog is followed by a derived-index rebuild (``reindex`` — never a
        second fetch). Same ``None`` condition as :attr:`catalog_client`.
        """
        return self._openrouter_resolver

    @property
    def metadata_resolver(self) -> ChainedModelMetadataResolver:
        """The shared cost-pricing chain (static + optional OpenRouter catalog).

        The SAME instance every loop is built with as its ``cost_source`` (Spec
        M2, D-M2-1). Spec M3 (T3a): exposed on ``app.state.metadata_resolver`` so
        the image-gen path can price a token-metered OpenRouter image model
        (``openai/gpt-5.4-image-2``) — the catalog link supplies the
        ``estimate_catalog`` fallback when the response carries no ``usage.cost``.
        """
        return self._metadata_resolver

    @staticmethod
    def _build_metadata_resolver() -> tuple[
        OpenRouterCatalogClient | None,
        OpenRouterModelMetadataResolver | None,
        ChainedModelMetadataResolver,
    ]:
        """Compose the shared metadata chain (static + optional OpenRouter catalog).

        Spec M2 (D-M2-1): hoisted OUT of ``_build_intelligent_router`` so the
        chain exists regardless of the routing flag — the loops' resolver-backed
        cost estimates (``compute_turn_cost``) read it on every turn. The static
        per-provider tables are always available (the authoritative, offline
        source); when ``PERSONA_OPENROUTER_API_KEY`` is set, the OpenRouter
        catalog is added as the broad-coverage fallback
        (D-23-X-resolver-precedence: static-authoritative-on-overlap).

        Catalog client construction is network-free (D-22-11). The first fetch
        belongs to the lifespan warm / D-M2-6 TTL path — NEVER the turn path:
        ``compute_turn_cost`` resolves with ``allow_fetch=False``, so a cold
        index is an honest miss (static / unpriced), not a blocking fetch. The
        client carries the D-M2-6 TTL (``catalog_ttl_from_env``); the OR link
        is returned alongside so the freshness task can ``reindex`` it.
        """
        import os

        client: OpenRouterCatalogClient | None = None
        openrouter: OpenRouterModelMetadataResolver | None = None
        api_key = os.environ.get("PERSONA_OPENROUTER_API_KEY", "").strip()
        if api_key:
            base_url = os.environ.get("PERSONA_OPENROUTER_BASE_URL", "").strip() or None
            client = OpenRouterCatalogClient(
                api_key, base_url=base_url, ttl_s=catalog_ttl_from_env()
            )
            openrouter = OpenRouterModelMetadataResolver(client)
        resolver = ChainedModelMetadataResolver(
            static=StaticModelMetadataResolver(), openrouter=openrouter
        )
        return client, openrouter, resolver

    @staticmethod
    def _build_intelligent_router(
        tier_registry: TierRegistry,
        latency_tracker: FirstTokenLatencyTracker,
        resolver: ChainedModelMetadataResolver,
    ) -> IntelligentRouter:
        """Compose the IntelligentRouter over the SHARED metadata chain (D-M2-1).

        The resolver is the factory-scoped instance ``_build_metadata_resolver``
        composed — the same one every loop's cost path reads — so routing and
        pricing can never disagree about a model's metadata. The shared
        ``latency_tracker`` lets the router consult live per-model latency
        (D-23-6).
        """
        return IntelligentRouter(
            tier_registry=tier_registry,
            metadata_resolver=resolver,
            latency_tracker=latency_tracker,
        )

    def _build_day_spent_cents_provider(self, persona_id: str | None) -> Callable[[], float] | None:
        """The soft per-day cost-bias ramp's real cross-session spend source (R7-D-1).

        Discharges D-23-X: sums today's recorded ``turn_logs.cost_cents`` for the
        request owner's conversations with this persona over the current UTC day —
        the DURABLE cross-session number the ramp needs (replacing the deferred
        ``0.0``). Bound to the request owner at DISPATCH time via the
        ``current_user_id`` contextvar (the K3/K4 owner-provider pattern), so one
        provider serves every turn under the request's RLS scope.

        FAIL-SOFT to ``0.0`` on any error / no bound owner / no persona (= the pre-R7
        behaviour): a routing bias must never crash or perturb a turn, and the
        community/no-DB path is unaffected. Feeds the SOFT ramp only — the HARD
        per-day guard is R7's credits day-cap (``book_day_spend``); the ramp reads
        ``turn_logs`` (cents), NOT the ``day_spend`` credits counter (units differ).
        """
        if persona_id is None:
            return None
        from persona_api.middleware.rls_context import current_user_id

        engine = self._engine

        def _provider() -> float:
            owner = current_user_id.get()
            if not owner:
                return 0.0
            try:
                return self._sum_day_spent_cents(engine, owner_id=owner, persona_id=persona_id)
            except Exception:
                _logger.debug("day_spent_cents provider failed; ramp reads 0.0 (fail-soft)")
                return 0.0

        return _provider

    @staticmethod
    def _sum_day_spent_cents(engine: Engine, *, owner_id: str, persona_id: str) -> float:
        """Sum ``turn_logs.cost_cents`` for owner+persona over the current UTC day.

        The durable cross-session per-day spend the soft ramp reads (R7-D-1). Range
        predicate on ``created_at`` (``>= UTC-midnight-today``) is sargable via
        ``idx_turn_logs_conversation_created``; the persona/owner scope rides the
        ``conversations`` join. Extracted for direct testing of the aggregate.
        """
        with engine.begin() as conn:
            total = conn.execute(
                text(
                    "SELECT COALESCE(SUM(tl.cost_cents), 0.0) FROM turn_logs tl "
                    "JOIN conversations c ON c.id = tl.conversation_id "
                    "WHERE c.owner_id = :owner AND c.persona_id = :persona "
                    "AND tl.created_at >= "
                    "date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"
                ),
                {"owner": owner_id, "persona": persona_id},
            ).scalar_one()
        return float(total or 0.0)

    # -- shared per-request pieces ------------------------------------------

    def _load_persona(self, persona_id: str) -> Persona:
        """Load + validate the persona's YAML from the RLS-scoped row (→ 404)."""
        with self._engine.begin() as conn:
            row = (
                conn.execute(select(personas_t.c.yaml).where(personas_t.c.id == persona_id))
                .mappings()
                .first()
            )
        if row is None:
            raise PersonaNotFoundError("persona not found", context={"id": persona_id})
        import yaml

        raw = yaml.safe_load(str(row["yaml"]))
        if isinstance(raw, dict):
            raw["persona_id"] = persona_id
        return Persona.model_validate(raw)

    def _build_stores(self) -> dict[str, MemoryStore]:
        """The four typed stores over the edition's memory backend.

        Cloud uses ``PostgresBackend`` (RLS-scoped engine); community injects a
        ``ChromaBackend`` (file-based) — Spec 33 D-33-X-memory-chroma-community.
        When no backend is injected, defaults to ``PostgresBackend`` (today's
        behavior) so existing callers are unaffected.
        """
        backend = self._memory_backend or PostgresBackend(
            engine=self._engine, embedder=self._embedder
        )
        audit = self._resolve_audit_logger()
        return {
            "identity": IdentityStore(backend=backend, audit_logger=audit),
            "self_facts": SelfFactsStore(backend=backend, audit_logger=audit),
            "worldview": WorldviewStore(backend=backend, audit_logger=audit),
            "episodic": EpisodicStore(backend=backend, audit_logger=audit),
        }

    async def _build_toolbox(
        self,
        persona: Persona,
        scanned_skills: list[object],
        deferred_input_files_holder: list[SandboxFile] | None = None,
    ) -> object:
        """Build the toolbox (+ use_skill when the persona has skills + code_execution
        when the sandbox pool is configured). MCP clients are tracked for shutdown.

        ``deferred_input_files_holder`` is the Spec 16 M1a shared bucket:
        a mutable ``list[SandboxFile]`` the caller passes in BEFORE the loop
        is constructed (the loop will share the same list as its
        ``deferred_input_files`` attribute). The ``code_execution`` tool's
        ``deferred_input_files_provider`` callable is a drain-and-clear
        closure over the same list — when the runtime appends staged
        supplements at the use_skill intercept, the next ``code_execution``
        dispatch sees them, consumes them, and clears the holder so a
        second dispatch in the same turn doesn't re-stage. ``None`` (test
        path) ⇒ no provider wired; staging is a no-op.
        """
        extra: list[object] = []
        # Spec 28 — the workspace persister (hexagonal adapter). Built once per
        # toolbox and injected into every byte-producing tool so chat-path
        # outputs persist + surface as ToolResult.artifacts (closes Spec 25
        # §2.9). None when no workspace_root is configured (CLI / test path) ⇒
        # tools produce their pre-Spec-28 result shape (criterion #9).
        workspace_persister = (
            WorkspaceDirPersister(file_storage=self._file_storage, persona_id=persona.persona_id)
            if self._file_storage is not None and persona.persona_id is not None
            else None
        )
        # SECURITY (cross-context isolation): the built-in ``file_read`` /
        # ``file_write`` tools must NOT resolve against the flat process-wide
        # ``tools_sandbox_root`` — that surfaces files any other persona /
        # conversation left under the shared parent. Inject a per-REQUEST scope
        # provider that returns ``<workspace_root>/<owner_id>/<persona_id>`` from
        # the same sandbox request context ``code_execution`` + the workspace
        # persister already use. The toolbox is cached/built once, so the
        # provider is resolved at *dispatch* time, not build time. No bound
        # context ⇒ provider returns ``None`` ⇒ the file tools fail closed
        # (deny), never falling back to the shared root.
        file_sandbox_root = self._build_file_sandbox_root_provider(persona.persona_id)
        if scanned_skills:
            extra.append(make_use_skill_tool(scanned_skills))  # type: ignore[arg-type]
        if self._sandbox_pool is not None:
            # Spec 12 T10: API-composed code_execution wires
            # (a) lazy-eager pool acquire via pre_execute_hook (D-12-17),
            # (b) D-12-3 flat per-execution credits deduction on outcome=="ok",
            # (c) Spec 16 M1a deferred-input-files drain-and-clear (D-16-2,
            #     D-16-2-state-location).
            provider: Callable[[], list[SandboxFile]] | None
            if deferred_input_files_holder is not None:
                # Bind the holder via a closure (not a default argument) so
                # mypy's narrowing carries through. The closure captures the
                # non-None holder by reference; mutations are visible across
                # the use_skill intercept (writer) and the tool dispatch
                # (drainer).
                holder = deferred_input_files_holder

                def _drain_and_clear() -> list[SandboxFile]:
                    """Return current staged supplements + clear the holder.

                    Atomic enough for the single-threaded asyncio loop the
                    tool dispatches on: ``copy()`` snapshot + ``clear()``
                    happen between awaits. The runtime's use_skill intercept
                    appends to ``holder``; the tool's dispatch drains.
                    """
                    snapshot = list(holder)
                    holder.clear()
                    return snapshot

                provider = _drain_and_clear
            else:
                provider = None
            extra.append(
                make_pool_code_execution_tool(
                    pool=self._sandbox_pool,
                    rls_engine=self._engine,
                    credits_policy=self._credits_policy,
                    persona_id=persona.persona_id,
                    deferred_input_files_provider=provider,
                    workspace_root=self._workspace_root,
                )
            )
        # Spec 15 T16 + Spec 25 §2.9 — register ``generate_image`` when an
        # image backend is composed. The persona's ``tools`` allow-list
        # (Spec 03 D-03-7) is still the final gate inside
        # ``build_default_toolbox`` — personas that don't declare
        # ``generate_image`` see no advertised tool, but the registration
        # itself is unconditional once a backend is available. Mirrors the
        # ``code_execution`` graceful-absence shape: no backend → no tool;
        # the model surfaces a structured error if it tries to invoke one
        # that isn't registered.
        if self._image_backend is not None:
            extra.append(
                make_generate_image_tool(
                    backend=self._image_backend,
                    persona_id=persona.persona_id,
                    persona_visual_style=persona.identity.visual_style,
                    persister=workspace_persister,
                )
            )
        # Spec 28 B3 — render_diagram is runtime-wired (needs the persister to
        # store the diagram source for client-side SVG rendering). Composed here
        # when a workspace persister is available; the persona allow-list still
        # gates whether it is advertised (inside build_default_toolbox).
        if workspace_persister is not None:
            extra.append(
                make_render_diagram_tool(
                    persister=workspace_persister,
                    persona_id=persona.persona_id,
                )
            )
        # Spec 26 T07 — text_summarize is runtime-wired (D-26-7 / T1): it needs a
        # model, so it is NOT a build_default_toolbox built-in. Compose it here
        # with the SMALL tier (architecture §5.3 — summarization is boilerplate)
        # so the persona's runtime can dispatch it. The persona's allow-list
        # (inside build_default_toolbox) is still the final gate. Its AC-#2
        # wiring proof is a runtime-factory integration test
        # (D-26-X-text-summarize-wiring-test-kind). The registry is always
        # present in production; the guard keeps partial test-composition paths
        # (which stub the registry) booting cleanly.
        #
        # Graceful absence (sandbox_pool / image_backend precedent): building the
        # small-tier backend can fail at construction time — no API key
        # configured (``AuthenticationError`` ⊂ ``ProviderError``) or no tier
        # resolvable (``TierNotConfiguredError``), e.g. in keyless test/CI
        # environments. That must NOT break loop/run CREATION, which never
        # required a live backend at build time before Spec 26. On failure we
        # skip text_summarize entirely: the tool is simply absent (like
        # code_execution / generate_image when their deps are unconfigured), and
        # the genuine missing-key error still surfaces if the model later tries
        # to generate.
        if self._tier_registry is not None:
            try:
                # Spec P9: summarization is the background surface (small) —
                # stated via the policy instead of an incidental literal.
                small_backend = self._plan_tier_registry().get(tier_for("background"))
            except (
                ProviderError,
                TierNotConfiguredError,
                RegistryTierNotConfiguredError,  # the sibling-class latent gap (T3 finding)
            ) as exc:
                _logger.warning(
                    "text_summarize not wired — background-tier backend unavailable: {error}",
                    error=type(exc).__name__,
                )
            else:
                extra.append(make_text_summarize_tool(backend=small_backend))
        # Spec K2 (T8d / D-K2-1) — the on-by-default ``record_user_fact`` direct-write
        # tool. Composed when a graph store is wired (``enable_graph_writes``); the
        # persona's ``tools`` allow-list (inside ``build_default_toolbox``) is still
        # the final gate on whether it is advertised. The owner is resolved at
        # DISPATCH time from the RLS ``current_user_id`` contextvar (the same one the
        # checkout listener scopes the merge connection to), so a non-request call
        # fails closed. The structural means-redaction backstop
        # (``contains_self_harm_means``) is inside the tool — a self-harm-means write
        # is rejected, never stored (D-K2-7).
        if self._graph_store is not None and self._record_user_fact_enabled:
            from persona_runtime.extraction.direct_write import make_record_user_fact_tool

            from persona_api.middleware.rls_context import current_user_id

            extra.append(
                make_record_user_fact_tool(
                    graph_store=self._graph_store,
                    owner_provider=current_user_id.get,
                    persona_id=persona.persona_id,
                )
            )
        # Spec A4 (A4-D-5) — the read-only ``task_introspect`` tool: the persona's only window
        # onto its own standing tasks, grounded in typed state (no transcripts → no confabulated
        # progress). The reader is resolved per dispatch from the RLS ``current_user_id`` contextvar
        # so it is owner-scoped and fails closed off-request; the persona allow-list still gates it.
        from persona.tools.builtin.task_introspection import make_task_introspection_tool

        extra.append(
            make_task_introspection_tool(
                reader_provider=self._build_task_reader_provider(),
                persona_id=persona.persona_id,
            )
        )
        # R9-075 — the read-only ``schedule_introspect`` tool: the persona's window onto the
        # calendar. Personas could CREATE schedules (A1/A10) and the web could RENDER them (A8),
        # but the toolbox had no read surface for either, so "what's on my calendar this week?"
        # was answered from imagination. Reads through the SAME occurrences model the calendar
        # renders (no second recurrence path). The reader is resolved per dispatch from the RLS
        # ``current_user_id`` contextvar — owner-scoped, fail-closed off-request — and the tool
        # is auto-allowed in ``build_default_toolbox`` (``SELF_KNOWLEDGE_TOOLS``): it is not a
        # capability a persona opts into, and no persona's YAML allow-list names it.
        from persona.tools.builtin.schedule_introspection import make_schedule_introspection_tool

        extra.append(
            make_schedule_introspection_tool(
                reader_provider=self._build_schedule_reader_provider(),
                persona_id=persona.persona_id,
            )
        )
        # Spec 27 (D-27-3) — lazily spawn the built-in MCP servers THIS persona
        # references (mcp:<server>:) and hand their loopback URLs to the factory.
        # A persona that uses no built-in MCP spawns nothing.
        #
        # Spec P4 — the builtin ``filesystem`` server runs out-of-process and can't
        # read the request ContextVar, so its per-(owner, persona) scope is threaded
        # in at SPAWN. The scoped root is resolved HERE, from the SAME source of
        # truth the in-process file tools use (``_resolve_filesystem_scope_root``
        # delegates to ``_build_file_sandbox_root_provider``) so the subprocess and
        # the in-process tools can never disagree about hosted-vs-CLI or the scope
        # for a given request (P4-D-3/D-7). Resolution reads the request context,
        # which the loop-build sites bind early (chat_service / run_service).
        filesystem_scope_root = self._resolve_filesystem_scope_root(persona.persona_id)
        builtin_mcp_servers = await self._builtin_mcp.resolve(
            list(persona.tools), filesystem_scope_root=filesystem_scope_root
        )
        # Spec R8 (R8-D-5) — refresh-before-inject: rotate any near-expiry OAuth access
        # token BEFORE the header is built, so the client below injects a fresh token.
        # A hard refresh failure drops that server to not-connected (fail-closed). Runs
        # only when an APIConfig + persona_id are present (the hosted path).
        await self._refresh_oauth_before_inject(persona)
        # Spec 30 (D-30-4/6) — the persona's ASSIGNED bring-your-own MCP servers,
        # built as SSRF-pinned clients (the LIVE connect path: resolve-then-pin
        # + auth header from the decrypted credential). Empty when no servers are
        # assigned or no credential key is configured.
        byo_clients = self._build_byo_mcp_clients(persona)
        # Spec N6 (N6-D-1, R4-C1-21) — the persona's ASSIGNED image-runtime MCP servers,
        # resolved THROUGH the per-tenant runtime to per-tenant ``/mcp`` URLs. The
        # returned clients ride the SAME ``extra_mcp_clients`` path as BYO (N6-D-2 — the
        # only handoff is the URL; the N4 client is unchanged). Empty on the CLI /
        # community / when the runtime is unconfigured or nothing image-runtime is assigned.
        image_clients = await self._build_image_runtime_mcp_clients(persona)
        toolbox, mcp_clients = await build_default_toolbox(
            self._core_config,
            persona,
            extra_tools=extra or None,  # type: ignore[arg-type]
            workspace_persister=workspace_persister,
            extra_mcp_servers=builtin_mcp_servers or None,
            extra_mcp_clients=(byo_clients + image_clients) or None,
            file_sandbox_root=file_sandbox_root,
            mcp_search_catalog=self._mcp_search_catalog(),
        )
        self._mcp_clients.extend(mcp_clients)
        return toolbox

    def _mcp_search_catalog(self) -> MCPCatalog | None:
        """The edition-vetted catalog ``mcp_search`` may surface (N4-D-6 search mirror).

        Filters the mirror to the adoptable set so the persona never proposes an app it
        cannot adopt: cloud → the operator-vetted remote subset (empty allowlist → empty),
        community → every remote-with-url entry. ``None`` when there is no APIConfig (the
        CLI / test path) → ``build_default_toolbox`` falls back to the default mirror.
        """
        if self._api_config is None:
            return None
        return vetted_catalog_for_search(
            edition=self._api_config.edition,
            vetted=self._api_config.mcp_adopt_vetted_list,
            catalog=load_mirror_catalog(),
        )

    def _build_file_sandbox_root_provider(
        self, persona_id: str | None
    ) -> Callable[[], Path | None] | None:
        """Build the per-request sandbox-root provider for the file tools.

        SECURITY (cross-context isolation): returns a zero-arg callable that the
        ``file_read`` / ``file_write`` tools invoke at *dispatch* time to resolve
        ``<workspace_root>/<owner_id>/<persona_id>`` from the bound
        :class:`SandboxRequestContext` — the same contextvar
        ``code_execution`` + :class:`WorkspaceDirPersister` rely on. The toolbox
        is cached/built once; resolving at dispatch keeps each call scoped to the
        owner/persona of the request that triggered it.

        The callable returns ``None`` when no request context is bound (e.g. a
        non-request path) so the file tools fail closed — they read / write
        NOTHING rather than fall back to the flat shared root.

        Returns ``None`` (no provider) when ``workspace_root`` is unconfigured or
        ``persona_id`` is absent (CLI / test path); ``build_default_toolbox``
        then falls back to ``config.tools_sandbox_root`` (the single-tenant CLI
        root, which is NOT reachable from the hosted API).
        """
        if self._workspace_root is None or persona_id is None:
            return None

        # Local import: keep the persona-core import graph free of the api-only
        # sandbox context (mirrors the lazy-import discipline elsewhere here).
        from persona_api.sandbox import get_sandbox_request_context

        workspace_root = self._workspace_root

        def _resolve() -> Path | None:
            ctx = get_sandbox_request_context()
            if ctx is None:
                return None
            return workspace_root / ctx.owner_id / persona_id

        return _resolve

    def _resolve_filesystem_scope_root(self, persona_id: str | None) -> Path | None:
        """Resolve the builtin ``filesystem`` MCP subprocess's scoped root (Spec P4).

        SINGLE SOURCE OF TRUTH with the in-process file tools (P4-D-3/D-7): this
        delegates to :meth:`_build_file_sandbox_root_provider` so the subprocess and
        the in-process ``file_read`` / ``file_write`` tools can never disagree about
        whether a request is hosted-or-CLI, nor about the scoped root for that
        request. The three cases mirror the in-process semantics exactly:

        - **provider present (hosted) + context bound** → ``provider()`` returns
          ``<workspace_root>/<owner>/<persona>`` — the scoped root baked into the
          child at spawn.
        - **provider present (hosted) + context unbound** → ``provider()`` returns
          ``None`` ⇒ the child is spawned WITHOUT a scope and fails closed
          (serve-and-deny, P4-D-4). Never the shared root (which here would be a
          cross-tenant leak).
        - **provider absent (CLI / no workspace_root / no persona_id)** → the
          single-tenant ``config.tools_sandbox_root`` — the legitimate flat root,
          mirroring ``build_default_toolbox``'s in-process fallback (there is no
          owner/persona to scope to, and no other tenant to leak across).

        Returns the ``Path`` to thread into the spawn, or ``None`` to fail closed.
        """
        provider = self._build_file_sandbox_root_provider(persona_id)
        if provider is None:
            # CLI / test path — single-tenant flat root (the in-process fallback).
            return self._core_config.tools_sandbox_root
        # Hosted path — resolve from the bound request context (None ⇒ deny).
        return provider()

    def _build_byo_mcp_clients(self, persona: Persona) -> list[MCPClient]:
        """Build SSRF-pinned MCP clients for the persona's assigned BYO servers (D-30-4/6).

        Resolves the assignment (the authorization), decrypts each credential
        transiently to form the auth header, and constructs an ``MCPClient`` with
        ``enforce_ssrf=True`` so the user-supplied URL is resolve-then-pinned +
        re-validated on EVERY request — the live runtime path, not just
        test-connection. Returns ``[]`` when no APIConfig (no key) or no
        persona_id; the clients are connected (gracefully) inside the toolbox build.
        """
        if self._api_config is None or persona.persona_id is None:
            return []
        # Local import: keeps the persona-core CLI/test import path free of the
        # api-only BYO-MCP store + avoids any import cycle at module load.
        from persona.tools.mcp.client import MCPClient

        from persona_api.mcp import store as mcp_store

        servers = mcp_store.decrypted_servers_for_persona(
            rls_engine=self._engine,
            config=self._api_config,
            persona_id=persona.persona_id,
        )
        # Spec N6: an assigned IMAGE-runtime server (its catalog entry is
        # ``server_type == "server"``) is NOT a remote endpoint — it has no reachable
        # ``url`` and is connected via the per-tenant runtime instead
        # (``_build_image_runtime_mcp_clients``). Skip it here so the BYO path never tries
        # to SSRF-connect its placeholder URL (and never double-handles it).
        image_names = set(self._image_runtime_entries())
        clients: list[MCPClient] = []
        for s in servers:
            if str(s["name"]) in image_names:
                continue  # image-runtime → the N6 runtime path handles it
            # Spec R8: ``oauth`` injects like ``bearer`` — the (refreshed) per-user
            # access token is the decrypted credential. A skipped/un-authorized oauth
            # server never reaches here (decrypted_servers_for_persona fails it closed).
            headers = (
                {"Authorization": f"Bearer {s['credential']}"}
                if s["auth_method"] in ("bearer", "oauth") and s["credential"]
                else None
            )
            clients.append(
                MCPClient(
                    server_name=str(s["name"]),
                    server_url=str(s["url"]),
                    persona_id=persona.persona_id,
                    enforce_ssrf=True,  # LIVE pinned path (resolve-then-pin per request)
                    headers=headers,
                    # Spec R8 (R8-D-5): reconnect-on-401 for oauth servers only — a
                    # mid-session 401 refreshes+rotates the token and rebuilds the
                    # transport once (fail-closed on failure). None for PAT/no-auth.
                    reauth=self._make_oauth_reauth(str(s["id"]))
                    if s["auth_method"] == "oauth"
                    else None,
                )
            )
        return clients

    def _image_runtime_entries(self) -> dict[str, MCPServerCatalogEntry]:
        """The catalog entries that are image-runtime servers (``server_type == "server"``).

        Keyed by name — the discriminator for "connect via the per-tenant runtime, not as a
        remote endpoint". Built from the merged catalog (in-memory; the mirror snapshot).
        """
        from persona_api.services import catalog_service

        return {
            e.name: e for e in catalog_service.merged_mcp_catalog() if e.server_type == "server"
        }

    async def _build_image_runtime_mcp_clients(self, persona: Persona) -> list[MCPClient]:
        """Resolve the persona's assigned image-runtime servers → per-tenant ``/mcp`` clients (N6).

        For each assigned server whose catalog entry is an image-runtime server, resolve the
        tenant's secret (:class:`FernetGatewaySecretResolver` → spawn env, N6-D-2) and
        ``ensure`` its per-tenant Fly Machine (N6-D-1); a ``running`` instance yields a real
        ``/mcp`` URL that the EXISTING N4 :class:`MCPClient` connects to unchanged
        (acceptance #4 — the R4-C1-21 close). A not-running instance simply contributes no
        client (the not-connected signal, T6, reports it). Returns ``[]`` on the CLI /
        community / when the runtime is unconfigured or no owner is bound.
        """
        if self._mcp_runtime is None or self._api_config is None or persona.persona_id is None:
            return []
        from persona_api.sandbox import get_sandbox_request_context

        ctx = get_sandbox_request_context()
        if ctx is None:
            return []  # no owner bound → fail closed (never guess the tenant)
        entries = self._image_runtime_entries()
        if not entries:
            return []
        from persona.tools.mcp.client import MCPClient

        from persona_api.mcp import store as mcp_store
        from persona_api.mcp.secret_resolver import FernetGatewaySecretResolver, build_spawn_env

        resolver = FernetGatewaySecretResolver(rls_engine=self._engine, config=self._api_config)
        assigned = mcp_store.list_servers_for_persona(
            rls_engine=self._engine, persona_id=persona.persona_id
        )
        clients: list[MCPClient] = []
        for s in assigned:
            if not s.get("enabled"):
                continue
            entry = entries.get(str(s["name"]))
            if entry is None:
                continue  # remote / manual BYO — handled by _build_byo_mcp_clients
            secret_env = build_spawn_env(resolver, owner_id=ctx.owner_id, entry=entry)
            inst = await self._mcp_runtime.ensure(
                owner_id=ctx.owner_id,
                server_id=str(s["id"]),
                image=entry.image,
                secret_env=secret_env,
            )
            if inst.state == "running" and inst.endpoint_url:
                clients.append(
                    MCPClient(
                        server_name=str(s["name"]),
                        server_url=inst.endpoint_url,
                        persona_id=persona.persona_id,
                        # The endpoint is an internal Fly 6PN DNS URL (operator-trust,
                        # not user-supplied) → not SSRF-pinned, like the N1 gateway (D-N1-2).
                        enforce_ssrf=False,
                    )
                )
        return clients

    def _make_oauth_reauth(
        self, server_id: str
    ) -> Callable[[], Awaitable[dict[str, str] | None]] | None:
        """Build the reconnect-on-401 callback for an oauth server (R8-D-5), or None.

        None on the CLI/test path (no APIConfig) — reconnect-on-401 is a hosted-only
        concern. The closure captures the server_id + this factory's engine/config so
        the client can refresh+rotate without any api import of its own.
        """
        if self._api_config is None:
            return None
        from persona_api.mcp.oauth import service as oauth_service

        config = self._api_config

        async def _reauth() -> dict[str, str] | None:
            return await oauth_service.reauthenticate_server(
                rls_engine=self._engine, config=config, server_id=server_id
            )

        return _reauth

    async def _refresh_oauth_before_inject(self, persona: Persona) -> None:
        """Refresh any near-expiry OAuth access token before the header build (R8-D-5).

        No-op on the CLI/test path (no APIConfig or persona_id). Best-effort: a refresh
        failure for one server is handled inside the service (that server drops to
        not-connected); it never blocks the toolbox build.
        """
        if self._api_config is None or persona.persona_id is None:
            return
        from persona_api.mcp.oauth import service as oauth_service

        await oauth_service.refresh_persona_oauth_servers(
            rls_engine=self._engine,
            config=self._api_config,
            persona_id=persona.persona_id,
        )

    def _scan_skills(self, persona: Persona) -> tuple[SkillScanner, list[object]]:
        # ``BUILTIN_ROOT`` (re-exported from persona-core ``persona.skills``)
        # is the single source of truth shared with ``catalog_service`` so
        # both surfaces resolve declared skills against the same on-disk
        # directory. Without this path, every persona-declared skill would
        # log a "declared skill not found" warning at every chat turn and
        # the loop would never inject any skill content.
        scanner = SkillScanner(skill_paths=[BUILTIN_ROOT])
        scanned = scanner.scan(
            declared_skills=persona.skills,
            tool_allow_list=list(persona.tools) if persona.tools else None,
        )
        # Spec P5 (P5-D-4): resolve the document_generation SKILL.md's fidelity fence to
        # the variant matching this deployment's sandbox template, applied to the builtin
        # scan (doc-gen is a builtin). The flag is computed by the composition root from
        # PERSONA_SANDBOX_TEMPLATE and stored on the factory; core consumes it here via
        # the pure ``apply_docgen_fidelity``. A no-op for every other skill — the single
        # consumption point of the flag, riding the existing skill path.
        resolved_builtins = [
            apply_docgen_fidelity(spec, full_fidelity=self._docgen_full_fidelity)
            for spec in scanned
        ]
        # Spec S2 (C1): merge in declared EXTERNAL skills from the file-on-volume mirror,
        # after fidelity resolution (external skills carry no doc-gen fence, so ordering
        # is immaterial to behaviour). Availability ≠ enablement (S2-R-3 / the N2
        # boundary): the mirror makes external skills *available*, but only the names the
        # persona DECLARED are loaded here — the auto-sync never enables a skill on a
        # persona. Trust + provenance ride from the mirror snapshot (source-assigned,
        # never re-derived). Fail-soft: an absent/corrupt mirror yields an empty external
        # set, so builtins are unaffected.
        merged: list[object] = list(resolved_builtins)
        merged.extend(self._declared_mirror_skills(persona, merged))
        return scanner, merged

    def _declared_mirror_skills(self, persona: Persona, scanned: Sequence[object]) -> list[object]:
        """Resolve a persona's declared external skills against the skill mirror (S2 C1)."""
        from persona.config import PersonaCoreConfig
        from persona.skills.aliases import resolve_skill_aliases
        from persona.skills.catalog import expand_collections
        from persona.skills.skill_mirror import (
            declared_mirror_skills,
            load_skill_mirror,
            resolve_skill_mirror_read_path,
        )

        expanded = resolve_skill_aliases(expand_collections(list(persona.skills)))
        resolved_names = {getattr(s, "name", "") for s in scanned}
        mirror_specs = load_skill_mirror(
            resolve_skill_mirror_read_path(PersonaCoreConfig().skill_mirror_path)
        )
        return list(
            declared_mirror_skills(
                expanded, resolved_names=resolved_names, mirror_specs=mirror_specs
            )
        )

    # -- the closures the routes call ---------------------------------------

    def _build_task_origination(
        self,
    ) -> tuple[
        StandingIntentRecognizer | None,
        AmendmentInterpreter | None,
        SteeringInterpreter | None,
        RescheduleInterpreter | None,
    ]:
        """Build the A4/A8 loop-side interpreters (standing + amendment + steering + reschedule).

        The precision layer of the contract flow (A4-D-2/T9/T9b + A8-T6) — all four share one
        recognition-tier backend (Spec P9, P9-D-2: ``mid`` minimum via
        ``PERSONA_API_RECOGNITION_TIER`` — small was the R4 confabulation root; recognition
        gates whether the persona can ACT). **Fail-soft** (the text_summarize precedent): if
        the backend is unavailable (keyless env / no tier), returns all ``None`` so the loop's
        A4/A8 gates stay inert and ordinary chat is byte-unchanged — never a construction
        failure.
        """
        if self._tier_registry is None:
            return None, None, None, None
        recognition_tier = tier_for(
            "recognition",
            override=(self._api_config.recognition_tier if self._api_config is not None else None),
        )
        try:
            backend = self._plan_tier_registry().get(recognition_tier)
        # Both TierNotConfiguredError flavours: the registry's own (tier name
        # unresolvable, persona_runtime.errors) AND the MODELS-list all-fail
        # (persona.backends.errors) — sibling classes, not aliases. Catching
        # only the backends one left the registry's raise a latent
        # construction crash (found by the T3 composition test).
        except (ProviderError, TierNotConfiguredError, RegistryTierNotConfiguredError) as exc:
            _logger.warning(
                "task origination not wired — recognition-tier ({tier}) backend unavailable: "
                "{error}",
                tier=recognition_tier,
                error=type(exc).__name__,
            )
            return None, None, None, None
        # A7 (T8): the event-trigger judge shares the recognition-tier backend and is wired ONLY
        # when event triggers are enabled (the feature-gate posture). OFF ⇒ the recogniser is
        # byte-identical to the A4-only path (the event branch is inert). A recognised event watch
        # flows through the SAME echo → confirm → OriginationService door as a schedule.
        from persona.events import EventTriggerSettings
        from persona_runtime.task_origination import (
            ModelAmendmentInterpreter,
            ModelEventTriggerIntentJudge,
            ModelRescheduleInterpreter,
            ModelStandingIntentJudge,
            ModelSteeringInterpreter,
            StandingIntentRecognizer,
        )

        event_judge = (
            ModelEventTriggerIntentJudge(backend=backend)
            if EventTriggerSettings().enabled
            else None
        )
        recognizer = StandingIntentRecognizer(
            ModelStandingIntentJudge(
                backend=backend,
                default_timezone=self._core_config.default_timezone,
                timezone_provider=self._build_user_timezone_provider(),
            ),
            event_judge=event_judge,
        )
        return (
            recognizer,
            ModelAmendmentInterpreter(backend=backend),
            ModelSteeringInterpreter(backend=backend),
            ModelRescheduleInterpreter(backend=backend),
        )

    def _build_initiative_verb_interpreter(self) -> InitiativeVerbInterpreter | None:
        """The conservative dial-verb interpreter (Spec A5, T10) — None when disabled."""
        from persona.initiative import InitiativeSettings
        from persona_runtime.initiative.verbs import ModelInitiativeVerbInterpreter

        settings = InitiativeSettings()
        if not settings.enabled:
            return None
        try:
            backend = self._plan_tier_registry().get(settings.scan_tier)
        except Exception:  # noqa: BLE001 — keyless env: the gate stays inert, never breaks chat
            return None
        return ModelInitiativeVerbInterpreter(backend)

    def _build_initiative_pending_provider(
        self, persona_id: str | None
    ) -> Callable[[], str | None] | None:
        """The LEDGER pending-proposal read for the confirm/decline floors (Spec A5, T10).

        A closure over the RLS ``current_user_id`` contextvar (the owner-scoped-
        closure precedent): the loop asks "is a proposal pending for THIS
        owner+persona?" per turn; the answer comes from the durable notice row —
        never conversation metadata (reload-durable; the state.md A4 finding is
        exactly what this sidesteps). Fail-soft: any error reads as no-pending.
        """
        from persona.initiative import InitiativeSettings

        from persona_api.initiative.store import InitiativeLedger
        from persona_api.middleware.rls_context import current_user_id

        settings = InitiativeSettings()
        if not settings.enabled or persona_id is None:
            return None
        ledger = InitiativeLedger(self._engine)

        def _pending() -> str | None:
            owner = current_user_id.get()
            if not owner:
                return None
            try:
                notice = ledger.latest_pending_proposal(
                    owner, persona_id, max_age_days=settings.hold_max_days
                )
            except Exception:  # noqa: BLE001 — a pending read must never break a turn
                return None
            return notice.id if notice is not None else None

        return _pending

    def _build_user_timezone_provider(self) -> Callable[[], str]:
        """The per-user cadence-timezone provider (Spec A8, A8-D-9 — the K6 seam realised).

        Resolves ``users.timezone`` for the caller (from the RLS ``current_user_id``
        contextvar) and falls back to ``PERSONA_DEFAULT_TIMEZONE`` when unset/blank/
        invalid (:func:`persona.timezone.resolve_timezone`). Injected into the
        standing-intent judge so a drafted cadence is anchored in the user's own zone.
        Fail-soft: off-request (no owner) or on any lookup error it returns the config
        default, so origination never breaks on a timezone read. Mirrors
        :meth:`_build_task_reader_provider` (the owner-scoped-closure precedent).
        """
        from persona.timezone import resolve_timezone

        from persona_api.middleware.rls_context import current_user_id
        from persona_api.services import user_service

        default_tz = self._core_config.default_timezone
        engine = self._engine

        def _provider() -> str:
            owner = current_user_id.get()
            if not owner:
                return default_tz
            try:
                profile = user_service.get_user_profile(engine, user_id=owner)
            except Exception:  # noqa: BLE001 — a tz lookup must never break origination
                _logger.warning("user timezone lookup failed; using the config default")
                return default_tz
            stored = profile.get("timezone") if profile else None
            return resolve_timezone(stored if isinstance(stored, str) else None, default=default_tz)

        return _provider

    def _build_user_quiet_hours_provider(self) -> Callable[[], QuietHours | None]:
        """The per-user quiet-hours provider (Spec A8, A8-D-6 — off-until-set).

        Resolves ``users.quiet_hours_start/end`` for the caller (RLS contextvar) into a
        :class:`~persona.schedules.QuietHours`, or ``None`` when unset/invalid (off) — so the
        reschedule re-echo warns + offers the nearest edge only when a window exists. Fail-soft:
        off-request or on any error it returns ``None`` (no warn), never breaking a turn.
        """
        from persona.schedules import QuietHours

        from persona_api.middleware.rls_context import current_user_id
        from persona_api.services import user_service

        engine = self._engine

        def _provider() -> QuietHours | None:
            owner = current_user_id.get()
            if not owner:
                return None
            try:
                profile = user_service.get_user_profile(engine, user_id=owner)
            except Exception:  # noqa: BLE001 — a quiet-hours lookup must never break a turn
                _logger.warning("user quiet-hours lookup failed; treating as off")
                return None
            if not profile:
                return None
            start, end = profile.get("quiet_hours_start"), profile.get("quiet_hours_end")
            if not isinstance(start, int) or not isinstance(end, int):
                return None
            try:
                return QuietHours(start_minute=start, end_minute=end)
            except ValueError:
                return None  # an empty/corrupt window → off

        return _provider

    def _build_task_reader_provider(self) -> Callable[[], TaskStateReader | None]:
        """The owner-scoped task-state reader provider (Spec A4, T7 + composition-root wiring).

        Shared by the ``task_introspect`` tool and the loop's steering gate — both resolve the
        caller's reader at dispatch from the RLS ``current_user_id`` contextvar (owner-scoped,
        fail-closed off-request). The stores are engine-scoped, built once per provider.
        """
        from persona_api.middleware.rls_context import current_user_id
        from persona_api.tasks.reader import APITaskStateReader
        from persona_api.tasks.store import CheckpointStore, TaskStore

        task_store = TaskStore(self._engine)
        checkpoint_store = CheckpointStore(self._engine)

        def _provider() -> TaskStateReader | None:
            owner = current_user_id.get()
            if not owner:
                return None
            return APITaskStateReader(task_store, checkpoint_store, owner)

        return _provider

    def _build_schedule_reader_provider(self) -> Callable[[], ScheduleReader | None]:
        """The owner-scoped schedule reader provider (R9-075).

        Backs the ``schedule_introspect`` tool: resolves the caller's reader at dispatch from
        the RLS ``current_user_id`` contextvar, so a toolbox built once stays scoped to the
        owner of each request and fails closed off-request (no owner ⇒ ``None`` ⇒ the tool
        reports it has no calendar access rather than answering blind). Mirrors
        :meth:`_build_task_reader_provider`.

        The occurrence caps come from the ``APIConfig``; on the CLI / test path where none was
        injected, a default-constructed config supplies the same defaults the hosted service
        uses, so the tool behaves identically rather than being silently absent.
        """
        from persona_api.config import APIConfig
        from persona_api.middleware.rls_context import current_user_id
        from persona_api.schedules.reader import APIScheduleReader

        config = self._api_config if self._api_config is not None else APIConfig()
        engine = self._engine

        def _provider() -> ScheduleReader | None:
            owner = current_user_id.get()
            if not owner:
                return None
            return APIScheduleReader(engine, config, owner)

        return _provider

    def _plan_tier_selection(
        self,
    ) -> tuple[TierRegistry, Callable[[str], ChatBackend | None] | None]:
        """Resolve the caller's plan → (tier_registry, preferred_backend_provider) (Spec M4, T5a).

        THE free-tier no-paid-fallback gate, enforced at loop CONSTRUCTION. Returns:

        * **Gating off** (``_free_tier_registry is None`` — community, or no free set configured):
          ``(self._tier_registry, build_openrouter_passthrough)`` — byte-identical to pre-M4
          (every user resolves the paid tiers + the ``preferred_model`` passthrough).
        * **Free plan** (cloud, the caller's ``subscription.plan_code == 'free'``): ``(the FREE-ONLY
          registry, None)`` — the whole tier chain is free-only AND ``preferred_backend_provider``
          is disabled (``preferred_model`` is inert, no escape hatch), so a free user can NEVER
          reach a paid model.
        * **Paid plan** (plus / pro): ``(self._tier_registry, build_openrouter_passthrough)`` — the
          full paid tiers + fallback + preferred override, unchanged.

        The plan is read RLS-scoped from the caller's ``subscription`` row (the ``current_user_id``
        contextvar owner); an absent row / no scope defaults to ``free`` (fail-safe — the
        restrictive set), so a lookup miss can never open the paid tiers to a free user.
        """
        if self._free_tier_registry is None:
            return self._tier_registry, build_openrouter_passthrough  # gating off (community/paid)
        from persona_api.middleware.rls_context import current_user_id
        from persona_api.services import subscription_service

        user_id = current_user_id.get()
        plan_code = "free"
        if user_id:
            row = subscription_service.get_subscription(self._engine, user_id=user_id)
            if row is not None:
                plan_code = str(row.get("plan_code") or "free")
        if plan_code == "free":
            # Free-only registry + NO preferred override (preferred_model inert; no escape hatch).
            return self._free_tier_registry, None
        return self._tier_registry, build_openrouter_passthrough  # paid plan → full tiers

    def _plan_tier_registry(self) -> TierRegistry:
        """The plan-selected tier registry for the current owner (Spec M4, T5c).

        The background sibling of :meth:`_plan_tier_selection`: a FREE owner's owner-billed
        background LLM (summarize / recognition / initiative-scan / title) resolves the
        free-only registry (never a paid fallback); paid / community resolve the paid tiers.
        Same per-call owner-plan read. An empty free registry ``.get`` raises
        ``TierNotConfiguredError`` — the existing fail-soft catches at these sites treat that
        as "surface not wired" (skip), which is the fail-closed behaviour (no paid fallback).
        """
        return self._plan_tier_selection()[0]

    async def build_conversation_loop(self, persona_id: str) -> ConversationLoop:
        """Construct the ConversationLoop for ``persona_id`` (KEYSTONE 1, T08).

        Wires the Spec 16 M1a deferred-input-files holder (D-16-2 /
        D-16-2-state-location): a single ``list[SandboxFile]`` is shared
        between the loop's public ``deferred_input_files`` attribute and
        the ``code_execution`` tool's drain-and-clear provider closure.
        The use_skill intercept appends to this list; the next
        ``code_execution`` dispatch drains it.
        """
        persona = self._load_persona(persona_id)
        # Spec M4 (T5a): the free-tier no-paid-fallback gate — a FREE caller resolves the
        # free-only registry + a disabled preferred_model override; paid / community are
        # byte-identical (paid tiers + passthrough).
        chat_tier_registry, chat_preferred_provider = self._plan_tier_selection()
        scanner, scanned = self._scan_skills(persona)
        # M1a shared holder — created BEFORE the toolbox so the
        # code_execution tool's drain-and-clear provider closes over the
        # same list the loop will write to.
        deferred_holder: list[SandboxFile] = []
        toolbox = await self._build_toolbox(
            persona,
            scanned,
            deferred_input_files_holder=deferred_holder,
        )
        from persona_runtime.wellbeing import surfacing_guidance as wellbeing_surfacing_guidance

        recognizer, amendment_interpreter, steering_interpreter, reschedule_interpreter = (
            self._build_task_origination()
        )
        loop = ConversationLoop(
            persona=persona,
            stores=self._build_stores(),
            toolbox=toolbox,  # type: ignore[arg-type]
            skill_scanner=scanner,
            skill_injector=SkillInjector(),
            scanned_skills=scanned,  # type: ignore[arg-type]
            history_manager=ConversationHistoryManager(),
            prompt_builder=PromptBuilder(),
            # Spec P9 (P9-D-1): the deliberate surface→tier policy — chat
            # resolves frontier every turn (pin still honored at the loop's
            # override short-circuit). The heuristic cascade is retired from
            # the default path, retained dormant.
            # Spec M4 (T5a): the plan-resolved registry (free → free-only; paid/community →
            # the paid tiers). PolicyRouter picks a tier NAME; the registry returns the
            # pre-built (free-only for a free user) MultiModelChatBackend — so the whole
            # fallback walk stays within the plan's model set.
            router=PolicyRouter(tier_registry=chat_tier_registry),
            tier_registry=chat_tier_registry,
            turn_log_writer=self._turn_log_writer,
            # Spec 23 T13: app-scoped intelligent-routing wiring. The shared
            # latency tracker persists per-model EWMA across requests; the
            # IntelligentRouter is consulted only when the persona opted in
            # (routing.intelligent.enabled) — default-off personas route
            # byte-identically (criterion 11).
            latency_tracker=self._latency_tracker,
            intelligent_router=self._intelligent_router,
            # Spec M2 (D-M2-1): the shared metadata chain prices every turn
            # (resolver-backed ``compute_turn_cost`` — cost estimation works
            # with intelligent routing OFF; the flag gates only the router).
            cost_source=self._metadata_resolver,
            # Spec M1 (M1-T4): the per-persona ``preferred_model`` choice (T1,
            # additive-optional) resolves through the OpenRouter passthrough (T2) — any
            # catalog id, no tier pre-registration needed. Fail-open: an unset key or a
            # bad choice returns None from the provider and the loop's override
            # short-circuit (T3) falls back to the tier default, so this is
            # byte-identical for personas that never set ``preferred_model``.
            # Spec M4 (T5a): ``None`` for a FREE caller — the preferred_model override is
            # DISABLED (loop.py: ``preferred_backend_provider is None`` ⇒ the persona's
            # preferred_model is inert), so a free user's ``preferred_model=<any paid id>``
            # can never front a paid model. Paid/community keep the OpenRouter passthrough.
            preferred_backend_provider=chat_preferred_provider,
            # Spec R7 (R7-D-1 discharge of D-23-X): the soft per-day cost-bias ramp's
            # real cross-session spend source (today's recorded turn_logs cost for
            # this owner+persona). Fail-soft to 0.0; replaces the old construction-
            # time fail-loud guard for an unenforceable per-day cap.
            day_spent_cents_provider=self._build_day_spent_cents_provider(persona.persona_id),
            # K3: the owner-scoped graph-knowledge retrieval (None until graph
            # writes were enabled — additive, zero-graph otherwise).
            graph_retrieval=self._build_graph_retrieval(),
            # Spec S1 (S1-D-7 / S1-D-X-consent-wiring): the injection audit sink
            # (R5: backend-selected via the shared factory — JSONL default, Postgres opt-in).
            audit_logger=self._resolve_audit_logger(),
            # Spec S3 (S3-D-2): the REAL consent store replaces S1's DenyUnvettedConsent
            # stub. Runs on the same RLS-scoped engine → sees only the owner's consent
            # rows. Empty store ≡ DenyUnvettedConsent (no row → denied): swapping the
            # stub in can never OPEN access; only a recorded consent does.
            skill_consent=PostgresSkillConsentStore(self._engine),
            # K4: the per-category care-text the surfacing slot rides (K4-D-3). Stateless;
            # wired only when the graph is composed, so a zero-graph loop is byte-identical.
            graph_surfacing_guidance=(
                wellbeing_surfacing_guidance if self._graph_store is not None else None
            ),
            # R6: the app-scoped crisis encoder (euphemistic/non-English recall). None ⇒
            # V11 lexical-only. The same instance is shared across every loop + the agentic
            # loop, so there is ONE classifier for the whole process.
            crisis_encoder=self._crisis_encoder,
            # K6 (K6-D-6): the persona addresses the user by their real name (resolved
            # per turn from our users table). K6-D-4 addendum: the runtime prompt path
            # is the SELF node's creation trigger (None when no graph store → no sync,
            # byte-identical). Both fail-soft — a name/anchor hiccup never fails a turn.
            user_name_provider=self._build_user_name_provider(),
            self_node_sync=self._build_self_node_sync(),
            # Spec A4 (composition-root activation): the contract flow's loop-side. The recognizer
            # + amendment/steering interpreters are the small-tier precision layer (fail-soft: None
            # in a keyless env → the gates stay inert, chat byte-unchanged). This lands TOGETHER
            # with the worker-side OriginationService/TaskSteeringService (app.py) so a confirmed
            # contract that emits ``task_originated`` is actually created — never a false
            # "I've set that up". The reader powers grounded introspection + steering resolution.
            standing_recognizer=recognizer,
            amendment_interpreter=amendment_interpreter,
            steering_interpreter=steering_interpreter,
            task_reader_provider=self._build_task_reader_provider(),
            # Spec A8 (T6): the conversational reschedule verb. The interpreter resolves the target
            # + new cadence; the tz/quiet-hours providers resolve the user's frame per turn (the K6
            # seam). Fail-soft: None → the reschedule gate stays inert. Lands with the worker-side
            # TaskRescheduleService (app.py) so a confirmed reschedule applies through the CAS door.
            reschedule_interpreter=reschedule_interpreter,
            timezone_provider=self._build_user_timezone_provider(),
            quiet_hours_provider=self._build_user_quiet_hours_provider(),
            # Spec A5 (T10): the initiative-verb family — the dial verbs + the
            # LEDGER-anchored confirm/decline (reload-durable; the T9 Option-C
            # consolidation). Both None when PERSONA_INITIATIVE_ENABLED is off —
            # the gate stays inert, chat byte-unchanged (criterion 9's posture).
            initiative_verb_interpreter=self._build_initiative_verb_interpreter(),
            initiative_pending_provider=self._build_initiative_pending_provider(persona.persona_id),
            # K9 (K9-D-1/D-10/D-11): the unified recall (fuse-don't-route over pyramid + graph,
            # reranked+gated) and the always-in-context core-memory block reader. The unified path
            # is env-gated OFF (``unified_enabled``) → today's two-path recall until flipped; the
            # core-block reader is turn-path READ-ONLY (background refresh rides the K8 engine job).
            # Both fail-soft — a recall/block hiccup degrades to memoryless, never fails a turn.
            unified_recall=self._build_unified_recall(persona_id),
            core_block_provider=self._build_core_block_provider(persona_id),
        )
        # Replace the loop's default-empty deferred_input_files with the
        # SHARED holder (same identity), so the use_skill intercept's
        # ``self.deferred_input_files.extend(...)`` mutates the same list
        # the tool's provider drains. Per D-16-2-state-location the
        # attribute is public for exactly this composition-root binding.
        loop.deferred_input_files = deferred_holder
        return loop

    async def build_agentic_loop(self, persona_id: str) -> AgenticLoop:
        """Construct the AgenticLoop for ``persona_id`` (KEYSTONE 2, T11).

        Wires the Spec 16 M1a deferred-input-files holder symmetrically to
        :meth:`build_conversation_loop`.
        """
        persona = self._load_persona(persona_id)
        # Spec M4 (T5a): the free-tier gate — a FREE owner's agentic run resolves the
        # free-only registry (agentic wires no preferred_model override, so the registry
        # swap alone keeps the whole step-tier fallback walk free-only).
        agentic_tier_registry, _ = self._plan_tier_selection()
        _scanner, scanned = self._scan_skills(persona)
        deferred_holder: list[SandboxFile] = []
        toolbox = await self._build_toolbox(
            persona,
            scanned,
            deferred_input_files_holder=deferred_holder,
        )
        loop = AgenticLoop(
            persona=persona,
            stores=self._build_stores(),
            toolbox=toolbox,  # type: ignore[arg-type]
            skill_injector=SkillInjector(),
            scanned_skills=scanned,  # type: ignore[arg-type]
            prompt_builder=PromptBuilder(),
            # Spec P9 (P9-D-1): step tiers come from _tier_for_step (D-06-6),
            # not this router — composed for Protocol parity with the chat loop.
            # Spec M4 (T5a): the plan-resolved registry (free → free-only; paid/community
            # → the paid tiers). Agentic step tiers all resolve through this registry.
            router=PolicyRouter(tier_registry=agentic_tier_registry),
            tier_registry=agentic_tier_registry,
            # Spec S1 (S1-D-7): injection audit sink (R5: backend-selected).
            audit_logger=self._resolve_audit_logger(),
            # Spec S3 (S3-D-2): the real consent store (empty ≡ DenyUnvettedConsent).
            skill_consent=PostgresSkillConsentStore(self._engine),
            # R6: same shared crisis encoder as the chat loop — ONE classifier.
            crisis_encoder=self._crisis_encoder,
        )
        loop.deferred_input_files = deferred_holder
        return loop

    def build_action_executor(self, persona_id: str) -> ToolboxActionExecutor:
        """The un-gated single-tool executor for approved-action replay (Spec A6, T-seam).

        Returns a :class:`ToolboxActionExecutor` that, on ``execute``, builds the persona's real
        toolbox (the SAME ``_build_toolbox`` the chat/agentic loops use — plain, *un-gated*; A3's
        ``PolicyGatedToolbox`` is a leg-only wrapper) and dispatches the EXACT recorded
        ``(tool_name, arguments)`` verbatim. The approval is the authorization, so no re-gate; the
        model never re-derives. The toolbox build is deferred to the first ``execute`` (only an
        actual APPROVE of a pending proposal pays it). Must run inside the owner's RLS scope — the
        approvals route binds it via middleware; an off-request driver must set the owner GUC.
        """

        async def _build() -> Toolbox:
            persona = self._load_persona(persona_id)
            _scanner, scanned = self._scan_skills(persona)
            return cast("Toolbox", await self._build_toolbox(persona, scanned))

        return ToolboxActionExecutor(_build)

    async def build_title(self, first_message: str) -> str:
        """Generate a short (≤5-word) conversation title from the first message,
        on the TITLE tier (R9-020: mid by default via the ``title`` surface row,
        env-overridable via ``PERSONA_API_TITLE_TIER`` — the exact recognition
        precedent). Titles are user-read chrome; the background/small tier
        echoed the titling instruction, so R4's sanitizer fell back to
        first-words on ~every conversation. Returns the title text; the caller
        (chat_service) sanitizes + applies it best-effort."""
        from datetime import UTC, datetime

        from persona.schema.conversation import ConversationMessage

        title_tier = tier_for(
            "title",
            override=(self._api_config.title_tier if self._api_config is not None else None),
        )
        backend = self._plan_tier_registry().get(title_tier)
        now = datetime.now(UTC)
        prompt = [
            ConversationMessage(
                role="system",
                content=(
                    "Summarise the user's message as a conversation title of at most "
                    "5 words. Output ONLY the title — no quotes, no punctuation, no prose."
                ),
                created_at=now,
            ),
            ConversationMessage(role="user", content=first_message, created_at=now),
        ]
        response = await backend.chat(prompt, temperature=0.0, max_tokens=24)
        return response.content.strip().strip('"').splitlines()[0] if response.content else ""

    # -- lifecycle ----------------------------------------------------------

    async def aclose(self) -> None:
        """Shutdown: close the tier registry + every MCP client (D-05-4) + reap
        the built-in MCP server subprocesses (Spec 27 D-27-3)."""
        await self._tier_registry.aclose()
        for client in self._mcp_clients:
            await client.disconnect()
        await self._builtin_mcp.aclose()

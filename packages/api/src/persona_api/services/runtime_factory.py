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

from persona.audit import JSONLAuditLogger
from persona.backends.errors import ProviderError, TierNotConfiguredError
from persona.backends.metadata import (
    ChainedModelMetadataResolver,
    OpenRouterModelMetadataResolver,
    StaticModelMetadataResolver,
)
from persona.backends.openrouter_catalog import OpenRouterCatalogClient
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
from persona_runtime.loop import ConversationLoop
from persona_runtime.prompt import PromptBuilder
from persona_runtime.router import Router
from persona_runtime.routing import FirstTokenLatencyTracker, IntelligentRouter
from sqlalchemy import select

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

    from persona.graph.protocol import GraphStore
    from persona.imagegen import ImageBackend
    from persona.sandbox.result import SandboxFile
    from persona.stores.backend import Backend
    from persona.stores.embedder import Embedder
    from persona.stores.protocol import MemoryStore
    from persona.tasks.reader import TaskStateReader
    from persona.tools.mcp.catalog import MCPCatalog
    from persona.tools.mcp.client import MCPClient
    from persona_runtime.logging import TurnLogWriter
    from persona_runtime.prompt import GraphContext
    from persona_runtime.task_origination import (
        AmendmentInterpreter,
        StandingIntentRecognizer,
        SteeringInterpreter,
    )
    from persona_runtime.tier import TierRegistry
    from sqlalchemy import Engine

    from persona_api.config import APIConfig
    from persona_api.editions import CreditsPolicy
    from persona_api.sandbox.pool import SandboxPool

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
        turn_log_writer: TurnLogWriter,
        audit_root: Path,
        core_config: PersonaCoreConfig | None = None,
        sandbox_pool: SandboxPool | None = None,
        workspace_root: Path | None = None,
        image_backend: ImageBackend | None = None,
        api_config: APIConfig | None = None,
        credits_policy: CreditsPolicy | None = None,
        memory_backend: Backend | None = None,
        docgen_full_fidelity: bool = False,
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
        # Spec 33 (D-33-X-creditspolicy-di): the code_execution credit deduction
        # flows through the injected policy. Defaults to the metered policy so a
        # RuntimeFactory built without an explicit policy keeps today's behavior.
        self._credits_policy: CreditsPolicy = credits_policy or MeteredCreditsPolicy()
        # Spec 33 (D-33-X-memory-chroma-community): the edition's typed-memory
        # transport. None ⇒ PostgresBackend built per-request (today's behavior).
        self._memory_backend = memory_backend
        self._tier_registry = tier_registry
        self._turn_log_writer = turn_log_writer
        self._audit_root = audit_root
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
        self._intelligent_router = self._build_intelligent_router(
            tier_registry, self._latency_tracker
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
        from persona.audit import JSONLAuditLogger
        from persona.graph import build_graph_store

        self._graph_store = build_graph_store(
            engine=self._engine,
            embedder=self._embedder,
            audit_logger=JSONLAuditLogger(audit_root),
        )
        _logger.info("graph writes enabled (record_user_fact composed into toolboxes)")

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
        from datetime import UTC, datetime

        from persona.graph.config import GraphSettings
        from persona.graph.retrieval import HybridRetriever
        from persona.wellbeing_policy import is_gate_eligible, parse_category
        from persona_runtime.graph_selection import make_graph_retrieval, recency_bucket
        from persona_runtime.graph_window import get_recent_window
        from persona_runtime.wellbeing import FlaggedNode, make_allowlist_provider, recency_band

        from persona_api.middleware.rls_context import current_user_id

        settings = GraphSettings()
        retriever = HybridRetriever(store=self._graph_store, settings=settings)
        store = self._graph_store

        # K4 (K4-D-2): the gate-eligible flagged nodes the allowlist provider gates over —
        # the owner's wellbeing-tagged nodes narrowed to the gate-eligible categories, each
        # with its recency band (computed from provenance). Most owners have none → the
        # provider returns None (no subtraction) → the hot path stays free.
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

        allowlist_provider = make_allowlist_provider(
            flagged_nodes=flagged,
            owner_node_ids=lambda owner_id: set(store.node_ids_for_owner(owner_id)),
        )
        # The recent-window source: the per-turn ContextVar every conversational loop sets
        # before retrieval (K4-D-X-gating-signal-seam) — so the gate reads the conversation,
        # not the bare query (no uncanny re-closing). Unset ⇒ empty ⇒ query-only (fail-safe).
        return make_graph_retrieval(
            retriever=retriever,
            owner_provider=current_user_id.get,
            settings=settings,
            allowlist_provider=allowlist_provider,
            recent_window_provider=get_recent_window,
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
                _logger.warning("user-name resolution failed (non-fatal)", exc_info=True)
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
                _logger.warning("self-node sync failed (non-fatal)", exc_info=True)

        return sync

    @staticmethod
    def _build_intelligent_router(
        tier_registry: TierRegistry, latency_tracker: FirstTokenLatencyTracker
    ) -> IntelligentRouter:
        """Compose the IntelligentRouter (static metadata + optional OpenRouter).

        The static per-provider tables are always available (the authoritative,
        offline source). When ``PERSONA_OPENROUTER_API_KEY`` is set, the
        OpenRouter catalog is added as the broad-coverage fallback
        (D-23-X-resolver-precedence: static-authoritative-on-overlap). Catalog
        client construction is network-free (D-22-11); the first ``list_models``
        fetch is lazy + fail-open (D-22-1). The shared ``latency_tracker`` lets
        the router consult live per-model latency (D-23-6).
        """
        import os

        openrouter = None
        api_key = os.environ.get("PERSONA_OPENROUTER_API_KEY", "").strip()
        if api_key:
            base_url = os.environ.get("PERSONA_OPENROUTER_BASE_URL", "").strip() or None
            openrouter = OpenRouterModelMetadataResolver(
                OpenRouterCatalogClient(api_key, base_url=base_url)
            )
        resolver = ChainedModelMetadataResolver(
            static=StaticModelMetadataResolver(), openrouter=openrouter
        )
        return IntelligentRouter(
            tier_registry=tier_registry,
            metadata_resolver=resolver,
            latency_tracker=latency_tracker,
        )

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
        audit = JSONLAuditLogger(self._audit_root)
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
            WorkspaceDirPersister(
                workspace_root=self._workspace_root, persona_id=persona.persona_id
            )
            if self._workspace_root is not None and persona.persona_id is not None
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
                small_backend = self._tier_registry.get("small")
            except (ProviderError, TierNotConfiguredError) as exc:
                _logger.warning(
                    "text_summarize not wired — small-tier backend unavailable: {error}",
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
        toolbox, mcp_clients = await build_default_toolbox(
            self._core_config,
            persona,
            extra_tools=extra or None,  # type: ignore[arg-type]
            workspace_persister=workspace_persister,
            extra_mcp_servers=builtin_mcp_servers or None,
            extra_mcp_clients=byo_clients or None,
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
        clients: list[MCPClient] = []
        for s in servers:
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
            resolve_skill_mirror_write_path,
        )

        expanded = resolve_skill_aliases(expand_collections(list(persona.skills)))
        resolved_names = {getattr(s, "name", "") for s in scanned}
        mirror_specs = load_skill_mirror(
            resolve_skill_mirror_write_path(PersonaCoreConfig().skill_mirror_path)
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
    ]:
        """Build the A4 loop-side interpreters (standing recognizer + amendment + steering).

        The precision layer of the contract flow (A4-D-2/T9/T9b) — all three share one small-tier
        backend. **Fail-soft** (the text_summarize precedent): if the small backend is unavailable
        (keyless env / no tier), returns ``(None, None, None)`` so the loop's A4 gates stay inert
        and ordinary chat is byte-unchanged — never a construction failure.
        """
        if self._tier_registry is None:
            return None, None, None
        try:
            backend = self._tier_registry.get("small")
        except (ProviderError, TierNotConfiguredError) as exc:
            _logger.warning(
                "task origination not wired — small-tier backend unavailable: {error}",
                error=type(exc).__name__,
            )
            return None, None, None
        from persona_runtime.task_origination import (
            ModelAmendmentInterpreter,
            ModelStandingIntentJudge,
            ModelSteeringInterpreter,
            StandingIntentRecognizer,
        )

        recognizer = StandingIntentRecognizer(
            ModelStandingIntentJudge(
                backend=backend, default_timezone=self._core_config.default_timezone
            )
        )
        return (
            recognizer,
            ModelAmendmentInterpreter(backend=backend),
            ModelSteeringInterpreter(backend=backend),
        )

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

        recognizer, amendment_interpreter, steering_interpreter = self._build_task_origination()
        loop = ConversationLoop(
            persona=persona,
            stores=self._build_stores(),
            toolbox=toolbox,  # type: ignore[arg-type]
            skill_scanner=scanner,
            skill_injector=SkillInjector(),
            scanned_skills=scanned,  # type: ignore[arg-type]
            history_manager=ConversationHistoryManager(),
            prompt_builder=PromptBuilder(),
            router=Router(),
            tier_registry=self._tier_registry,
            turn_log_writer=self._turn_log_writer,
            # Spec 23 T13: app-scoped intelligent-routing wiring. The shared
            # latency tracker persists per-model EWMA across requests; the
            # IntelligentRouter is consulted only when the persona opted in
            # (routing.intelligent.enabled) — default-off personas route
            # byte-identically (criterion 11). A persona with an unenforceable
            # per-day cap fails loud at this construction (D-23-7 ruling).
            latency_tracker=self._latency_tracker,
            intelligent_router=self._intelligent_router,
            # K3: the owner-scoped graph-knowledge retrieval (None until graph
            # writes were enabled — additive, zero-graph otherwise).
            graph_retrieval=self._build_graph_retrieval(),
            # Spec S1 (S1-D-7 / S1-D-X-consent-wiring): the injection audit sink
            # (same JSONL posture the stores use).
            audit_logger=JSONLAuditLogger(self._audit_root),
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
            router=Router(),
            tier_registry=self._tier_registry,
            # Spec S1 (S1-D-7): injection audit sink.
            audit_logger=JSONLAuditLogger(self._audit_root),
            # Spec S3 (S3-D-2): the real consent store (empty ≡ DenyUnvettedConsent).
            skill_consent=PostgresSkillConsentStore(self._engine),
        )
        loop.deferred_input_files = deferred_holder
        return loop

    async def build_title(self, first_message: str) -> str:
        """Generate a short (≤5-word) conversation title from the first message,
        using the **small** tier (pure boilerplate — architecture §5.1.1). Returns
        the title text; the caller (chat_service) applies it best-effort."""
        from datetime import UTC, datetime

        from persona.schema.conversation import ConversationMessage

        backend = self._tier_registry.get("small")
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

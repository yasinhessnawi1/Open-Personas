"""API-side ``code_execution`` tool factory composing pool + credits (spec 12 T10).

Bridges:

- :class:`persona_api.sandbox.pool.SandboxPool` — owns the substrate session
  lifecycle (per-tenant cap, idle reap, composition-root invariant).
- :func:`persona.sandbox.make_code_execution_tool` — the T03 tool body that
  the runtime/agentic loop dispatch through unchanged.
- :func:`persona_api.services.credits_service.deduct` — flat per-execution
  billing per D-12-3 (mirrors D-08-6: only successful executions are billed).
- :class:`SandboxRequestContext` — per-request ``(owner_id, conversation_id)``
  thread via contextvars so the loop builders' signatures stay stable.

**Lazy-eager acquire (D-12-17):** the pool's ``acquire()`` is called from the
tool's ``pre_execute_hook`` on first dispatch — not at toolbox-build time.
This matches D-12-17's "warm=0 with lazy-eager prewarm" semantics: a
conversation that never invokes ``code_execution`` never spawns a substrate
sandbox. Pool ``SandboxError`` raises (quota, unavailability) flow through
the T03 catch-and-convert path and surface as ``ToolResult(is_error=True)``.

**Credits hook:** ``credits_service.deduct`` is sync (short DB transaction);
the hook wraps it with :func:`asyncio.to_thread` so the event loop isn't
blocked. Hook failure is caught inside ``make_code_execution_tool`` so a
billing-write outage cannot break the tool's structured result (the model
still sees the successful execution).
"""

from __future__ import annotations

import asyncio
from pathlib import Path  # noqa: TC003 — runtime use in workspace path resolution
from typing import TYPE_CHECKING

from persona.logging import get_logger
from persona.sandbox.result import SandboxFile
from persona.sandbox.tool import make_code_execution_tool

from persona_api.editions import MeteredCreditsPolicy
from persona_api.sandbox.context import get_sandbox_request_context

if TYPE_CHECKING:
    from collections.abc import Callable

    from persona.sandbox.result import (
        ExecutionResult,
        NetworkPolicy,
        ResourceLimits,
    )
    from persona.tools.audit import ToolAuditLogger
    from persona.tools.protocol import AsyncTool
    from sqlalchemy import Engine

    from persona_api.editions import CreditsPolicy
    from persona_api.sandbox.pool import SandboxPool

__all__ = ["make_pool_code_execution_tool"]

_logger = get_logger("sandbox.runtime_tool")

# D-12-3 flat per-execution credit cost (v0.1). v0.2 switches to duration-
# based against E2B's per-second billing per D-12-3's "Reversibility ~1 day".
_CODE_EXECUTION_CREDIT_COST = 1


def make_pool_code_execution_tool(
    *,
    pool: SandboxPool,
    rls_engine: Engine,
    credits_policy: CreditsPolicy | None = None,
    network_policy: NetworkPolicy | None = None,
    resource_limits: ResourceLimits | None = None,
    audit_logger: ToolAuditLogger | None = None,
    persona_id: str | None = None,
    credit_cost: int = _CODE_EXECUTION_CREDIT_COST,
    deferred_input_files_provider: Callable[[], list[SandboxFile]] | None = None,
    workspace_root: Path | None = None,
) -> AsyncTool:
    """Build the API-composed ``code_execution`` tool.

    On dispatch (per call):

      1. Read the per-request :class:`SandboxRequestContext` from contextvars
         (set by ``chat_service.stream_chat``). Without context, the tool
         dispatches in stateless one-shot mode (no pool acquire).
      2. ``pre_execute_hook`` lazily acquires the pool session for
         ``(owner_id, conversation_id)``. Pool ``SandboxError`` (quota,
         unavailability) flows through the T03 catch path → structured
         ``ToolResult(is_error=True)`` so the model recovers.
      3. T03 body dispatches to ``pool.sandbox.execute(code, session_id=...)``.
      4. ``on_execute_success`` (outcome=="ok") deducts ``credit_cost`` credits
         via :func:`credits_service.deduct` wrapped in :func:`asyncio.to_thread`.

    Args:
        pool: The hosted :class:`SandboxPool` (composition-root-owned).
        rls_engine: The RLS-scoped engine used by ``credits_service.deduct``.
        network_policy / resource_limits: Per-persona policy (D-12-4 / D-12-1).
        audit_logger / persona_id: Forwarded to the T03 factory for audit emission.
        credit_cost: Flat per-execution credit deduction (D-12-3 default = 1).
        deferred_input_files_provider: Spec 16 M1a wiring — a drain-and-clear
            callable that returns the list of supplements the runtime
            staged on the use_skill intercept (D-16-2 / D-16-2-state-location).
            Wired by :class:`RuntimeFactory` to the per-request loop's public
            ``deferred_input_files`` attribute; called once per dispatch and
            its return value is passed as ``input_files=`` to the substrate.
            ``None`` ⇒ no supplements staged (pre-M1a tests / CLI / contexts
            that never activate skills).
        workspace_root: Global workspace root (the directory the API serves
            via ``GET /v1/personas/:id/uploads/{ref:path}``). When set
            together with ``persona_id``, the tool gains two production
            behaviours (D-17-X-bytes-persistence + D-12-X-read-produced-file):

            * **Persist after dispatch.** Each entry in
              ``ExecutionResult.produced_files`` is copied from the sandbox
              session's host_out into ``workspace_root/owner_id/persona_id/<ref>``
              via :meth:`CodeSandbox.copy_produced_file_to`. The existing
              upload-serve route then reads them via the slash-aware ref
              logic at ``image_service.fetch:300``.
            * **Stage intermediate/* before dispatch.** Files at
              ``workspace_root/owner_id/persona_id/intermediate/`` are
              augmented onto ``deferred_input_files_provider``'s output so the
              cross-turn re-load discipline (the SKILL.md's parquet cache
              pattern) survives Spec 12 sessions that get reaped.

            ``None`` ⇒ no persistence and no staging (CLI / tests / contexts
            where bytes-to-workspace is out of scope).

    Returns:
        An :class:`AsyncTool` named ``code_execution`` ready to register in
        the toolbox via :func:`build_default_toolbox`'s ``extra_tools`` slot.
    """
    # Spec 33 (D-33-X-creditspolicy-di): production (RuntimeFactory) passes the
    # edition's policy; default to metered so a direct call keeps today's behavior.
    policy: CreditsPolicy = credits_policy or MeteredCreditsPolicy()

    def _session_id_provider() -> str | None:
        ctx = get_sandbox_request_context()
        return ctx.session_id if ctx is not None else None

    async def _pre_execute_hook() -> None:
        """Lazy-eager: acquire (or re-acquire) the pool session before execute.

        Pool ``acquire`` is idempotent on ``(user_id, conversation_id)`` — a
        re-acquire bumps last-used (so the reaper sees it fresh) without
        spawning a second substrate sandbox. Without a request context, this
        is a no-op (CLI / one-shot path).
        """
        ctx = get_sandbox_request_context()
        if ctx is None:
            return
        await pool.acquire(user_id=ctx.owner_id, conversation_id=ctx.conversation_id)

    async def _on_execute_success(_result: ExecutionResult) -> None:
        """Credits hook fired by the T03 body on outcome=="ok".

        Spec M3 (T7): sandbox exec is a **standalone infra-flat** surface — a
        genuinely standalone op (no enclosing billed turn/run subsumes it), so it
        keeps its own ledger row. Reclassified into the M3 schema: basis
        ``infra_flat`` (a zero-provider-cost surface — the ~1-credit charge is the
        per-exec infra rate, not a provider cost), ``cost_cents=0.0`` (no provider
        cost to record), reason ``sandbox:infra_flat``. The flat ``credit_cost``
        amount is unchanged (D-12-3).
        """
        ctx = get_sandbox_request_context()
        if ctx is None:
            # No request context → CLI / one-shot path; no billing.
            return
        await asyncio.to_thread(
            policy.deduct,
            rls_engine=rls_engine,
            user_id=ctx.owner_id,
            amount=credit_cost,
            reason="sandbox:infra_flat",
            cost_cents=0.0,
            cost_basis="infra_flat",
        )

    def _resolve_persona_workspace() -> Path | None:
        """Compute ``<workspace_root>/<owner_id>/<persona_id>`` per-call.

        Returns ``None`` when any of (workspace_root, persona_id, ctx) is
        missing — the persist + stage paths are disabled. This matches the
        CLI / one-shot semantics: no ctx ⇒ no per-tenant workspace.
        """
        if workspace_root is None or persona_id is None:
            return None
        ctx = get_sandbox_request_context()
        if ctx is None:
            return None
        return workspace_root / ctx.owner_id / persona_id

    def _augmented_input_files_provider() -> list[SandboxFile]:
        """Compose Spec 16 M1a supplements + Spec 17 cross-turn intermediate.

        D-17-X-bytes-persistence inverse flow: before each dispatch, stage
        ``<persona_workspace>/intermediate/*`` files into ``input_files`` so
        the SKILL.md's parquet re-load pattern works across Spec 12 session
        reaps (D-12-17). Composes with the existing supplements provider;
        does not replace it.
        """
        files: list[SandboxFile] = []
        if deferred_input_files_provider is not None:
            files.extend(deferred_input_files_provider())
        persona_workspace = _resolve_persona_workspace()
        if persona_workspace is None:
            return files
        intermediate_dir = persona_workspace / "intermediate"
        if not intermediate_dir.is_dir():
            return files
        for path in sorted(intermediate_dir.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(persona_workspace).as_posix()
            try:
                content = path.read_bytes()
            except OSError as exc:
                _logger.warning(
                    "intermediate file read failed; skipping",
                    path=rel,
                    exc_type=type(exc).__name__,
                )
                continue
            files.append(
                SandboxFile(
                    path=rel,
                    content_bytes=content,
                    size_bytes=len(content),
                    media_type="application/octet-stream",
                )
            )
        return files

    def _classify_for_sidecar(
        ref: str,
    ) -> tuple[str, str] | None:
        """Classify a produced-file ref into (type, producing_spec) for the
        F5 sidecar, or return None for refs that get no sidecar.

        Mirrors the three-branch persist-target policy below:

        * ``charts/`` → ("chart", "17") — Spec 17 matplotlib.
        * ``intermediate/`` → None — not user-facing; no sidecar so the
          F5 artifact list doesn't surface intermediate cache files.
        * ``uploads/<filename>.<ext>``:
            - document extensions (.docx/.pptx/.xlsx/.pdf) → ("doc", "16")
            - data extensions (.parquet/.csv/.json) → ("data", "12")
            - else → ("doc", "12") — safe fallback for general bare refs.
        """
        if ref.startswith("charts/"):
            return ("chart", "17")
        if ref.startswith("intermediate/"):
            return None
        suffix = ref.rsplit(".", 1)[-1].lower() if "." in ref else ""
        if suffix in {"docx", "pptx", "xlsx", "pdf"}:
            return ("doc", "16")
        if suffix in {"parquet", "csv", "json"}:
            return ("data", "12")
        return ("doc", "12")

    async def _persist_produced_file(session_id: str, ref: str) -> str | None:
        """Copy a produced file from the sandbox session to the persona workspace.

        D-17-X-bytes-persistence call site. The inner
        :func:`make_code_execution_tool` body invokes this for every entry
        in ``ExecutionResult.produced_files`` after dispatch. The sandbox's
        :meth:`copy_produced_file_to` enforces the
        :data:`PRODUCED_FILE_CAP_BYTES` cap and raises
        :class:`ProducedFileSizeError` (a :class:`SandboxError` subclass) on
        overage — caught by the existing T03 catch-and-convert path so the
        model sees a structured error explaining the cap.

        **D-F4-X-bare-ref-resolution (Spec F4 Phase 5 T02c).** Three-branch
        persist-target policy so :func:`image_service.fetch`'s slash-aware
        resolver at ``image_service.py:300`` serves every produced file
        through ``GET /v1/personas/:id/uploads/{ref:path}`` without 404:

          * ``charts/<id>.png`` (Spec 17 matplotlib) → stays at workspace
            root. The ``charts/`` prefix is load-bearing for F4's
            inline-vs-download discriminator (D-17-X-inline-hint-shape);
            the slash triggers the resolver's pass-through branch.
          * ``intermediate/<name>.parquet`` (Spec 17 cross-turn cache) →
            stays at workspace root. Not user-facing; consumed by the
            next-turn input-staging in
            :func:`_augmented_input_files_provider` above.
          * everything else (Spec 16 docx/pptx/xlsx/pdf produced as bare
            ``<filename>.<ext>``; general bare-ref produced files) →
            routes into ``uploads/<filename>.<ext>`` so the resolver's
            slash-less branch
            (``relative = f"{_UPLOAD_DIR_NAME}/{ref}"``) lands on the
            right path.

        Pre-T02c, Spec 16 docs persisted at workspace root and the GET
        resolver looked under ``uploads/`` → every document download
        404'd. 10th entry in the additive-precedent chain (D-01-12 → … →
        this). No Spec 12 / Spec 16 re-open required.
        """
        persona_workspace = _resolve_persona_workspace()
        if persona_workspace is None:
            return None
        if ref.startswith("charts/") or ref.startswith("intermediate/"):
            target = persona_workspace / ref
        else:
            target = persona_workspace / "uploads" / ref
        await pool.sandbox.copy_produced_file_to(session_id, ref, target)

        # F5 T06 — D-F5-X-artifact-metadata-convention: write a sidecar so
        # the F5 artifact-list endpoint can filter/sort produced files.
        # ``intermediate/`` returns None — NO sidecar, preserving the
        # D-F4-X-bare-ref-resolution invariant (those files aren't
        # user-facing and shouldn't surface in the artifact view).
        # Best-effort — failure logs but does not abort the persist.
        classification = _classify_for_sidecar(ref)
        if classification is None:
            # Intermediate cache file — persisted (for cross-turn staging) but
            # NOT surfaced as a user-facing artifact (Spec 28: return None so the
            # code_execution tool omits it from ToolResult.artifacts).
            return None
        artifact_type, producing_spec = classification
        try:
            from persona_api.services.artifact_metadata import (  # noqa: PLC0415
                WorkspaceArtifactMetadata,
                utcnow,
                write_artifact_sidecar,
            )

            # Scope the sidecar to THIS conversation so the F5 artifact walk
            # (queried with ``?conversation_id=``) surfaces the file in the
            # conversation's Files viewer. A ``None`` conversation_id is filtered
            # out of the scoped listing — the produced file would persist on disk
            # but never appear in the panel. ``original_name`` is the produced
            # ref's basename (code_execution keeps real names, e.g.
            # ``marketing_strategy.pdf``), so the viewer shows a human name.
            ctx = get_sandbox_request_context()
            write_artifact_sidecar(
                target,
                WorkspaceArtifactMetadata(
                    source="generated",
                    type=artifact_type,  # type: ignore[arg-type]
                    producing_spec=producing_spec,  # type: ignore[arg-type]
                    conversation_id=ctx.conversation_id if ctx is not None else None,
                    created_at=utcnow(),
                    original_name=ref.rsplit("/", 1)[-1] or None,
                ),
            )
        except Exception:  # noqa: BLE001 — sidecar failure non-fatal
            # No logger wired here; the persist succeeded, the sidecar
            # is enrichment. Future telemetry can surface failures via
            # audit log if needed.
            pass

        # Spec 28 — surface the persisted file as a ToolResult.artifact. The ref
        # is workspace-relative (the GET /uploads/{ref:path} route serves it).
        return target.relative_to(persona_workspace).as_posix()

    return make_code_execution_tool(
        pool.sandbox,
        network_policy=network_policy,
        resource_limits=resource_limits,
        audit_logger=audit_logger,
        persona_id=persona_id,
        session_id_provider=_session_id_provider,
        pre_execute_hook=_pre_execute_hook,
        on_execute_success=_on_execute_success,
        deferred_input_files_provider=_augmented_input_files_provider,
        produced_file_persister=_persist_produced_file,
    )

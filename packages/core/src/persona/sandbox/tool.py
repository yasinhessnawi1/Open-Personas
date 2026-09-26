"""The ``code_execution`` first-class tool factory (spec 12 T03).

Wraps any :class:`CodeSandbox` into an :class:`AsyncTool` registered in the
existing :class:`Toolbox`. The persona's tool allow-list machinery (D-03-7)
gates whether a given persona can call ``code_execution`` at all; the spec-11
fix-#1 ``_dispatch`` wrappers in both loops catch ``ToolNotAllowedError`` and
:class:`SandboxError` and convert to ``ToolResult(is_error=True, ...)`` so the
SSE stream never crashes.

§9 acceptance touched by T03:
- **#1** trivial snippet round-trip — the tool's wire shape.
- **#10** large stdout truncated at ``max_stdout_bytes`` with an EXPLICIT
  marker, never dropped silently. The marker is the literal prefix
  ``[truncated: N bytes omitted]`` so a downstream consumer can recognise it
  without ambiguity. Asserted by T04's "truncated-code is recognisably
  truncated" test row.
- **#11** allow-list — the Toolbox enforces the literal-only allow-list
  unchanged (D-03-7); this factory just produces an :class:`AsyncTool` named
  ``code_execution``.
- **#13** audit emission — every execution emits exactly one
  :class:`ToolAuditEvent` with ``action="execute"`` (D-12-8); ``metadata``
  carries ``code`` (truncated to 4 KiB with the same marker), ``code_sha256``
  (full-fidelity reference), ``outcome``, ``duration_ms``, ``exit_status``.

Decisions exercised:
- **D-12-4**: ``NetworkPolicy`` is constructed by the factory FROM the persona,
  not passed by the model in the tool call. The model only supplies ``code``.
- **D-12-1**: ``session_id`` is provided by an injected ``session_id_provider``
  callable that the composition root (T10) sets per-conversation (tenant-
  isolated as ``f"{owner_id}:{conversation_id}"``; kickoff trip-up #6).
  Default returns ``None`` → stateless one-shot.
- **D-12-6**: ``SandboxError`` family is caught and converted to
  ``ToolResult(is_error=True, ...)`` — the loops' ``_dispatch`` wrappers also
  catch as a second line of defence (spec-11 fix #1 discipline).
- **D-12-8**: Audit emits even on failure (the audit trail is the forensic
  record of what was attempted, not just what succeeded). Failed-write
  pattern (D-03-21) is the contrast — we audit *all* executions.
- **D-12-14**: :class:`ExecutionResult` is Pydantic-frozen; tool factory reads
  fields via attribute access.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from persona.logging import get_logger
from persona.sandbox.errors import ProducedFileRefusedError, SandboxError
from persona.sandbox.result import (
    ExecutionResult,
    NetworkPolicy,
    RefusedFile,
    ResourceLimits,
    SandboxFile,
    refused_file,
)
from persona.schema.tools import PersistedArtifact, ToolResult
from persona.tools.audit import ToolAuditEvent
from persona.tools.protocol import AsyncTool, tool

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from persona.sandbox.protocol import CodeSandbox
    from persona.tools.audit import ToolAuditLogger

__all__ = ["TRUNCATION_MARKER_PREFIX", "make_code_execution_tool"]

_logger = get_logger("sandbox.tool")

# The truncation marker is a *literal prefix* so downstream consumers can
# recognise truncated stdout / audit-code without ambiguity (T04 asserts the
# marker is present and parseable). Includes the byte count so the consumer
# knows how much was omitted.
TRUNCATION_MARKER_PREFIX = "[truncated:"
"""Literal prefix used by every truncation marker the tool factory writes.

Full marker shape: ``"\\n\\n[truncated: N bytes omitted]"`` where ``N`` is
the byte count omitted. Downstream consumers (the audit log, the model
itself reading stdout) can detect truncation by searching for this prefix.
"""

# D-12-8 audit-code cap: 4 KiB covers the median LLM-generated snippet
# (~500 chars per R-12-4) with headroom. Over the cap, code is truncated in
# the audit log but the unconditional code_sha256 keeps full-fidelity recovery.
_AUDIT_CODE_CAP_BYTES = 4 * 1024

# Default factory description fed to the model. Kept terse — the model's
# system prompt is the right place for additional usage guidance.
# Spec 25 T11b (§2.9): affirmative-capability framing — lead with "YOU CAN".
_DEFAULT_DESCRIPTION = (
    "YOU CAN run Python code. Use this tool whenever the user asks for "
    "calculations, data analysis, file processing, charts, or documents — "
    "do not say you cannot run code: call this tool. It executes Python in a "
    "secure sandbox and returns stdout, stderr, and any files the code writes "
    "to the workspace. "
    "NO internet access: the sandbox has NO network egress. Do NOT run "
    "pip/apt/npm/curl/wget or any install command — it will hang and time out. "
    "Use ONLY pre-installed libraries: numpy, pandas, matplotlib, python-docx, "
    "openpyxl, Pillow (PIL). reportlab and python-pptx are NOT available."
)


def make_code_execution_tool(
    sandbox: CodeSandbox,
    *,
    network_policy: NetworkPolicy | None = None,
    resource_limits: ResourceLimits | None = None,
    audit_logger: ToolAuditLogger | None = None,
    persona_id: str | None = None,
    session_id_provider: Callable[[], str | None] | None = None,
    pre_execute_hook: Callable[[], Awaitable[None]] | None = None,
    on_execute_success: Callable[[ExecutionResult], Awaitable[None]] | None = None,
    deferred_input_files_provider: Callable[[], list[SandboxFile]] | None = None,
    produced_file_persister: Callable[[str, str], Awaitable[str | None]] | None = None,
    description: str = _DEFAULT_DESCRIPTION,
) -> AsyncTool:
    """Build the ``code_execution`` :class:`AsyncTool`.

    Args:
        sandbox: The :class:`CodeSandbox` implementation to dispatch to —
            :class:`LocalDockerSandbox` (T05) for the CLI / open-source path;
            ``HostedSandbox`` (T08) for the hosted path.
        network_policy: Per-persona network policy (D-12-4 — constructed by
            the caller from the persona's YAML, never passed by the model).
            ``None`` defaults to ``NetworkPolicy()`` (egress disabled).
        resource_limits: Per-persona resource caps. ``None`` defaults to
            :class:`ResourceLimits` (sensible conservative defaults from
            spec §4.1).
        audit_logger: Optional :class:`ToolAuditLogger`. When provided, every
            execution emits one :class:`ToolAuditEvent` with
            ``action="execute"`` (D-12-8 / acceptance §9 #13).
        persona_id: Persona identifier for audit records. ``None`` for CLI
            development; audit lines then route to ``_cli.tools.jsonl``.
        session_id_provider: Callable returning the current conversation's
            session_id (tenant-isolated as ``f"{owner_id}:{conversation_id}"``
            per kickoff trip-up #6). ``None`` ⇒ stateless one-shot execution.
            Defaults to ``lambda: None`` if not supplied. Resolved lazily on
            every dispatch — supports composition roots that use contextvars
            for per-request session state (D-08-1 pattern).
        pre_execute_hook: Async hook awaited BEFORE the substrate call. The api
            wires this to ``pool.acquire(...)`` so the per-tenant sandbox session
            is lazy-eager-acquired on first dispatch (D-12-17: warm=0;
            substrate cold-start paid here, not at conversation creation).
            Hook may raise :class:`SandboxError` — caught by the same path as
            substrate failures and surfaced to the model as a structured
            ``ToolResult(is_error=True)``. ``None`` ⇒ no prelude (CLI / tests).
        on_execute_success: Async hook fired AFTER a successful execute
            (``result.outcome == "ok"``) and BEFORE the tool's :class:`ToolResult`
            is returned. The api wires this to the **flat per-execution credits
            deduction** per **D-12-3** (mirrors D-08-6: only successful executions
            are billed; OOM/timeout/killed are not). Hook exceptions are caught
            and logged so a credits-write failure cannot break the tool's result.
            ``None`` ⇒ no hook (CLI path; tests that don't exercise billing).
        produced_file_persister: Async callable ``(session_id, ref) ->
            workspace_ref | None`` invoked AFTER execute for each entry in
            ``result.produced_files``. The api wires this to a closure that
            calls :meth:`CodeSandbox.copy_produced_file_to` with a destination
            under the persona workspace (``D-17-X-bytes-persistence``) and
            **returns the resulting workspace-relative ref** (Spec 28
            D-28-X-sandbox-consolidation-scope) so the tool can surface each
            persisted file as a :class:`PersistedArtifact` on
            :attr:`ToolResult.artifacts` — the OUTPUT shape is unified with the
            bytes-persister tools; the file-copy mechanism is unchanged. Fires
            for every produced file regardless of outcome (partial-success runs
            may still produce charts before erroring). ``ProducedFileSizeError``
            propagates and is caught by the ``SandboxError`` catch-and-convert
            path above — the model sees a structured error explaining the cap
            and can produce a smaller file. A ``None`` return ⇒ that file was
            not surfaced as an artifact (still persisted). ``None`` callback ⇒
            no persistence (CLI / tests that don't need the bytes outside the
            sandbox).
        description: Tool description fed to the model. The default covers
            the spec §6 wording; override to surface persona-specific guidance.

    Returns:
        An :class:`AsyncTool` named ``code_execution`` registered against the
        provided sandbox. The model supplies only ``code: str`` — every other
        parameter (session, policy, limits) is bound at factory time.

    Notes:
        The returned tool follows the @tool / Toolbox contract unchanged:
        argument-validation errors and body-raised exceptions are converted
        to ``ToolResult(is_error=True, ...)`` by the ``@tool`` decorator
        (D-03-5). :class:`SandboxError` subclasses (D-12-6 family) are caught
        here at the body and surfaced as structured failure results so the
        model can recover (kickoff trip-up: ``SandboxUnavailableError`` =
        "Docker not available" → model explains to user). The conversation /
        agentic loops' ``_dispatch`` wrappers catch any escape as a second
        line of defence (spec-11 fix #1).
    """
    network = network_policy if network_policy is not None else NetworkPolicy()
    limits = resource_limits if resource_limits is not None else ResourceLimits()
    _provider = session_id_provider if session_id_provider is not None else (lambda: None)

    @tool(name="code_execution", description=description)
    async def code_execution(code: str) -> ToolResult:
        session_id = _provider()
        code_sha256 = hashlib.sha256(code.encode("utf-8")).hexdigest()
        # D-25-4 per-turn telemetry: flipped True only when a vanished session
        # was transparently recreated for THIS dispatch. Surfaced in the
        # ToolResult metadata so the runtime turn loop can mirror it into the
        # additive ``TurnLog.sandbox_session_recreated`` field (wired in
        # Cluster B/D — Spec 18 D-18-1 NOT reopened).
        session_recreated = False
        # Spec 28 — collected on success below; declared here so the final
        # ToolResult can reference it (the SandboxError path returns early).
        produced_artifacts: list[PersistedArtifact] = []
        # Names of produced files discarded for being 0 bytes (empty/invalid) —
        # surfaced to the model so it regenerates them rather than assuming the
        # write succeeded.
        empty_files: list[str] = []
        refused_files: list[RefusedFile] = []
        try:
            if pre_execute_hook is not None:
                await pre_execute_hook()
            deferred = deferred_input_files_provider() if deferred_input_files_provider else None

            async def _run() -> ExecutionResult:
                return await sandbox.execute(
                    code,
                    session_id=session_id,
                    timeout_s=limits.wall_clock_s,
                    limits=limits,
                    network=network,
                    input_files=deferred,
                )

            try:
                result = await _run()
            except SandboxError as exc:
                # D-25-4 session auto-recovery: when a stateful session has
                # vanished underneath us (substrate reaped it; pod restart;
                # idle-timeout race), recreate it once and retry EXACTLY ONCE.
                # Only ONE recovery attempt — looping here would mask a real,
                # persistent session failure (kickoff "don't auto-recover more
                # than once" discipline). A non-``no_session`` error, a one-shot
                # (``session_id is None``), a create_session failure, or a
                # second no_session all fall through to the structured-error
                # path below.
                if exc.context.get("reason") != "no_session" or session_id is None:
                    raise
                _logger.warning(
                    "code_execution session vanished; recreating once (D-25-4)",
                    persona_id=persona_id or "<unknown>",
                    session_id=session_id,
                )
                await sandbox.create_session(session_id, limits=limits, network=network)
                session_recreated = True
                # Single retry. If THIS raises (no_session again, or anything
                # else), it propagates to the structured-error path — there is
                # deliberately no second recovery attempt.
                result = await _run()
            # D-17-X-bytes-persistence: persist produced bytes to the API
            # workspace so the existing GET /uploads/{ref:path} route can
            # serve them. ProducedFileSizeError propagates here and is caught
            # by the SandboxError block below so the model gets a structured
            # error and can produce a smaller file (D-12-X-read-produced-file).
            # Fires for every produced file regardless of outcome — a
            # partial-success run that produced a chart before erroring still
            # gets its bytes persisted so the model can reference them.
            if produced_file_persister is not None and session_id is not None:
                for sf in result.produced_files:
                    # A 0-byte produced file is never a usable artifact: it can
                    # only render as a broken download (an empty PDF →
                    # InvalidPDFException), and persisting it would OVERWRITE a
                    # good earlier file at the same workspace path (produced files
                    # keep their real names, so an iterating model that re-emits
                    # the same filename clobbers the prior version). Skip it
                    # entirely — no copy, no surface, no clobber. The empty file
                    # still appears in ``result.produced_files`` with size 0, so
                    # the model sees it and can regenerate.
                    if sf.size_bytes == 0:
                        _logger.debug(
                            "skipping 0-byte produced file",
                            path=sf.path,
                            persona_id=persona_id or "<unknown>",
                        )
                        empty_files.append(sf.path)
                        continue
                    try:
                        workspace_ref = await produced_file_persister(session_id, sf.path)
                    except ProducedFileRefusedError as refusal:
                        # Spec WIN T1.5: one refused file (a name the workspace does
                        # not allow, or a link in the way) is skipped and explained;
                        # every other produced file is still saved.
                        refused_files.append(
                            refused_file(sf.path, refusal.context.get("reason", str(refusal)))
                        )
                        continue
                    # Spec 28 — surface the persisted file as an artifact so the
                    # chat UI renders a file card. None ⇒ persisted but not
                    # surfaced (e.g. CLI / no workspace). The file-copy callback
                    # is the persistence mechanism (D-17-X), unchanged.
                    if workspace_ref is not None:
                        produced_artifacts.append(
                            PersistedArtifact(
                                workspace_path=workspace_ref,
                                mime_type=sf.media_type,
                                size_bytes=sf.size_bytes,
                                rendered_inline=sf.media_type.startswith("image/"),
                            )
                        )
                # F4 operator-pass diagnostic: surface what was just persisted
                # so a downstream "no chart in chat" investigation has the
                # discovered-file inventory without needing extra tracing.
                _logger.debug(
                    "code_execution produced files persisted",
                    persona_id=persona_id or "<unknown>",
                    session_id=session_id,
                    produced_count=len(result.produced_files),
                    produced=[
                        {"path": sf.path, "media_type": sf.media_type}
                        for sf in result.produced_files
                    ],
                )
        except SandboxError as exc:
            # D-12-6 catch-and-convert: any sandbox-family error surfaces as
            # a structured failure result so the model can recover. The
            # loops' _dispatch wrappers catch unrelated exceptions as a
            # second line of defence (spec-11 fix #1).
            _logger.warning(
                "code_execution dispatch failed",
                exc_type=type(exc).__name__,
                persona_id=persona_id or "<unknown>",
            )
            _emit_audit_for_error(
                audit_logger=audit_logger,
                persona_id=persona_id,
                session_id=session_id,
                code=code,
                code_sha256=code_sha256,
                exc=exc,
            )
            return ToolResult(
                tool_name="code_execution",
                content=f"{type(exc).__name__}: {exc}",
                is_error=True,
                data={"error_type": type(exc).__name__, "context": exc.context},
                metadata={
                    "outcome": "error",
                    "session_id": session_id or "",
                    "code_sha256": code_sha256,
                    # D-25-4: True when a recreate+retry still ended in failure
                    # (the recovery was attempted this turn but did not save it).
                    "sandbox_session_recreated": str(session_recreated),
                },
            )

        # D-12-3 credits hook — fires on outcome=="ok" only (mirrors D-08-6
        # "failed turn deducts nothing"). Hook failure is logged and swallowed
        # so a billing-write error cannot break the tool's structured result.
        if on_execute_success is not None and result.outcome == "ok":
            try:
                await on_execute_success(result)
            except Exception as exc:  # noqa: BLE001 — hook failure must not break the tool
                _logger.warning(
                    "code_execution credits hook failed; tool result unchanged",
                    exc_type=type(exc).__name__,
                    persona_id=persona_id or "<unknown>",
                    session_id=session_id or "",
                )

        formatted = _format_result_for_model(result, limits)
        # An empty produced file is a silent failure: the code "ran" but wrote
        # nothing usable (a 0-byte PDF can't open). Tell the model explicitly so
        # it regenerates rather than reporting success. This also flips the
        # result to is_error so the agentic loop treats the turn as recoverable.
        if empty_files:
            names = ", ".join(sorted(empty_files))
            formatted = (
                f"{formatted}\n\n"
                f"ERROR: these produced files were empty (0 bytes) and were "
                f"discarded: {names}. The write did not succeed — regenerate the "
                f"file and verify it is non-empty before finishing."
            )
        all_refused = (*result.refused_files, *refused_files)
        if all_refused:
            listed = "; ".join(f"{r.path} ({r.reason})" for r in all_refused)
            formatted = (
                f"{formatted}\n\n"
                f"ERROR: these produced files were not saved, because their names or "
                f"locations are not allowed in the workspace: {listed}. Save files "
                f"under plain relative names such as 'report.pdf' or 'charts/plot.png'."
            )
        _emit_audit_for_result(
            audit_logger=audit_logger,
            persona_id=persona_id,
            session_id=session_id,
            code=code,
            code_sha256=code_sha256,
            result=result,
        )
        # F4 operator-pass diagnostic: log what is about to enter the
        # ToolResult.data envelope so the rich-output investigation has the
        # final shape (paths + media types) without re-running the sandbox.
        _logger.debug(
            "code_execution result materialised",
            persona_id=persona_id or "<unknown>",
            session_id=session_id or "",
            outcome=result.outcome,
            produced_count=len(result.produced_files),
            produced=[
                {"path": sf.path, "media_type": sf.media_type} for sf in result.produced_files
            ],
        )
        return ToolResult(
            tool_name="code_execution",
            content=formatted,
            # outcome != "ok" surfaces as is_error so the model recovers (the
            # loops also feed is_error back without crashing the stream). An
            # empty produced file is also a recoverable failure even when the
            # code exited 0 — the deliverable wasn't actually written.
            is_error=result.outcome != "ok" or bool(empty_files) or bool(all_refused),
            data={
                "outcome": result.outcome,
                "exit_status": result.exit_status,
                "duration_ms": result.duration_ms,
                "produced_files": [
                    {"path": f.path, "size_bytes": f.size_bytes, "media_type": f.media_type}
                    for f in result.produced_files
                ],
                "truncated_stdout": result.truncated_stdout,
                "truncated_files": result.truncated_files,
            },
            truncated=result.truncated_stdout or result.truncated_files,
            metadata={
                "outcome": result.outcome,
                "session_id": session_id or "",
                "duration_ms": f"{result.duration_ms:.1f}",
                "exit_status": str(result.exit_status),
                "code_sha256": code_sha256,
                # D-25-4: True when this dispatch transparently recreated a
                # vanished session and the retry succeeded.
                "sandbox_session_recreated": str(session_recreated),
            },
            # Spec 28 — produced files surfaced as artifacts (output-shape unify;
            # persistence stays the D-17-X file-copy callback).
            artifacts=tuple(produced_artifacts),
        )

    return code_execution


# ----- internals ---------------------------------------------------------------


def _format_result_for_model(result: ExecutionResult, limits: ResourceLimits) -> str:
    """Render an :class:`ExecutionResult` for the model to read.

    Layout:
      1. ``stdout`` — truncated at ``limits.max_stdout_bytes`` with the
         explicit marker if exceeded (acceptance §9 #10).
      2. A trailing outcome line so the model can see immediately what
         happened (``outcome=ok exit=0 duration_ms=12.3``).
      3. If non-empty, ``stderr`` under a ``-- stderr --`` divider.
      4. If any, a ``-- files --`` list of produced files (one per line:
         ``path (size_bytes, media_type)``).

    Truncation marker is the literal prefix :data:`TRUNCATION_MARKER_PREFIX`
    so downstream consumers (T04 security suite, the audit log) can detect
    truncation unambiguously.
    """
    stdout, stdout_truncated = _truncate(result.stdout, limits.max_stdout_bytes)
    sections: list[str] = []
    if stdout:
        sections.append(stdout)
    elif not stdout_truncated:
        # Make absence of stdout visible to the model when nothing else
        # would land in the rendered output (e.g., outcome=ok exit=0
        # produced one file but printed nothing).
        sections.append("(no stdout)")

    sections.append(
        f"-- outcome --\noutcome={result.outcome} "
        f"exit_status={result.exit_status} "
        f"duration_ms={result.duration_ms:.1f}"
    )

    if result.stderr:
        stderr, _ = _truncate(result.stderr, limits.max_stdout_bytes)
        sections.append(f"-- stderr --\n{stderr}")

    if result.produced_files:
        rendered_files = "\n".join(
            f"{f.path} ({f.size_bytes} bytes, {f.media_type})" for f in result.produced_files
        )
        suffix = "\n[truncated: produced file list capped]" if result.truncated_files else ""
        sections.append(f"-- files --\n{rendered_files}{suffix}")

    return "\n\n".join(sections)


def _truncate(text: str, max_bytes: int) -> tuple[str, bool]:
    """Truncate ``text`` to at most ``max_bytes`` UTF-8 bytes with an explicit marker.

    Returns ``(truncated_text, was_truncated)``. The marker is appended to the
    returned text when truncation fires; the marker itself adds bytes but the
    overall byte count of the returned string is allowed to slightly exceed
    ``max_bytes`` so the cap is on *content*, not on the rendered length —
    keeps the rendering predictable and the marker unambiguous.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    omitted = len(encoded) - max_bytes
    # Decode the kept prefix, replacing any partial trailing multi-byte char
    # to avoid raising on a mid-character cut.
    kept = encoded[:max_bytes].decode("utf-8", errors="replace")
    marker = f"\n\n{TRUNCATION_MARKER_PREFIX} {omitted} bytes omitted]"
    return kept + marker, True


def _truncate_for_audit(code: str) -> tuple[str, bool]:
    """Truncate code for audit-log inline storage (D-12-8: 4 KiB cap).

    The unconditional ``code_sha256`` companion field keeps full-fidelity
    forensic recovery — this is the inline preview, not the full record.
    """
    return _truncate(code, _AUDIT_CODE_CAP_BYTES)


def _emit_audit_for_result(
    *,
    audit_logger: ToolAuditLogger | None,
    persona_id: str | None,
    session_id: str | None,
    code: str,
    code_sha256: str,
    result: ExecutionResult,
) -> None:
    """Emit one ``action="execute"`` audit event for a completed execution."""
    if audit_logger is None:
        return
    code_preview, code_truncated = _truncate_for_audit(code)
    audit_logger.emit(
        ToolAuditEvent(
            timestamp=datetime.now(UTC),
            persona_id=persona_id,
            tool_name="code_execution",
            action="execute",
            resource=session_id or "oneshot",
            is_error=result.outcome != "ok",
            metadata={
                "code": code_preview,
                "code_truncated": str(code_truncated),
                "code_sha256": code_sha256,
                "outcome": result.outcome,
                "exit_status": str(result.exit_status),
                "duration_ms": f"{result.duration_ms:.1f}",
                "truncated_stdout": str(result.truncated_stdout),
                "truncated_files": str(result.truncated_files),
            },
        )
    )


def _emit_audit_for_error(
    *,
    audit_logger: ToolAuditLogger | None,
    persona_id: str | None,
    session_id: str | None,
    code: str,
    code_sha256: str,
    exc: SandboxError,
) -> None:
    """Emit one ``action="execute"`` audit event for a backend-raised failure.

    Distinct from :func:`_emit_audit_for_result` because there's no
    :class:`ExecutionResult` to read — the failure mode lives in the
    exception type and its ``context``.
    """
    if audit_logger is None:
        return
    code_preview, code_truncated = _truncate_for_audit(code)
    audit_logger.emit(
        ToolAuditEvent(
            timestamp=datetime.now(UTC),
            persona_id=persona_id,
            tool_name="code_execution",
            action="execute",
            resource=session_id or "oneshot",
            is_error=True,
            metadata={
                "code": code_preview,
                "code_truncated": str(code_truncated),
                "code_sha256": code_sha256,
                "outcome": "error",
                "error_type": type(exc).__name__,
                **{f"ctx_{k}": v for k, v in exc.context.items()},
            },
        )
    )

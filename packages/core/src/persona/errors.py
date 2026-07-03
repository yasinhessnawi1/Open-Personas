"""Domain exceptions for persona-core.

Every exception raised from persona-core domain logic is a subclass of
:class:`PersonaError`. Provider-specific exceptions (chromadb, httpx, etc.)
are caught at the adapter boundary and re-raised as domain exceptions so
callers depend on our types rather than on a transitive dependency.

Every domain exception accepts a ``context`` dictionary that is included in
``str(self)``. This makes log messages structured without forcing callers to
build a message-template every time — the exception carries the data it needs
to be useful in a log line.

See ``docs/specs/spec_01/decisions.md`` D-01-12 for the structured-context
rationale and ``docs/specs/spec_01/spec_01_core.md`` §11.7.
"""

from __future__ import annotations

__all__ = [
    "AuditWriteError",
    "AuthenticationError",
    "BrokenVersionChainError",
    "CalculatorError",
    "ChannelUnreachableError",
    "CheckpointTooLargeError",
    "CreditsExhaustedError",
    "DailySpendCapExceededError",
    "ApprovalNotFoundError",
    "DuplicateJobTypeError",
    "GatedActionProposedError",
    "InvalidRecurrenceRuleError",
    "JobStateError",
    "MCPBuiltinServerError",
    "MCPConnectionError",
    "MCPServerUnavailableError",
    "MessageDeliveryError",
    "OriginationForbiddenError",
    "PermanentJobError",
    "PersonaError",
    "PersonaNotFoundError",
    "PersonaSelfWriteForbiddenError",
    "RuntimeWriteForbiddenError",
    "SandboxViolationError",
    "ScheduleNotFoundError",
    "ScheduleStateError",
    "SchemaVersionMismatchError",
    "SkillArgumentValidationError",
    "SkillCompositionDepthError",
    "SkillCycleError",
    "SkillManifestError",
    "SkillNameCollisionError",
    "StoreNotFoundError",
    "TaskLegFailedError",
    "TaskNotFoundError",
    "TaskStateError",
    "ToolExecutionError",
    "ToolNotAllowedError",
    "UnknownDocumentFormatError",
    "UnknownDocumentTemplateError",
    "UnknownJobTypeError",
    "VettedSourceAuthenticityError",
]


class PersonaError(Exception):
    """Base exception for all persona-core errors.

    Args:
        message: Human-readable error description.
        context: Structured context that gets appended to ``str(self)`` so
            log records carry the data callers need to debug. Keys and values
            are stringified at format time.
    """

    def __init__(
        self,
        message: str = "",
        *,
        context: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.context: dict[str, str] = dict(context) if context else {}

    def __str__(self) -> str:
        if not self.context:
            return self.message
        ctx = " ".join(f"{k}={v}" for k, v in self.context.items())
        if self.message:
            return f"{self.message} [{ctx}]"
        return f"[{ctx}]"


class SchemaVersionMismatchError(PersonaError):
    """Raised when a persona YAML's ``schema_version`` is not supported.

    The message includes a hint about migration paths.
    """


class PersonaNotFoundError(PersonaError):
    """Raised when a persona cannot be located (by id or by path)."""


class RuntimeWriteForbiddenError(PersonaError):
    """Raised when a write violates a store's per-source policy.

    Common contexts: identity-store writes from any source, self_facts /
    worldview writes without ``force=True``. See spec 01 §5.2.
    """


class PersonaSelfWriteForbiddenError(RuntimeWriteForbiddenError):
    """Raised specifically when a ``persona_self`` write fails policy.

    Subclass of :class:`RuntimeWriteForbiddenError` so callers can either
    catch the broader error or disambiguate the persona-self path (e.g., to
    log a more specific reason or skip a self-update retry loop).
    """


class StoreNotFoundError(PersonaError):
    """Raised when a store kind is requested that the registry does not know."""


class BrokenVersionChainError(PersonaError):
    """Raised when a version chain is malformed.

    Common causes: duplicate version numbers within one ``logical_id``,
    a ``superseded_by`` pointer that does not match the next version's id,
    or a rollback target that does not exist.
    """


class AuditWriteError(PersonaError):
    """Raised when the audit logger fails to record an event.

    The store does not swallow this — failing to audit a mutation is a
    correctness issue, not an operational one.
    """


class ToolNotAllowedError(PersonaError):
    """Raised when a tool call targets a tool not in the persona's allow-list."""


class ApprovalNotFoundError(PersonaError):
    """Raised when an approval proposal/decision lookup misses (no oracle — A3 store reads)."""


class GatedActionProposedError(PersonaError):
    """Raised by the A3 ``PolicyGatedToolbox`` when an unattended leg hits a *gated* action.

    The control-flow signal that ends a leg into ``waiting(on_user)`` (A3-D-X-gate-mechanism):
    the wrapper records the :class:`persona.approvals.ActionProposal` durably, then raises
    this so it propagates through the **unmodified** agentic loop (whose ``_dispatch`` catches
    only :class:`ToolNotAllowedError` / :class:`ToolExecutionError`) to the A2 leg executor,
    which catches it and maps it to a ``WAITING_APPROVAL`` disposition. Carries
    ``context["proposal_id"]`` so the executor / continuation can drive the approval flow.

    Lives in the central error module (not ``persona.tools`` or ``persona.approvals``) so the
    raiser (approvals) and the catcher (runtime executor) share the type without a tools ↔
    approvals import cycle (A3-D-X-import-boundary). A *gated* action raises; a *denied* one
    does not — denial stays a recoverable ``ToolResult(is_error=True)`` the model adapts to.

    Carries ``activity_status = "awaiting_approval"`` (the ActivityStatusCarrier seam): P2's
    outer ``ObservedToolbox`` reads it to emit an awaiting-approval activity end before the
    exception propagates (the merge-back composition — P2 outermost, A3 inner).
    """

    #: The activity-status marker P2's ObservedToolbox reads on an awaiting-approval gate.
    activity_status: str = "awaiting_approval"


class CalculatorError(PersonaError):
    """Raised when the ``calculator`` tool rejects an expression.

    Spec 26 (D-26-X-calculator-ast-scope). The ``calculator`` tool evaluates a
    strict whitelist of arithmetic AST nodes (no ``eval``); this exception
    surfaces a disallowed node type (e.g. attribute access, a non-whitelisted
    function/name), a parse error, or a DoS-guard violation (expression too
    long, exponent/AST too large, factorial argument too big). The ``@tool``
    decorator boundary turns it into a ``ToolResult(is_error=True)`` so the
    model sees a recoverable error rather than a raised exception. ``context``
    carries the offending fragment + the guard that tripped.
    """


class ToolExecutionError(PersonaError):
    """Raised when a tool execution fails inside the toolbox."""


class UnknownDocumentFormatError(PersonaError):
    """Raised when ``document_generation`` is asked for a format with no handler.

    Carries ``context={"format": ..., "available": ...}`` so the caller (and
    logs) name the rejected format and the registered alternatives. Reading-B
    dispatch error (D-24-1); the format catalogue lives in
    :mod:`persona.skills.document_generation.registry`.
    """


class UnknownDocumentTemplateError(PersonaError):
    """Raised when ``document_generation`` is asked for an unregistered template.

    Carries ``context={"template": ..., "available": ...}``. Templates are
    bundled Markdown files registered in
    :mod:`persona.skills.document_generation.registry` (D-24-2).
    """


class SandboxViolationError(PersonaError):
    """Raised when a file operation attempts to escape its sandbox directory."""


class MCPConnectionError(PersonaError):
    """Raised when an MCP server cannot be reached in fail-loud mode.

    Spec 03 §7.3: the Toolbox auto-load path catches connection errors and
    logs a warning instead (graceful degradation per D-03-20), but explicit
    callers that invoke ``MCPClient.connect(strict=True)`` get this exception.
    """


class MCPServerUnavailableError(PersonaError):
    """Raised when a registered MCP server is unreachable in strict mode.

    Subclass of :class:`PersonaError` (flat hierarchy per D-03-1). Used by
    :class:`persona.tools.mcp.client.MCPClient` when ``strict=True`` and the
    underlying transport fails.
    """


class MCPBuiltinServerError(PersonaError):
    """Raised for a built-in MCP server problem (Spec 27, flat per D-03-1).

    Covers an unknown built-in server name at the serve entrypoint and the
    launcher's spawn/health-probe failures. Distinct from
    :class:`MCPServerUnavailableError` (a *configured external* server is
    unreachable) — this is about a *built-in* server Persona ships and launches.
    """


class MCPUrlNotAllowedError(PersonaError):
    """Raised when a user-supplied MCP server URL fails the SSRF guard (Spec 30, D-30-4).

    The bring-your-own MCP surface lets a user supply an outbound URL the runtime
    connects to — a server-side request forgery surface (the live MCP-SSRF CVE
    class). The guard (:mod:`persona.tools.mcp.ssrf`) rejects a non-``https``
    scheme, a missing/unresolvable host, or a host resolving to a
    private/loopback/link-local/cloud-metadata/CGNAT/reserved address (incl.
    IPv4-mapped + NAT64-embedded forms). ``context["reason"]`` carries the
    *category* only — never the resolved internal IP (no reconnaissance aid).
    Enforced at add/test time AND on every live connect (resolve-then-pin) so a
    rebind between check and use cannot slip through.
    """


class AuthenticationError(PersonaError):
    """Raised when a request has no valid bearer token (→ 401 at the API edge).

    Relocated from ``persona_api.errors`` to persona-core at spec V1 T03
    (D-V1-X-jwt-verifier-extraction) so persona-voice can raise it from the
    extracted :func:`persona.auth.jwt_verifier.make_jwt_verifier` without taking
    a persona-api dependency. ``persona_api.errors`` re-exports for back-compat.
    """


class CreditsExhaustedError(PersonaError):
    """Raised when a user's credit balance cannot cover an operation (→ 402).

    Relocated from ``persona_api.errors`` to persona-core at Spec 19 L6c
    (D-19-X-credits-service-domain-relocation) so persona-voice can raise it
    from :func:`persona.credits.service.require_credits` without taking a
    persona-api dependency (voice surface is latency-critical per R-V1-1 — no
    HTTP/RPC hop). ``persona_api.errors`` re-exports for back-compat.
    """


class DailySpendCapExceededError(PersonaError):
    """Raised when a user hits the per-UTC-day spend cap (→ 429; Spec R7, R7-D-5).

    The primary denial-of-wallet guard: an operation would push the user's
    same-day credit spend past ``CREDITS_MAX_PER_DAY``. Raised by
    :func:`persona.credits.service.deduct` when its atomic conditional day-spend
    write (:func:`persona.credits.service.book_day_spend`) rejects the booking
    ``WHERE spent + :cost <= :cap`` — BEFORE any credits are decremented (the
    enclosing transaction rolls back, so nothing is booked; FAIL-CLOSED).

    Distinct from :class:`CreditsExhaustedError` (→ 402, "out of credits
    forever"): this is "capped for *today*, back tomorrow", so the API edge maps
    it to **429 + ``Retry-After`` = seconds until the next UTC midnight**.
    ``context`` carries ``cap``, ``spent``, ``requested_cost``, and
    ``reset_epoch`` (the UTC-midnight epoch) — never any internal leakage. Home
    in persona-core (alongside :class:`CreditsExhaustedError`) so persona-voice
    can raise it too without a persona-api dependency; ``persona_api.errors``
    re-exports for back-compat.
    """


class SkillManifestError(PersonaError):
    """Raised when a ``SKILL.md`` file is malformed.

    Spec 04 (D-04-3). Surfaces from
    :func:`persona.skills._frontmatter.parse_skill_markdown` for: missing
    opening/closing front-matter delimiter, malformed YAML in the front
    matter, or a non-mapping YAML value where the front-matter dict was
    expected.

    The ``context`` always carries ``{"path": "<absolute path>"}`` and may
    carry ``{"reason": "<truncated yaml.YAMLError detail>"}`` when the
    underlying problem was a YAML parse failure. The scanner's per-skill
    envelope (D-04-4) catches this exception and logs a structured warning;
    the persona keeps loading with the offending skill omitted.
    """


class VettedSourceAuthenticityError(PersonaError):
    """Raised when a ``vetted``-tier external source fails provenance verification.

    Spec S2 (S2-D-4, the B1 carve-out). ``vetted`` is the consent-bypass tier
    (``SkillTrust.requires_consent`` is ``False``), so it has **no downstream
    backstop** — its integrity rests entirely on the source being *genuinely* the
    pinned canonical coordinate. If the fetched checkout is not the canonical repo
    or is not at the pinned commit (S2-D-5's code-constant pin), the adapter
    **refuses to stamp ``vetted``** and raises this — a vetted-source failure is a
    **sync error, never a silent downgrade** to a consent-gated tier (fail-closed,
    not fail-quiet). ``context`` carries the expected vs. fetched coordinate.
    """


class SkillArgumentValidationError(PersonaError):
    """Raised when ``use_skill`` arguments fail a skill's ``parameters`` schema.

    Spec 24 (D-24-8). The skill's declared JSON Schema is compiled to a frozen
    ``extra="forbid"`` Pydantic model and the call arguments are validated
    strictly at activation time. ``context`` carries
    ``{"skill": ..., "errors": ...}``. The ``use_skill`` tool catches this and
    returns ``ToolResult(is_error=True)`` so the model can self-correct.
    """


class SkillNameCollisionError(PersonaError):
    """Raised when the ``skills.toml`` catalog has a name clash (D-24-6).

    Spec 24. A collection name that duplicates a skill id is ambiguous under the
    uniform ``kind:ref`` addressing scheme and is rejected at catalog load
    (fail-loud, per R-24-1 — unlike Semantic Kernel's silent last-write-wins).
    ``context`` carries ``{"name": ...}``.
    """


class SkillCycleError(PersonaError):
    """Raised when a ``use_skill`` activation would revisit a skill already in
    the active composition chain (A→B→A).

    Spec 24 (D-24-4). Cycle detection is a visited-set of skill names along the
    active chain; the check runs **before** the depth check so a cycle is
    diagnosed as a cycle, not a depth overflow. ``context`` carries
    ``{"requested": ..., "chain": "A→B"}``. The runtime intercept catches this
    and informs the model with a system message rather than failing the turn.
    """


class SkillCompositionDepthError(PersonaError):
    """Raised when a ``use_skill`` activation would exceed the composition depth cap.

    Spec 24 (D-24-4). The cap (``MAX_SKILL_COMPOSITION_DEPTH`` = 3) bounds skill
    chaining (research→draft→format) without enabling runaway. ``context``
    carries ``{"requested": ..., "chain": ..., "max_depth": ...}``. The runtime
    intercept catches this and informs the model rather than failing the turn.
    """


class InvalidAutonomyLevelError(PersonaError):
    """Raised when an autonomy value is not one of the three supported levels.

    Spec 21 (D-21-1 / D-21-11). Surfaces from
    :func:`persona.autonomy.resolve_autonomy` when a persona_self autonomy
    chain head stores a value outside ``{"cautious", "balanced", "decisive"}``
    (a corrupted or hand-edited chain). Fail-loud rather than silently falling
    back to the YAML default — a malformed learned-autonomy value is a data
    integrity problem the caller must see. ``context`` carries the offending
    value, the ``logical_id``, and the ``persona_id``.
    """


class MessageDeliveryError(PersonaError):
    """Raised when delivering an originated message fails as a FAULT (Spec C0, T1).

    The base for delivery faults the routing layer re-raises at its boundary
    (a deliverer raising, a serialisation failure, an unexpected channel error).
    Ordinary unreachability is NOT a fault — that is reported as
    ``DeliveryOutcome.pending`` (criterion 6), never raised. ``context`` carries
    the ``owner_user_id`` / channel descriptor so the log line is structured.
    """


class ChannelUnreachableError(MessageDeliveryError):
    """Raised when no channel can reach the user and the caller demanded delivery.

    Subclass of :class:`MessageDeliveryError` so callers can catch the broad
    delivery error or disambiguate the unreachable case. Distinct from the v1
    fail-soft path where an undeliverable message is queued/present-on-next-open
    (D-C0-4): this is the explicit-failure variant a caller opts into.
    """


class OriginationForbiddenError(PersonaError):
    """Raised when a persona attempts to originate to a user that is not its owner.

    Spec C0 criterion 9 (the hard ownership boundary, D-C0-X-rls-ownership). A
    persona may originate ONLY to the user who owns it; a cross-tenant attempt is
    a privacy breach and is rejected rather than half-written. NOT a
    :class:`MessageDeliveryError` — this is an authorisation failure, not a
    delivery failure. ``context`` carries the owning + target user ids (never any
    further tenant data).
    """


class AutonomyCooldownError(PersonaError):
    """Raised when a ``persona_self`` autonomy update is attempted within cooldown.

    Spec 21 (D-21-4). The learner rate-limits self-revision to at most once
    per session and once per UTC day; a second attempt inside either window
    raises this rather than appending a churn version. ``context`` carries the
    ``persona_id``, the cooldown window that tripped (``session`` | ``day``),
    and the head version's ``written_at`` timestamp.
    """


class JobStateError(PersonaError):
    """Raised when an illegal job state transition is attempted.

    Spec A0 (D-A0-2). The durable job state machine
    (``queued → claimed → running → succeeded | failed | dead``) rejects any
    move not in its transition table — e.g. resurrecting a terminal job or
    claiming a job that is already running. ``context`` carries ``from`` and
    ``to`` so the log line names the rejected edge.
    """


class DuplicateJobTypeError(PersonaError):
    """Raised when a job type is registered twice in a :class:`JobRegistry`.

    Spec A0 (D-A0-2). The registry is Toolbox-style and explicit; a duplicate
    registration would let a second runtime silently shadow a handler, so it
    fails loud at composition time. ``context`` carries the offending ``type``.
    """


class UnknownJobTypeError(PersonaError):
    """Raised when an unregistered job type is resolved from a :class:`JobRegistry`.

    Spec A0 (D-A0-2). ``context`` carries the requested ``type`` and the
    ``known`` registered types so the miss is debuggable.
    """


class PermanentJobError(PersonaError):
    """Raised by a job handler to signal a NON-retryable (permanent) failure.

    Spec A0 (T6). The durable queue is at-least-once: a handler that raises an
    ordinary exception is retried with backoff and dead-letters at exhaustion.
    A handler raises this when the failure will never succeed on retry (malformed
    input, a permanent provider rejection) — the executor moves the job straight
    to the terminal ``failed`` state, no retries. ``context`` carries the cause.
    """


class InvalidRecurrenceRuleError(PersonaError):
    """Raised when an RFC-5545 recurrence rule is malformed or unsupported.

    Spec A1 (D-A1-1). A :class:`~persona.schedules.RecurrenceRule` parsed from an
    RRULE string that is unparseable, or that names a ``FREQ`` outside A1's
    supported set, fails loud at the boundary rather than mis-firing in the tick.
    ``context`` carries the offending rule string and/or the rejected token.
    """


class ScheduleNotFoundError(PersonaError):
    """Raised when a schedule id is looked up and does not exist (or is not visible).

    Spec A1. The durable schedule store raises this on a miss — including a
    cross-tenant lookup that RLS scopes to zero rows, so a missing row and a
    foreign row are indistinguishable to the caller (no existence oracle).
    ``context`` carries the requested ``schedule_id``.
    """


class ScheduleStateError(PersonaError):
    """Raised when an illegal operation is attempted on a schedule's state.

    Spec A1. E.g. recording a fire against a one-time schedule that has already
    completed, or otherwise driving a schedule through a transition its current
    state forbids. ``context`` carries the schedule id and the rejected operation.
    """


class TaskNotFoundError(PersonaError):
    """Raised when a task (or one of its checkpoints) is not found for the owner.

    Spec A2. RLS-scoped: a cross-tenant id is indistinguishable from a missing one
    (both raise this — no existence oracle). ``context`` carries the ``task_id``.
    """


class TaskStateError(PersonaError):
    """Raised when an illegal operation is attempted on a task's lifecycle state.

    Spec A2. E.g. completing a task that never started, resuming a task that is not
    waiting, or any transition the state machine forbids. ``context`` carries the
    rejected ``from``/``to`` states (or the offending operation).
    """


class TaskLegFailedError(PersonaError):
    """Raised when a task leg's run failed — a TRANSIENT signal for the job to retry.

    Spec A2. A single leg error is not terminal: the handler raises this so A0 re-delivers
    (the leg made no durable progress — no checkpoint was appended, so the head is
    unadvanced and the retry re-runs the same leg). Only A0's exhaustion (dead-letter after
    the retry cap) is terminal; the task→FAILED/``waiting(on_user)`` reaction to a dead-letter
    is A3/T9. ``context`` carries the ``task_id``.
    """


class CheckpointTooLargeError(PersonaError):
    """Raised when a task checkpoint's accumulating core exceeds its token budget.

    Spec A2 (D-A2-1, the architectural lock). The checkpoint's bounded fields
    (``progress_conclusions`` + ``decisions`` + ``lessons``) must stay *intent,
    not history*; a write that still exceeds the budget after reflect-and-compact
    raises this at the boundary rather than persisting an unbounded, ossifying
    checkpoint. ``context`` carries the task/leg/sequence ids plus the offending
    ``token_count`` and the ``token_budget`` it breached.
    """

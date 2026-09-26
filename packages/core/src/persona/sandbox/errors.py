"""Domain exceptions for the code-execution sandbox (spec 12 T01).

Per **D-12-6**, ``SandboxError`` is an **intermediate parent** under
:class:`persona.errors.PersonaError`. Spec 12 ships four cohesive subtypes
together (``CodeSandboxError``, ``ExecutionTimeoutError``, ``ResourceLimitError``,
``SandboxUnavailableError``) — D-03-1's "introduce parent only when a third
lands" condition is met. The parent makes ``except SandboxError`` ergonomic
in both loops' ``_dispatch`` wrappers without listing every leaf.

Each leaf carries ``context: dict[str, str]`` (inherited from
:class:`persona.errors.PersonaError`) so log messages are structured.

Catch-and-convert pattern (carries from spec-11 fix #1, spec-03 D-03-3):
both the conversation loop's ``_dispatch`` and the agentic loop's
``_dispatch`` already catch ``ToolNotAllowedError`` / ``ToolExecutionError``
and convert to ``ToolResult(is_error=True, ...)``. They will be widened in T03
to also catch ``SandboxError`` so an unreachable Docker daemon or substrate
outage surfaces to the model as a structured tool failure it can recover
from, never as a crashed SSE stream.
"""

from __future__ import annotations

from persona.errors import PersonaError

__all__ = [
    "CodeSandboxError",
    "ExecutionTimeoutError",
    "ProducedFileRefusedError",
    "ProducedFileSizeError",
    "ResourceLimitError",
    "SandboxError",
    "SandboxQuotaExceededError",
    "SandboxUnavailableError",
]


class SandboxError(PersonaError):
    """Parent for all sandbox-related failures (D-12-6).

    Direct children:

    - :class:`CodeSandboxError` — generic substrate / backend internal failure.
    - :class:`ExecutionTimeoutError` — wall-clock cap hit.
    - :class:`ResourceLimitError` — memory / disk / pids cap hit.
    - :class:`SandboxUnavailableError` — Docker daemon down (local) or
      substrate API outage / per-user concurrency cap reached (hosted).
    """


class CodeSandboxError(SandboxError):
    """Generic execution failure raised by a :class:`CodeSandbox` backend.

    Used for any backend-internal error that doesn't fit a more specific
    subclass (substrate API error, image pull failure, container creation
    rejected, etc.).
    """


class ExecutionTimeoutError(SandboxError):
    """Execution hit ``ResourceLimits.wall_clock_s`` and was killed.

    Distinct from :class:`ResourceLimitError` because "timeout" is the
    wall-clock budget being exhausted, not a memory/disk/pids cap being
    crossed. The T03 tool factory maps this to ``outcome="timeout"`` on the
    synthesised :class:`ExecutionResult`.

    Conventional ``context`` keys:

    - ``wall_clock_s`` — the cap that was hit (stringified).
    - ``session_id`` — sandbox session, if stateful.
    """


class ResourceLimitError(SandboxError):
    """A CPU / memory / disk / pids cap was exceeded and the substrate killed
    the execution.

    The T03 tool factory inspects ``context`` to pick the right ``outcome``
    on the synthesised :class:`ExecutionResult` (``"oom"`` for memory,
    ``"killed"`` otherwise — e.g. pids-limit, disk quota).

    Conventional ``context`` keys:

    - ``limit`` — one of ``"memory"`` / ``"disk"`` / ``"pids"`` / ``"cpu"``.
    - ``cap`` — the configured cap (stringified).
    - ``session_id`` — sandbox session, if stateful.
    """


class SandboxUnavailableError(SandboxError):
    """The sandbox backend is unavailable.

    For :class:`LocalDockerSandbox` (T05): raised when the Docker daemon
    isn't reachable. Per **D-12-5** (no degraded fallback), the tool surfaces
    this error to the model and the model recovers / explains to the user;
    there is no unsandboxed code-execution path.

    For ``HostedSandbox`` (T08): raised on substrate API outages.
    Per-user concurrency cap exhaustion is :class:`SandboxQuotaExceededError`
    (a more specific subtype; T09).
    """


class ProducedFileSizeError(SandboxError):
    """A produced file exceeds the per-file size cap during persist (D-12-X-read-produced-file).

    Raised by :meth:`CodeSandbox.copy_produced_file_to` /
    :meth:`CodeSandbox.read_produced_file_bytes` when the sandbox-produced
    file at ``ref`` exceeds 100 MB. Flows through Spec 06 tool-error-recovery
    so the model gets an explainable error and can correct (resize chart,
    slim PDF, sample dataframe before export, etc.) — never an OOM crash on
    the hosted memory ceiling.

    Distinct from :class:`ResourceLimitError` (which is about substrate-side
    runtime caps): this is the *persist-time* cap, applied when the runtime
    is about to copy bytes out of the sandbox into the API workspace.

    Conventional ``context`` keys:

    - ``ref`` — the workspace-relative produced-file path.
    - ``size_bytes`` — actual file size.
    - ``cap_bytes`` — the configured cap (default 100 MB).
    - ``session_id`` — sandbox session.
    """


class ProducedFileRefusedError(SandboxError):
    """A produced file's name or destination is not allowed in the workspace (Spec WIN, T1.5).

    Raised by a ``produced_file_persister`` when the file's workspace destination is
    refused: the name breaks a path rule (on Windows a reserved device name or a
    trailing dot, for example) or a link sits in the way. The ``code_execution`` tool
    catches it PER FILE: that file is skipped and the model is told why, in a human
    sentence, while every other produced file is still saved. It never ends the turn.

    Conventional ``context`` keys: ``ref`` (the produced file) and ``reason``.
    """


class SandboxQuotaExceededError(SandboxError):
    """Per-user concurrency cap exhausted (T09 pool).

    Raised by :class:`persona_api.sandbox.pool.SandboxPool.acquire` when
    the user already holds ``max_per_user`` active sandboxes. Distinct
    from :class:`SandboxUnavailableError` (which is about substrate
    reachability) — quota is a per-tenant policy decision, not an
    infrastructure outage. The runtime maps this to a structured
    ``ToolResult(is_error=True, ...)`` so the model can explain the
    quota condition rather than crashing the stream.

    Conventional ``context`` keys:

    - ``user_id`` — the tenant whose quota was hit.
    - ``current_count`` — sandboxes the user currently holds (stringified).
    - ``cap`` — the configured per-user cap (stringified).
    """

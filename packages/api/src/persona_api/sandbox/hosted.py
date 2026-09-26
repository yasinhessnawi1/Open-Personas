"""HostedSandbox — E2B Firecracker-microVM backend (spec 12 T08; D-12-12 substrate).

Adapter that wraps the E2B Code Interpreter Python SDK (``e2b-code-interpreter``)
into the :class:`persona.sandbox.protocol.CodeSandbox` Protocol so the same
``code_execution`` tool factory + runtime + agentic loop work unchanged across
local and hosted backends — the load-bearing reversibility property D-12-12
buys via the Protocol design.

**Threat-model scope (D-12-13):** isolation is **substrate-provided** by E2B's
Firecracker microVM. We do NOT replicate the R-12-2 Docker hardening here —
those flags are kernel-namespace knobs that don't apply to a microVM with its
own guest kernel. The equivalent hosted controls are the E2B SDK's native
``allow_internet_access`` (egress on/off), the substrate's CPU/memory caps,
and the SDK's ``kill()`` lifecycle.

**§9 acceptance contract:** the same T04 attack catalog
(``packages/core/tests/integration/sandbox/_attacks.py``) parametrises onto
``[hosted]`` — when E2B is reachable, the 26 attacks run against this backend
and verify the substrate delivers the same containment as
``LocalDockerSandbox``. The reversibility cost quantified in D-12-12 buys this.

**Lock-gates (D-12-12 §Lock criteria):** before T09 ships, T08 measures the
five gates against a real E2B Hobby account:
  1. Cold-start p95 < 2.5s (n>=20)
  2. 20-sandbox concurrent fan-out, p95 ready < 5s
  3. Adversarial egress denial — every IP in §9 #7 catalog blocked with
     ``allow_internet_access=False`` regardless of allow-list
  4. Mid-exec kill: SDK raises clean domain exception; no zombies
  5. 7-day realistic-load cost projection < $50/mo
The script that measures these lives at
``packages/api/tests/integration/sandbox/test_e2b_lock_gates.py``.
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path  # noqa: TC003 — runtime use in copy_produced_file_to
from typing import TYPE_CHECKING, Any

from persona.logging import get_logger
from persona.sandbox.errors import (
    CodeSandboxError,
    ExecutionTimeoutError,
    ProducedFileSizeError,
    SandboxUnavailableError,
)
from persona.sandbox.protocol import PRODUCED_FILE_CAP_BYTES
from persona.sandbox.result import (
    ExecutionOutcome,
    ExecutionResult,
    NetworkPolicy,
    RefusedFile,
    ResourceLimits,
    SandboxFile,
    guess_media_type,
    produced_file_path_violation,
    refused_file,
)
from persona.tools._sandbox import write_file_under_root

from persona_api.sandbox.config import SandboxWallClockConfig

if TYPE_CHECKING:
    # The E2B SDK is lazy-imported at use-site (so importing this module
    # without the SDK installed is a graceful SandboxUnavailableError at
    # runtime, not an ImportError at module-load). For type-checking, pull
    # the ``Sandbox`` symbol into scope so the SDK-bearing parameter
    # annotations carry the real type instead of ``Any``.
    from e2b import EntryInfo as E2BEntryInfo
    from e2b_code_interpreter import Sandbox as E2BSandbox

__all__ = ["HostedSandbox", "detect_env_setup"]

_logger = get_logger("sandbox.hosted")


# Spec 25 D-25-2 — env-setup detection. Explicit leading-token match against a
# fixed package-manager set; explicit prefix list over regex/heuristic so the
# match is predictable, testable, and never trips on user code that merely
# *mentions* "pip" (the false-positive failure mode D-25-2 rejects). The set is
# the load-bearing contract; growing it is a deliberate, reviewable edit.
# Two-token forms whose first token alone is ambiguous (``npm``/``yarn`` are
# also plausible variable names) — require the install verb. D-25-2.
_ENV_SETUP_TWO_TOKEN_FORMS = frozenset({("npm", "install"), ("yarn", "add")})
# Single-token forms that are unambiguous on their own (a line that *starts*
# with these is overwhelmingly a shell invocation, not Python identifiers).
_ENV_SETUP_UNAMBIGUOUS_TOKENS = frozenset({"pip", "pip3", "apt", "apt-get", "wget", "curl", "uv"})
# The realistic ``subprocess.*([sys.executable, "-m", "pip", "install", ...])``
# shape from the operator log: a Python call that shells out to a package
# manager. Detect ``"-m", "pip"`` / ``"-m", "pip3"`` and a quoted package
# manager token inside a subprocess argv list.
_SUBPROCESS_PIP_RE = re.compile(
    r"""subprocess\.\w+\s*\(\s*\[[^\]]*?-m["']\s*,\s*["'](pip3?|uv)["']""",
    re.DOTALL,
)


def _line_is_env_setup(line: str) -> bool:
    """Return ``True`` when a single physical line is a package-manager call.

    A line is env-setup when, after stripping leading whitespace and an
    optional IPython ``!`` shell-escape, its leading token (lower-cased) is an
    unambiguous package manager, or its first two tokens are an
    ``npm install`` / ``yarn add`` form. D-25-2 explicit-prefix discipline.
    """
    stripped = line.strip()
    if stripped.startswith("!"):  # IPython shell-escape (``!pip install ...``)
        stripped = stripped[1:].lstrip()
    if not stripped:
        return False
    tokens = stripped.split()
    head = tokens[0].lower()
    if head in _ENV_SETUP_UNAMBIGUOUS_TOKENS:
        return True
    return len(tokens) >= 2 and (head, tokens[1].lower()) in _ENV_SETUP_TWO_TOKEN_FORMS


def detect_env_setup(code: str) -> bool:
    """Detect whether ``code`` invokes a package manager (D-25-2).

    Pure helper (no I/O, no sandbox) so the dual-policy cap selection is
    testable in isolation. The sandbox executes a Python ``code`` string, so
    "env-setup" is the code string's *intent*: it either (a) is a shell-style
    package-manager line (``pip install ...``, ``!pip install ...``,
    ``npm install ...``) or (b) shells out to a package manager via the
    realistic ``subprocess.check_call([sys.executable, "-m", "pip",
    "install", ...])`` shape seen in the operator log.

    Matches the explicit leading-token set ``{pip, pip3, apt, apt-get, wget,
    curl, npm, yarn, uv}`` (+ the ``npm install`` / ``yarn add`` two-token
    forms) so user code that merely mentions "pip" in a string or comment is
    NOT a false positive.

    Args:
        code: The Python code string about to be executed.

    Returns:
        ``True`` if the code invokes a package manager (→ setup wall-clock
        cap), ``False`` otherwise (→ exec wall-clock cap).
    """
    if _SUBPROCESS_PIP_RE.search(code):
        return True
    return any(_line_is_env_setup(line) for line in code.splitlines())


# T12 SCP-12-4 — hosted substrate template-class floors (E2B Hobby tier).
# The T12 audit measured MemTotal=2 GiB and RLIMIT_AS=-1 inside the default
# sandbox; tmpfs ~1 GiB at /tmp. User-supplied ResourceLimits below these
# floors are advisory only and emit a warning at sandbox construction.
# Future custom-template work (D-12-12 follow-up) can raise these by
# requesting a different template; lowering is not supported at Hobby tier.
_HOSTED_SUBSTRATE_MEMORY_FLOOR_MB = 2048
_HOSTED_SUBSTRATE_DISK_FLOOR_MB = 1024

#: The documented writable output directory the model is told to write to. The
#: prompt builder (``persona_runtime.prompt``) and every document-generation
#: skill instruct the model to write produced files under ``/workspace/out``
#: (the LocalDockerSandbox mounts it read-write and runs with it as the working
#: dir; the sandbox image even ``WORKDIR``\\ s it). The E2B substrate boots with
#: ``/home/user`` as the working dir and has NO ``/workspace/out`` — so model
#: code writing to ``/workspace/out/<file>`` hit ``FileNotFoundError`` on the
#: hosted path. We ensure the directory exists per sandbox so the documented
#: out-dir contract holds across both substrates (parity with
#: ``LocalDockerSandbox._make_workspace_dirs``).
_HOSTED_WORKSPACE_OUT = "/workspace/out"

#: Per-execute bootstrap prepended to user code on the hosted path so the
#: documented out-dir always exists before the code runs (belt-and-braces for an
#: SDK without ``files.make_dir``, or a reaped dir). One ``os.makedirs`` with
#: ``exist_ok=True`` — idempotent and ~free.
_WORKSPACE_OUT_BOOTSTRAP = (
    f"import os as _os; _os.makedirs({_HOSTED_WORKSPACE_OUT!r}, exist_ok=True)"
)

#: Recursion depth for the produced-file listing under ``/workspace/out``. The
#: E2B SDK ``files.list(path, depth=...)`` defaults to ``1`` (non-recursive),
#: which would miss the load-bearing ``charts/<id>.png`` sub-dir (D-17-X chart
#: prefix). A bounded depth recurses into the documented sub-dirs without an
#: unbounded walk of a pathological tree (the count + size caps are the real
#: limits; this just has to reach the conventional ``charts/`` nesting).
_PRODUCED_FILE_LIST_DEPTH = 5

#: The E2B ``FileType.DIR`` enum value (the string ``"dir"``). Compared by value
#: so the lazy-import module never has to import the SDK enum at module load.
_E2B_DIR_TYPE_VALUE = "dir"


def _is_sandbox_reaped(stderr: str) -> bool:
    """True if an execution error indicates the E2B sandbox was reaped/gone.

    Spec 25 §2.4 (operator-pass 2026-06-13): E2B reaps an idle sandbox
    server-side; the next ``run_code`` then raises a ``TimeoutException`` whose
    payload carries ``"The sandbox was not found"`` / ``"code":502``. The SDK
    error is caught + marshalled into an ``outcome="error"`` ``ExecutionResult``
    by :meth:`HostedSandbox._run_and_marshal`; this detector lets the stateful
    execute path re-surface it as ``reason="no_session"`` so the tool wrapper's
    auto-recovery (D-25-4 / T09) recreates the session instead of the model
    retrying the SAME dead sandbox forever. Matched on the marshalled stderr;
    a genuine user-code error arrives via ``execution.error`` (a Python
    traceback), not this SDK-exception shape.
    """
    low = stderr.lower()
    return ("not found" in low and "sandbox" in low) or "502" in low


class HostedSandbox:
    """E2B Firecracker-microVM backend satisfying the :class:`CodeSandbox` Protocol.

    Args:
        api_key: E2B API key. ``None`` ⇒ the SDK reads ``E2B_API_KEY`` from the
            environment natively (preferred — keeps secrets out of constructor calls).
        template: E2B sandbox template. ``None`` ⇒ the SDK default
            (``code-interpreter-v1``, the data-stack-preinstalled image).
            Production may override with a persona-sandbox-derived template
            once T06's image is published to E2B's registry (D-12-2 / R-12-3).
        timeout_default_s: Default per-sandbox idle timeout (substrate-side
            sandbox lifetime ceiling). Per-execute ``timeout_s`` is enforced
            by the SDK's ``request_timeout`` (separate dimension).
        wallclock_config: Spec 25 D-25-2/3 dual wall-clock policy. ``None`` ⇒
            the env-driven defaults (30s exec / 120s env-setup) are read from
            the process environment. Env-setup commands (package-manager
            invocations per :func:`detect_env_setup`) get the longer setup cap.

    Raises:
        SandboxUnavailableError: When the E2B API is unreachable or the
            account is unauthenticated (the substrate's equivalent of
            ``LocalDockerSandbox``'s "Docker daemon unreachable").
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        template: str | None = None,
        timeout_default_s: int = 300,
        wallclock_config: SandboxWallClockConfig | None = None,
    ) -> None:
        self._api_key = api_key  # None ⇒ SDK reads E2B_API_KEY env var
        self._template = template
        self._timeout_default_s = timeout_default_s
        # D-25-3 dual wall-clock policy. None ⇒ env-driven defaults.
        self._wallclock = wallclock_config or SandboxWallClockConfig()
        self._closed = False
        # Per-session E2B sandbox references — sandbox_id ↔ Sandbox SDK object.
        # Kept in memory; T09 pool replaces this with a warm-pool + reaper.
        self._sessions: dict[str, E2BSandbox] = {}
        _logger.debug(
            "HostedSandbox initialised",
            template=template or "<default>",
            timeout_default_s=timeout_default_s,
            wallclock_exec_s=self._wallclock.exec_cap_s,
            wallclock_setup_s=self._wallclock.setup_cap_s,
        )

    # -- CodeSandbox Protocol methods -------------------------------------

    async def execute(
        self,
        code: str,
        *,
        language: str = "python",
        session_id: str | None = None,
        timeout_s: float | None = None,
        limits: ResourceLimits | None = None,
        network: NetworkPolicy | None = None,
        input_files: list[SandboxFile] | None = None,
    ) -> ExecutionResult:
        """Run ``code`` in an E2B sandbox; return the result.

        Stateless one-shot (``session_id=None``) creates a fresh sandbox,
        runs the code, kills the sandbox. Stateful (``session_id`` set) runs
        in the persistent E2B sandbox from :meth:`create_session` — variable
        state DOES persist (E2B's sandbox runs a single long-lived IPython
        kernel; this is the D-12-1 mental model the spec calls for, hosted
        side, no scaling caveat).

        **Spec 25 D-25-2/3 dual wall-clock policy.** The effective wall-clock
        cap is chosen per call from ``code``'s intent:

        * Env-setup commands (package-manager invocations per
          :func:`detect_env_setup`) get the longer setup cap
          (``wallclock_config.setup_cap_s``, default 120s) — a one-off
          ``pip install`` shouldn't be killed at the 30s exec budget.
        * Ordinary code keeps the exec cap. When the caller passes an explicit
          ``timeout_s`` (the conventional ``limits.wall_clock_s`` exec budget)
          it is the exec baseline; ``None`` ⇒ ``wallclock_config.exec_cap_s``
          (default 30s).

        Which cap applied is recorded in the timeout-error metadata
        (``cap_applied`` ∈ {``"exec"``, ``"setup"``}) so the TurnLog surfaces
        it.
        """
        if self._closed:
            msg = "HostedSandbox is closed"
            raise SandboxUnavailableError(msg, context={"reason": "closed"})
        if language != "python":
            msg = f"unsupported language: {language!r}"
            raise CodeSandboxError(msg, context={"language": language})

        limits = limits or ResourceLimits()
        network = network or NetworkPolicy()
        input_files = input_files or []

        # D-25-2/3 dual-policy cap selection (pure helper keeps it testable).
        is_env_setup = detect_env_setup(code)
        cap_applied = "setup" if is_env_setup else "exec"
        if is_env_setup:
            effective_timeout_s = self._wallclock.setup_cap_s
        elif timeout_s is not None:
            effective_timeout_s = timeout_s
        else:
            effective_timeout_s = self._wallclock.exec_cap_s

        # T12 F-T12-RES-02 fix: enforce the wall-clock cap at the async boundary.
        # The E2B SDK's ``run_code(timeout=...)`` maps to httpx read-timeout,
        # NOT substrate wall-clock — a silent CPU-bound ``while True: pass``
        # hung > 90 s in the T12 probe before OS SIGKILL took effect, breaking
        # spec §9 #8. Wrap the to_thread call in :func:`asyncio.wait_for` with
        # a small grace window for substrate-side cleanup; on timeout, kill
        # the substrate sandbox (if stateful) and raise
        # :class:`ExecutionTimeoutError` so the existing T03 catch path
        # converts it to ``ToolResult(outcome="timeout", is_error=True)``.
        grace_s = 2.0
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    self._execute_sync,
                    code,
                    session_id=session_id,
                    timeout_s=effective_timeout_s,
                    limits=limits,
                    network=network,
                    input_files=input_files,
                ),
                timeout=effective_timeout_s + grace_s,
            )
        except TimeoutError as exc:
            # Force-kill the substrate sandbox if stateful so a runaway
            # session doesn't continue consuming substrate-seconds after
            # we've abandoned it.
            if session_id is not None:
                sandbox = self._sessions.pop(session_id, None)
                if sandbox is not None:
                    await asyncio.to_thread(self._safe_kill, sandbox)
            msg = (
                f"code_execution exceeded wall_clock_s={effective_timeout_s} "
                f"(cap={cap_applied}, grace +{grace_s}s) — substrate kill forced"
            )
            raise ExecutionTimeoutError(
                msg,
                context={
                    "wall_clock_s": str(effective_timeout_s),
                    "cap_applied": cap_applied,
                    "session_id": session_id or "",
                },
            ) from exc

    async def create_session(
        self,
        session_id: str,
        *,
        limits: ResourceLimits,
        network: NetworkPolicy,
    ) -> None:
        """Create a long-lived E2B sandbox tracked by ``session_id``.

        Idempotent: an existing session is reused. Per D-12-7 every session
        is destroyed by :meth:`aclose`; the T09 pool adds reaping.
        """
        if self._closed:
            msg = "HostedSandbox is closed"
            raise SandboxUnavailableError(msg, context={"reason": "closed"})
        if session_id in self._sessions:
            return
        await asyncio.to_thread(
            self._create_session_sync,
            session_id=session_id,
            limits=limits,
            network=network,
        )

    async def destroy_session(self, session_id: str) -> None:
        """Kill the E2B sandbox for ``session_id``; idempotent."""
        sandbox = self._sessions.pop(session_id, None)
        if sandbox is None:
            return
        await asyncio.to_thread(self._safe_kill, sandbox)

    async def aclose(self) -> None:
        """Kill every live session; mark closed (D-12-7 explicit Protocol)."""
        if self._closed:
            return
        self._closed = True
        session_ids = list(self._sessions.keys())
        for session_id in session_ids:
            await self.destroy_session(session_id)

    async def copy_produced_file_to(
        self,
        session_id: str,
        ref: str,
        target_path: Path,
        *,
        root: Path,
    ) -> None:
        """Copy a produced file from the E2B sandbox to a host target path.

        D-12-X-read-produced-file hosted impl: ``sandbox.files.read`` then a write
        under ``root`` through the no-follow helpers (Spec WIN, T1.5): memory ==
        file size (the E2B SDK doesn't stream), bounded by
        :data:`PRODUCED_FILE_CAP_BYTES`. ``target_path`` must already be resolved
        under ``root``; a link at the destination is refused, never followed.
        """
        data = await self.read_produced_file_bytes(session_id, ref)
        await asyncio.to_thread(write_file_under_root, target_path, data, root=root)

    async def read_produced_file_bytes(
        self,
        session_id: str,
        ref: str,
    ) -> bytes:
        """Read produced file bytes from the E2B sandbox.

        D-12-X-read-produced-file hosted impl: ``sandbox.files.read`` for
        the substrate-native fetch; the size cap is enforced post-read
        (E2B's SDK doesn't expose a server-side size check pre-fetch — we
        rely on the substrate-side resource caps to prevent runaway file
        creation in the first place).
        """
        sandbox = self._sessions.get(session_id)
        if sandbox is None:
            msg = f"session {session_id!r} not found; cannot read produced file"
            raise CodeSandboxError(
                msg,
                context={"reason": "no_session", "session_id": session_id, "ref": ref},
            )
        try:
            data = await asyncio.to_thread(self._read_e2b_bytes, sandbox, ref)
        except CodeSandboxError:
            raise
        except Exception as exc:  # noqa: BLE001 — SDK error hierarchy abstracted
            msg = f"E2B sandbox file read failed for {ref!r}: {type(exc).__name__}: {exc}"
            raise CodeSandboxError(
                msg,
                context={
                    "reason": "e2b_read_failed",
                    "session_id": session_id,
                    "ref": ref,
                },
            ) from exc
        size_bytes = len(data)
        if size_bytes > PRODUCED_FILE_CAP_BYTES:
            msg = (
                f"produced file {ref!r} is {size_bytes} bytes, "
                f"exceeds {PRODUCED_FILE_CAP_BYTES}-byte cap"
            )
            raise ProducedFileSizeError(
                msg,
                context={
                    "ref": ref,
                    "size_bytes": str(size_bytes),
                    "cap_bytes": str(PRODUCED_FILE_CAP_BYTES),
                    "session_id": session_id,
                },
            )
        return data

    @staticmethod
    def _resolve_out_ref(ref: str) -> str:
        """Resolve a produced-file ``ref`` to its absolute substrate path.

        Out-dir parity: produced files are written under the documented
        ``/workspace/out`` (same contract as the local substrate). A relative
        ``ref`` is resolved there; an already-absolute ``ref`` is honoured
        verbatim (the model may emit a full path).
        """
        if ref.startswith("/"):
            return ref
        return f"{_HOSTED_WORKSPACE_OUT}/{ref}"

    @classmethod
    def _read_e2b_bytes(cls, sandbox: E2BSandbox, ref: str) -> bytes:
        """Sync E2B file read from the documented ``/workspace/out`` out-dir."""
        result = sandbox.files.read(cls._resolve_out_ref(ref), format="bytes")
        if not isinstance(result, bytes):
            # E2B's typed overloads return str by default; format="bytes" must yield bytes.
            return bytes(result)
        return result

    # -- Sync internals (called via asyncio.to_thread) ---------------------

    def _execute_sync(
        self,
        code: str,
        *,
        session_id: str | None,
        timeout_s: float,
        limits: ResourceLimits,
        network: NetworkPolicy,
        input_files: list[SandboxFile],
    ) -> ExecutionResult:
        """Synchronous execute. Runs in :func:`asyncio.to_thread`."""
        if session_id is None:
            sandbox = self._create_sandbox(limits=limits, network=network)
            try:
                return self._run_and_marshal(sandbox, code, timeout_s, input_files, limits)
            finally:
                self._safe_kill(sandbox)
        # Stateful: reuse the session container
        sandbox = self._sessions.get(session_id)
        if sandbox is None:
            msg = f"session {session_id!r} does not exist; call create_session() first"
            raise CodeSandboxError(msg, context={"reason": "no_session", "session_id": session_id})
        result = self._run_and_marshal(sandbox, code, timeout_s, input_files, limits)
        # Spec 25 §2.4 (operator-pass 2026-06-13): the E2B substrate reaps an
        # idle sandbox server-side; the next run_code raised a TimeoutException
        # ("The sandbox was not found", code 502) that _run_and_marshal captured
        # as an outcome="error" result. Evict the dead handle + re-surface as
        # reason="no_session" so the tool wrapper auto-recovers (D-25-4 / T09:
        # recreate + retry once). Without this the model retries the SAME dead
        # sandboxId and never recovers (the reported failure mode).
        if result.outcome == "error" and _is_sandbox_reaped(result.stderr):
            self._sessions.pop(session_id, None)
            msg = (
                f"session {session_id!r} sandbox was reaped by the substrate "
                "(idle timeout); recreate it"
            )
            raise CodeSandboxError(
                msg,
                context={
                    "reason": "no_session",
                    "session_id": session_id,
                    "cause": "substrate_reaped",
                },
            )
        return result

    def _create_session_sync(
        self,
        *,
        session_id: str,
        limits: ResourceLimits,
        network: NetworkPolicy,
    ) -> None:
        sandbox = self._create_sandbox(limits=limits, network=network)
        self._sessions[session_id] = sandbox

    def _create_sandbox(
        self,
        *,
        limits: ResourceLimits,
        network: NetworkPolicy,
    ) -> E2BSandbox:
        """Create an E2B sandbox with the persona's policy applied."""
        try:
            from e2b_code_interpreter import Sandbox
        except ImportError as exc:
            msg = (
                "e2b-code-interpreter SDK not installed; "
                "install via `pip install 'e2b-code-interpreter>=1.0,<2'`"
            )
            raise SandboxUnavailableError(msg, context={"reason": "sdk_missing"}) from exc

        # T12 F-T12-RES-01 documentation hook (SCP-12-4 — substrate-class limit
        # ceiling): the E2B SDK constructor accepts only ``timeout`` and
        # ``allow_internet_access`` here; ``memory_mb`` / ``cpu_cores`` /
        # ``disk_mb`` from :class:`ResourceLimits` are SILENTLY DROPPED at the
        # SDK boundary — there is no SDK kwarg to pass them, and the substrate
        # enforces its template-class floor (~2 GiB / 1 vCPU / ~1 GiB tmpfs at
        # Hobby tier). The T12 audit confirmed a 900 MB allocation with
        # ``memory_mb=512`` returned ``outcome="ok"``. Emit a one-time-per-
        # construction warning when the user-supplied caps are below the
        # substrate-class floor so production telemetry surfaces the gap;
        # actual enforcement requires a custom E2B template (D-12-12 follow-up).
        if (
            limits.memory_mb < _HOSTED_SUBSTRATE_MEMORY_FLOOR_MB
            or limits.disk_mb < _HOSTED_SUBSTRATE_DISK_FLOOR_MB
        ):
            _logger.warning(
                "hosted substrate enforces template-class floor; "
                "user-supplied caps are advisory only (SCP-12-4)",
                requested_memory_mb=limits.memory_mb,
                substrate_memory_floor_mb=_HOSTED_SUBSTRATE_MEMORY_FLOOR_MB,
                requested_disk_mb=limits.disk_mb,
                substrate_disk_floor_mb=_HOSTED_SUBSTRATE_DISK_FLOOR_MB,
            )

        kwargs: dict[str, Any] = {
            "timeout": int(max(limits.wall_clock_s, self._timeout_default_s)),
        }
        if self._template is not None:
            kwargs["template"] = self._template
        if self._api_key is not None:
            kwargs["api_key"] = self._api_key
        # Network policy — E2B controls egress at the sandbox level. The SDK
        # accepts ``allow_internet_access`` at creation in recent versions;
        # the R-12-5 catalog blocks metadata + private ranges at the substrate
        # regardless of this flag (E2B's own infrastructure deny-list).
        if not network.enabled:
            kwargs["allow_internet_access"] = False
        try:
            sandbox = Sandbox(**kwargs)
        except Exception as exc:  # noqa: BLE001 — SDK raises a hierarchy we don't import
            _logger.warning(
                "E2B sandbox creation failed",
                exc_type=type(exc).__name__,
                msg=str(exc)[:200],
            )
            msg = f"E2B sandbox creation failed: {type(exc).__name__}: {exc}"
            raise SandboxUnavailableError(msg, context={"reason": "e2b_create_failed"}) from exc
        # Out-dir parity (the documented ``/workspace/out`` contract). The local
        # substrate creates + mounts it read-write; the E2B substrate must too,
        # or model code writing to ``/workspace/out/<file>`` raises
        # ``FileNotFoundError`` (and the turn thrashes). Best-effort: a substrate
        # that already has the dir, or an SDK without ``make_dir``, must not break
        # creation — the per-execute bootstrap (``_run_and_marshal``) is the
        # belt-and-braces fallback.
        self._ensure_workspace_out(sandbox)
        return sandbox

    @staticmethod
    def _ensure_workspace_out(sandbox: E2BSandbox) -> None:
        """Ensure the documented ``/workspace/out`` dir exists on the sandbox.

        The model is told to write produced files under ``/workspace/out`` (the
        prompt builder + every document-generation skill). The E2B substrate has
        no such directory by default, so we create it once per sandbox. Uses the
        SDK ``files.make_dir`` when available; failure is logged and swallowed
        (the per-execute bootstrap re-ensures it before user code runs).
        """
        make_dir = getattr(getattr(sandbox, "files", None), "make_dir", None)
        if make_dir is None:
            return
        try:
            make_dir(_HOSTED_WORKSPACE_OUT)
        except Exception as exc:  # noqa: BLE001 — SDK error hierarchy abstracted
            _logger.debug(
                "could not pre-create /workspace/out (will retry in-exec)",
                exc_type=type(exc).__name__,
            )

    def _run_and_marshal(
        self,
        sandbox: E2BSandbox,
        code: str,
        timeout_s: float,
        input_files: list[SandboxFile],
        limits: ResourceLimits,
    ) -> ExecutionResult:
        """Run ``code`` on ``sandbox``; marshal the E2B result into :class:`ExecutionResult`."""
        # Seed input files via the SDK's filesystem API. Best-effort: file
        # marshalling is the D-12-11 hosted bridge.
        for f in input_files:
            if f.content_bytes is None:
                continue
            try:
                sandbox.files.write(f"/home/user/{f.path}", f.content_bytes)
            except Exception as exc:  # noqa: BLE001 — defensive; T08 minimal scope
                _logger.warning(
                    "input-file seed failed",
                    path=f.path,
                    exc_type=type(exc).__name__,
                )

        # Out-dir parity belt-and-braces: ensure the documented /workspace/out
        # exists before user code runs, in case the SDK lacks ``files.make_dir``
        # or the dir was reaped. Idempotent + cheap; runs in the same kernel so
        # the directory persists for subsequent stateful calls. Prepended (not a
        # separate run_code) so it cannot consume a turn or perturb timing.
        code = f"{_WORKSPACE_OUT_BOOTSTRAP}\n{code}"

        started = time.perf_counter()
        try:
            execution = sandbox.run_code(code, timeout=timeout_s)
        except Exception as exc:  # noqa: BLE001 — SDK error hierarchy abstracted
            duration_ms = (time.perf_counter() - started) * 1000.0
            return ExecutionResult(
                stdout="",
                stderr=f"{type(exc).__name__}: {exc}",
                exit_status=-1,
                outcome="error",
                duration_ms=duration_ms,
            )

        duration_ms = (time.perf_counter() - started) * 1000.0

        # Marshal E2B's Execution into ExecutionResult.
        stdout = "".join(execution.logs.stdout) if execution.logs.stdout else ""
        stderr = "".join(execution.logs.stderr) if execution.logs.stderr else ""
        outcome: ExecutionOutcome
        if execution.error is not None:
            stderr = stderr + f"\n{execution.error.name}: {execution.error.value}"
            outcome = "error"
            exit_status = 1
        else:
            outcome = "ok"
            exit_status = 0

        # Produced-file discovery (parity with LocalDockerSandbox). The local
        # substrate walks the rw ``/workspace/out`` mount; the E2B substrate has
        # no host mount, so we list + read over the SDK filesystem API. Runs for
        # every outcome — a partial-success run that wrote a chart before
        # erroring still surfaces it (mirrors the produced_file_persister which
        # fires regardless of outcome). Best-effort: a listing failure logs and
        # yields no produced files rather than masking the run's real result.
        produced, files_truncated, refused = self._discover_produced_files(sandbox, limits)

        return ExecutionResult(
            stdout=stdout,
            stderr=stderr,
            exit_status=exit_status,
            outcome=outcome,
            produced_files=produced,
            duration_ms=duration_ms,
            truncated_files=files_truncated,
            refused_files=refused,
        )

    @classmethod
    def _discover_produced_files(
        cls, sandbox: E2BSandbox, limits: ResourceLimits
    ) -> tuple[tuple[SandboxFile, ...], bool, tuple[RefusedFile, ...]]:
        """List + read files produced under ``/workspace/out`` on the E2B sandbox.

        Hosted analogue of :meth:`LocalDockerSandbox._discover_produced_files`.
        The local substrate walks the read-write host mount; E2B has no host
        mount, so this lists the documented out-dir via the SDK
        (``files.list(path, depth=...)`` → ``list[EntryInfo]`` with ``path`` /
        ``type`` / ``size`` per the verified SDK shape). Like the local path it
        returns metadata-only :class:`SandboxFile` entries (``content_bytes``
        stays ``None``) — the bytes are read on demand by the api-side
        ``produced_file_persister`` via :meth:`copy_produced_file_to` /
        :meth:`read_produced_file_bytes` (which is where
        :data:`PRODUCED_FILE_CAP_BYTES` is authoritatively enforced).

        Caps mirror the local path exactly (D-12-10): the per-file
        ``max_produced_file_mb`` cap skips an oversize file (marking the run
        truncated so the model knows), and the ``max_produced_files`` count cap
        stops enumeration. ``media_type`` is inferred from the extension so a
        produced PNG surfaces as ``image/png`` and renders inline.

        Returns ``(files, was_truncated, refused)``; ``was_truncated`` is ``True``
        when either cap fired. ``refused`` holds files whose relative path is not
        safe to bring into the workspace (Spec WIN, T1.5): code in the sandbox can
        create any Linux name, and a backslash, a colon, a leading ``/`` or a
        ``..`` becomes a separator, a drive, an alternate data stream or an escape
        once joined onto a host path. A missing out-dir or any SDK listing error
        yields ``((), False, ())``; discovery never converts a successful run into a
        failure.
        """
        try:
            entries = sandbox.files.list(_HOSTED_WORKSPACE_OUT, depth=_PRODUCED_FILE_LIST_DEPTH)
        except Exception as exc:  # noqa: BLE001 — SDK error hierarchy abstracted
            # Most commonly a NotFoundException when no out-dir was created.
            _logger.debug(
                "produced-file listing failed (no out-dir / SDK error)",
                exc_type=type(exc).__name__,
            )
            return (), False, ()

        per_file_cap_bytes = limits.max_produced_file_mb * 1024 * 1024
        prefix = f"{_HOSTED_WORKSPACE_OUT}/"
        produced: list[SandboxFile] = []
        refused: list[RefusedFile] = []
        truncated = False
        # Sort by absolute path for a deterministic, sortable order (parity with
        # the local path's ``sorted(host_out.rglob("*"))``).
        for entry in sorted(entries, key=lambda e: e.path):
            if not cls._entry_is_file(entry):
                continue
            abs_path = entry.path
            if not abs_path.startswith(prefix):
                # Defensive: the SDK should only return entries under the listed
                # dir, but never surface a path outside the documented out-dir.
                continue
            violation = produced_file_path_violation(abs_path[len(prefix) :])
            if violation is not None:
                refused.append(refused_file(abs_path[len(prefix) :], violation))
                continue
            if len(produced) >= limits.max_produced_files:
                truncated = True
                break
            size = int(entry.size)
            if size > per_file_cap_bytes:
                # Surface that the run was truncated (the model wrote it) but do
                # not report an oversize file the persister cannot copy.
                truncated = True
                continue
            rel = abs_path[len(prefix) :]
            produced.append(
                SandboxFile(
                    path=rel,
                    size_bytes=size,
                    media_type=guess_media_type(rel),
                )
            )
        return tuple(produced), truncated, tuple(refused)

    @staticmethod
    def _entry_is_file(entry: E2BEntryInfo) -> bool:
        """True when an E2B ``EntryInfo`` denotes a regular file (not a dir).

        The SDK ``EntryInfo.type`` is a ``FileType`` enum (``FILE`` / ``DIR``);
        compared by its ``.value`` string (``"file"``) to avoid importing the
        SDK enum into this lazy-import module. Defensive: an entry without a
        recognisable type is treated as a file (it will simply fail the read
        and be skipped if it is not).
        """
        file_type = getattr(entry, "type", None)
        type_value = getattr(file_type, "value", file_type)
        return type_value != _E2B_DIR_TYPE_VALUE

    @staticmethod
    def _safe_kill(sandbox: E2BSandbox) -> None:
        """Kill a sandbox; swallow errors (idempotent / already-killed)."""
        try:
            sandbox.kill()
        except Exception as exc:  # noqa: BLE001 — defensive
            _logger.debug(
                "sandbox kill failed (already gone?)",
                exc_type=type(exc).__name__,
            )

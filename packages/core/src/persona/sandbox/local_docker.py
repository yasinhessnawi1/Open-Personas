"""LocalDockerSandbox — hardened Docker-based sandbox for the open-source CLI.

**Threat-model scope (D-12-13):** this backend's hardening — drop-all
capabilities, custom seccomp, read-only root, ``network=none`` default,
pids/memory/cpu caps, two-mount workspace, non-root user — is for
**single-tenant defence-in-depth on the user's own machine**. It is NOT
the model for the hosted multi-tenant path; ``HostedSandbox`` (T08, behind
the D-12-12 E2B lock-gates) inherits its isolation from the Firecracker
microVM substrate, with E2B SDK controls as the hosted analogues. The two
backends share a Protocol (T02), not a hardening configuration.

**T05a scope:** one-shot stateless execution + lifecycle plumbing
(:meth:`aclose`).

**T05b scope:** outcome classification refinements (oom / killed via
``container.attrs["State"]["OOMKilled"]``), stdout/stderr byte-level
truncation with the T03 marker, ANSI strip + container-path scrub,
produced-file count/size caps (D-12-10 snapshot-then-diff).

**T05c scope (scaled — pragmatic v0.1):** session container lifecycle +
``docker exec``-based dispatch. ``create_session`` spawns a long-lived
container; subsequent ``execute(session_id=...)`` calls dispatch code into
the same container via ``docker exec``, so **filesystem state persists**
across calls (the workspace mount is the same). **Variable-level Python
state does NOT persist** across calls because each ``docker exec`` is a
fresh Python process — this is the v0.1 limitation; v0.2 lands an
IPython-kernel persistent-interpreter for true variable persistence
(D-12-1 long-term path). The §9 #3 acceptance is partial; §9 #4 is full.

Tenant-isolated session IDs (kickoff trip-up #6): the runtime composes
``session_id`` as ``f"{owner_id}:{conversation_id}"`` so a different tenant
with a colliding ``conversation_id`` cannot share session state. The
Protocol takes ``session_id`` opaque; tenant-prefixing is the caller's
invariant.

**R-12-2 configuration:** the ``docker.client.containers.run(...)`` kwargs
in :data:`_BASE_CONTAINER_KWARGS` are the research-justified hardened
defaults. Every flag is one R-12-2 row; the unit tests pin the config so
a future refactor can't silently weaken the security posture.

**Workspace bridge (D-12-9):** two-mount in/out — ``/workspace/in`` is
read-only (the model can't tamper with its own inputs), ``/workspace/out``
is read-write (the only writable host path). Both are per-execution
temporary directories that get cleaned up by :meth:`aclose` / the
per-exec ``finally`` clause.

**Docker SDK is sync, the Protocol is async:** every blocking call goes
through :func:`asyncio.to_thread` so the FastAPI event loop / CLI REPL
never blocks on substrate I/O.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import shutil
import time
import uuid
from datetime import UTC, datetime  # noqa: TC003 — runtime use below
from pathlib import Path  # noqa: TC003 — runtime use in _make_workspace_dirs
from typing import TYPE_CHECKING, Any, cast

# Docker SDK is the [sandbox] extra (D-12-5 + spec 12 kickoff: "LocalDockerSandbox
# lazy-imports docker at construction so the module stays importable without the
# SDK installed"). Wrap imports so `import persona.sandbox.local_docker` works on
# a minimal install; LocalDockerSandbox raises SandboxUnavailableError at
# construction when the SDK is absent, and `is_docker_available()` returns False.
try:
    import docker
    from docker.errors import APIError, DockerException, ImageNotFound, NotFound

    _DOCKER_SDK_AVAILABLE = True
except ImportError:
    _DOCKER_SDK_AVAILABLE = False
    docker = None  # type: ignore[assignment, unused-ignore]

    # Sentinel exception classes. These are NEVER raised — `_resolve_client()`
    # raises `SandboxUnavailableError` before any code path that would catch
    # them runs — but they're imported at module level so `except APIError`
    # clauses scattered through the module remain syntactically valid.
    # N818 (Error suffix) suppressed per class — these names mirror the
    # upstream ``docker.errors`` API exactly so that ``except APIError`` /
    # ``except DockerException`` / etc. clauses elsewhere in the module
    # resolve to the real class when [sandbox] is installed and to these
    # sentinels otherwise. Renaming would break the runtime import on the
    # try branch.
    class APIError(Exception):  # type: ignore[no-redef, unused-ignore]  # noqa: N818
        """Sentinel — real ``docker.errors.APIError`` when [sandbox] is installed."""

    class DockerException(Exception):  # type: ignore[no-redef, unused-ignore]  # noqa: N818
        """Sentinel — real ``docker.errors.DockerException`` when [sandbox] is installed."""

    class ImageNotFound(Exception):  # type: ignore[no-redef, unused-ignore]  # noqa: N818
        """Sentinel — real ``docker.errors.ImageNotFound`` when [sandbox] is installed."""

    class NotFound(Exception):  # type: ignore[no-redef, unused-ignore]  # noqa: N818
        """Sentinel — real ``docker.errors.NotFound`` when [sandbox] is installed."""


if TYPE_CHECKING:
    from docker.models.containers import Container
    from docker.types import LogConfig as _DockerLogConfig
    from docker.types import Ulimit as _DockerUlimit

from persona.logging import get_logger
from persona.sandbox.errors import (
    CodeSandboxError,
    SandboxUnavailableError,
)
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

__all__ = ["LocalDockerSandbox"]

_logger = get_logger("sandbox.local_docker")


# ---------------------------------------------------------------------------
# R-12-2 hardened container config
# ---------------------------------------------------------------------------

#: Default sandbox image tag. T06 ships ``persona-sandbox:0.1.0`` with the
#: R-12-3 pinned data/document stack. Override via env / constructor param.
DEFAULT_IMAGE = "persona-sandbox:0.1.0"

#: Path inside the container where the model's code is dropped. The container
#: ENTRYPOINT runs ``python -u`` on this file. See :func:`_build_command`.
_CODE_PATH_IN_CONTAINER = "/workspace/in/__persona_main__.py"

#: Two-mount workspace per D-12-9. ``/workspace/in`` is read-only (the model
#: cannot overwrite its inputs); ``/workspace/out`` is read-write (the only
#: writable host path).
_WORKSPACE_IN = "/workspace/in"
_WORKSPACE_OUT = "/workspace/out"

#: tmpfs mount options per R-12-2: hardened against escape (``noexec``,
#: ``nosuid``, ``nodev``) but writable for Python's ``__pycache__`` and
#: matplotlib's font cache. ``noexec`` is safe for ``.pyc`` (CPython parses,
#: doesn't ``mmap(PROT_EXEC)``) and blocks the drop-then-dlopen attack path.
_TMPFS_MOUNTS = {
    "/tmp": "size=64m,mode=1777,noexec,nosuid,nodev",
    "/var/tmp": "size=16m,mode=1777,noexec,nosuid,nodev",
}

#: R-12-2 base kwargs for ``docker.client.containers.run``. Every flag
#: justified line-by-line in the research; each represents one threat-model
#: row. The unit tests pin this dict so a future refactor cannot silently
#: weaken the posture.
_BASE_CONTAINER_KWARGS: dict[str, Any] = {
    # Identity + filesystem
    "user": "65534:65534",  # nobody:nogroup — non-root per CIS §5
    "read_only": True,  # rootfs is a read-only overlay
    "tmpfs": _TMPFS_MOUNTS,
    "working_dir": _WORKSPACE_OUT,
    # Kernel-level isolation
    "cap_drop": ["ALL"],  # Python needs zero caps (R-12-2)
    "security_opt": [
        "no-new-privileges:true",  # prctl PR_SET_NO_NEW_PRIVS (R-12-2)
        # T05b adds the custom seccomp profile path; T05a uses Docker's default.
    ],
    # Resource caps (DoS defence — T05b refines per ResourceLimits)
    "mem_limit": "512m",
    "memswap_limit": "512m",  # = mem_limit ⇒ swap disabled (R-12-2)
    "nano_cpus": 1_000_000_000,  # 1.0 CPU
    "pids_limit": 128,  # forkbomb cap, threading-headroom-aware
    # Runtime hygiene — auto_remove is FALSE in T05b so we can inspect
    # ``attrs["State"]["OOMKilled"]`` after the container exits but before
    # it's removed. Ephemerality is enforced by the explicit
    # ``_best_effort_remove`` in the per-exec ``finally`` clause instead.
    "auto_remove": False,
    "init": True,  # tini PID 1 — reaps zombies (T05c IPython subprocs)
    "ipc_mode": "private",  # explicit; no /dev/shm sharing
    "hostname": "sandbox",  # no host info leak via gethostname()
    # Environment hygiene — minimal, no host PATH leakage
    "environment": {
        "PYTHONDONTWRITEBYTECODE": "0",  # allow .pyc in tmpfs (noexec-safe)
        "PYTHONUNBUFFERED": "1",  # stream stdout in real time
        "HOME": "/home/nobody",
        "TMPDIR": "/tmp",
        # D-12-X-venv-path-ordering: prepend /opt/venv/bin so the persona-sandbox
        # image's venv-installed tooling (python-docx, openpyxl, python-pptx,
        # reportlab, etc.) is reachable via ``python`` / ``pip`` without forcing
        # SKILL.md packs to teach a ``sys.path.insert`` workaround. The explicit
        # system-path tail preserves R-12-2's hardening intent (no host PATH
        # leakage, no shell-injection vector). Surfaced by Spec 16 T09/T10 as a
        # production bug: without the venv prefix, ``from docx import Document``
        # raises ModuleNotFoundError inside the running container.
        "PATH": "/opt/venv/bin:/usr/local/bin:/usr/bin:/bin",
        "MPLCONFIGDIR": "/home/nobody/.mpl",
    },
}

#: Log driver config — R-12-2 log-bomb defence. Cap per-container log size at
#: 10 MiB; the Python side enforces the additional stdout cap from
#: ``ResourceLimits.max_stdout_bytes`` (T05b) when reading container logs.
#:
#: Lazy via :func:`_log_config` because :class:`docker.types.LogConfig` is in
#: the [sandbox] extra and the module must import without it. First call after
#: the SDK is available constructs once and caches.
_LOG_CONFIG_CACHE: _DockerLogConfig | None = None


def _log_config() -> _DockerLogConfig:
    """Return the cached R-12-2 LogConfig (lazy — requires [sandbox] extra)."""
    global _LOG_CONFIG_CACHE
    if _LOG_CONFIG_CACHE is None:
        from docker.types import LogConfig

        _LOG_CONFIG_CACHE = LogConfig(
            type="json-file",
            config={"max-size": "10m", "max-file": "1"},
        )
    return _LOG_CONFIG_CACHE


#: R-12-2 ulimits — fd / process exhaustion caps. ``RLIMIT_NPROC`` is
#: per-real-UID (kickoff trip-up notes this for the concurrent-sandbox
#: hosted case; local CLI is single-sandbox-at-a-time, so this is moot here).
#:
#: Lazy via :func:`_ulimits` for the same reason as :func:`_log_config`.
_ULIMITS_CACHE: list[_DockerUlimit] | None = None


def _ulimits() -> list[_DockerUlimit]:
    """Return the cached R-12-2 Ulimits (lazy — requires [sandbox] extra)."""
    global _ULIMITS_CACHE
    if _ULIMITS_CACHE is None:
        from docker.types import Ulimit

        _ULIMITS_CACHE = [
            Ulimit(name="nofile", soft=128, hard=128),
            Ulimit(name="nproc", soft=128, hard=128),
        ]
    return _ULIMITS_CACHE


#: ANSI escape sequence pattern — stripped from stderr so traceback color
#: codes don't pollute the model's view. Matches CSI sequences (the common
#: ``\x1b[31m``-style) + the bare ``\x1b`` byte. T05b sanitisation.
_ANSI_PATTERN = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b[@-_]")

#: Absolute container paths that the substrate uses internally. Stderr
#: tracebacks include these (``/workspace/in/__persona_main__.py``,
#: ``/usr/local/lib/python3.11/...``); we replace them with relative
#: workspace paths so the model doesn't see substrate internals (kickoff
#: trip-up #5: workspace path leakage in stack traces). The substitution
#: is conservative — only collapses what we know about, no path-scrubbing
#: heuristics that could clobber legitimate user output.
_PATH_REWRITES: tuple[tuple[str, str], ...] = (
    (_CODE_PATH_IN_CONTAINER, "<your code>"),
    (_WORKSPACE_IN + "/", ""),
    (_WORKSPACE_OUT + "/", ""),
)


# ---------------------------------------------------------------------------
# The backend
# ---------------------------------------------------------------------------


class LocalDockerSandbox:
    """Hardened Docker-based :class:`CodeSandbox` backend (T05a — one-shot).

    Args:
        image: Docker image tag for the sandbox. Defaults to
            :data:`DEFAULT_IMAGE` (T06's ``persona-sandbox:0.1.0``).
        workspace_root: Host directory under which per-execution temporary
            workspace mounts are created. Must exist and be writable by the
            current process. Each execution gets a subdir
            ``<workspace_root>/<exec_id>/{in,out}/``.
        docker_client: Optional pre-constructed Docker client. Tests inject
            a mock; in production this is ``None`` and the constructor
            resolves :func:`docker.from_env` lazily on first :meth:`execute`.
        platform: Optional explicit platform pin (e.g. ``"linux/amd64"`` —
            avoids surprise emulation on M-series Macs). ``None`` = let
            Docker pick.

    Raises:
        SandboxUnavailableError: On :meth:`execute` when the Docker daemon
            is unreachable.
    """

    def __init__(
        self,
        *,
        image: str = DEFAULT_IMAGE,
        workspace_root: Path,
        docker_client: docker.DockerClient | None = None,
        platform: str | None = None,
    ) -> None:
        self._image = image
        self._workspace_root = workspace_root
        self._docker_client = docker_client
        self._platform = platform
        self._closed = False
        # T05c: per-session container references (session_id → container).
        # Held in memory; persistence across process restarts is a v0.2
        # concern (the pool in T09 handles that for the hosted path).
        self._sessions: dict[str, Container] = {}
        # T05c: per-session workspace dirs (session_id → (host_in, host_out)).
        # Kept alive across executions in the same session so workspace
        # state persists. Cleaned up by :meth:`destroy_session`.
        self._session_workspaces: dict[str, tuple[Path, Path]] = {}
        if not workspace_root.exists():
            workspace_root.mkdir(parents=True, exist_ok=True)
        _logger.debug(
            "LocalDockerSandbox initialised",
            image=image,
            workspace_root=str(workspace_root),
            platform=platform or "<auto>",
        )

    # -- CodeSandbox Protocol methods -------------------------------------

    async def execute(
        self,
        code: str,
        *,
        language: str = "python",
        session_id: str | None = None,
        timeout_s: float = 30.0,
        limits: ResourceLimits | None = None,
        network: NetworkPolicy | None = None,
        input_files: list[SandboxFile] | None = None,
    ) -> ExecutionResult:
        """Run ``code`` in a fresh ephemeral container; return the result.

        T05a implements stateless one-shot execution (``session_id=None``).
        Passing a ``session_id`` raises :class:`NotImplementedError` until
        T05c lands kernel-style persistent sessions (D-12-1).

        Network is OFF by default per spec §4.2 / D-12-4. T05a uses
        ``network_mode="none"`` when ``network.enabled=False``; T07 wires the
        custom bridge + DOCKER-USER egress filter for the
        ``network.enabled=True`` path. For T05a, enabling network falls back
        to bridge mode WITHOUT the R-12-5 egress block — that's why T07
        must land before any persona declares ``code_execution.network``.

        Args:
            code: The Python source to execute. Treated as adversarial.
            language: Source language. Only ``"python"`` accepted in v0.1.
            session_id: Stateful-session identifier. **T05a only supports
                ``None``** (stateless one-shot); T05c implements sessions.
            timeout_s: Per-execution wall-clock cap. Substrate kills past this.
            limits: Resource caps. T05b refines the substrate-side enforcement.
            network: Egress policy (default off).
            input_files: Files to seed into ``/workspace/in`` before exec.

        Returns:
            :class:`ExecutionResult`. Outcome classification in T05a is
            rudimentary (T05b refines).
        """
        if self._closed:
            msg = "LocalDockerSandbox is closed"
            raise SandboxUnavailableError(msg, context={"reason": "closed"})
        if language != "python":
            msg = f"unsupported language: {language!r}"
            raise CodeSandboxError(msg, context={"language": language})

        limits = limits or ResourceLimits()
        network = network or NetworkPolicy()
        input_files = input_files or []

        if session_id is not None:
            # T05c: dispatch into the long-lived session container via
            # ``docker exec``. Filesystem state persists (the workspace
            # mount is the same); variable state does NOT (each exec is a
            # fresh Python process — known limitation, v0.2 IPython kernel).
            return await asyncio.to_thread(
                self._execute_in_session_sync,
                code,
                session_id=session_id,
                timeout_s=timeout_s,
                limits=limits,
                input_files=input_files,
            )

        return await asyncio.to_thread(
            self._execute_sync,
            code,
            timeout_s=timeout_s,
            limits=limits,
            network=network,
            input_files=input_files,
        )

    async def create_session(
        self,
        session_id: str,
        *,
        limits: ResourceLimits,
        network: NetworkPolicy,
    ) -> None:
        """Create a long-lived session container (T05c).

        Spins up a keepalive container (``tail -f /dev/null`` entrypoint —
        matches Anthropic / E2B prior-art for "container as workspace").
        Subsequent :meth:`execute` calls with the same ``session_id``
        dispatch code into this container via ``docker exec``, sharing
        the workspace filesystem across calls.

        Idempotent: creating a session with an already-existing id is a
        no-op (the existing container is reused).

        Per D-12-7: every session container is destroyed by :meth:`aclose`.
        """
        if self._closed:
            msg = "LocalDockerSandbox is closed"
            raise SandboxUnavailableError(msg, context={"reason": "closed"})
        if session_id in self._sessions:
            _logger.debug("create_session: idempotent reuse", session_id=session_id)
            return
        await asyncio.to_thread(
            self._create_session_sync,
            session_id=session_id,
            limits=limits,
            network=network,
        )

    async def destroy_session(self, session_id: str) -> None:
        """Destroy a session container and free its workspace (T05c).

        Idempotent: destroying a non-existent session is a no-op (it may
        have been reaped by an earlier :meth:`aclose`)."""
        container = self._sessions.pop(session_id, None)
        workspace = self._session_workspaces.pop(session_id, None)
        if container is not None:
            await asyncio.to_thread(self._best_effort_remove, container)
        if workspace is not None:
            await asyncio.to_thread(self._cleanup_workspace_dirs, *workspace)

    async def aclose(self) -> None:
        """Release substrate-side resources.

        T05c: destroy every live session container + workspace. T09 adds
        warm-pool teardown when the pool wires this backend.
        """
        if self._closed:
            return
        self._closed = True
        # Snapshot session_ids so we don't mutate dict during iteration.
        session_ids = list(self._sessions.keys())
        for session_id in session_ids:
            await self.destroy_session(session_id)
        client = self._docker_client
        if client is None:
            return
        try:
            await asyncio.to_thread(client.close)
        except DockerException as exc:  # pragma: no cover — defensive
            _logger.warning("docker client close failed", exc_type=type(exc).__name__)

    async def copy_produced_file_to(
        self,
        session_id: str,
        ref: str,
        target_path: Path,
        *,
        root: Path,
    ) -> None:
        """Copy a produced file from the session's host_out to a target path.

        D-12-X-read-produced-file local impl: reads ``<host_out>/<ref>`` under the
        :data:`PRODUCED_FILE_CAP_BYTES` cap, then writes it under ``root`` through
        the no-follow helpers (Spec WIN, T1.5), so a link at the destination is
        refused rather than followed out of the workspace.
        """
        host_out = self._resolve_produced_source(session_id, ref)
        await asyncio.to_thread(
            self._copy_produced_sync, host_out, target_path, session_id, ref, root
        )

    async def read_produced_file_bytes(
        self,
        session_id: str,
        ref: str,
    ) -> bytes:
        """Read produced file bytes for audit/debug small-file paths.

        D-12-X-read-produced-file local impl: a guarded
        ``source.read_bytes()`` after the size cap is checked.
        """
        host_out = self._resolve_produced_source(session_id, ref)
        return await asyncio.to_thread(self._read_produced_sync, host_out, session_id, ref)

    def _resolve_produced_source(self, session_id: str, ref: str) -> Path:
        """The session's host ``out`` folder, where the produced file ``ref`` lives.

        Raises :class:`CodeSandboxError` if the session has no recorded
        workspace (was never created / already reaped).
        """
        if session_id not in self._session_workspaces:
            msg = f"session {session_id!r} has no recorded workspace; cannot read produced file"
            raise CodeSandboxError(
                msg,
                context={"reason": "no_session", "session_id": session_id, "ref": ref},
            )
        _host_in, host_out = self._session_workspaces[session_id]
        return host_out

    @staticmethod
    def _copy_produced_sync(
        host_out: Path, target_path: Path, session_id: str, ref: str, root: Path
    ) -> None:
        """Read ``ref`` from ``host_out`` without following a link, write it under ``root``."""
        from persona.tools._sandbox import write_file_under_root  # noqa: PLC0415

        data = LocalDockerSandbox._read_produced_sync(host_out, session_id, ref)
        write_file_under_root(target_path, data, root=root)

    @staticmethod
    def _read_produced_sync(host_out: Path, session_id: str, ref: str) -> bytes:
        """Read ``ref`` from ``host_out`` under the size cap, never through a link.

        Spec WIN T1.6 (X-1): the container controls ``host_out``; a link anywhere in
        ``ref`` (or a file with a second name) is refused with
        :class:`ProducedFileRefusedError`, which the tool reports for this one file.
        """
        from persona.sandbox._produced_io import read_produced_file  # noqa: PLC0415
        from persona.sandbox.protocol import PRODUCED_FILE_CAP_BYTES  # noqa: PLC0415

        return read_produced_file(
            host_out, ref, session_id=session_id, cap_bytes=PRODUCED_FILE_CAP_BYTES
        )

    # -- Sync internals (called via asyncio.to_thread) ---------------------

    def _execute_sync(
        self,
        code: str,
        *,
        timeout_s: float,
        limits: ResourceLimits,
        network: NetworkPolicy,
        input_files: list[SandboxFile],
    ) -> ExecutionResult:
        """Synchronous one-shot execute. Runs inside :func:`asyncio.to_thread`."""
        client = self._resolve_client()
        exec_id = uuid.uuid4().hex[:12]
        host_in, host_out = self._make_workspace_dirs(exec_id)
        try:
            self._seed_workspace(host_in, code, input_files)
            return self._run_container(
                client,
                host_in=host_in,
                host_out=host_out,
                timeout_s=timeout_s,
                limits=limits,
                network=network,
                exec_id=exec_id,
            )
        finally:
            self._cleanup_workspace_dirs(host_in, host_out)

    def _resolve_client(self) -> docker.DockerClient:
        """Lazy-resolve the docker client. Maps daemon-unreachable to
        :class:`SandboxUnavailableError` (D-12-5: no degraded fallback;
        the tool surfaces the error to the model). Also raises when the
        ``[sandbox]`` extra isn't installed (module imported but SDK absent).

        Injected ``docker_client`` (used by unit tests to bypass the SDK)
        wins over the SDK-availability check so mocked tests stay green on
        minimal installs.
        """
        if self._docker_client is not None:
            return self._docker_client
        if not _DOCKER_SDK_AVAILABLE:
            raise SandboxUnavailableError(
                "docker SDK not installed. Install persona-core[sandbox] to "
                "enable LocalDockerSandbox (D-12-5: no degraded fallback).",
                context={"reason": "sdk_missing"},
            )
        try:
            client = docker.from_env()
            # Ping forces a daemon round-trip so we fail fast on "no Docker".
            client.ping()
        except DockerException as exc:
            _logger.warning(
                "docker daemon unreachable",
                exc_type=type(exc).__name__,
                hint="install Docker or check `docker ps`",
            )
            msg = (
                "Docker daemon unreachable. "
                "Install Docker to enable code execution (D-12-5: no degraded fallback)."
            )
            raise SandboxUnavailableError(
                msg,
                context={"reason": "docker_unreachable", "error": str(exc)[:200]},
            ) from exc
        self._docker_client = client
        return client

    def _make_workspace_dirs(self, exec_id: str) -> tuple[Path, Path]:
        """Create per-execution ``in/`` (ro-mounted) and ``out/`` (rw-mounted)
        host directories under :attr:`_workspace_root`."""
        exec_root = self._workspace_root / exec_id
        host_in = exec_root / "in"
        host_out = exec_root / "out"
        host_in.mkdir(parents=True, exist_ok=False)
        host_out.mkdir(parents=True, exist_ok=False)
        # Make /workspace/out world-writable so the container's `nobody` UID
        # (65534) can write into the host mount regardless of the host UID
        # that created the dir. The dir lives only for this execution.
        host_out.chmod(0o777)  # noqa: S103 — ephemeral per-exec dir
        return host_in, host_out

    def _cleanup_workspace_dirs(self, host_in: Path, host_out: Path) -> None:
        """Remove per-execution workspace directories.

        Best-effort: failures are logged, not raised. Mounted files may be
        owned by UID 65534 (the container's nobody), but the parent dir is
        owned by the host process — :func:`shutil.rmtree` handles this fine
        as the host process owns the directory it created.
        """
        for path in (host_in, host_out):
            try:
                shutil.rmtree(path, ignore_errors=True)
            except OSError as exc:  # pragma: no cover — defensive
                _logger.warning(
                    "workspace cleanup failed",
                    path=str(path),
                    exc_type=type(exc).__name__,
                )
        # Try to remove the parent exec dir too; ignore if not empty
        # (concurrent debug inspection may leave files).
        with contextlib.suppress(OSError):
            host_in.parent.rmdir()

    def _seed_workspace(
        self,
        host_in: Path,
        code: str,
        input_files: list[SandboxFile],
    ) -> None:
        """Write the code script + any input files into the read-only mount."""
        code_path = host_in / "__persona_main__.py"
        code_path.write_text(code, encoding="utf-8")
        for f in input_files:
            target = host_in / f.path
            target.parent.mkdir(parents=True, exist_ok=True)
            if f.content_bytes is not None:
                target.write_bytes(f.content_bytes)

    def _run_container(
        self,
        client: docker.DockerClient,
        *,
        host_in: Path,
        host_out: Path,
        timeout_s: float,
        limits: ResourceLimits,
        network: NetworkPolicy,
        exec_id: str,
    ) -> ExecutionResult:
        """Build kwargs, run, wait, capture output, classify outcome.

        T05a outcome classification is rudimentary: ``ok`` on exit 0,
        ``error`` on non-zero, ``timeout`` on wait-timeout. T05b reads
        ``container.attrs["State"]["OOMKilled"]`` to refine to ``"oom"``
        and adds disk/pids classification.
        """
        kwargs = self._build_container_kwargs(
            host_in=host_in,
            host_out=host_out,
            limits=limits,
            network=network,
            exec_id=exec_id,
        )
        started = time.perf_counter()
        container = None
        try:
            container = client.containers.run(**kwargs)
        except ImageNotFound as exc:
            msg = (
                f"sandbox image not found: {self._image!r}. "
                "Build the image (T06) or set PERSONA_SANDBOX_IMAGE to an available tag."
            )
            raise SandboxUnavailableError(
                msg,
                context={"reason": "image_missing", "image": self._image},
            ) from exc
        except APIError as exc:
            raise CodeSandboxError(
                f"docker API error: {exc}",
                context={"reason": "api_error"},
            ) from exc

        try:
            return self._wait_and_classify(
                container=container,
                started=started,
                timeout_s=timeout_s,
                host_out=host_out,
                limits=limits,
            )
        finally:
            # ``auto_remove=True`` removes the container on exit — but only
            # if it exited normally. On timeout we killed it; the auto-remove
            # races with the kill so we explicitly try to remove.
            self._best_effort_remove(container)

    def _build_container_kwargs(
        self,
        *,
        host_in: Path,
        host_out: Path,
        limits: ResourceLimits,
        network: NetworkPolicy,
        exec_id: str,
    ) -> dict[str, Any]:
        """Merge :data:`_BASE_CONTAINER_KWARGS` with per-execution overrides.

        Per-execution: image, command, the two workspace mounts, the
        network_mode (T07 wires the custom bridge for ``network.enabled=True``;
        T05a falls back to bridge mode with a logged warning when
        ``network.enabled=True`` AND T07 hasn't landed yet — see _BASE doc).
        Resource overrides from :class:`ResourceLimits` (T05b expands).
        """
        kwargs = dict(_BASE_CONTAINER_KWARGS)
        kwargs["image"] = self._image
        kwargs["command"] = self._build_command()
        kwargs["name"] = f"persona-sandbox-{exec_id}"
        kwargs["volumes"] = {
            str(host_in): {"bind": _WORKSPACE_IN, "mode": "ro"},
            str(host_out): {"bind": _WORKSPACE_OUT, "mode": "rw"},
        }
        # Network mode (T07): default-off uses ``"none"``; opt-in uses the
        # custom :data:`SANDBOX_BRIDGE_NAME` bridge so the R-12-5 egress
        # filter (DOCKER-USER chain — applied at host setup via
        # :func:`apply_egress_rules`) blocks metadata + RFC-1918 + IPv6
        # link-local REGARDLESS of the persona's allow-list (D-12-4 / SSRF
        # prior art from spec 11).
        if network.enabled:
            from persona.sandbox.egress import SANDBOX_BRIDGE_NAME

            kwargs["network_mode"] = SANDBOX_BRIDGE_NAME
        else:
            kwargs["network_mode"] = "none"
        # Resource overrides (T05b refines)
        kwargs["mem_limit"] = f"{limits.memory_mb}m"
        kwargs["memswap_limit"] = f"{limits.memory_mb}m"  # no swap
        kwargs["nano_cpus"] = int(limits.cpu_cores * 1_000_000_000)
        kwargs["pids_limit"] = max(_BASE_CONTAINER_KWARGS["pids_limit"], 16)
        kwargs["ulimits"] = list(_ulimits())
        kwargs["log_config"] = _log_config()
        # Background mode so we can wait() with a timeout.
        kwargs["detach"] = True
        # Stdout/err captured via container.logs(...) after wait().
        kwargs["stdout"] = True
        kwargs["stderr"] = True
        if self._platform is not None:
            kwargs["platform"] = self._platform
        return kwargs

    @staticmethod
    def _build_command() -> list[str]:
        """The container's entrypoint command — run the seeded script."""
        return ["python", "-u", _CODE_PATH_IN_CONTAINER]

    def _wait_and_classify(
        self,
        *,
        container: Container,
        started: float,
        timeout_s: float,
        host_out: Path,
        limits: ResourceLimits,
    ) -> ExecutionResult:
        """Wait for the container, classify the outcome, capture output.

        T05b: refines the T05a classifier with ``oom`` detection via
        ``container.attrs["State"]["OOMKilled"]`` and ``killed`` for
        substrate-side terminations (signal != normal exit). Reads from
        :meth:`container.reload` so the post-exec state is fresh.
        """
        exit_status = -1
        outcome: str = "error"
        timed_out = False
        try:
            wait_result = container.wait(timeout=timeout_s)
            exit_status = int(wait_result.get("StatusCode", -1))
        except Exception as exc:  # noqa: BLE001 — sdk raises requests.Timeout-equivalent
            # The docker SDK wraps a urllib3 ReadTimeoutError; we accept any
            # exception here as "the wait timed out" and proceed to kill.
            _logger.info(
                "container wait timed out — killing",
                exc_type=type(exc).__name__,
                timeout_s=timeout_s,
            )
            timed_out = True
            with contextlib.suppress(NotFound, APIError):
                container.kill()
                # After kill the container has a final exit status;
                # ``wait`` again to capture it (non-blocking now).
                with contextlib.suppress(Exception):  # noqa: BLE001
                    wait_result = container.wait(timeout=2.0)
                    exit_status = int(wait_result.get("StatusCode", -1))

        # Reload attrs to get the post-exec State.OOMKilled flag (the SDK
        # caches State at creation; reload reads the live state). NotFound
        # only if auto_remove has somehow won the race — defensive.
        oom_killed = False
        with contextlib.suppress(NotFound, APIError):
            container.reload()
            oom_killed = bool(container.attrs.get("State", {}).get("OOMKilled", False))

        duration_ms = (time.perf_counter() - started) * 1000.0
        stdout, stdout_truncated = self._capture_stdout(container, limits)
        stderr, stderr_truncated = self._capture_stderr(container, limits)
        produced, files_truncated, refused = self._discover_produced_files(host_out, limits)

        if oom_killed:
            outcome = "oom"
        elif timed_out:
            outcome = "timeout"
        elif exit_status == 0:
            outcome = "ok"
        elif exit_status < 0:
            outcome = "killed"
        else:
            outcome = "error"

        return ExecutionResult(
            stdout=stdout,
            stderr=stderr,
            exit_status=exit_status,
            outcome=cast("ExecutionOutcome", outcome),
            produced_files=produced,
            duration_ms=duration_ms,
            # ``truncated_stdout`` fires if EITHER stream was capped. The
            # field is named stdout-only for historical reasons (spec §4.1)
            # but encodes "any captured output was truncated".
            truncated_stdout=stdout_truncated or stderr_truncated,
            truncated_files=files_truncated,
            refused_files=refused,
        )

    @classmethod
    def _capture_stdout(cls, container: Container, limits: ResourceLimits) -> tuple[str, bool]:
        """Capture stdout bytes; truncate per ``limits.max_stdout_bytes``.

        Truncation is performed on the byte stream BEFORE decode so we don't
        spend memory decoding a multi-MB log only to throw it away. Returns
        ``(decoded, was_truncated)``."""
        try:
            raw = container.logs(stdout=True, stderr=False)
        except (NotFound, APIError) as exc:  # pragma: no cover — defensive
            _logger.debug("stdout capture failed", exc_type=type(exc).__name__)
            return "", False
        if not raw:
            return "", False
        return cls._truncate_bytes(raw, limits.max_stdout_bytes)

    @classmethod
    def _capture_stderr(cls, container: Container, limits: ResourceLimits) -> tuple[str, bool]:
        """Capture stderr bytes; truncate, strip ANSI, scrub container paths.

        Kickoff trip-up #5: workspace path leakage in stack traces. Sanitisation
        replaces the canonical container paths (``/workspace/in/...``) with
        the relative names the model already knows. ANSI strip prevents
        terminal-control sequences from polluting the model's view."""
        try:
            raw = container.logs(stdout=False, stderr=True)
        except (NotFound, APIError) as exc:  # pragma: no cover — defensive
            _logger.debug("stderr capture failed", exc_type=type(exc).__name__)
            return "", False
        if not raw:
            return "", False
        text, truncated = cls._truncate_bytes(raw, limits.max_stdout_bytes)
        return cls._sanitise_stderr(text), truncated

    @staticmethod
    def _truncate_bytes(raw: bytes, max_bytes: int) -> tuple[str, bool]:
        """Truncate ``raw`` to ``max_bytes`` and decode with replacement.

        Returns ``(decoded_text, was_truncated)``. The marker is the literal
        :data:`persona.sandbox.tool.TRUNCATION_MARKER_PREFIX` so downstream
        consumers (T03 tool factory, the audit log) detect truncation
        unambiguously. Pinned by the T04 audit-truncation discipline test.
        """
        from persona.sandbox.tool import TRUNCATION_MARKER_PREFIX

        if len(raw) <= max_bytes:
            return raw.decode("utf-8", errors="replace"), False
        omitted = len(raw) - max_bytes
        kept = raw[:max_bytes].decode("utf-8", errors="replace")
        marker = f"\n\n{TRUNCATION_MARKER_PREFIX} {omitted} bytes omitted]"
        return kept + marker, True

    @staticmethod
    def _sanitise_stderr(text: str) -> str:
        """Strip ANSI escape sequences and rewrite container-internal paths."""
        text = _ANSI_PATTERN.sub("", text)
        for src, dst in _PATH_REWRITES:
            text = text.replace(src, dst)
        return text

    @staticmethod
    def _discover_produced_files(
        host_out: Path, limits: ResourceLimits
    ) -> tuple[tuple[SandboxFile, ...], bool, tuple[RefusedFile, ...]]:
        """Snapshot-then-diff (D-12-10) — list files produced under the
        rw mount, applying the per-file size cap and the total-count cap.

        Returns ``(files, was_truncated, refused)``. ``was_truncated=True`` if
        EITHER cap fired (count > ``max_produced_files`` OR any single
        file > ``max_produced_file_mb``). ``refused`` holds files whose path is
        not safe to bring into the workspace (Spec WIN, T1.5)."""
        from persona.sandbox._produced_io import list_produced_files  # noqa: PLC0415

        per_file_cap_bytes = limits.max_produced_file_mb * 1024 * 1024
        produced: list[SandboxFile] = []
        refused: list[RefusedFile] = []
        truncated = False
        # Spec WIN T1.6 (X-1): the container controls this folder, links included, so
        # the walk never follows or reports a link (a symlink to a host secret would
        # otherwise be listed, then copied into the workspace).
        for rel, size in list_produced_files(host_out):
            violation = produced_file_path_violation(rel)
            if violation is not None:
                refused.append(refused_file(rel, violation))
                continue
            if len(produced) >= limits.max_produced_files:
                truncated = True
                break
            if size > per_file_cap_bytes:
                # Surface the file's existence (the model wrote it) but
                # mark the run as truncated so the model knows.
                truncated = True
                continue
            produced.append(
                SandboxFile(
                    path=rel,
                    size_bytes=size,
                    # Infer the media type from the extension so a produced PNG
                    # surfaces as ``image/png`` and renders inline (the frontend
                    # keys inline rendering on ``media_type.startswith("image/")``
                    # with no extension fallback). D-12-X-produced-media-type.
                    media_type=guess_media_type(rel),
                )
            )
        return tuple(produced), truncated, tuple(refused)

    @staticmethod
    def _best_effort_remove(container: Container | None) -> None:
        """Try ``container.remove(force=True)``. ``auto_remove=True`` usually
        wins; we catch the race when we killed on timeout."""
        if container is None:
            return
        with contextlib.suppress(NotFound, APIError):
            container.remove(force=True)

    # -- T05c session-mode internals --------------------------------------

    def _create_session_sync(
        self,
        *,
        session_id: str,
        limits: ResourceLimits,
        network: NetworkPolicy,
    ) -> None:
        """Spawn a long-lived session container; track it in ``self._sessions``.

        Entrypoint is ``tail -f /dev/null`` — keepalive that consumes
        essentially zero CPU. The container outlives the call; subsequent
        :meth:`execute` calls reach it via ``docker exec``."""
        client = self._resolve_client()
        host_in, host_out = self._make_session_workspace_dirs(session_id)
        kwargs = dict(_BASE_CONTAINER_KWARGS)
        kwargs["image"] = self._image
        # Keepalive — container does nothing on its own; we exec into it.
        kwargs["command"] = ["tail", "-f", "/dev/null"]
        kwargs["name"] = f"persona-sandbox-session-{session_id_to_safe_name(session_id)}"
        kwargs["volumes"] = {
            str(host_in): {"bind": _WORKSPACE_IN, "mode": "ro"},
            str(host_out): {"bind": _WORKSPACE_OUT, "mode": "rw"},
        }
        if network.enabled:
            from persona.sandbox.egress import SANDBOX_BRIDGE_NAME

            kwargs["network_mode"] = SANDBOX_BRIDGE_NAME
        else:
            kwargs["network_mode"] = "none"
        kwargs["mem_limit"] = f"{limits.memory_mb}m"
        kwargs["memswap_limit"] = f"{limits.memory_mb}m"
        kwargs["nano_cpus"] = int(limits.cpu_cores * 1_000_000_000)
        kwargs["ulimits"] = list(_ulimits())
        kwargs["log_config"] = _log_config()
        kwargs["detach"] = True
        if self._platform is not None:
            kwargs["platform"] = self._platform
        try:
            container = client.containers.run(**kwargs)
        except ImageNotFound as exc:
            msg = (
                f"sandbox image not found: {self._image!r}. "
                "Build the image (T06) or set PERSONA_SANDBOX_IMAGE."
            )
            raise SandboxUnavailableError(
                msg, context={"reason": "image_missing", "image": self._image}
            ) from exc
        except APIError as exc:
            raise CodeSandboxError(
                f"docker API error creating session: {exc}",
                context={"reason": "api_error", "session_id": session_id},
            ) from exc
        self._sessions[session_id] = container
        self._session_workspaces[session_id] = (host_in, host_out)
        _logger.debug("session created", session_id=session_id)

    def _make_session_workspace_dirs(self, session_id: str) -> tuple[Path, Path]:
        """Create per-session ``in/`` + ``out/`` host dirs that persist
        across executions in the same session (D-12-1 filesystem-level
        state continuity)."""
        safe = session_id_to_safe_name(session_id)
        exec_root = self._workspace_root / f"session-{safe}"
        host_in = exec_root / "in"
        host_out = exec_root / "out"
        host_in.mkdir(parents=True, exist_ok=True)
        host_out.mkdir(parents=True, exist_ok=True)
        host_out.chmod(0o777)  # noqa: S103 — session-scoped, ephemeral
        return host_in, host_out

    def _execute_in_session_sync(
        self,
        code: str,
        *,
        session_id: str,
        timeout_s: float,
        limits: ResourceLimits,
        input_files: list[SandboxFile],
    ) -> ExecutionResult:
        """Dispatch ``code`` into an existing session container via ``docker exec``.

        **Filesystem state persists** (same workspace mount, same container);
        **variable state does NOT** (each exec is a fresh ``python`` process).
        v0.2 IPython kernel switches this to a single persistent interpreter."""
        if session_id not in self._sessions:
            msg = f"session {session_id!r} does not exist; call create_session() first"
            raise CodeSandboxError(msg, context={"reason": "no_session", "session_id": session_id})
        container = self._sessions[session_id]
        host_in, host_out = self._session_workspaces[session_id]
        # Write the code script + any input files into the persistent host_in.
        # The container sees them at /workspace/in/__persona_main__.py.
        self._seed_workspace(host_in, code, input_files)

        started = time.perf_counter()
        try:
            exit_status, raw_stdout, raw_stderr = self._docker_exec(container, timeout_s=timeout_s)
        except SandboxUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001
            duration_ms = (time.perf_counter() - started) * 1000.0
            return ExecutionResult(
                stdout="",
                stderr=f"docker exec failed: {type(exc).__name__}: {exc}",
                exit_status=-1,
                outcome="error",
                produced_files=(),
                duration_ms=duration_ms,
            )

        duration_ms = (time.perf_counter() - started) * 1000.0
        stdout, stdout_truncated = self._truncate_bytes(raw_stdout, limits.max_stdout_bytes)
        stderr, stderr_truncated = self._truncate_bytes(raw_stderr, limits.max_stdout_bytes)
        stderr = self._sanitise_stderr(stderr)
        produced, files_truncated, refused = self._discover_produced_files(host_out, limits)

        if exit_status == 0:
            outcome: str = "ok"
        elif exit_status < 0:
            outcome = "killed"
        else:
            outcome = "error"

        return ExecutionResult(
            stdout=stdout,
            stderr=stderr,
            exit_status=exit_status,
            outcome=cast("ExecutionOutcome", outcome),
            produced_files=produced,
            duration_ms=duration_ms,
            truncated_stdout=stdout_truncated or stderr_truncated,
            truncated_files=files_truncated,
            refused_files=refused,
        )

    @staticmethod
    def _docker_exec(container: Container, *, timeout_s: float) -> tuple[int, bytes, bytes]:
        """Run ``python -u /workspace/in/__persona_main__.py`` inside the
        session container; return ``(exit_status, stdout_bytes, stderr_bytes)``.

        The Docker SDK's ``container.exec_run`` is synchronous; the timeout
        cap comes from the substrate-side ``ulimit`` + ``pids_limit`` rather
        than a wall-clock kill (which would require manual ``docker exec
        --detach-keys``-style coordination). v0.2 ``IPython`` kernel mode
        adds explicit per-call wall-clock enforcement."""
        del timeout_s  # v0.1: container-side limits enforce; per-exec timeout v0.2
        result = container.exec_run(
            ["python", "-u", _CODE_PATH_IN_CONTAINER],
            stdout=True,
            stderr=True,
            demux=True,  # split stdout/stderr in the response
        )
        exit_status = int(result.exit_code if result.exit_code is not None else -1)
        # demux=True ⇒ output is (stdout_bytes, stderr_bytes); each may be None
        if isinstance(result.output, tuple):
            stdout_bytes = result.output[0] or b""
            stderr_bytes = result.output[1] or b""
        else:
            # Some SDK versions return bytes when no demux; fall back gracefully
            stdout_bytes = result.output or b""
            stderr_bytes = b""
        return exit_status, stdout_bytes, stderr_bytes


def session_id_to_safe_name(session_id: str) -> str:
    """Sanitise ``session_id`` for use in Docker container names.

    Docker container names must match ``[a-zA-Z0-9][a-zA-Z0-9_.-]*``;
    tenant-isolated session_ids like ``user-7:conv-42`` contain ``:``
    which Docker rejects. We replace runs of invalid chars with ``_`` and
    truncate (Docker caps at 253 chars, well above any plausible session_id)."""
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", session_id)
    return safe[:200] or "session"


def _utcnow() -> datetime:  # pragma: no cover — test seam (avoids new Date in tests)
    return datetime.now(UTC)


# Re-export for the T05a tests + the conftest skip-if-no-docker check.
def is_docker_available() -> bool:
    """True if the docker SDK is installed AND the daemon is reachable.

    Used by the T04 conftest to skip the security-suite parametrisations
    when Docker isn't running OR when the ``[sandbox]`` extra isn't
    installed (CI without docker, dev machines with the daemon stopped).
    """
    if not _DOCKER_SDK_AVAILABLE:
        return False
    try:
        client = docker.from_env()
        client.ping()
        client.close()
    except DockerException:
        return False
    return True


def is_sandbox_image_available(tag: str = DEFAULT_IMAGE) -> bool:
    """True if ``tag`` is locally pullable from the Docker daemon.

    Integration fixtures that need a real sandbox container (chart-quality
    suite, stateful-iteration suite, T04 security suite) should skip when
    this returns ``False``: CI runners that have the Docker daemon but
    no pre-built ``persona-sandbox:0.1.0`` image otherwise crash at
    container-create time with ``SandboxUnavailableError`` (reason
    ``image_missing``). Returns ``False`` if the SDK is absent, the
    daemon is unreachable, or the image is not in the local registry.
    """
    if not _DOCKER_SDK_AVAILABLE:
        return False
    try:
        client = docker.from_env()
        try:
            client.images.get(tag)
        except ImageNotFound:
            return False
        finally:
            client.close()
    except DockerException:
        return False
    return True


# Avoid unused-import warning on the timestamp re-export
_ = _utcnow

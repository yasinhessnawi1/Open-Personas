"""Lazy per-server supervisor for built-in MCP servers (Spec 27 T4, D-27-3).

Built-in MCP servers (``time`` / ``calculator`` / ``filesystem`` / ``weather``)
ship as thin FastMCP Streamable-HTTP servers (D-27-2) that run as **subprocesses**
of the API process — the MCP protocol is process-separated (D-03-19), and a
subprocess gives us the same lifecycle discipline as the Spec-12 sandbox.

**Lazy** (D-27-3): the supervisor is constructed at API startup with the enabled
set, but spawns **nothing**. The first time a persona resolves an
``mcp:<server>:`` tool, :meth:`ensure` spawns that one server (one-time cold
boot, process-wide, amortized) and returns its loopback URL; a crashed server is
re-spawned on the next resolution. On a 1GB Machine this keeps the footprint
proportional to the servers actually used, not the servers enabled.

Security (D-27-12): servers bind ``127.0.0.1`` ONLY (never off-box); when an
operator sets a privilege-drop uid the spawned children ``setuid``/``setgid`` to
it (otherwise children inherit the API process user — uid 1000 in production).

Shutdown reaps every spawned subprocess (SIGTERM → bounded wait → SIGKILL),
mirroring ``LocalDockerSandbox.aclose``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from persona.logging import get_logger
from persona.tools.mcp.builtin import DEFAULT_BIND_HOST
from persona.tools.mcp.catalog import authored_server_names

__all__ = ["BuiltinMCPSupervisor"]

_logger = get_logger("api.mcp.builtin_launcher")

#: The persona allow-list prefix a built-in server tool carries: ``mcp:<name>:``.
_MCP_PREFIX = "mcp:"

#: The one built-in that holds per-(owner, persona) state and so is spawned
#: scope-keyed rather than as a process-wide singleton (Spec P4-D-5). The
#: stateless servers (``time`` / ``calculator`` / ``weather``) stay D-27-3
#: singletons.
_FILESYSTEM = "filesystem"

#: Dedicated env channel carrying the supervisor-computed, pre-resolved scoped
#: root into a filesystem child at spawn (Spec P4-D-2). The child reads ONLY this
#: var; absent ⇒ it fails closed (serve-and-deny, P4-D-4) — never the shared root.
_FILESYSTEM_SCOPE_ROOT_ENV = "PERSONA_FILESYSTEM_SCOPE_ROOT"

#: Scope-cache key for a filesystem child spawned WITHOUT a bound scope (the
#: hosted-but-unbound defense-in-depth path). One denying child is reused for it.
_NO_SCOPE_KEY = "\x00no-scope"


@dataclass
class _ServerState:
    """Mutable per-server launch state (port + live process)."""

    port: int | None = None
    proc: subprocess.Popen[bytes] | None = None

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


class BuiltinMCPSupervisor:
    """Spawns + tracks built-in MCP server subprocesses on demand.

    Args:
        enabled: The built-in server names to make available (typically
            ``PersonaCoreConfig.mcp_builtin_enabled_parsed``). Names that are not
            authored built-ins are ignored (defensive — config already validates).
        host: Bind address for every server. Loopback only (D-27-12); overriding
            this off ``127.0.0.1`` is unsupported and logged.
        python_executable: Interpreter used to spawn ``python -m
            persona.tools.mcp.builtin``. Defaults to the API's own interpreter so
            children inherit its environment + user (D-27-12).
        child_uid: Optional POSIX uid the children drop to at spawn. ``None`` →
            inherit the API process user.
        spawn_timeout_s: How long to wait for a spawned server's port to open
            before treating the spawn as failed (graceful degradation).
    """

    def __init__(
        self,
        enabled: tuple[str, ...] | list[str],
        *,
        host: str = DEFAULT_BIND_HOST,
        python_executable: str = sys.executable,
        child_uid: int | None = None,
        spawn_timeout_s: float = 20.0,
    ) -> None:
        if host != DEFAULT_BIND_HOST:
            _logger.warning(
                "built-in MCP servers must bind loopback only; ignoring host override",
                requested=host,
            )
            host = DEFAULT_BIND_HOST
        self._host = host
        self._python = python_executable
        self._child_uid = child_uid
        self._spawn_timeout_s = spawn_timeout_s
        authored = authored_server_names()
        # Preserve declared order; keep only authored (launchable) names.
        # ``filesystem`` stays in this dict as an *enabled marker* (enabled_servers
        # / needed_builtins read it) but is NEVER spawned into — its live children
        # live in ``_fs_states`` below, keyed by (owner, persona) scope (P4-D-1).
        self._states: dict[str, _ServerState] = {
            name: _ServerState() for name in enabled if name in authored
        }
        #: Per-scope filesystem children (Spec P4-D-1): one child per resolved
        #: scoped root. Reaping trigger = ``aclose()`` (supervisor shutdown) only —
        #: there is no per-turn/session reap (the cache amortizes the cold spawn),
        #: so this dict GROWS MONOTONICALLY within a process lifetime, bounded by
        #: distinct enabling (owner, persona) pairs. That grow-only shape is the
        #: cost option (a) pays; an idle-eviction cap (option c) is the additive
        #: upgrade if child-count growth is ever observed unbounded (the P4-D-1
        #: tripwire). Guarded by ``_lock`` like the singleton states.
        self._fs_states: dict[str, _ServerState] = {}
        self._lock = asyncio.Lock()

    @property
    def enabled_servers(self) -> tuple[str, ...]:
        """The built-in servers this supervisor can launch, in order."""
        return tuple(self._states)

    @property
    def running_server_count(self) -> int:
        """How many servers currently have a live subprocess (lazy-spawn proof).

        Counts the singleton servers (``_states``, where the ``filesystem`` marker
        never runs) plus every live per-scope filesystem child (``_fs_states``).
        """
        singletons = sum(1 for s in self._states.values() if s.is_running())
        filesystem = sum(1 for s in self._fs_states.values() if s.is_running())
        return singletons + filesystem

    def needed_builtins(self, declared_tools: list[str] | tuple[str, ...]) -> set[str]:
        """Built-in servers a persona's allow-list actually references.

        Maps each ``mcp:<name>:<tool>`` entry to ``<name>`` and keeps only the
        names this supervisor has enabled — so a persona that uses no built-in
        MCP spawns nothing.
        """
        needed: set[str] = set()
        for entry in declared_tools:
            if not entry.startswith(_MCP_PREFIX):
                continue
            rest = entry[len(_MCP_PREFIX) :]
            name = rest.split(":", 1)[0]
            if name in self._states:
                needed.add(name)
        return needed

    async def resolve(
        self,
        declared_tools: list[str] | tuple[str, ...],
        *,
        filesystem_scope_root: Path | None = None,
    ) -> dict[str, str]:
        """Ensure every needed built-in is up; return ``{name: url}`` for the live ones.

        Servers that fail to spawn are omitted (graceful degradation per D-03-20)
        — ``build_default_toolbox`` connects ``strict=False`` so an omitted server
        simply advertises no tools and the persona is unaffected.

        Args:
            declared_tools: The persona's ``tools`` allow-list; only ``mcp:<name>:``
                entries select a built-in.
            filesystem_scope_root: The supervisor-computed, pre-resolved scoped root
                for THIS request's (owner, persona) — ``<workspace_root>/<owner>/<persona>``
                on the hosted path, ``config.tools_sandbox_root`` on the single-tenant
                CLI path, or ``None`` when hosted-but-unbound (Spec P4-D-3). It is
                threaded into the ``filesystem`` child at spawn (P4-D-2); ``None`` ⇒
                the child is spawned without a scope and fails closed (serve-and-deny,
                P4-D-4). Ignored for the stateless built-ins.
        """
        urls: dict[str, str] = {}
        for name in self.needed_builtins(declared_tools):
            if name == _FILESYSTEM:
                url = await self._ensure_filesystem(filesystem_scope_root)
            else:
                url = await self.ensure(name)
            if url is not None:
                urls[name] = url
        return urls

    async def ensure(self, name: str) -> str | None:
        """Ensure built-in server ``name`` is running; return its URL or ``None``.

        Idempotent + amortized: a server already up returns its URL immediately;
        a dead one is re-spawned (D-27-3 restart-on-resolution). Returns ``None``
        when ``name`` is not enabled or the spawn/health-probe failed.
        """
        if name == _FILESYSTEM:
            # filesystem is scope-keyed (P4-D-1); a bare ensure has no scope ⇒
            # route to the fail-closed (serve-and-deny) child, never an unscoped
            # singleton in ``_states``.
            return await self._ensure_filesystem(None)
        state = self._states.get(name)
        if state is None:
            return None
        async with self._lock:
            if state.is_running() and state.port is not None:
                return self._url(state.port)
            # Reap a dead process before re-spawning.
            if state.proc is not None and not state.is_running():
                _logger.warning("built-in MCP server died; re-spawning", server=name)
                await self._terminate(state)
            return await self._spawn(name, state)

    async def _ensure_filesystem(self, scope_root: Path | None) -> str | None:
        """Ensure a filesystem child scoped to ``scope_root`` is running; return its URL.

        One child per (owner, persona) scope (Spec P4-D-1): keyed by the resolved
        scoped root so concurrent owners never share a child. ``scope_root`` is
        threaded in over :data:`_FILESYSTEM_SCOPE_ROOT_ENV`; ``None`` (hosted-but-
        unbound) spawns a child with the scope var SCRUBBED so it fails closed
        (serve-and-deny, P4-D-4) — never inheriting a stray value or the shared
        root. Idempotent + restart-on-death, mirroring :meth:`ensure`.
        """
        if _FILESYSTEM not in self._states:
            return None  # filesystem not enabled
        key = str(scope_root) if scope_root is not None else _NO_SCOPE_KEY
        async with self._lock:
            state = self._fs_states.get(key)
            if state is not None and state.is_running() and state.port is not None:
                return self._url(state.port)
            if state is not None and state.proc is not None and not state.is_running():
                _logger.warning("filesystem MCP child died; re-spawning", scope=key)
                await self._terminate(state)
            if state is None:
                state = _ServerState()
                self._fs_states[key] = state
            # Thread the pre-resolved scope in; scrub it when unscoped so the child
            # cannot inherit a stray value from the parent and must fail closed.
            env = dict(os.environ)
            if scope_root is not None:
                env[_FILESYSTEM_SCOPE_ROOT_ENV] = str(scope_root)
            else:
                env.pop(_FILESYSTEM_SCOPE_ROOT_ENV, None)
            return await self._spawn(_FILESYSTEM, state, env=env)

    async def _spawn(
        self, name: str, state: _ServerState, *, env: dict[str, str] | None = None
    ) -> str | None:
        port = _free_port()
        argv = [
            self._python,
            "-m",
            "persona.tools.mcp.builtin",
            name,
            "--host",
            self._host,
            "--port",
            str(port),
        ]
        try:
            proc = subprocess.Popen(  # noqa: S603 — fixed argv, trusted interpreter
                argv,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                preexec_fn=self._preexec(),  # noqa: PLW1509 — intentional privilege drop (POSIX)
                env=env,  # None ⇒ inherit parent env (singletons); dict ⇒ scoped child
            )
        except OSError as exc:
            _logger.warning("built-in MCP server spawn failed", server=name, error=str(exc))
            return None
        try:
            await asyncio.to_thread(_wait_for_port, self._host, port, self._spawn_timeout_s)
        except TimeoutError:
            _logger.warning("built-in MCP server never opened its port", server=name, port=port)
            proc.terminate()
            with contextlib.suppress(Exception):
                proc.wait(timeout=5)
            return None
        state.port = port
        state.proc = proc
        _logger.info("built-in MCP server spawned", server=name, port=port, pid=proc.pid)
        return self._url(port)

    def _preexec(self) -> Callable[[], None] | None:
        """Build the POSIX ``preexec_fn`` that drops privileges, or ``None``.

        Returns a callable only when a ``child_uid`` is configured AND the
        platform supports ``os.setuid`` (POSIX). The child drops its group then
        its user before exec — so even a server-side bug cannot act as the API
        user.
        """
        uid = self._child_uid
        if uid is None or not hasattr(os, "setuid"):
            return None

        def _drop_privileges() -> None:  # pragma: no cover — runs in the child process
            os.setgid(uid)
            os.setuid(uid)

        return _drop_privileges

    def _url(self, port: int) -> str:
        return f"http://{self._host}:{port}/mcp"

    async def aclose(self) -> None:
        """Reap every spawned built-in server subprocess (SIGTERM → wait → SIGKILL).

        This is the ONLY reaping trigger for the per-scope filesystem children
        (Spec P4-D-1) — there is no per-turn/session reap — so shutdown is where the
        monotonically-grown ``_fs_states`` cache is released.
        """
        for name, state in self._states.items():
            if state.proc is not None:
                _logger.info("stopping built-in MCP server", server=name)
                await self._terminate(state)
        for scope_key, state in self._fs_states.items():
            if state.proc is not None:
                _logger.info("stopping filesystem MCP child", scope=scope_key)
                await self._terminate(state)

    @staticmethod
    async def _terminate(state: _ServerState) -> None:
        proc = state.proc
        state.proc = None
        state.port = None
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            await asyncio.to_thread(proc.wait, 10)
        except subprocess.TimeoutExpired:  # pragma: no cover — defensive
            proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.to_thread(proc.wait, 5)


def _free_port() -> int:
    """Grab an ephemeral loopback port (closed before the server re-binds it)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((DEFAULT_BIND_HOST, 0))
        return int(s.getsockname()[1])


def _wait_for_port(host: str, port: int, timeout_s: float) -> None:
    """Block until ``host:port`` accepts a connection or the timeout elapses."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            if s.connect_ex((host, port)) == 0:
                return
        time.sleep(0.2)
    raise TimeoutError(f"server never opened {host}:{port}")

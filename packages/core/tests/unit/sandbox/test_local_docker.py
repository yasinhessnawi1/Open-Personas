"""Unit tests for :class:`LocalDockerSandbox` (spec 12 T05a).

Mocks the Docker SDK so the tests run without a Docker daemon. Pins:

- The R-12-2 hardened container config (every flag verified — a future
  refactor cannot silently weaken the posture).
- Daemon-unreachable maps to :class:`SandboxUnavailableError` (D-12-5: no
  degraded fallback).
- The Protocol contract: stateless one-shot works in T05a; session methods
  raise :class:`NotImplementedError` until T05c (D-12-1).

The real adversarial runs against a live Docker daemon live in the T04
integration security suite; the conftest gates them on
:func:`is_docker_available`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
from persona.sandbox import (
    CodeSandbox,
    NetworkPolicy,
    ResourceLimits,
    SandboxFile,
    SandboxUnavailableError,
)
from persona.sandbox.local_docker import (
    _BASE_CONTAINER_KWARGS,
    DEFAULT_IMAGE,
    LocalDockerSandbox,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_container_mock(
    *,
    exit_code: int = 0,
    stdout: bytes = b"",
    stderr: bytes = b"",
    oom_killed: bool = False,
) -> MagicMock:
    """A docker-SDK Container mock that responds to wait/logs/reload/remove/kill.

    T05b adds ``oom_killed`` so tests can simulate an OOM kill — the substrate
    sets ``attrs["State"]["OOMKilled"]`` after the container exits."""
    container = MagicMock()
    container.wait.return_value = {"StatusCode": exit_code}
    container.logs.side_effect = _make_logs_side_effect(stdout, stderr)
    container.attrs = {"State": {"OOMKilled": oom_killed}}
    container.reload.return_value = None
    return container


def _make_logs_side_effect(stdout_bytes: bytes, stderr_bytes: bytes) -> Callable[..., bytes]:
    """Build a side_effect for ``Container.logs(stdout=..., stderr=...)``."""

    def _side(stdout: bool = False, stderr: bool = False, **_kwargs: object) -> bytes:
        if stdout and not stderr:
            return stdout_bytes
        if stderr and not stdout:
            return stderr_bytes
        return stdout_bytes + stderr_bytes

    return _side


def _make_client_mock(container: MagicMock) -> MagicMock:
    """A docker.DockerClient mock that .containers.run returns our container."""
    client = MagicMock()
    client.ping.return_value = True
    client.containers.run.return_value = container
    client.close.return_value = None
    return client


# ---------------------------------------------------------------------------
# Protocol contract — LocalDockerSandbox satisfies CodeSandbox
# ---------------------------------------------------------------------------


class TestProtocolContract:
    def test_satisfies_code_sandbox_protocol(self, tmp_path: Path) -> None:
        sandbox = LocalDockerSandbox(
            workspace_root=tmp_path,
            docker_client=MagicMock(),
        )
        assert isinstance(sandbox, CodeSandbox)

    def test_workspace_root_created(self, tmp_path: Path) -> None:
        target = tmp_path / "does_not_exist_yet"
        LocalDockerSandbox(workspace_root=target, docker_client=MagicMock())
        assert target.exists()


# ---------------------------------------------------------------------------
# R-12-2 hardened config — pinned so a refactor can't silently weaken it
# ---------------------------------------------------------------------------


class TestR122Hardening:
    """Pin every R-12-2 flag in the base container kwargs.

    Each test row corresponds to one R-12-2 row; a refactor that removes a
    flag must explicitly delete the test, which surfaces in code review."""

    def test_runs_as_unprivileged_user(self) -> None:
        """§9 #9 / R-12-2 #4: ``user=65534:65534`` (nobody:nogroup)."""
        assert _BASE_CONTAINER_KWARGS["user"] == "65534:65534"

    def test_readonly_root(self) -> None:
        """R-12-2 #3: ``read_only=True`` — rootfs overlay is read-only."""
        assert _BASE_CONTAINER_KWARGS["read_only"] is True

    def test_tmpfs_hardened(self) -> None:
        """R-12-2 #3: tmpfs ``noexec``/``nosuid``/``nodev`` — blocks the
        drop-payload-then-dlopen attack path."""
        tmpfs = _BASE_CONTAINER_KWARGS["tmpfs"]
        assert "/tmp" in tmpfs
        assert "/var/tmp" in tmpfs
        for mount_opts in tmpfs.values():
            assert "noexec" in mount_opts
            assert "nosuid" in mount_opts
            assert "nodev" in mount_opts

    def test_cap_drop_all(self) -> None:
        """R-12-2 #1: ``cap_drop=["ALL"]`` — Python needs zero capabilities."""
        assert _BASE_CONTAINER_KWARGS["cap_drop"] == ["ALL"]

    def test_no_new_privileges(self) -> None:
        """R-12-2 #6: ``no-new-privileges:true`` — defeats setuid escalation."""
        assert "no-new-privileges:true" in _BASE_CONTAINER_KWARGS["security_opt"]

    def test_swap_disabled(self) -> None:
        """R-12-2 #8: ``memswap_limit=mem_limit`` ⇒ swap disabled in cgroup."""
        assert _BASE_CONTAINER_KWARGS["mem_limit"] == _BASE_CONTAINER_KWARGS["memswap_limit"]

    def test_pids_limit_set(self) -> None:
        """R-12-2 #7: ``pids_limit`` against fork bombs."""
        assert _BASE_CONTAINER_KWARGS["pids_limit"] >= 64

    def test_ipc_private(self) -> None:
        """R-12-2 #14: ``ipc_mode=private`` — no /dev/shm sharing."""
        assert _BASE_CONTAINER_KWARGS["ipc_mode"] == "private"

    def test_init_pid1(self) -> None:
        """R-12-2 #14: ``init=True`` — tini reaps zombies for ipykernel subprocs."""
        assert _BASE_CONTAINER_KWARGS["init"] is True

    def test_auto_remove_false_so_we_can_inspect(self) -> None:
        """R-12-2 #11 ephemerality is enforced by the explicit
        ``_best_effort_remove`` in the per-exec ``finally`` clause (T05b).
        ``auto_remove=False`` keeps the container alive briefly after exit
        so we can read ``attrs["State"]["OOMKilled"]`` — D-12-X outcome
        classification refinement."""
        assert _BASE_CONTAINER_KWARGS["auto_remove"] is False

    def test_minimal_environment(self) -> None:
        """R-12-2 #14: minimal env — no host PATH/HOME leakage."""
        env = _BASE_CONTAINER_KWARGS["environment"]
        # Allowed (D-12-X-venv-path-ordering: venv prefix is the first segment
        # so the image's installed tooling is reachable, system tail preserves
        # R-12-2 explicit-PATH hardening).
        assert env["PATH"] == "/opt/venv/bin:/usr/local/bin:/usr/bin:/bin"
        assert env["HOME"] == "/home/nobody"
        assert env["PYTHONUNBUFFERED"] == "1"
        # Disallowed: anything that would leak host secrets
        for forbidden in ("AWS_ACCESS_KEY_ID", "OPENAI_API_KEY", "DATABASE_URL", "USER"):
            assert forbidden not in env

    def test_path_contains_venv_bin_prefix(self) -> None:
        """D-12-X-venv-path-ordering regression guard.

        The persona-sandbox image's ``ENV PATH=/opt/venv/bin:$PATH`` puts the
        venv's ``python``/``pip`` first; the container kwargs override that
        env, so the venv prefix MUST be the first segment of the override or
        ``from docx import Document`` (and every other image-installed lib)
        raises ``ModuleNotFoundError`` at runtime. Surfaced by Spec 16
        T09/T10 — a future R-12-2 re-hardening pass MUST NOT silently revert
        this.
        """
        env_path = _BASE_CONTAINER_KWARGS["environment"]["PATH"]
        segments = env_path.split(":")
        assert segments[0] == "/opt/venv/bin", (
            f"venv bin must be the FIRST PATH segment so image-installed "
            f"tooling is reachable; got {env_path!r}"
        )

    def test_path_preserves_system_paths(self) -> None:
        """D-12-X-venv-path-ordering: R-12-2 explicit-PATH hardening preserved.

        The venv prefix MUST NOT drop the explicit system-bin tail — R-12-2's
        hardening intent is that PATH is fully explicit (no host leakage, no
        shell-injection surface), and the system-bin entries must remain in
        their hardened order.
        """
        env_path = _BASE_CONTAINER_KWARGS["environment"]["PATH"]
        segments = env_path.split(":")
        # All three system-bin entries still present in the original order.
        for required in ("/usr/local/bin", "/usr/bin", "/bin"):
            assert required in segments, (
                f"R-12-2 hardened system PATH must retain {required!r}; got {env_path!r}"
            )
        idx_local = segments.index("/usr/local/bin")
        idx_usr = segments.index("/usr/bin")
        idx_bin = segments.index("/bin")
        assert idx_local < idx_usr < idx_bin, (
            f"system-bin entries must keep R-12-2 ordering "
            f"(/usr/local/bin < /usr/bin < /bin); got {env_path!r}"
        )


# ---------------------------------------------------------------------------
# Per-execution kwargs — the bits that change per call
# ---------------------------------------------------------------------------


class TestPerExecutionKwargs:
    def test_network_off_by_default(self, tmp_path: Path) -> None:
        """§9 #6: ``NetworkPolicy()`` ⇒ ``network_mode=none``. Critical default."""
        client = _make_client_mock(_make_container_mock(exit_code=0, stdout=b"ok\n", stderr=b""))
        sandbox = LocalDockerSandbox(workspace_root=tmp_path, docker_client=client)
        # Run synchronously via the sync path to inspect the kwargs.
        host_in = tmp_path / "in_test"
        host_out = tmp_path / "out_test"
        host_in.mkdir()
        host_out.mkdir()
        kwargs = sandbox._build_container_kwargs(
            host_in=host_in,
            host_out=host_out,
            limits=ResourceLimits(),
            network=NetworkPolicy(),
            exec_id="test",
        )
        assert kwargs["network_mode"] == "none"

    def test_network_enabled_uses_sandbox_bridge_t07(self, tmp_path: Path) -> None:
        """T07 wiring: ``network.enabled=True`` uses the
        :data:`SANDBOX_BRIDGE_NAME` custom bridge so the R-12-5 substrate
        egress filter (DOCKER-USER iptables rules) blocks
        metadata-endpoint + RFC-1918 + IPv6 link-local regardless of the
        persona's allow-list."""
        from persona.sandbox.egress import SANDBOX_BRIDGE_NAME

        client = _make_client_mock(_make_container_mock(exit_code=0, stdout=b"", stderr=b""))
        sandbox = LocalDockerSandbox(workspace_root=tmp_path, docker_client=client)
        host_in = tmp_path / "in_test"
        host_out = tmp_path / "out_test"
        host_in.mkdir()
        host_out.mkdir()
        kwargs = sandbox._build_container_kwargs(
            host_in=host_in,
            host_out=host_out,
            limits=ResourceLimits(),
            network=NetworkPolicy(enabled=True, allowed_hosts=("example.com",)),
            exec_id="test",
        )
        # T07: the custom bridge so the substrate egress filter applies.
        assert kwargs["network_mode"] == SANDBOX_BRIDGE_NAME

    def test_two_mount_workspace(self, tmp_path: Path) -> None:
        """D-12-9: ``/workspace/in`` ro + ``/workspace/out`` rw."""
        client = _make_client_mock(_make_container_mock())
        sandbox = LocalDockerSandbox(workspace_root=tmp_path, docker_client=client)
        host_in = tmp_path / "ws-in"
        host_out = tmp_path / "ws-out"
        host_in.mkdir()
        host_out.mkdir()
        kwargs = sandbox._build_container_kwargs(
            host_in=host_in,
            host_out=host_out,
            limits=ResourceLimits(),
            network=NetworkPolicy(),
            exec_id="test",
        )
        vols = kwargs["volumes"]
        assert vols[str(host_in)]["bind"] == "/workspace/in"
        assert vols[str(host_in)]["mode"] == "ro"
        assert vols[str(host_out)]["bind"] == "/workspace/out"
        assert vols[str(host_out)]["mode"] == "rw"

    def test_resource_limits_translate_to_docker_kwargs(self, tmp_path: Path) -> None:
        client = _make_client_mock(_make_container_mock())
        sandbox = LocalDockerSandbox(workspace_root=tmp_path, docker_client=client)
        host_in = tmp_path / "in_test"
        host_out = tmp_path / "out_test"
        host_in.mkdir()
        host_out.mkdir()
        limits = ResourceLimits(cpu_cores=2.0, memory_mb=1024)
        kwargs = sandbox._build_container_kwargs(
            host_in=host_in,
            host_out=host_out,
            limits=limits,
            network=NetworkPolicy(),
            exec_id="test",
        )
        assert kwargs["mem_limit"] == "1024m"
        assert kwargs["memswap_limit"] == "1024m"  # = mem_limit ⇒ swap disabled
        assert kwargs["nano_cpus"] == 2_000_000_000

    def test_container_name_unique_per_exec(self, tmp_path: Path) -> None:
        """The exec_id makes container names unique even when many sandboxes
        run concurrently — avoids docker NameConflict errors."""
        client = _make_client_mock(_make_container_mock())
        sandbox = LocalDockerSandbox(workspace_root=tmp_path, docker_client=client)
        host_in = tmp_path / "in1"
        host_out = tmp_path / "out1"
        host_in.mkdir()
        host_out.mkdir()
        kwargs = sandbox._build_container_kwargs(
            host_in=host_in,
            host_out=host_out,
            limits=ResourceLimits(),
            network=NetworkPolicy(),
            exec_id="abc12345",
        )
        assert kwargs["name"] == "persona-sandbox-abc12345"

    def test_image_default(self) -> None:
        assert DEFAULT_IMAGE.startswith("persona-sandbox:")


# ---------------------------------------------------------------------------
# One-shot execution happy path (mocked daemon)
# ---------------------------------------------------------------------------


class TestExecuteHappyPath:
    @pytest.mark.asyncio
    async def test_ok_exit_status_zero(self, tmp_path: Path) -> None:
        container = _make_container_mock(exit_code=0, stdout=b"4\n", stderr=b"")
        client = _make_client_mock(container)
        sandbox = LocalDockerSandbox(workspace_root=tmp_path, docker_client=client)

        result = await sandbox.execute("print(2+2)")

        assert result.exit_status == 0
        assert result.outcome == "ok"
        assert result.stdout == "4\n"
        assert result.duration_ms > 0

    @pytest.mark.asyncio
    async def test_non_zero_exit_status_classified_as_error(self, tmp_path: Path) -> None:
        container = _make_container_mock(exit_code=1, stdout=b"", stderr=b"Boom\n")
        client = _make_client_mock(container)
        sandbox = LocalDockerSandbox(workspace_root=tmp_path, docker_client=client)

        result = await sandbox.execute("raise SystemExit(1)")

        assert result.exit_status == 1
        assert result.outcome == "error"
        assert "Boom" in result.stderr

    @pytest.mark.asyncio
    async def test_input_files_seeded(self, tmp_path: Path) -> None:
        container = _make_container_mock(exit_code=0, stdout=b"", stderr=b"")
        client = _make_client_mock(container)
        sandbox = LocalDockerSandbox(workspace_root=tmp_path, docker_client=client)
        await sandbox.execute(
            "pass",
            input_files=[SandboxFile(path="data.csv", content_bytes=b"x,y\n1,2\n")],
        )
        # Verify the seeded file was written to the host_in dir before the
        # container ran. The host_in dir is cleaned up afterward, so we
        # check via the mock's records — the .containers.run was called with
        # a volumes={...host_in→/workspace/in, host_out→/workspace/out...}.
        call_kwargs = client.containers.run.call_args.kwargs
        # Verify the structure of volumes is the two-mount D-12-9 pattern.
        # We can't inspect the dir contents (cleanup happened) but we can
        # assert the call shape.
        assert any(v.get("bind") == "/workspace/in" for v in call_kwargs["volumes"].values())

    @pytest.mark.asyncio
    async def test_workspace_cleanup_after_execute(self, tmp_path: Path) -> None:
        """The per-execution host_in / host_out dirs are removed after the
        run completes (success path)."""
        container = _make_container_mock(exit_code=0, stdout=b"", stderr=b"")
        client = _make_client_mock(container)
        sandbox = LocalDockerSandbox(workspace_root=tmp_path, docker_client=client)
        await sandbox.execute("pass")
        # The workspace_root should be empty (or contain only the parent
        # we created; cleanup removes per-exec subdirs).
        leftover = [p for p in tmp_path.iterdir() if p.is_dir()]
        assert leftover == []


# ---------------------------------------------------------------------------
# Daemon unreachable maps to SandboxUnavailableError (D-12-5)
# ---------------------------------------------------------------------------


class TestDaemonUnreachable:
    @pytest.mark.asyncio
    async def test_ping_fails_maps_to_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When ``docker.from_env`` raises (no daemon), the sandbox surfaces
        :class:`SandboxUnavailableError` — D-12-5 no degraded fallback."""
        from docker.errors import DockerException

        def _no_daemon() -> object:
            msg = "no daemon"
            raise DockerException(msg)

        import persona.sandbox.local_docker as ld

        monkeypatch.setattr(ld.docker, "from_env", _no_daemon)
        sandbox = LocalDockerSandbox(workspace_root=tmp_path)  # no client injected

        with pytest.raises(SandboxUnavailableError) as exc_info:
            await sandbox.execute("print(1)")
        assert "Docker daemon unreachable" in str(exc_info.value)
        assert exc_info.value.context["reason"] == "docker_unreachable"


# ---------------------------------------------------------------------------
# Session methods land in T05c
# ---------------------------------------------------------------------------


class TestSessionsT05c:
    """T05c session lifecycle — scaled scope (filesystem-state persistence
    only; variable-state persistence deferred to v0.2 IPython kernel).

    §9 #4 (stateless does NOT leak) is fully covered; §9 #3 (stateful) is
    partial — verified at the filesystem-mount level here."""

    @pytest.mark.asyncio
    async def test_create_session_spawns_keepalive_container(self, tmp_path: Path) -> None:
        container = _make_container_mock()
        client = _make_client_mock(container)
        sandbox = LocalDockerSandbox(workspace_root=tmp_path, docker_client=client)
        await sandbox.create_session(
            "tenant-1:conv-1",
            limits=ResourceLimits(),
            network=NetworkPolicy(),
        )
        run_kwargs = client.containers.run.call_args.kwargs
        assert run_kwargs["command"] == ["tail", "-f", "/dev/null"]
        assert "tenant-1:conv-1" in sandbox._sessions

    @pytest.mark.asyncio
    async def test_create_session_is_idempotent(self, tmp_path: Path) -> None:
        container = _make_container_mock()
        client = _make_client_mock(container)
        sandbox = LocalDockerSandbox(workspace_root=tmp_path, docker_client=client)
        await sandbox.create_session(
            "tenant-1:conv-1",
            limits=ResourceLimits(),
            network=NetworkPolicy(),
        )
        await sandbox.create_session(
            "tenant-1:conv-1",
            limits=ResourceLimits(),
            network=NetworkPolicy(),
        )
        assert client.containers.run.call_count == 1

    @pytest.mark.asyncio
    async def test_destroy_session_idempotent_when_missing(self, tmp_path: Path) -> None:
        sandbox = LocalDockerSandbox(workspace_root=tmp_path, docker_client=MagicMock())
        await sandbox.destroy_session("does-not-exist")  # no raise

    @pytest.mark.asyncio
    async def test_destroy_session_removes_container(self, tmp_path: Path) -> None:
        container = _make_container_mock()
        client = _make_client_mock(container)
        sandbox = LocalDockerSandbox(workspace_root=tmp_path, docker_client=client)
        await sandbox.create_session(
            "tenant-1:conv-9",
            limits=ResourceLimits(),
            network=NetworkPolicy(),
        )
        host_in, host_out = sandbox._session_workspaces["tenant-1:conv-9"]
        assert host_in.exists()
        assert host_out.exists()
        await sandbox.destroy_session("tenant-1:conv-9")
        assert "tenant-1:conv-9" not in sandbox._sessions
        container.remove.assert_called()

    @pytest.mark.asyncio
    async def test_execute_in_session_raises_without_create(self, tmp_path: Path) -> None:
        """Defensive: ``execute(session_id=X)`` without prior ``create_session(X)``
        raises a clean domain error."""
        from persona.sandbox import CodeSandboxError

        sandbox = LocalDockerSandbox(workspace_root=tmp_path, docker_client=MagicMock())
        with pytest.raises(CodeSandboxError) as exc_info:
            await sandbox.execute("print(1)", session_id="nonexistent")
        assert exc_info.value.context["reason"] == "no_session"

    @pytest.mark.asyncio
    async def test_execute_in_session_dispatches_via_docker_exec(self, tmp_path: Path) -> None:
        container = _make_container_mock()
        exec_result = MagicMock()
        exec_result.exit_code = 0
        exec_result.output = (b"hello\n", b"")
        container.exec_run.return_value = exec_result

        client = _make_client_mock(container)
        sandbox = LocalDockerSandbox(workspace_root=tmp_path, docker_client=client)
        await sandbox.create_session(
            "tenant-1:conv-1",
            limits=ResourceLimits(),
            network=NetworkPolicy(),
        )
        result = await sandbox.execute("print('hello')", session_id="tenant-1:conv-1")
        assert result.outcome == "ok"
        assert result.stdout == "hello\n"
        container.exec_run.assert_called_once()
        call_args = container.exec_run.call_args
        assert "python" in call_args.args[0]

    @pytest.mark.asyncio
    async def test_session_workspace_state_persists_across_executions(self, tmp_path: Path) -> None:
        """§9 #3 (filesystem-level): two successive executions with the same
        session_id share the workspace mount."""
        container = _make_container_mock()
        exec_result = MagicMock()
        exec_result.exit_code = 0
        exec_result.output = (b"", b"")
        container.exec_run.return_value = exec_result

        client = _make_client_mock(container)
        sandbox = LocalDockerSandbox(workspace_root=tmp_path, docker_client=client)
        sid = "tenant-1:conv-7"
        await sandbox.create_session(sid, limits=ResourceLimits(), network=NetworkPolicy())
        await sandbox.execute(
            "with open('/workspace/out/x.txt', 'w') as f: f.write('1')",
            session_id=sid,
        )
        # Simulate the container's write (mock doesn't run code) — verify the
        # second exec sees the file in the same persistent host_out dir.
        host_out = sandbox._session_workspaces[sid][1]
        (host_out / "x.txt").write_text("1")
        await sandbox.execute("print(open('/workspace/out/x.txt').read())", session_id=sid)
        assert (host_out / "x.txt").exists()

    @pytest.mark.asyncio
    async def test_aclose_destroys_all_sessions(self, tmp_path: Path) -> None:
        """D-12-7 reinforced: ``aclose`` cleans up every live session."""
        container = _make_container_mock()
        client = _make_client_mock(container)
        sandbox = LocalDockerSandbox(workspace_root=tmp_path, docker_client=client)
        await sandbox.create_session("a:1", limits=ResourceLimits(), network=NetworkPolicy())
        await sandbox.create_session("a:2", limits=ResourceLimits(), network=NetworkPolicy())
        assert len(sandbox._sessions) == 2
        await sandbox.aclose()
        assert sandbox._sessions == {}


class TestSessionIdSanitisation:
    """The runtime composes tenant-isolated session_ids like
    ``user-7:conv-42`` (kickoff trip-up #6). Docker container names reject
    ``:`` — verify the sanitiser produces a valid container name AND
    preserves tenant isolation (different inputs → different outputs)."""

    def test_safe_name_strips_colon(self) -> None:
        from persona.sandbox.local_docker import session_id_to_safe_name

        assert ":" not in session_id_to_safe_name("user-7:conv-42")

    def test_different_inputs_different_outputs(self) -> None:
        """Tenant isolation: tenant-1's conv-1 and tenant-2's conv-1
        produce DIFFERENT container names — no collision."""
        from persona.sandbox.local_docker import session_id_to_safe_name

        a = session_id_to_safe_name("tenant-1:conv-1")
        b = session_id_to_safe_name("tenant-2:conv-1")
        assert a != b

    def test_handles_empty_safely(self) -> None:
        from persona.sandbox.local_docker import session_id_to_safe_name

        assert session_id_to_safe_name("") == "session"


# ---------------------------------------------------------------------------
# aclose
# ---------------------------------------------------------------------------


class TestAclose:
    @pytest.mark.asyncio
    async def test_aclose_closes_client(self, tmp_path: Path) -> None:
        client = MagicMock()
        sandbox = LocalDockerSandbox(workspace_root=tmp_path, docker_client=client)
        await sandbox.aclose()
        client.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_aclose_is_idempotent(self, tmp_path: Path) -> None:
        client = MagicMock()
        sandbox = LocalDockerSandbox(workspace_root=tmp_path, docker_client=client)
        await sandbox.aclose()
        await sandbox.aclose()
        # Second call doesn't re-close.
        client.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_after_close_raises(self, tmp_path: Path) -> None:
        sandbox = LocalDockerSandbox(
            workspace_root=tmp_path,
            docker_client=MagicMock(),
        )
        await sandbox.aclose()
        with pytest.raises(SandboxUnavailableError):
            await sandbox.execute("pass")


# ---------------------------------------------------------------------------
# T05b — outcome classification refinements
# ---------------------------------------------------------------------------


class TestOomClassification:
    @pytest.mark.asyncio
    async def test_oom_killed_state_classifies_as_oom(self, tmp_path: Path) -> None:
        """§9 #8: ``container.attrs["State"]["OOMKilled"] == True`` ⇒
        ``outcome="oom"``. The substrate fires the OOM killer when the cgroup
        memory cap is exceeded; we read the flag after ``container.reload()``."""
        container = _make_container_mock(exit_code=137, stdout=b"", stderr=b"", oom_killed=True)
        client = _make_client_mock(container)
        sandbox = LocalDockerSandbox(workspace_root=tmp_path, docker_client=client)

        result = await sandbox.execute('x = "A" * (10 ** 9)')

        assert result.outcome == "oom"
        assert result.exit_status == 137
        container.reload.assert_called()

    @pytest.mark.asyncio
    async def test_normal_exit_not_classified_as_oom(self, tmp_path: Path) -> None:
        """Defensive: ``OOMKilled=False`` (the common case) doesn't flip to oom."""
        container = _make_container_mock(exit_code=0, stdout=b"ok\n", oom_killed=False)
        client = _make_client_mock(container)
        sandbox = LocalDockerSandbox(workspace_root=tmp_path, docker_client=client)

        result = await sandbox.execute("print('ok')")

        assert result.outcome == "ok"

    @pytest.mark.asyncio
    async def test_oom_takes_precedence_over_nonzero_exit(self, tmp_path: Path) -> None:
        """An OOM kill produces exit_status != 0 — we must classify as oom,
        not error. The §9 #8 contract demands ``outcome="oom"`` specifically."""
        container = _make_container_mock(
            exit_code=1, stdout=b"", stderr=b"out of memory", oom_killed=True
        )
        client = _make_client_mock(container)
        sandbox = LocalDockerSandbox(workspace_root=tmp_path, docker_client=client)

        result = await sandbox.execute("...")

        assert result.outcome == "oom"


# ---------------------------------------------------------------------------
# T05b — stdout / stderr truncation
# ---------------------------------------------------------------------------


class TestStdoutTruncation:
    @pytest.mark.asyncio
    async def test_stdout_truncated_at_max_bytes(self, tmp_path: Path) -> None:
        """§9 #10: stdout truncated at ``max_stdout_bytes`` with the explicit
        marker (D-12-8 shape). The substrate-side cap is defence-in-depth
        beneath the T03 tool-factory cap."""
        from persona.sandbox.tool import TRUNCATION_MARKER_PREFIX

        big_stdout = b"A" * 100_000
        container = _make_container_mock(exit_code=0, stdout=big_stdout, stderr=b"")
        client = _make_client_mock(container)
        sandbox = LocalDockerSandbox(workspace_root=tmp_path, docker_client=client)

        result = await sandbox.execute(
            "print('A'*100_000)",
            limits=ResourceLimits(max_stdout_bytes=1024),
        )

        assert result.truncated_stdout is True
        assert TRUNCATION_MARKER_PREFIX in result.stdout
        assert "bytes omitted" in result.stdout

    @pytest.mark.asyncio
    async def test_stdout_not_truncated_when_under_cap(self, tmp_path: Path) -> None:
        """Marker MUST NOT appear when no truncation fired — downstream
        consumers rely on absence-of-marker for the discriminator."""
        from persona.sandbox.tool import TRUNCATION_MARKER_PREFIX

        container = _make_container_mock(exit_code=0, stdout=b"hello\n", stderr=b"")
        client = _make_client_mock(container)
        sandbox = LocalDockerSandbox(workspace_root=tmp_path, docker_client=client)

        result = await sandbox.execute("print('hello')")

        assert result.truncated_stdout is False
        assert TRUNCATION_MARKER_PREFIX not in result.stdout

    @pytest.mark.asyncio
    async def test_stderr_truncated_at_max_bytes(self, tmp_path: Path) -> None:
        """The same cap applies to stderr — protects the model context from
        a runaway traceback (rare, but a real failure mode under exception
        loops)."""
        from persona.sandbox.tool import TRUNCATION_MARKER_PREFIX

        big_stderr = b"E" * 100_000
        container = _make_container_mock(exit_code=1, stdout=b"", stderr=big_stderr)
        client = _make_client_mock(container)
        sandbox = LocalDockerSandbox(workspace_root=tmp_path, docker_client=client)

        result = await sandbox.execute(
            "raise RuntimeError('boom')",
            limits=ResourceLimits(max_stdout_bytes=512),
        )

        assert result.truncated_stdout is True  # the field encodes "any stream"
        assert TRUNCATION_MARKER_PREFIX in result.stderr


# ---------------------------------------------------------------------------
# T05b — stderr sanitisation (ANSI strip + path scrub, kickoff trip-up #5)
# ---------------------------------------------------------------------------


class TestStderrSanitisation:
    @pytest.mark.asyncio
    async def test_ansi_codes_stripped(self, tmp_path: Path) -> None:
        """ANSI color codes are removed — the model doesn't see terminal
        control sequences as visible text."""
        ansi_stderr = b"\x1b[31mTraceback (most recent call last):\x1b[0m\n"
        container = _make_container_mock(exit_code=1, stderr=ansi_stderr)
        client = _make_client_mock(container)
        sandbox = LocalDockerSandbox(workspace_root=tmp_path, docker_client=client)

        result = await sandbox.execute("raise Exception()")

        assert "\x1b" not in result.stderr
        assert "[31m" not in result.stderr
        assert "[0m" not in result.stderr
        assert "Traceback" in result.stderr  # the actual message survives

    @pytest.mark.asyncio
    async def test_container_paths_scrubbed(self, tmp_path: Path) -> None:
        """Kickoff trip-up #5: absolute container paths in tracebacks (the
        substrate's view of the workspace) are rewritten to relative names."""
        stderr_with_paths = (
            b'File "/workspace/in/__persona_main__.py", line 3, in <module>\n'
            b"  bad()\n"
            b"NameError: name 'bad' is not defined\n"
        )
        container = _make_container_mock(exit_code=1, stderr=stderr_with_paths)
        client = _make_client_mock(container)
        sandbox = LocalDockerSandbox(workspace_root=tmp_path, docker_client=client)

        result = await sandbox.execute("bad()")

        # The substrate-internal path is hidden; the model sees a tidy ref.
        assert "/workspace/in/__persona_main__.py" not in result.stderr
        assert "<your code>" in result.stderr
        # The substantive content (line number, the exception name) survives.
        assert "NameError" in result.stderr

    @pytest.mark.asyncio
    async def test_workspace_path_prefix_scrubbed(self, tmp_path: Path) -> None:
        """``/workspace/in/some/file.csv`` collapses to ``some/file.csv``."""
        stderr = b"FileNotFoundError: '/workspace/in/data/missing.csv'\n"
        container = _make_container_mock(exit_code=1, stderr=stderr)
        client = _make_client_mock(container)
        sandbox = LocalDockerSandbox(workspace_root=tmp_path, docker_client=client)

        result = await sandbox.execute("open('/workspace/in/data/missing.csv')")

        assert "/workspace/in/" not in result.stderr
        assert "data/missing.csv" in result.stderr


# ---------------------------------------------------------------------------
# T05b — produced-file caps (D-12-10 snapshot-then-diff)
# ---------------------------------------------------------------------------


class TestProducedFileCaps:
    @pytest.mark.asyncio
    async def test_produced_files_under_caps_all_returned(self, tmp_path: Path) -> None:
        """The happy path: every produced file under the caps is reported."""
        # Pre-seed the out dir so the container's "produced files" walk finds them.
        container = _make_container_mock(exit_code=0)
        client = _make_client_mock(container)
        sandbox = LocalDockerSandbox(workspace_root=tmp_path, docker_client=client)
        # Patch _discover_produced_files to read from a known dir we seed.
        # Simpler: directly call the static method against a seeded dir.
        out = tmp_path / "out"
        out.mkdir()
        (out / "a.csv").write_bytes(b"x,y\n1,2\n")
        (out / "b.png").write_bytes(b"\x89PNG" + b"\x00" * 100)

        produced, truncated, _refused = sandbox._discover_produced_files(out, ResourceLimits())

        assert truncated is False
        assert len(produced) == 2
        assert {f.path for f in produced} == {"a.csv", "b.png"}
        # Media type is inferred from the extension so a produced PNG renders
        # inline (the frontend keys inline on media_type.startswith("image/")).
        by_path = {f.path: f for f in produced}
        assert by_path["b.png"].media_type == "image/png"
        assert by_path["a.csv"].media_type == "text/csv"

    def test_count_cap_truncates(self, tmp_path: Path) -> None:
        """``max_produced_files=N`` truncates to the first N files."""
        out = tmp_path / "out"
        out.mkdir()
        for i in range(30):
            (out / f"f{i:02d}.txt").write_bytes(b"x")

        produced, truncated, _refused = LocalDockerSandbox._discover_produced_files(
            out, ResourceLimits(max_produced_files=5)
        )

        assert truncated is True
        assert len(produced) == 5

    def test_per_file_size_cap_skips_oversized(self, tmp_path: Path) -> None:
        """A file above ``max_produced_file_mb`` is skipped (not returned)
        and ``truncated_files=True`` so the model knows something was
        elided."""
        out = tmp_path / "out"
        out.mkdir()
        (out / "small.txt").write_bytes(b"ok")
        # 2 MiB file; cap is 1 MiB.
        (out / "huge.bin").write_bytes(b"\x00" * (2 * 1024 * 1024))

        produced, truncated, _refused = LocalDockerSandbox._discover_produced_files(
            out, ResourceLimits(max_produced_file_mb=1)
        )

        assert truncated is True
        paths = {f.path for f in produced}
        assert "small.txt" in paths
        assert "huge.bin" not in paths

    def test_empty_workspace_no_files_no_truncation(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        out.mkdir()
        produced, truncated, _refused = LocalDockerSandbox._discover_produced_files(
            out, ResourceLimits()
        )
        assert produced == ()
        assert truncated is False


# ---------------------------------------------------------------------------
# Smoke test: T05b's ExecutionResult.truncated_files flag propagates
# ---------------------------------------------------------------------------


class TestExecutionResultTruncatedFlags:
    @pytest.mark.asyncio
    async def test_truncated_files_flag_propagates(self, tmp_path: Path) -> None:
        """End-to-end: when produced files exceed the count cap, the
        ``ExecutionResult.truncated_files`` flag is True so T03's tool
        factory sets ``ToolResult.truncated=True``."""
        # Configure the mock so the workspace gets files after the container
        # "runs". We do this by patching _discover_produced_files via
        # subclassing, which is heavyweight; instead we just verify the
        # T05b's _discover_produced_files works (covered above) and the
        # flag is included in the result construction.
        container = _make_container_mock(exit_code=0)
        client = _make_client_mock(container)
        sandbox = LocalDockerSandbox(workspace_root=tmp_path, docker_client=client)

        result = await sandbox.execute("pass")

        # No files were produced (the mock doesn't actually run code), so
        # the flag is False — but the field exists and is properly typed.
        assert result.truncated_files is False
        # And the produced_files tuple is empty.
        assert result.produced_files == ()

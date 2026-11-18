"""The supervisor starts every built-in MCP child with an allow-listed env (R9-258).

``subprocess.Popen`` is replaced by a recorder so no process starts: the test reads
the ``env`` each spawn would have been given. The env-building rules themselves are
pinned in ``packages/core/tests/unit/tools/mcp/builtin/test_child_env.py``.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest
from persona_api.mcp import builtin_launcher
from persona_api.mcp.builtin_launcher import BuiltinMCPSupervisor

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.asyncio

_SECRETS = {
    "DATABASE_URL": "r9-258-sentinel-database-url",
    "STRIPE_SECRET_KEY": "r9-258-sentinel-stripe",
    "CLERK_SECRET_KEY": "r9-258-sentinel-clerk",
    "ANTHROPIC_API_KEY": "r9-258-sentinel-anthropic",
    "OPENAI_API_KEY": "r9-258-sentinel-openai",
    "SOME_CONNECTOR_TOKEN": "r9-258-sentinel-connector-token",
    "SOME_SIGNING_SECRET": "r9-258-sentinel-signing-secret",
    "SENTRY_DSN": "r9-258-sentinel-sentry-dsn",
    "ANY_VENDOR_KEY": "r9-258-sentinel-vendor-key",
    "PERSONA_FILESYSTEM_SCOPE_ROOT": "/shared/flat/root",
}

_NEEDED = {
    "PATH": "/opt/venv/bin:/usr/bin",
    "HOME": "/var/lib/persona",
    "USER": "persona",
    "LOGNAME": "persona",
    "TIKTOKEN_CACHE_DIR": "/opt/tiktoken",
    "SSL_CERT_FILE": "/etc/ssl/certs/ca-certificates.crt",
    "HTTPS_PROXY": "http://proxy.internal:3128",
}

#: Names each server must receive from the parent (beyond the shared base).
_PER_SERVER = {
    "time": ("PATH", "HOME", "USER", "LOGNAME", "TIKTOKEN_CACHE_DIR"),
    "calculator": ("PATH", "HOME", "USER", "LOGNAME", "TIKTOKEN_CACHE_DIR"),
    "filesystem": ("PATH", "HOME", "USER", "LOGNAME", "TIKTOKEN_CACHE_DIR"),
    "weather": (
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "TIKTOKEN_CACHE_DIR",
        "SSL_CERT_FILE",
        "HTTPS_PROXY",
    ),
}


class _FakeProc:
    pid = 4242

    def poll(self) -> None:
        return None

    def terminate(self) -> None:
        return None

    def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002 (Popen shape)
        return 0


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    """Record the env of every spawn; the api env holds secrets and the needed names."""
    for name, value in {**_SECRETS, **_NEEDED}.items():
        monkeypatch.setenv(name, value)
    envs: list[dict[str, str]] = []

    def _popen(_argv: list[str], **kwargs: object) -> _FakeProc:
        env = kwargs.get("env")
        assert env is not None, "a built-in child must never inherit the whole api env"
        assert isinstance(env, dict)
        envs.append(dict(env))
        return _FakeProc()

    monkeypatch.setattr(subprocess, "Popen", _popen)
    monkeypatch.setattr(builtin_launcher, "_wait_for_port", lambda *_a: None)
    return envs


def _leaks(env: dict[str, str]) -> list[str]:
    values = set(_SECRETS.values())
    return sorted(k for k, v in env.items() if k in _SECRETS or v in values)


@pytest.mark.parametrize("server", sorted(_PER_SERVER))
async def test_each_child_gets_its_needs_and_no_secret(
    server: str, spawned: list[dict[str, str]]
) -> None:
    sup = BuiltinMCPSupervisor((server,))
    assert await sup.resolve([f"mcp:{server}:x"], filesystem_scope_root=None) != {}
    (env,) = spawned
    assert _leaks(env) == []
    for name in _PER_SERVER[server]:
        assert env.get(name) == _NEEDED[name], f"{server} child lost {name}"


@pytest.mark.parametrize("server", ["time", "calculator", "filesystem"])
async def test_offline_children_get_no_proxy_or_ca_bundle(
    server: str, spawned: list[dict[str, str]]
) -> None:
    await BuiltinMCPSupervisor((server,)).resolve([f"mcp:{server}:x"])
    (env,) = spawned
    assert "HTTPS_PROXY" not in env
    assert "SSL_CERT_FILE" not in env


async def test_scoped_filesystem_child_gets_its_scope_and_no_secret(
    spawned: list[dict[str, str]], tmp_path: Path
) -> None:
    scope = tmp_path / "owner" / "persona"
    sup = BuiltinMCPSupervisor(("filesystem",))
    await sup.resolve(["mcp:filesystem:read_file"], filesystem_scope_root=scope)
    (env,) = spawned
    # The launcher's own override, not the parent's stray shared root.
    assert env.pop("PERSONA_FILESYSTEM_SCOPE_ROOT") == str(scope)
    assert _leaks(env) == []
    assert env["PATH"] == _NEEDED["PATH"]


async def test_unscoped_filesystem_child_never_inherits_a_parent_scope(
    spawned: list[dict[str, str]],
) -> None:
    # P4-D-4: the parent's stray PERSONA_FILESYSTEM_SCOPE_ROOT must not reach it.
    await BuiltinMCPSupervisor(("filesystem",)).resolve(["mcp:filesystem:write_file"])
    (env,) = spawned
    assert "PERSONA_FILESYSTEM_SCOPE_ROOT" not in env

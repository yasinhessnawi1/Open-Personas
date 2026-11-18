"""The catalog syncs run git with a scrubbed env, not the api's (R9-258, D-ORCH-11-18-i).

The MCP mirror sync and the skill sources sync shell out to ``git clone`` from the api
process. A git that inherits the api env carries every DSN, key and token for as long
as it runs, readable by a same-uid built-in MCP child through
``/proc/<git pid>/environ``. These tests record each ``subprocess.run`` call and check
the env git gets.

Authentication today: none in the env. Every clone is an ``https://github.com/...``
URL; the only place a credential can travel is the URL itself (argv), which is passed
through untouched.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest
from persona.child_env import BASE_CHILD_ENV, GIT_CHILD_ENV, git_env
from persona.skills.skill_sources_sync import clone_at_ref
from persona.tools.mcp.mirror_sync import _clone_registry

if TYPE_CHECKING:
    from pathlib import Path

_SECRETS = {
    "DATABASE_URL": "postgres://su:pw@db/persona",
    "STRIPE_SECRET_KEY": "sk_live_git_sentinel",
    "CLERK_SECRET_KEY": "sk_clerk_git_sentinel",
    "OPENAI_API_KEY": "sk-git-sentinel",
    "PERSONA_TELEGRAM_BOT_TOKEN": "tok-git-sentinel",
    "GITHUB_TOKEN": "ghp_git_sentinel",
    "SOME_SIGNING_SECRET": "sec-git-sentinel",
}

_NEEDED = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/var/lib/persona",
    "LANG": "C.UTF-8",
    "HTTPS_PROXY": "http://proxy.internal:3128",
    "SSL_CERT_FILE": "/etc/ssl/certs/ca-certificates.crt",
    "GIT_SSL_CAINFO": "/etc/ssl/certs/ca-certificates.crt",
}

#: A token in the URL is the one credential channel git has here; it must pass as is.
_URL_WITH_TOKEN = "https://x-access-token:url-credential@github.com/o/r.git"


@pytest.fixture
def git_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], dict[str, str] | None]]:
    for name, value in {**_SECRETS, **_NEEDED}.items():
        monkeypatch.setenv(name, value)
    calls: list[tuple[list[str], dict[str, str] | None]] = []

    def _run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        env = kwargs.get("env")
        calls.append((list(argv), dict(env) if isinstance(env, dict) else None))
        return subprocess.CompletedProcess(argv, 0, stdout="0123abcd\n", stderr="")

    monkeypatch.setattr(subprocess, "run", _run)
    return calls


def _assert_scrubbed(env: dict[str, str] | None) -> None:
    assert env is not None, "git must never inherit the whole api env"
    values = set(_SECRETS.values())
    leaked = sorted(k for k, v in env.items() if k in _SECRETS or v in values)
    assert leaked == []
    for name, value in _NEEDED.items():
        assert env.get(name) == value, f"git lost {name}"
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_the_mcp_mirror_clone_gets_a_scrubbed_env(
    git_calls: list[tuple[list[str], dict[str, str] | None]], tmp_path: Path
) -> None:
    _clone_registry(_URL_WITH_TOKEN, tmp_path / "dest")
    ((argv, env),) = git_calls
    _assert_scrubbed(env)
    assert _URL_WITH_TOKEN in argv  # the URL credential still flows, unchanged


def test_every_skill_source_git_call_gets_a_scrubbed_env(
    git_calls: list[tuple[list[str], dict[str, str] | None]], tmp_path: Path
) -> None:
    checkout = clone_at_ref(_URL_WITH_TOKEN, "0123abcd", tmp_path / "dest")
    assert [argv[1:3] for argv, _ in git_calls] == [
        ["clone", "--depth"],
        ["-C", str(tmp_path / "dest")],
        ["-C", str(tmp_path / "dest")],
        ["-C", str(tmp_path / "dest")],
    ]
    for _argv, env in git_calls:
        _assert_scrubbed(env)
    assert _URL_WITH_TOKEN in git_calls[0][0]
    assert checkout.commit == "0123abcd"


def test_git_env_is_the_base_plus_git_names_only() -> None:
    parent = {**_SECRETS, **_NEEDED, "PERSONA_FILESYSTEM_SCOPE_ROOT": "/x"}
    env = git_env(parent)
    assert set(env) <= BASE_CHILD_ENV | GIT_CHILD_ENV | {"GIT_TERMINAL_PROMPT"}
    assert "PERSONA_FILESYSTEM_SCOPE_ROOT" not in env


def test_git_never_prompts_even_if_the_parent_says_otherwise() -> None:
    # No tty in the container: a prompt would only hang until the job's timeout.
    assert git_env({"GIT_TERMINAL_PROMPT": "1"})["GIT_TERMINAL_PROMPT"] == "0"


@pytest.mark.parametrize(
    "name",
    [
        "GIT_SSL_CAINFO",
        "GIT_SSL_CAPATH",
        "SSL_CERT_FILE",
        "HTTPS_PROXY",
        "https_proxy",
        "NO_PROXY",
        "XDG_CONFIG_HOME",
    ],
)
def test_git_gets_its_transport_and_config_names(name: str) -> None:
    assert git_env({name: "v"})[name] == "v"


@pytest.mark.parametrize("name", ["SSLKEYLOGFILE", "GIT_ASKPASS", "SSH_AUTH_SOCK", "GITHUB_TOKEN"])
def test_git_gets_no_credential_channel_it_does_not_use(name: str) -> None:
    # Every clone here is anonymous https; nothing in the code reads these.
    assert name not in git_env({name: "v"})

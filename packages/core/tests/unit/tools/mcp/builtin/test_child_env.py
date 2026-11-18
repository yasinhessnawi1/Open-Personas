"""The environment a built-in MCP child is started with (R9-258).

A built-in MCP child gets an allow-listed environment: the base names every
child needs plus the names its own server declares next to its registration.
Everything else in the api's environment (database DSNs, provider, Stripe and
Clerk keys, connector tokens) stays in the parent.
"""

from __future__ import annotations

import re

import pytest
from persona.tools.mcp.builtin import SERVER_BUILDERS, SERVER_CHILD_ENV, child_env
from persona.tools.mcp.builtin._child_env import BASE_CHILD_ENV, build_child_env

#: Secrets an api process really holds, by name. None may reach a child.
_NAMED_SECRETS = (
    "DATABASE_URL",
    "APP_DATABASE_URL",
    "PERSONA_VOICE_DATABASE_URL",
    "STRIPE_SECRET_KEY",
    "STRIPE_WEBHOOK_SECRET",
    "CLERK_SECRET_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "PERSONA_API_KEY",
    "PERSONA_MCP_SECRET_KEY",
    "PERSONA_TELEGRAM_BOT_TOKEN",
    "PERSONA_SENTRY_DSN",
    "PERSONA_APP_DB_PASSWORD",
    "AZURE_OPENAI_AD_TOKEN",
    "SSLKEYLOGFILE",
    "PERSONA_FILESYSTEM_SCOPE_ROOT",
)

#: The secret-shaped suffixes the brief names; a made-up name with each must not pass.
_SECRET_SUFFIXES = ("_KEY", "_SECRET", "_TOKEN", "_DSN", "_PASSWORD")

_SECRET_SHAPE = re.compile(r"(_KEY|_SECRET|_TOKEN|_DSN|_PASSWORD|_URL|_CREDENTIALS?)$", re.I)


def _parent() -> dict[str, str]:
    """A parent env holding every base name, every declared name, and the secrets."""
    env = {name: f"value-of-{name}" for name in BASE_CHILD_ENV}
    for names in SERVER_CHILD_ENV.values():
        env.update({name: f"value-of-{name}" for name in names})
    env.update({name: f"sentinel-secret-{name}" for name in _NAMED_SECRETS})
    for suffix in _SECRET_SUFFIXES:
        name = f"SOME_CONNECTOR{suffix}"
        env[name] = f"sentinel-secret-{name}"
    return env


@pytest.mark.parametrize("server", sorted(SERVER_BUILDERS))
def test_no_secret_reaches_any_child(server: str) -> None:
    env = child_env(server, _parent())
    leaked = sorted(name for name in env if "sentinel-secret" in env[name])
    assert leaked == [], f"{server} child would see {leaked}"


@pytest.mark.parametrize("server", sorted(SERVER_BUILDERS))
@pytest.mark.parametrize("suffix", _SECRET_SUFFIXES)
def test_secret_suffixes_never_pass(server: str, suffix: str) -> None:
    name = f"ANY_PROVIDER{suffix}"
    env = child_env(server, {**_parent(), name: "sentinel"})
    assert name not in env


@pytest.mark.parametrize("server", sorted(SERVER_BUILDERS))
def test_child_holds_only_allow_listed_names(server: str) -> None:
    env = child_env(server, _parent())
    allowed = BASE_CHILD_ENV | SERVER_CHILD_ENV[server]
    assert set(env) <= allowed


@pytest.mark.parametrize("server", sorted(SERVER_BUILDERS))
def test_every_base_name_reaches_every_child(server: str) -> None:
    env = child_env(server, _parent())
    missing = sorted(BASE_CHILD_ENV - set(env))
    assert missing == [], f"{server} child lost {missing}"


def test_every_registered_server_declares_its_child_env() -> None:
    # The per-server allow-list sits next to the registration; a new server must add one.
    assert set(SERVER_CHILD_ENV) == set(SERVER_BUILDERS)


def test_no_allow_listed_name_is_shaped_like_a_secret() -> None:
    names = set(BASE_CHILD_ENV).union(*SERVER_CHILD_ENV.values())
    assert sorted(n for n in names if _SECRET_SHAPE.search(n)) == []


@pytest.mark.parametrize(
    "name",
    ["PATH", "HOME", "USER", "LOGNAME", "LANG", "TZ", "TMPDIR", "TIKTOKEN_CACHE_DIR"],
)
def test_base_carries_what_every_child_needs(name: str) -> None:
    # USER and LOGNAME: the data uid has no passwd entry (R9-292). TIKTOKEN_CACHE_DIR:
    # every child imports tiktoken, and the image bakes its encoding there (offline).
    assert name in BASE_CHILD_ENV


@pytest.mark.parametrize(
    "name",
    ["SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "USERPROFILE", "USERNAME"],
)
def test_base_carries_the_windows_runtime_names(name: str) -> None:
    # Without SYSTEMROOT a Windows child cannot even open a socket.
    assert name in BASE_CHILD_ENV


@pytest.mark.parametrize(
    "name",
    [
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    ],
)
def test_weather_reaches_its_outbound_settings(name: str) -> None:
    # weather is the one built-in that calls out (open-meteo over HTTPS, via httpx).
    env = child_env("weather", {**_parent(), name: "x"})
    assert env[name] == "x"


@pytest.mark.parametrize("server", ["time", "calculator", "filesystem"])
def test_offline_servers_get_no_outbound_settings(server: str) -> None:
    env = child_env(server, {**_parent(), "HTTPS_PROXY": "http://u:p@proxy:3128"})
    assert "HTTPS_PROXY" not in env
    assert "SSL_CERT_FILE" not in env


def test_filesystem_scope_reaches_the_child_only_as_an_override() -> None:
    # The launcher threads the scope root in; a stray parent value never passes.
    parent = {**_parent(), "PERSONA_FILESYSTEM_SCOPE_ROOT": "/shared/root"}
    assert "PERSONA_FILESYSTEM_SCOPE_ROOT" not in child_env("filesystem", parent)
    scoped = child_env("filesystem", parent, overrides={"PERSONA_FILESYSTEM_SCOPE_ROOT": "/w/o/p"})
    assert scoped["PERSONA_FILESYSTEM_SCOPE_ROOT"] == "/w/o/p"


def test_a_missing_name_is_simply_absent() -> None:
    # No new failure mode: a needed name the api does not have is left out, as today.
    env = build_child_env(frozenset({"SSL_CERT_FILE"}), {"PATH": "/bin"})
    assert env == {"PATH": "/bin"}


def test_the_parent_mapping_is_not_changed() -> None:
    parent = {"PATH": "/bin", "DATABASE_URL": "postgres://x"}
    build_child_env(frozenset(), parent, overrides={"PERSONA_FILESYSTEM_SCOPE_ROOT": "/r"})
    assert parent == {"PATH": "/bin", "DATABASE_URL": "postgres://x"}


def test_windows_names_match_whatever_their_case(monkeypatch: pytest.MonkeyPatch) -> None:
    # Windows env names are case-insensitive; a mixed-case parent name still matches.
    monkeypatch.setattr("persona.tools.mcp.builtin._child_env.sys.platform", "win32")
    env = build_child_env(frozenset(), {"SystemRoot": "C:\\Windows", "Stripe_Secret_Key": "s"})
    assert env == {"SystemRoot": "C:\\Windows"}


def test_posix_names_match_exactly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("persona.tools.mcp.builtin._child_env.sys.platform", "linux")
    env = build_child_env(frozenset(), {"Path": "/bin", "PATH": "/usr/bin"})
    assert env == {"PATH": "/usr/bin"}

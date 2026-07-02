"""Configuration for persona-core, loaded from environment variables.

All runtime knobs for the open-source library go here. Twelve-Factor: env vars
only, no YAML configuration files for runtime knobs (Hydra-style configs stay
out of product code; see ENGINEERING_STANDARDS.md §2.1).

Values are read once at process start via Pydantic Settings. Downstream code
should accept a ``PersonaCoreConfig`` instance through dependency injection
rather than read environment variables directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["PersonaCoreConfig"]


class PersonaCoreConfig(BaseSettings):
    """Environment-driven configuration for persona-core.

    Attributes:
        backend: Identifier of the chosen model backend (set in spec 02).
        api_key: API key for the chosen backend; never logged.
        model: Model identifier within the backend.
        chroma_path: Filesystem root for ChromaDB persistence and the default
            location of the JSONL audit log subdirectory.
        log_level: Minimum log level for the loguru sinks. Standard names
            (``DEBUG``, ``INFO``, ``WARNING``, ``ERROR``, ``CRITICAL``).
        log_format: ``pretty`` for colored, human-readable output (CLI default);
            ``json`` for JSON Lines suitable for shipping to a log aggregator.
        log_file: Optional file path to add as an additional log sink. Both
            stderr and this file receive the same records when set.
        audit_path: Optional override for the audit-log directory. When
            unset, audit files live at ``<chroma_path>/audit/<persona_id>.jsonl``
            (per D-01-6).
        web_search_provider: Search backend for the ``web_search`` tool
            (spec 03, D-03-9). ``brave`` is the only provider wired in v0.1;
            ``tavily`` and ``serpapi`` are stubs raising NotImplementedError.
        web_search_api_key: API key for ``web_search_provider`` (D-03-10).
            Read from ``PERSONA_WEB_SEARCH_API_KEY``; never logged.
        tools_sandbox_root: Per-CWD-relative sandbox root for the
            ``file_read``/``file_write`` tools (spec 03, D-03-23).
        currency_provider: Exchange-rate backend for the ``currency_convert``
            tool (spec 26, D-26-6). ``frankfurter`` (default, no key) and
            ``exchangerate_api`` (no key) are wired.
        currency_api_key: Optional API key for keyed currency providers
            (spec 26). Read from ``PERSONA_CURRENCY_API_KEY``; never logged.
        mcp_servers: Mapping of MCP server name → URL parsed from
            ``PERSONA_MCP_SERVERS`` (comma-separated ``name=url`` per
            D-03-22). Empty dict when the env var is unset.
        mcp_builtin_enabled: Which authored built-in MCP servers the operator
            opts into (``PERSONA_MCP_BUILTIN_ENABLED``, comma-separated; Spec 27
            D-27-4). Unset → the catalog default-enabled safe subset; expose the
            parsed tuple via :attr:`mcp_builtin_enabled_parsed`.
        mcp_builtin_uid: Optional POSIX uid the built-in MCP server subprocesses
            drop to at spawn (``PERSONA_MCP_BUILTIN_UID``; Spec 27 D-27-12). Unset
            (default) → children inherit the API process's user — in production the
            API runs as the non-root persona user (uid 1000), so its children do
            too. Set this only when the API itself runs as root and must drop
            privileges for the spawned servers.
    """

    model_config = SettingsConfigDict(env_prefix="PERSONA_", extra="ignore")

    backend: str = "anthropic"
    api_key: str = Field(default="", repr=False)
    model: str = "claude-sonnet-4-6"
    chroma_path: Path = Path(".chroma/")
    log_level: str = "INFO"
    # Spec A4 — the timezone a task cadence ("every morning at 08:00") is anchored in when the
    # user's own timezone is unknown. Read from ``PERSONA_DEFAULT_TIMEZONE`` (an IANA name);
    # rendered transparently in the contract echo so the user sees which zone the time is in.
    # K6 SEAM: once the user profile carries a per-user timezone, A4 reads that and falls back to
    # THIS default — A4 stays uncoupled to K6 (no dependency), the per-user value drops in later.
    default_timezone: str = "Europe/Oslo"
    log_format: Literal["pretty", "json"] = "pretty"
    log_file: Path | None = None
    audit_path: Path | None = None

    # Spec 03 — tools (T12).
    web_search_provider: Literal["brave", "tavily", "serpapi"] = "brave"
    web_search_api_key: SecretStr | None = None
    tools_sandbox_root: Path = Path("./.persona_work")
    # Spec 26 T04 — currency_convert. ``frankfurter`` (ECB-class reference
    # rates) is the default and needs NO key, so the tool works out of the box
    # on ``pip install`` (D-26-X-currency-no-key-default-rationale).
    # ``exchangerate_api`` is a no-key alternate. ``currency_api_key`` is only
    # consulted for keyed providers added later (provider-conditional guard,
    # D-26-6); never logged.
    currency_provider: Literal["frankfurter", "exchangerate_api"] = "frankfurter"
    currency_api_key: SecretStr | None = None
    # `mcp_servers` is stored as the raw comma-separated string so Pydantic
    # Settings doesn't try to JSON-parse the env value. The dict is computed
    # via `mcp_servers_parsed` below; downstream code uses that. D-03-22.
    mcp_servers: str = Field(default="", repr=False)

    @field_validator("default_timezone", mode="after")
    @classmethod
    def _validate_default_timezone(cls, value: str) -> str:
        """Reject an unknown IANA zone at startup (fail-fast, not at first task cadence)."""
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            msg = f"invalid PERSONA_DEFAULT_TIMEZONE {value!r} (must be an IANA zone)"
            raise ValueError(msg) from exc
        return value

    @field_validator("mcp_servers", mode="after")
    @classmethod
    def _validate_mcp_servers(cls, value: str) -> str:
        """Validate the comma-separated `name=url` env format (D-03-22).

        Empty string is valid (no MCP servers configured). Otherwise each
        entry must contain exactly one ``=``, the name must match
        ``[A-Za-z0-9_-]+``, the URL must be ``http://`` or ``https://``.
        Duplicate names raise. The actual dict is exposed via
        :attr:`mcp_servers_parsed`.
        """
        if not value:
            return ""
        # Run the parser purely for its side-effect of raising on malformed input.
        _parse_mcp_servers_string(value)
        return value

    @property
    def mcp_servers_parsed(self) -> dict[str, str]:
        """Parsed mapping of MCP server name → URL (D-03-22)."""
        if not self.mcp_servers:
            return {}
        return _parse_mcp_servers_string(self.mcp_servers)

    # Spec N1 (D-N1-1/2/5/8) — the Docker MCP Gateway as an aggregating MCP source.
    # Connect-only: Persona connects to an externally-managed gateway, never spawns it
    # (no Docker socket). The URL is OPERATOR deployment config (same trust tier as
    # ``mcp_servers`` / ``DATABASE_URL``) — NOT user-supplied — so it is connected
    # WITHOUT SSRF pinning (D-N1-2), exactly like the operator channel. It MUST be the
    # gateway's streamable-HTTP endpoint (``--transport streaming`` ``/mcp`` path), NOT
    # the legacy SSE transport (D-03-19). Unset → no gateway source (fail-soft).
    docker_mcp_gateway_url: str = ""
    # Optional bearer token for a non-loopback gateway (D-N1-5). A ``SecretStr`` so it
    # is never logged; it rides the existing client header path only
    # (``Authorization: Bearer …``) — never a prompt, tool spec, or audit line. A
    # loopback gateway needs none.
    docker_mcp_gateway_token: SecretStr | None = None

    # Spec N2 (N2-D-1) — the writable, reader-visible location of the auto-synced
    # Docker catalog mirror. On a deployed image the bundled package-data ``mirror.json``
    # is root-owned ``/app/...`` (baked in, lost on every redeploy), so the auto-refresh
    # writes here instead — a path on the mounted persistent volume (e.g.
    # ``/var/lib/persona/mcp/mirror.json``). The request-path loader prefers it over the
    # bundled snapshot (precedence: override → bundled → builtin ``catalog.toml``); unset
    # (dev / local) → the bundled snapshot is used and is also the sync's write target.
    mcp_mirror_path: Path | None = None

    # Spec S2 (S2-D-1) — the writable, reader-visible location of the auto-synced external
    # skill mirror (a second mirror on N2's substrate, parallel to ``mcp_mirror_path``). On a
    # deployed image the bundled package-data ``skill_mirror.json`` is root-owned + lost on
    # redeploy, so the auto-sync writes here — a path on the mounted volume (e.g.
    # ``/var/lib/persona/skill_mirror/skill_mirror.json``). Unset (dev / local) → the bundled
    # path is used. Fail-soft: absent/corrupt → empty external set (builtins unaffected).
    skill_mirror_path: Path | None = None

    # Spec 27 (D-27-4) — which built-in MCP servers an operator opts into. Stored
    # as a raw string so the "unset" case (None → catalog safe-subset) is
    # distinguishable from the "explicit empty" case ("" → opt out of all). The
    # tuple is computed via `mcp_builtin_enabled_parsed`; downstream uses that.
    mcp_builtin_enabled: str | None = Field(default=None, repr=False)
    # Spec 27 (D-27-12) — optional privilege-drop for spawned built-in MCP
    # servers. None → children inherit the API process uid (the production
    # default: the API container runs as the non-root persona user).
    mcp_builtin_uid: int | None = Field(default=None)

    @field_validator("mcp_builtin_enabled", mode="after")
    @classmethod
    def _validate_mcp_builtin_enabled(cls, value: str | None) -> str | None:
        """Validate the comma-separated built-in server list (D-27-4).

        ``None`` (env unset) → the catalog's default-enabled safe subset. An
        empty string opts out of all built-ins. Otherwise every name must be an
        **authored** built-in (``kind="builtin"`` in the catalog) — external
        bring-your-own servers (fetch/github) are configured via
        ``PERSONA_MCP_SERVERS``, not enabled here, so naming one fails loud
        (fail-fast on operator misconfiguration). Empty entries are skipped.
        """
        if value is None or not value.strip():
            return value
        # Side-effect: raises on an unknown / non-authored name.
        _parse_mcp_builtin_enabled(value)
        return value

    @property
    def mcp_builtin_enabled_parsed(self) -> tuple[str, ...]:
        """The enabled built-in MCP servers, de-duplicated, in declared order.

        ``None`` (unset) yields the catalog default-enabled subset (D-27-4); an
        explicit empty string yields ``()`` (opt out of all). "Enabled" means
        "registered + available", NOT "running" — lazy spawning (D-27-3) starts
        a server only on first use.
        """
        from persona.tools.mcp.catalog import default_enabled_server_names

        if self.mcp_builtin_enabled is None:
            return default_enabled_server_names()
        if not self.mcp_builtin_enabled.strip():
            return ()
        return _parse_mcp_builtin_enabled(self.mcp_builtin_enabled)


def _parse_mcp_servers_string(value: str) -> dict[str, str]:
    """Implementation of the D-03-22 parser used by validator + property."""
    import re

    result: dict[str, str] = {}
    for raw in value.split(","):
        entry = raw.strip()
        if not entry:
            continue
        if entry.count("=") < 1:
            msg = f"malformed PERSONA_MCP_SERVERS entry (missing '='): {entry!r}"
            raise ValueError(msg)
        name, url = entry.split("=", 1)
        name = name.strip()
        url = url.strip()
        if not name:
            msg = f"empty server name in PERSONA_MCP_SERVERS: {entry!r}"
            raise ValueError(msg)
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            msg = (
                f"invalid server name in PERSONA_MCP_SERVERS: {name!r} (must match [A-Za-z0-9_-]+)"
            )
            raise ValueError(msg)
        if not url:
            msg = f"empty URL for server {name!r} in PERSONA_MCP_SERVERS"
            raise ValueError(msg)
        if not (url.startswith("http://") or url.startswith("https://")):
            msg = (
                f"invalid URL for MCP server {name!r}: "
                f"{url!r} (must start with http:// or https://)"
            )
            raise ValueError(msg)
        if name in result:
            msg = f"duplicate MCP server name in PERSONA_MCP_SERVERS: {name!r}"
            raise ValueError(msg)
        result[name] = url
    return result


def _parse_mcp_builtin_enabled(value: str) -> tuple[str, ...]:
    """Parse + validate ``PERSONA_MCP_BUILTIN_ENABLED`` (D-27-4).

    Each comma-separated name must be an authored built-in server (``kind``
    ``"builtin"`` in the catalog). External servers are configured via
    ``PERSONA_MCP_SERVERS``, so naming one (or an unknown name) raises with a
    pointer to the right mechanism. Returns the de-duplicated names in order.
    """
    from persona.tools.mcp.catalog import authored_server_names, mcp_server_entry

    authored = authored_server_names()
    result: list[str] = []
    seen: set[str] = set()
    for raw in value.split(","):
        name = raw.strip()
        if not name:
            continue
        if name not in authored:
            entry = mcp_server_entry(name)
            if entry is not None and entry.kind == "external":
                msg = (
                    f"{name!r} is a bring-your-own external MCP server; configure it via "
                    "PERSONA_MCP_SERVERS, not PERSONA_MCP_BUILTIN_ENABLED"
                )
            else:
                msg = (
                    f"unknown built-in MCP server in PERSONA_MCP_BUILTIN_ENABLED: {name!r} "
                    f"(authored built-ins: {', '.join(sorted(authored))})"
                )
            raise ValueError(msg)
        if name not in seen:
            seen.add(name)
            result.append(name)
    return tuple(result)

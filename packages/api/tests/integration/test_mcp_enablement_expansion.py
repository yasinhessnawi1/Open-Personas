"""Integration proof: a bare ``mcp:<server>`` grant reaches the model (Spec N7, T1c).

Before N7-T1a/T1b (0084ab3 / c779e1d) a persona whose YAML ``tools`` carried the bare
server-grant form ``mcp:<name>`` — what the apps UX toggle writes (N3) — spawned that
server just fine (the builtin launcher / env-configured / gateway sources all already
resolved the REFERENCED server correctly, via ``needed_builtins``), but never actually
saw its tools: ``build_default_toolbox``'s allow-list stayed literal exact-match, and no
discovered tool is ever registered under the bare name itself. "Enable" toggled the app
on and nothing reached the model. N7-T1b closed the gap by expanding each server grant
into that server's REAL ``mcp:<name>:<tool>`` names at toolbox-build time (the same
mechanism ``byo_allow`` already used for assigned bring-your-own servers), fail-closed by
construction — only a server that actually connected THIS build contributes anything.

This file is the API-layer, real-composition proof (deliberately NOT the unit-level fake
in ``packages/core/tests/unit/tools/test_tools_factory.py::TestServerGrantExpansion``,
which patches the MCP transport): a persona ROW is seeded into Postgres and loaded
through :meth:`RuntimeFactory._load_persona` (the real YAML-on-disk -> ``Persona`` parse
every request goes through), then built through :meth:`RuntimeFactory._build_toolbox` —
the SAME layer :meth:`RuntimeFactory.build_conversation_loop` /
:meth:`RuntimeFactory.build_agentic_loop` call per request. ``_build_toolbox`` resolves
the persona's built-in MCP servers via the REAL
:class:`~persona_api.mcp.BuiltinMCPSupervisor` (spawns an actual ``time`` subprocess,
D-27-2) before handing its loopback URL to ``build_default_toolbox`` as
``extra_mcp_servers`` — the exact expansion pass N7-T1b added.

The ``time`` server's one advertised tool is ``mcp:time:datetime`` — confirmed both by
the server's own docstring (``persona/tools/mcp/builtin/time_server.py``: "Exposed as
``mcp:time:datetime`` to personas.") and, here, by actually connecting to the spawned
subprocess and dispatching a real call through it (the adapter derives the name as
``f"mcp:{server_name}:{tool_def.name}"`` from the FastMCP-registered function name —
never hand-guessed).

A second test pins the fail-CLOSED half (M1-T8 precedent): a grant for a server that is
never configured anywhere must admit NONE of that server's tools, and toolbox
composition must not break; the persona's other declared tool still works. The expansion
is fail-closed by construction, not fail-open.

A third test restores the "unconnectable" flavor that an earlier version of this file
had to drop: a grant for a server that IS env-configured (``PersonaCoreConfig.mcp_servers``,
the ``PERSONA_MCP_SERVERS`` shape) but points at a real refused loopback port. That used to
surface a genuine, reproducible, pre-existing gap one layer down —
``MCPClient.connect(strict=False)`` (``persona/tools/mcp/client.py``) wrapped the connect
attempt in ``except Exception``, but a real refused TCP connection through
``mcp.client.streamable_http`` propagates as ``asyncio.CancelledError`` (raised from inside
anyio's task-group teardown), a ``BaseException`` subclass NOT caught by that — graceful
degradation (``strict=False``) did not actually degrade gracefully for this real-world
failure shape; it crashed toolbox composition instead (R9-042, found by this file's T1c
work, fixed separately — the client now classifies a genuine connection failure hiding
behind the CancelledError shape and degrades on it, while still propagating a truly
external task cancellation untouched). With that fixed, this file pins all three flavors.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from persona.config import PersonaCoreConfig
from persona.schema.tools import ToolCall
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from persona_api.services.runtime_factory import RuntimeFactory
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy import Engine

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_TIME_YAML = """\
schema_version: "1.0"
identity:
  name: Chronos
  role: assistant
  background: |
    A helper whose owner enabled the time app via the apps toggle.
  language_default: en
  constraints: []
tools:
  - mcp:time
"""

_DEAD_GRANTS_YAML = """\
schema_version: "1.0"
identity:
  name: Ghost
  role: assistant
  background: |
    A helper whose granted apps never connect.
  language_default: en
  constraints: []
tools:
  - mcp:totally-unconfigured-app
  - mcp:another-unconfigured-app
  - file_read
"""

_UNCONNECTABLE_GRANTS_YAML = """\
schema_version: "1.0"
identity:
  name: Ghost2
  role: assistant
  background: |
    A helper whose one granted app is configured but unreachable.
  language_default: en
  constraints: []
tools:
  - mcp:deadserver
  - file_read
"""


def _unused_tcp_port() -> int:
    """A port nothing is listening on: bind ephemeral, read it back, close it.

    Guarantees an immediate ECONNREFUSED on connect (no hang, no privileges
    needed, portable) — a real refusal, not merely an unconfigured name.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _seed_persona_row(database_url: str, *, owner: str, persona_id: str, yaml_text: str) -> None:
    """Insert a (users, personas) pair directly via the superuser DSN (RLS bypass)."""
    su = make_rls_engine(database_url)
    with su.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
            {"i": owner, "e": f"{owner}@x"},
        )
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES (:i, :o, :y)"),
            {"i": persona_id, "o": owner, "y": yaml_text},
        )
    su.dispose()


def _cleanup_owner(database_url: str, *, owner: str) -> None:
    """Delete the owner row; ``ON DELETE CASCADE`` takes its personas with it."""
    su = make_rls_engine(database_url)
    with su.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id = :i"), {"i": owner})
    su.dispose()


def _make_factory(rls_engine: Engine, *, core_config: PersonaCoreConfig) -> RuntimeFactory:
    return RuntimeFactory(
        rls_engine=rls_engine,
        embedder=None,  # type: ignore[arg-type]
        tier_registry=None,  # type: ignore[arg-type]
        turn_log_writer=None,  # type: ignore[arg-type]
        audit_root=Path("/tmp/persona-n7-t1c-audit"),
        core_config=core_config,
    )


async def _teardown(
    factory: RuntimeFactory, rls_engine: Engine, su_url: str, *, owner: str
) -> None:
    for client in factory._mcp_clients:  # noqa: SLF001 — test-only reach into accumulated clients
        await client.disconnect()
    await factory._builtin_mcp.aclose()  # noqa: SLF001 — reap the real spawned subprocess
    rls_engine.dispose()
    _cleanup_owner(su_url, owner=owner)


async def test_bare_time_grant_spawns_the_real_server_and_advertises_its_tool(
    migrated_engine: Engine,  # noqa: ARG001
    tmp_path: Path,
) -> None:
    """The audit's exact bug, closed, end-to-end.

    A persona whose DB-stored YAML declares the bare ``mcp:time`` grant is loaded
    through the real RuntimeFactory composition path; the actually-spawned ``time``
    subprocess's real tool, ``mcp:time:datetime``, is advertised to the model, and
    one real dispatch through it returns a live result.
    """
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    su_url = os.environ["DATABASE_URL"]
    owner, persona_id = "user_n7_t1c", "persona_n7_t1c_time"
    _seed_persona_row(su_url, owner=owner, persona_id=persona_id, yaml_text=_TIME_YAML)

    rls_engine = make_rls_engine(app_url)
    factory = _make_factory(rls_engine, core_config=PersonaCoreConfig(tools_sandbox_root=tmp_path))
    token = current_user_id.set(owner)
    try:
        persona = factory._load_persona(persona_id)  # noqa: SLF001 — the real YAML->Persona load
        assert persona.tools == ["mcp:time"], "sanity: the bare grant survived the DB round-trip"

        toolbox = await factory._build_toolbox(persona, scanned_skills=[])  # noqa: SLF001

        names = toolbox.names()  # type: ignore[attr-defined]
        assert "mcp:time:datetime" in names, (
            f"the real time server's advertised tool must reach the model; got {names!r}"
        )
        assert toolbox.is_allowed("mcp:time:datetime")  # type: ignore[attr-defined]
        spec_names = {s.name for s in toolbox.get_specs()}  # type: ignore[attr-defined]
        assert "mcp:time:datetime" in spec_names
        # The bare grant is the documented allow-list form but no tool ever
        # registers under it — it is never itself advertised (byte-for-byte T1b).
        assert "mcp:time" not in names

        result = await toolbox.dispatch(  # type: ignore[attr-defined]
            ToolCall(
                name="mcp:time:datetime",
                args={"operation": "now", "timezone": "UTC"},
                call_id="n7-t1c-1",
            )
        )
        assert result.is_error is False, result.content
        assert "UTC" in result.content
    finally:
        current_user_id.reset(token)
        await _teardown(factory, rls_engine, su_url, owner=owner)


async def test_grant_of_a_dead_server_admits_nothing_and_the_turn_still_composes(
    migrated_engine: Engine,  # noqa: ARG001
    tmp_path: Path,
) -> None:
    """Fail-closed pin (M1-T8 precedent).

    Granting a server that is never configured anywhere — not a builtin, not in
    ``PERSONA_MCP_SERVERS``, not the gateway — yields NONE of that server's tools,
    and toolbox composition itself never breaks: the persona's other declared tool
    (``file_read``) still works. Enable actually enables (proven above); an unknown
    grant stays inert — the expansion is fail-closed, not fail-open.
    """
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    su_url = os.environ["DATABASE_URL"]
    owner, persona_id = "user_n7_t1c", "persona_n7_t1c_dead"
    _seed_persona_row(su_url, owner=owner, persona_id=persona_id, yaml_text=_DEAD_GRANTS_YAML)

    rls_engine = make_rls_engine(app_url)
    factory = _make_factory(rls_engine, core_config=PersonaCoreConfig(tools_sandbox_root=tmp_path))
    token = current_user_id.set(owner)
    try:
        persona = factory._load_persona(persona_id)  # noqa: SLF001
        assert persona.tools == [
            "mcp:totally-unconfigured-app",
            "mcp:another-unconfigured-app",
            "file_read",
        ]

        toolbox = await factory._build_toolbox(persona, scanned_skills=[])  # noqa: SLF001

        names = toolbox.names()  # type: ignore[attr-defined]
        assert not any(n.startswith("mcp:") for n in names), (
            f"a dead grant must admit nothing; got {names!r}"
        )
        assert "file_read" in names, "the turn still composes cleanly for the persona's other tool"
    finally:
        current_user_id.reset(token)
        await _teardown(factory, rls_engine, su_url, owner=owner)


async def test_grant_of_a_refused_server_degrades_gracefully_r9_042(
    migrated_engine: Engine,  # noqa: ARG001
    tmp_path: Path,
) -> None:
    """R9-042, closed — the "unconnectable" flavor this file used to have to drop.

    The persona grants ``mcp:deadserver``, which IS configured (via
    ``PersonaCoreConfig.mcp_servers`` — the real ``PERSONA_MCP_SERVERS`` shape) but
    points at a real closed loopback port: the connect is genuinely REFUSED, not
    merely absent (contrast with ``test_grant_of_a_dead_server_...`` above, which
    never configures the name at all). Before the fix this line raised a bare
    ``asyncio.CancelledError`` straight out of ``MCPClient.connect(strict=False)``
    and crashed toolbox composition for the whole turn. Now it must degrade exactly
    like the "nonexistent" flavor: admit none of that server's tools, and the
    persona's other declared tool (``file_read``) still works.
    """
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    su_url = os.environ["DATABASE_URL"]
    owner, persona_id = "user_n7_t1c", "persona_n7_t1c_unconnectable"
    _seed_persona_row(
        su_url, owner=owner, persona_id=persona_id, yaml_text=_UNCONNECTABLE_GRANTS_YAML
    )

    port = _unused_tcp_port()
    rls_engine = make_rls_engine(app_url)
    factory = _make_factory(
        rls_engine,
        core_config=PersonaCoreConfig(
            tools_sandbox_root=tmp_path,
            mcp_servers=f"deadserver=http://127.0.0.1:{port}/mcp",
        ),
    )
    token = current_user_id.set(owner)
    try:
        persona = factory._load_persona(persona_id)  # noqa: SLF001
        assert persona.tools == ["mcp:deadserver", "file_read"]

        # Pre-fix, this raised a bare asyncio.CancelledError and crashed the turn.
        toolbox = await factory._build_toolbox(persona, scanned_skills=[])  # noqa: SLF001

        names = toolbox.names()  # type: ignore[attr-defined]
        assert not any(n.startswith("mcp:deadserver") for n in names), (
            f"a refused server must admit nothing; got {names!r}"
        )
        assert "file_read" in names, "the turn still composes cleanly for the persona's other tool"
    finally:
        current_user_id.reset(token)
        await _teardown(factory, rls_engine, su_url, owner=owner)

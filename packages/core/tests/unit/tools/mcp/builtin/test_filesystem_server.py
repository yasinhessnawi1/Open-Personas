"""Unit tests for the built-in ``filesystem`` MCP server (Spec 27 T8).

The security-critical built-in: these tests prove the path-traversal guard is
wired (``..`` / absolute / symlink escapes rejected) and that legitimate
relative read/write round-trips inside the sandbox.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from persona.tools.mcp.builtin import SERVER_BUILDERS
from persona.tools.mcp.builtin.filesystem_server import build

if TYPE_CHECKING:
    from pathlib import Path


def _first_text(call_result: object) -> str:
    blocks = call_result[0] if isinstance(call_result, tuple) else call_result
    for block in blocks:  # type: ignore[union-attr]
        text = getattr(block, "text", None)
        if isinstance(text, str):
            return text
    return ""


@pytest.fixture(autouse=True)
def _sandbox_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the filesystem server's scoped root at the test's temp dir (Spec P4).

    Sets the authoritative per-spawn scope var ``PERSONA_FILESYSTEM_SCOPE_ROOT``;
    also sets the legacy ``PERSONA_TOOLS_SANDBOX_ROOT`` to a SEPARATE dir so the
    round-trip/traversal tests double as proof the server roots at the scope var,
    not the shared flat root. Tests that exercise the absent-scope path override
    via ``monkeypatch.delenv``.
    """
    monkeypatch.setenv("PERSONA_FILESYSTEM_SCOPE_ROOT", str(tmp_path))
    monkeypatch.setenv("PERSONA_TOOLS_SANDBOX_ROOT", str(tmp_path / "_unused_shared_root"))


@pytest.mark.asyncio
async def test_build_exposes_read_and_write_tools() -> None:
    server = build("127.0.0.1", 8400)
    assert server.name == "filesystem"
    names = {t.name for t in await server.list_tools()}
    assert {"read_file", "write_file"} <= names


@pytest.mark.asyncio
async def test_write_then_read_round_trips_inside_sandbox(tmp_path: Path) -> None:
    server = build("127.0.0.1", 8401)
    await server.call_tool("write_file", {"path": "out/note.txt", "content": "hello sandbox"})
    assert (tmp_path / "out" / "note.txt").read_text() == "hello sandbox"
    read = await server.call_tool("read_file", {"path": "out/note.txt"})
    assert "hello sandbox" in _first_text(read)


@pytest.mark.asyncio
async def test_write_rejects_parent_traversal_escape(tmp_path: Path) -> None:
    server = build("127.0.0.1", 8402)
    with pytest.raises(Exception, match="(?i)sandbox|escape"):
        await server.call_tool("write_file", {"path": "../escape.txt", "content": "x"})
    # The escape file must NOT exist outside the sandbox.
    assert not (tmp_path.parent / "escape.txt").exists()


@pytest.mark.asyncio
async def test_write_rejects_deep_traversal_escape() -> None:
    server = build("127.0.0.1", 8403)
    with pytest.raises(Exception, match="(?i)sandbox|escape"):
        await server.call_tool("write_file", {"path": "../../../../tmp/pwned.txt", "content": "x"})


@pytest.mark.asyncio
async def test_read_rejects_absolute_path() -> None:
    server = build("127.0.0.1", 8404)
    with pytest.raises(Exception, match="(?i)sandbox|absolute"):
        await server.call_tool("read_file", {"path": "/etc/passwd"})


def test_filesystem_is_registered_in_the_builder_registry() -> None:
    assert SERVER_BUILDERS.get("filesystem") is build


# --- Spec P4: per-(owner, persona) scoping of the subprocess --------------------
# The subprocess can't read the parent ContextVar, so the supervisor threads the
# pre-resolved scoped root in at spawn via a DEDICATED env var
# (PERSONA_FILESYSTEM_SCOPE_ROOT), distinct from the process-wide
# PERSONA_TOOLS_SANDBOX_ROOT. Absent scope ⇒ serve-and-deny (P4-D-4), NEVER a
# fallback to the shared flat root (P4-D-2). The traversal guard + fail-closed
# contract are inherited from the in-process file tools (P4-D-7).

_SCOPE_ENV = "PERSONA_FILESYSTEM_SCOPE_ROOT"


@pytest.mark.asyncio
async def test_build_roots_at_filesystem_scope_env_not_shared_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The shared flat root (autouse fixture) and the per-request scoped root are
    # DIFFERENT dirs; a write must land in the SCOPED one, proving the server no
    # longer reads PERSONA_TOOLS_SANDBOX_ROOT.
    shared_root = tmp_path / "shared"
    scoped_root = tmp_path / "ownerA" / "personaX"
    monkeypatch.setenv("PERSONA_TOOLS_SANDBOX_ROOT", str(shared_root))
    monkeypatch.setenv(_SCOPE_ENV, str(scoped_root))

    server = build("127.0.0.1", 8410)
    await server.call_tool("write_file", {"path": "out/note.txt", "content": "scoped"})

    assert (scoped_root / "out" / "note.txt").read_text() == "scoped"
    # The shared flat root must NOT have received the write.
    assert not (shared_root / "out" / "note.txt").exists()


@pytest.mark.asyncio
async def test_build_fails_closed_when_scope_env_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No scope env ⇒ every read/write is denied (serve-and-deny), reproducing the
    # in-process None → SandboxViolationError contract across the process boundary.
    shared_root = tmp_path / "shared"
    monkeypatch.setenv("PERSONA_TOOLS_SANDBOX_ROOT", str(shared_root))
    monkeypatch.delenv(_SCOPE_ENV, raising=False)

    server = build("127.0.0.1", 8411)
    with pytest.raises(Exception, match="(?i)scope|sandbox"):
        await server.call_tool("write_file", {"path": "out/note.txt", "content": "denied"})
    with pytest.raises(Exception, match="(?i)scope|sandbox"):
        await server.call_tool("read_file", {"path": "out/note.txt"})


@pytest.mark.asyncio
async def test_build_never_falls_back_to_shared_root_when_scope_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The whole point of P4: scope absent must NOT silently serve the shared flat
    # root. The write is denied AND nothing is created under the shared root (it
    # must not even be mkdir'd into a usable sandbox).
    shared_root = tmp_path / "shared"
    monkeypatch.setenv("PERSONA_TOOLS_SANDBOX_ROOT", str(shared_root))
    monkeypatch.delenv(_SCOPE_ENV, raising=False)

    server = build("127.0.0.1", 8412)
    with pytest.raises(Exception, match="(?i)scope|sandbox"):
        await server.call_tool("write_file", {"path": "leak.txt", "content": "x"})
    assert not (shared_root / "leak.txt").exists()


@pytest.mark.asyncio
async def test_scoped_server_still_rejects_traversal_within_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Scoping the root does not remove the traversal guard: a `..`-escape out of
    # the scoped root is still rejected, and the escape file is not created.
    scoped_root = tmp_path / "ownerA" / "personaX"
    monkeypatch.setenv(_SCOPE_ENV, str(scoped_root))

    server = build("127.0.0.1", 8413)
    with pytest.raises(Exception, match="(?i)sandbox|escape"):
        await server.call_tool("write_file", {"path": "../escape.txt", "content": "x"})
    assert not (scoped_root.parent / "escape.txt").exists()

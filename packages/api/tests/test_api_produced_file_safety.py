"""Produced files cannot be written outside the persona workspace (Spec WIN T1.5, H-1).

Code in the sandbox can create any Linux file name. Joined onto a host path, a name
with backslashes, a drive, ``..`` or a colon becomes, on Windows, a write anywhere on
the machine (a Startup folder: code execution at next logon) or an alternate data
stream; and a link planted where the file is saved would be followed on any platform.

Two layers are proven here, on every platform: discovery refuses the dangerous names
before any host path exists, and the persister, on its own, refuses them too (and a
planted link) with a human message to the model, while ordinary files still land.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from persona.sandbox.result import (
    ExecutionResult,
    NetworkPolicy,
    RefusedFile,
    ResourceLimits,
    SandboxFile,
)
from persona.tools._sandbox import write_file_under_root
from persona_api.sandbox import (
    SandboxRequestContext,
    make_pool_code_execution_tool,
    reset_sandbox_request_context,
    set_sandbox_request_context,
)
from persona_api.sandbox.hosted import _HOSTED_WORKSPACE_OUT, HostedSandbox
from persona_api.sandbox.pool import SandboxPool

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

BS = chr(92)
PAYLOAD = b"@echo pwned"


class _CopyingSandbox:
    """A sandbox fake whose copy writes like the real backends: under ``root``, no-follow."""

    def __init__(self) -> None:
        self.produced_files: tuple[SandboxFile, ...] = ()
        self.refused_files: tuple[RefusedFile, ...] = ()

    async def execute(
        self,
        code: str,  # noqa: ARG002 - protocol
        *,
        language: str = "python",  # noqa: ARG002 - protocol
        session_id: str | None = None,  # noqa: ARG002 - protocol
        timeout_s: float = 30.0,  # noqa: ARG002 - protocol
        limits: ResourceLimits | None = None,  # noqa: ARG002 - protocol
        network: NetworkPolicy | None = None,  # noqa: ARG002 - protocol
        input_files: list[SandboxFile] | None = None,  # noqa: ARG002 - protocol
    ) -> ExecutionResult:
        return ExecutionResult(
            stdout="",
            stderr="",
            exit_status=0,
            outcome="ok",
            produced_files=self.produced_files,
            refused_files=self.refused_files,
        )

    async def create_session(
        self,
        session_id: str,  # noqa: ARG002 - protocol
        *,
        limits: ResourceLimits,  # noqa: ARG002 - protocol
        network: NetworkPolicy,  # noqa: ARG002 - protocol
    ) -> None:
        return None

    async def destroy_session(self, session_id: str) -> None:  # noqa: ARG002 - protocol
        return None

    async def aclose(self) -> None:
        return None

    async def copy_produced_file_to(
        self,
        session_id: str,  # noqa: ARG002 - protocol
        ref: str,  # noqa: ARG002 - protocol
        target_path: Path,
        *,
        root: Path,
    ) -> None:
        write_file_under_root(target_path, PAYLOAD, root=root)

    async def read_produced_file_bytes(self, session_id: str, ref: str) -> bytes:  # noqa: ARG002
        return PAYLOAD


@pytest_asyncio.fixture
async def pool() -> AsyncIterator[tuple[SandboxPool, _CopyingSandbox]]:
    fake = _CopyingSandbox()
    sandbox_pool = SandboxPool(
        sandbox=fake,
        max_per_user=2,
        idle_timeout_s=60.0,
        reap_interval_s=60.0,
    )
    try:
        yield sandbox_pool, fake
    finally:
        await sandbox_pool.aclose()


def _layout(tmp_path: Path) -> tuple[Path, Path]:
    """A workspace four folders deep, so every ``..`` escape stays inside ``tmp_path``."""
    workspace_root = tmp_path / "a" / "b" / "c" / "workspaces"
    workspace_root.mkdir(parents=True)
    return workspace_root, workspace_root / "alice" / "persona-A"


def _files_under(base: Path) -> set[str]:
    return {
        Path(folder, name).relative_to(base).as_posix()
        for folder, _dirs, names in os.walk(base)
        for name in names
    }


async def _run(sandbox_pool: SandboxPool, workspace_root: Path) -> tuple[bool, str]:
    tool = make_pool_code_execution_tool(
        pool=sandbox_pool,
        rls_engine=object(),  # type: ignore[arg-type]
        persona_id="persona-A",
        workspace_root=workspace_root,
    )
    token = set_sandbox_request_context(
        SandboxRequestContext(owner_id="alice", conversation_id="c-1")
    )
    try:
        result = await tool.execute(code="print('hi')")
    finally:
        reset_sandbox_request_context(token)
    return result.is_error, result.content


def _attack_names(tmp_path: Path) -> list[str]:
    # A drive-absolute Windows path (aimed inside tmp_path, never at a real Startup
    # folder, so a mutation run can do no harm), backslash and slash escapes.
    windows_absolute = str(tmp_path / "startup" / "x.bat").replace("/", BS)
    return [
        windows_absolute,
        f"..{BS}..{BS}..{BS}..{BS}evil.bat",
        "../../../../evil.bat",
        "../../../../../../../../evil.bat",
    ]


@pytest.mark.asyncio
async def test_the_persister_refuses_dangerous_names_and_writes_nothing_outside(
    pool: tuple[SandboxPool, _CopyingSandbox], tmp_path: Path
) -> None:
    """Defence in depth: even if discovery let them through, the persister refuses."""
    sandbox_pool, fake = pool
    workspace_root, persona_workspace = _layout(tmp_path)
    attacks = _attack_names(tmp_path)
    fake.produced_files = (
        *(SandboxFile(path=name, size_bytes=5) for name in attacks),
        SandboxFile(path="chart.png", size_bytes=5, media_type="image/png"),
    )
    is_error, content = await _run(sandbox_pool, workspace_root)
    assert is_error
    assert "these produced files were not saved" in content
    assert "Save files under plain relative names" in content
    assert (persona_workspace / "uploads" / "chart.png").read_bytes() == PAYLOAD
    written = _files_under(tmp_path)
    assert written == {"a/b/c/workspaces/alice/persona-A/uploads/chart.png"} | {
        name for name in written if name.endswith(".f5.json")
    }, written


@pytest.mark.asyncio
async def test_a_link_planted_where_a_produced_file_is_saved_is_refused(
    pool: tuple[SandboxPool, _CopyingSandbox], tmp_path: Path
) -> None:
    sandbox_pool, fake = pool
    workspace_root, persona_workspace = _layout(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"ORIGINAL")
    (persona_workspace / "uploads").mkdir(parents=True)
    try:
        (persona_workspace / "uploads" / "chart.png").symlink_to(outside)
    except OSError as exc:
        if os.environ.get("CI"):
            pytest.fail(f"CI must be able to create symbolic links: {exc}")
        pytest.skip(f"creating a symbolic link needs Developer Mode or elevation here: {exc}")
    fake.produced_files = (SandboxFile(path="chart.png", size_bytes=5, media_type="image/png"),)
    is_error, content = await _run(sandbox_pool, workspace_root)
    assert is_error
    assert "chart.png" in content
    assert "were not saved" in content
    assert outside.read_bytes() == b"ORIGINAL"


@pytest.mark.asyncio
async def test_discovery_refusals_reach_the_model_as_a_human_note(
    pool: tuple[SandboxPool, _CopyingSandbox], tmp_path: Path
) -> None:
    sandbox_pool, fake = pool
    workspace_root, persona_workspace = _layout(tmp_path)
    fake.refused_files = (RefusedFile(path="x.png:hidden", reason="the name contains a colon"),)
    fake.produced_files = (SandboxFile(path="ok.txt", size_bytes=5),)
    is_error, content = await _run(sandbox_pool, workspace_root)
    assert is_error
    assert "x.png:hidden (the name contains a colon)" in content
    assert (persona_workspace / "uploads" / "ok.txt").read_bytes() == PAYLOAD


# --- discovery on the hosted backend -----------------------------------------------------
class _FileType(Enum):
    FILE = "file"


@dataclass
class _Entry:
    name: str
    type: _FileType
    path: str
    size: int


class _Files:
    def __init__(self, entries: list[_Entry]) -> None:
        self._entries = entries

    def list(self, path: str, *, depth: int | None = None) -> list[_Entry]:  # noqa: A003, ARG002
        return list(self._entries)


class _Execution:
    def __init__(self) -> None:
        class _Logs:
            stdout: list[str] = []
            stderr: list[str] = []

        self.logs = _Logs()
        self.error = None


class _E2B:
    def __init__(self, files: _Files) -> None:
        self.files = files

    def run_code(self, code: str, *, timeout: float) -> _Execution:  # noqa: ARG002
        return _Execution()


def test_hosted_discovery_refuses_every_dangerous_name_on_every_platform() -> None:
    names = {
        f"C:{BS}Users{BS}victim{BS}AppData{BS}Roaming{BS}Startup{BS}x.bat": "backslash",
        f"..{BS}..{BS}..{BS}..{BS}evil.bat": "backslash",
        "x.png:hidden": "colon",
        "bad\x07name.txt": "control characters",
        "chart.png": None,
        "charts/sales.png": None,
    }
    entries = [
        _Entry(name=n, type=_FileType.FILE, path=f"{_HOSTED_WORKSPACE_OUT}/{n}", size=3)
        for n in names
    ]
    result = HostedSandbox()._run_and_marshal(  # noqa: SLF001
        _E2B(_Files(entries)),
        "x",
        timeout_s=5.0,
        input_files=[],
        limits=ResourceLimits(),
    )
    assert {f.path for f in result.produced_files} == {"chart.png", "charts/sales.png"}
    refused = {r.path: r.reason for r in result.refused_files}
    assert set(refused) == {
        n.replace("\x07", "") for n, expected in names.items() if expected is not None
    }
    for name, expected in names.items():
        if expected is not None:
            assert expected in refused[name.replace("\x07", "")], (name, refused)

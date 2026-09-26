"""Guards on every production use of the sandbox opener and resolver (Spec WIN, T1.4).

1. Every call to a no-follow helper that takes a workspace root passes one. With
   ``root=None`` the Windows opener falls back to the file's own folder, which gives
   only the final-component guarantee; production code must never rely on that.
2. Code that resolves a sandbox path never touches the result with a plain filesystem
   call (``mkdir``, ``unlink``, ``rglob``, ``open``, ...), which would follow links:
   everything goes through the no-follow helpers.

Both are read from the source, so a new call site is covered the day it is written.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[5]
SOURCE_FILES = sorted(REPO.glob("packages/*/src/**/*.py"))

ROOTED_HELPERS = frozenset(
    {
        "open_nofollow",
        "read_nofollow_bytes",
        "write_nofollow_bytes",
        "is_regular_file_nofollow",
        "make_dirs_nofollow",
        "delete_regular_file_nofollow",
        "read_file_under_root",
        "write_file_under_root",
        "copy_produced_file_to",
        "write_artifact_sidecar",
        "read_artifact_sidecar",
        "delete_artifact_sidecar",
    }
)

#: Path / builtin operations that follow links, forbidden next to a resolved path.
FOLLOWING_CALLS = frozenset(
    {
        "mkdir",
        "makedirs",
        "unlink",
        "remove",
        "rmdir",
        "rglob",
        "glob",
        "iterdir",
        "scandir",
        "listdir",
        "walk",
        "read_bytes",
        "write_bytes",
        "read_text",
        "write_text",
        "touch",
        "rename",
        "replace",
        "is_file",
        "is_dir",
        "exists",
        "stat",
        "open",
    }
)


def _called_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _rooted_calls() -> list[tuple[str, int, ast.Call]]:
    found: list[tuple[str, int, ast.Call]] = []
    for path in SOURCE_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _called_name(node) in ROOTED_HELPERS:
                found.append((path.relative_to(REPO).as_posix(), node.lineno, node))
    return found


def test_the_scan_sees_the_known_call_sites() -> None:
    """Non-vacuous: the scan must find the call sites this guard exists for."""
    files = {name for name, _, _ in _rooted_calls()}
    for expected in (
        "packages/core/src/persona/tools/builtin/file_read.py",
        "packages/core/src/persona/tools/builtin/file_write.py",
        "packages/api/src/persona_api/storage.py",
        "packages/api/src/persona_api/services/chat_service.py",
    ):
        assert expected in files, f"{expected} no longer calls the no-follow helpers"
    assert len(_rooted_calls()) >= 12


def test_every_production_call_passes_the_workspace_root() -> None:
    missing = []
    for name, line, call in _rooted_calls():
        root = next((kw.value for kw in call.keywords if kw.arg == "root"), None)
        if root is None or (isinstance(root, ast.Constant) and root.value is None):
            missing.append(f"{name}:{line}")
    assert not missing, (
        "these calls omit root=, so on Windows they get only the final-component "
        f"guarantee instead of refusing a link anywhere below the workspace root: {missing}"
    )


def _outermost_scopes_resolving_a_sandbox_path() -> list[tuple[str, ast.AST]]:
    scopes: list[tuple[str, ast.AST]] = []
    for path in SOURCE_FILES:
        if path.name == "_sandbox.py":
            continue  # the helpers themselves hold the POSIX branches
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for top in tree.body:
            if not isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if any(
                isinstance(node, ast.Call) and _called_name(node) == "resolve_sandbox_path"
                for node in ast.walk(top)
            ):
                scopes.append((f"{path.relative_to(REPO).as_posix()}:{top.name}", top))
    return scopes


def test_code_that_resolves_a_sandbox_path_uses_only_no_follow_file_operations() -> None:
    scopes = _outermost_scopes_resolving_a_sandbox_path()
    names = {name for name, _ in scopes}
    assert any("storage.py:LocalFileStorage" in n for n in names), names
    assert any("file_write.py" in n for n in names), names
    offending = []
    for name, scope in scopes:
        for node in ast.walk(scope):
            if isinstance(node, ast.Call) and _called_name(node) in FOLLOWING_CALLS:
                offending.append(f"{name} line {node.lineno}: {_called_name(node)}()")
    assert not offending, (
        "a resolved sandbox path must be touched only through the no-follow helpers "
        f"(a plain call follows links): {offending}"
    )

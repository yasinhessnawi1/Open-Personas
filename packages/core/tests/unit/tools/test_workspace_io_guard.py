"""Every read or write under a workspace root goes through the no-follow layer (Spec WIN, T1.5).

A plain ``Path`` / ``open`` / ``os`` / ``shutil`` call follows links: a link planted in a
workspace can then redirect a write out of it (H-1: produced files), feed an outside
file to a model (M-1: replayed images), or, on Windows, make the machine contact a
network host. This guard reads every production module and fails on any
link-following call

1. on a path derived from a workspace root (``workspace_root``, ``persona_workspace``,
   ``file_read_root``, ...), in any module; or
2. anywhere in a module listed as doing workspace I/O,

unless that exact call is allowlisted below with the reason it is safe. The self-tests
at the bottom feed the guard the code H-1 and M-1 shipped with and require it to fail,
so the guard can never pass while seeing nothing.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[5]

#: Names (variables, parameters, attributes) that hold a workspace root.
WORKSPACE_ROOT_NAMES = frozenset(
    {"workspace_root", "_workspace_root", "persona_workspace", "file_read_root", "persona_root"}
)

#: Modules that do workspace I/O: every link-following call in them is checked.
WORKSPACE_IO_MODULES = frozenset(
    {
        "packages/api/src/persona_api/sandbox/hosted.py",
        "packages/api/src/persona_api/sandbox/runtime_tool.py",
        "packages/api/src/persona_api/services/artifact_metadata.py",
        "packages/api/src/persona_api/jobs/handlers/file_extract.py",
        "packages/api/src/persona_api/storage.py",
        "packages/core/src/persona/tools/builtin/file_read.py",
        "packages/core/src/persona/tools/builtin/file_write.py",
        "packages/core/src/persona/backends/openai_compat.py",
        "packages/core/src/persona/backends/ollama.py",
        "packages/core/src/persona/sandbox/local_docker.py",
    }
)

#: ``Path``-style methods that follow links (or create, list or remove through them).
FOLLOWING_METHODS = frozenset(
    {
        "mkdir",
        "unlink",
        "rmdir",
        "rglob",
        "glob",
        "iterdir",
        "read_bytes",
        "write_bytes",
        "read_text",
        "write_text",
        "touch",
        "is_file",
        "is_dir",
        "exists",
        "stat",
        "symlink_to",
        "hardlink_to",
        "open",
    }
)
#: The same through ``os`` / ``shutil`` functions.
FOLLOWING_MODULE_FUNCTIONS = frozenset(
    {
        ("os", "open"),
        ("os", "remove"),
        ("os", "unlink"),
        ("os", "makedirs"),
        ("os", "mkdir"),
        ("os", "rmdir"),
        ("os", "listdir"),
        ("os", "scandir"),
        ("os", "walk"),
        ("os", "stat"),
        ("shutil", "copyfile"),
        ("shutil", "copy"),
        ("shutil", "copy2"),
        ("shutil", "copytree"),
        ("shutil", "move"),
        ("shutil", "rmtree"),
    }
)

#: ``(module, function, call)`` that are safe, each with its reason.
ALLOWLIST: dict[tuple[str, str, str], str] = {
    (
        "packages/api/src/persona_api/services/persona_service.py",
        "delete_persona",
        "exists",
    ): "probes the persona's own folder before removing it; a link there is removed, "
    "not followed, by rmtree (next entry)",
    (
        "packages/api/src/persona_api/services/persona_service.py",
        "delete_persona",
        "rmtree",
    ): "shutil.rmtree on Python 3.12 removes symlinks and junctions without following "
    "them (verified on Windows with a junction, a nested junction, a directory symlink "
    "and a file symlink: every outside file survived)",
    (
        "packages/api/src/persona_api/app.py",
        "_lifespan",
        "mkdir",
    ): "creates the workspace root itself, which is operator configuration (trusted)",
    (
        "packages/core/src/persona/sandbox/local_docker.py",
        "__init__",
        "exists",
    ): "the local sandbox's own workspace root (operator configuration), not a persona workspace",
    (
        "packages/core/src/persona/sandbox/local_docker.py",
        "__init__",
        "mkdir",
    ): "creates the local sandbox's own workspace root (operator configuration)",
    (
        "packages/core/src/persona/sandbox/local_docker.py",
        "_make_workspace_dirs",
        "mkdir",
    ): "creates the local sandbox's own per-execution in/ and out/ scratch folders under "
    "its own root, with fresh names (exist_ok=False); no persona workspace is touched",
    (
        "packages/core/src/persona/sandbox/local_docker.py",
        "_make_session_workspace_dirs",
        "mkdir",
    ): "creates the local sandbox's own per-session in/ and out/ scratch folders under "
    "its own root; no persona workspace is touched",
    (
        "packages/core/src/persona/sandbox/local_docker.py",
        "_cleanup_workspace_dirs",
        "rmtree",
    ): "removes the sandbox's own per-execution scratch folders; shutil.rmtree on Python "
    "3.12 removes links inside them without following them",
    (
        "packages/core/src/persona/sandbox/local_docker.py",
        "_cleanup_workspace_dirs",
        "rmdir",
    ): "removes the sandbox's own empty per-execution folder (rmdir never follows a link)",
    (
        "packages/core/src/persona/sandbox/local_docker.py",
        "_seed_workspace",
        "write_text",
    ): "writes the code script into the sandbox's own in/ folder, which the container "
    "mounts read-only, so no link in it can have been planted by the code",
    (
        "packages/core/src/persona/sandbox/local_docker.py",
        "_seed_workspace",
        "mkdir",
    ): "creates folders for input files in the sandbox's own read-only-mounted in/ folder",
    (
        "packages/core/src/persona/sandbox/local_docker.py",
        "_seed_workspace",
        "write_bytes",
    ): "writes server-chosen input files into the sandbox's own read-only-mounted in/ folder",
}


def _call_name(call: ast.Call) -> tuple[str | None, str | None]:
    """``(owner, name)``: ``("os", "remove")`` for ``os.remove(...)``, ``(None, "mkdir")``
    for ``x.mkdir(...)``, ``(None, "open")`` for ``open(...)``."""
    func = call.func
    if isinstance(func, ast.Name):
        return None, func.id
    if isinstance(func, ast.Attribute):
        owner = func.value.id if isinstance(func.value, ast.Name) else None
        return owner, func.attr
    return None, None


def _mentions(node: ast.AST, names: set[str]) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id in names:
            return True
        if isinstance(sub, ast.Attribute) and sub.attr in names:
            return True
    return False


def _tainted_names(function: ast.AST) -> set[str]:
    """Workspace-root names plus every local assigned or looped from one (to a fixpoint)."""
    tainted = set(WORKSPACE_ROOT_NAMES)
    changed = True
    while changed:
        changed = False
        for node in ast.walk(function):
            targets: list[ast.AST] = []
            value: ast.AST | None = None
            if isinstance(node, ast.Assign):
                targets, value = list(node.targets), node.value
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and node.value is not None:
                targets, value = [node.target], node.value
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                targets, value = [node.target], node.iter
            elif isinstance(node, ast.NamedExpr):
                targets, value = [node.target], node.value
            if value is None or not _mentions(value, tainted):
                continue
            for target in targets:
                for sub in ast.walk(target):
                    if isinstance(sub, ast.Name) and sub.id not in tainted:
                        tainted.add(sub.id)
                        changed = True
    return tainted


def _is_following(call: ast.Call) -> bool:
    owner, name = _call_name(call)
    if owner in ("self", "cls"):
        return False  # a method of the object itself (e.g. a storage's own exists()), not a path
    if owner is None and name == "open" and isinstance(call.func, ast.Name):
        return True
    if (owner, name) in FOLLOWING_MODULE_FUNCTIONS:
        return True
    return (
        isinstance(call.func, ast.Attribute)
        and name in FOLLOWING_METHODS
        and owner
        not in (
            "os",
            "shutil",
        )
    )


def _call_subjects(call: ast.Call) -> list[ast.AST]:
    subjects: list[ast.AST] = [*call.args, *(kw.value for kw in call.keywords)]
    if isinstance(call.func, ast.Attribute):
        subjects.append(call.func.value)
    return subjects


def workspace_io_violations(module: str, source: str) -> list[str]:
    """Every unallowlisted link-following call on a workspace path in ``source``."""
    tree = ast.parse(source)
    strict = module in WORKSPACE_IO_MODULES
    found: list[str] = []
    functions = [
        node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    for function in functions:
        tainted = _tainted_names(function)
        for node in ast.walk(function):
            if not isinstance(node, ast.Call) or not _is_following(node):
                continue
            on_workspace = any(_mentions(subject, tainted) for subject in _call_subjects(node))
            if not (strict or on_workspace):
                continue
            name = _call_name(node)[1] or "?"
            if (module, function.name, name) in ALLOWLIST:
                continue
            found.append(f"{module}:{node.lineno} {function.name}() calls {name}()")
    return sorted(set(found))


def test_no_workspace_path_is_touched_by_a_link_following_call() -> None:
    sources = sorted(REPO.glob("packages/*/src/**/*.py"))
    assert len(sources) > 200, "the scan must see the whole source tree"
    found: list[str] = []
    for path in sources:
        module = path.relative_to(REPO).as_posix()
        found.extend(workspace_io_violations(module, path.read_text(encoding="utf-8")))
    assert not found, (
        "a workspace path must be read or written through the no-follow helpers "
        "(resolve_sandbox_path, read_nofollow_bytes, write_file_under_root, ...), or the "
        f"call allowlisted here with its reason: {found}"
    )


def test_every_workspace_io_module_and_allowlisted_call_exists() -> None:
    """Non-vacuous and never stale: a renamed module or call must be updated here."""
    for module in WORKSPACE_IO_MODULES:
        assert (REPO / module).is_file(), module
    for module, function, call in ALLOWLIST:
        tree = ast.parse((REPO / module).read_text(encoding="utf-8"))
        assert any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == function
            and any(
                isinstance(sub, ast.Call) and _call_name(sub)[1] == call for sub in ast.walk(node)
            )
            for node in ast.walk(tree)
        ), f"allowlisted {module} {function}() {call}() no longer exists"


# --- the guard catches the code that shipped H-1 and M-1 --------------------------------
_H1_RUNTIME_TOOL = """
async def _persist(session_id, ref):
    persona_workspace = _resolve_persona_workspace()
    target = persona_workspace / "uploads" / ref
    await pool.sandbox.copy_produced_file_to(session_id, ref, target)
    write_artifact_sidecar(target, meta)
"""
_H1_HOSTED = """
class HostedSandbox:
    @staticmethod
    def _write_bytes(target_path, data):
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(data)
"""
_H1_STAGING = """
def _augmented_input_files_provider():
    persona_workspace = _resolve_persona_workspace()
    intermediate_dir = persona_workspace / "intermediate"
    for path in sorted(intermediate_dir.rglob("*")):
        content = path.read_bytes()
"""
_M1_OPENAI = """
def _image_bytes(block, workspace_root):
    return (workspace_root / block.workspace_path).read_bytes()
"""
_M1_OLLAMA = """
class OllamaBackend:
    def _messages(self, block):
        image_bytes = (self._workspace_root / block.workspace_path).read_bytes()
"""


def test_the_guard_fails_on_the_code_that_shipped_h1() -> None:
    assert workspace_io_violations("packages/api/src/persona_api/sandbox/hosted.py", _H1_HOSTED)
    assert workspace_io_violations(
        "packages/api/src/persona_api/sandbox/runtime_tool.py", _H1_STAGING
    )
    # The produced-file destination is computed in one module and written in another:
    # the strict module list is what catches the writer.
    assert workspace_io_violations("elsewhere.py", _H1_STAGING)


def test_the_guard_fails_on_the_code_that_shipped_m1() -> None:
    assert workspace_io_violations(
        "packages/core/src/persona/backends/openai_compat.py", _M1_OPENAI
    )
    assert workspace_io_violations("elsewhere.py", _M1_OPENAI)
    assert workspace_io_violations("packages/core/src/persona/backends/ollama.py", _M1_OLLAMA)
    assert workspace_io_violations("elsewhere.py", _M1_OLLAMA)


_X1_LOCAL_DISCOVERY = """
class LocalDockerSandbox:
    @staticmethod
    def _discover_produced_files(host_out, limits):
        for path in sorted(host_out.rglob("*")):
            if not path.is_file():
                continue
            size = path.stat().st_size

    @staticmethod
    def _copy_produced_sync(source, target_path, session_id, ref, root):
        if not source.is_file():
            raise CodeSandboxError()
        write_file_under_root(target_path, source.read_bytes(), root=root)
"""


def test_the_guard_fails_on_the_code_that_shipped_x1() -> None:
    found = workspace_io_violations(
        "packages/core/src/persona/sandbox/local_docker.py", _X1_LOCAL_DISCOVERY
    )
    assert {v.rsplit(" calls ", 1)[1] for v in found} >= {
        "rglob()",
        "is_file()",
        "stat()",
        "read_bytes()",
    }

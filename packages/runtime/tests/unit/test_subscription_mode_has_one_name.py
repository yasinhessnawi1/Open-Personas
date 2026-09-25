"""The OpenRouter mode setting has one name in the product code (R9-224 on R9-213).

R9-213 Part 2 made ``SUBSCRIPTION_MODE_ENV`` public so the deploy, the boot line and
:data:`~persona_runtime.chain_report.UNIFIED_ENV_NAMES` agree on it; R9-224 had added a
second public constant for the same setting in the same module, and the rebase kept
main's. This pins that there is one: the resolver reads it, the report lists it, and
voice's ERROR line names it, all through the same constant.

An identity check cannot prove that: CPython interns identifier-like string literals,
so two separate copies of the name compare ``is``-identical. So the guard is
structural. It scans every production module's AST and requires that the name appears
as a string literal (docstrings aside) exactly once in the whole product, in the
constant's definition, so a second constant in the same module fails as surely as a
copy elsewhere; and that each consumer imports the constant rather than spelling it.

Out of scope, deliberately: ``scripts/check_deploy_wiring.py`` and
``scripts/fly_deploy_chains.py`` keep their own copies because they must not import the
runtime, and ``scripts/tests`` already fails if those copies differ from
``UNIFIED_ENV_NAMES``.
"""

from __future__ import annotations

import ast
from pathlib import Path

from persona_runtime.chain_report import UNIFIED_ENV_NAMES
from persona_runtime.openrouter_subscription import SUBSCRIPTION_MODE_ENV

_NAME = "PERSONA_OPENROUTER_SUBSCRIPTION_MODE"
_HOME = "packages/runtime/src/persona_runtime/openrouter_subscription.py"
_CONSUMERS = (
    "packages/runtime/src/persona_runtime/chain_report.py",
    "packages/voice/src/persona_voice/agent/openrouter_mode.py",
)
_MIN_MODULES_SCANNED = 200


def _repo_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "packages").is_dir() and (candidate / "pyproject.toml").is_file():
            return candidate
    msg = "could not locate the repository root from the test file"
    raise AssertionError(msg)


def _production_modules() -> dict[str, ast.Module]:
    root = _repo_root()
    paths = sorted(root.glob("packages/*/src/**/*.py"))
    assert len(paths) >= _MIN_MODULES_SCANNED, f"only {len(paths)} modules scanned"
    return {
        path.relative_to(root).as_posix(): ast.parse(path.read_text(encoding="utf-8"))
        for path in paths
    }


def _spellings(tree: ast.Module) -> int:
    """How many string literals other than docstrings contain the setting's name."""
    docstrings = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and _NAME in node.value
        and id(node) not in docstrings
    )


def _imports_the_constant(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == "persona_runtime.openrouter_subscription"
        and any(alias.name == "SUBSCRIPTION_MODE_ENV" for alias in node.names)
        for node in ast.walk(tree)
    )


def test_the_mode_setting_is_spelled_once_and_imported_everywhere_else() -> None:
    modules = _production_modules()

    spellings = {module: n for module, tree in modules.items() if (n := _spellings(tree))}
    assert spellings == {_HOME: 1}
    assert [module for module in _CONSUMERS if not _imports_the_constant(modules[module])] == []


def test_the_constant_is_the_one_the_deploy_set_lists() -> None:
    assert SUBSCRIPTION_MODE_ENV == _NAME
    assert UNIFIED_ENV_NAMES.count(SUBSCRIPTION_MODE_ENV) == 1

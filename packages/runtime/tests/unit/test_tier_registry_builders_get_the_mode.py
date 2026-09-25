"""Every tier registry is built with the OpenRouter subscription mode (R9-224).

The api and the connector service passed the mode to the registry builders; voice
called the same builders with no mode at all, so a chain the api filtered to its
``:free`` models ran unfiltered on a call. The argument is optional (a bare
``tier_registry_from_env()`` is the documented quick start), so nothing but this
guard stops the next composition root from forgetting it the same way.

It walks the AST of every production module rather than grepping, so a docstring
cannot satisfy it, and it follows ``from ... import ... as ...`` so an alias cannot
slip past it. A mode passed as a literal (``None`` or a hard-coded ``"paid"``) is a
failure too: the point is that the value comes from the one resolver.

It cannot pass while seeing nothing: the scan must cover a floor of modules, every
known composition root must be among the calls it found (so a package dropping out
of the scan fails), and the detector itself is proven on small sources first. It
checks every call rather than a count, so a new call site must comply without
anyone editing a number.
"""

from __future__ import annotations

import ast
from pathlib import Path

_BUILDERS = frozenset({"tier_registry_from_env", "free_tier_registry_from_env"})
_MODE_KEYWORD = "openrouter_subscription_mode"
_RESOLVER = "resolve_openrouter_subscription_mode"
_RESOLVER_HOME = "packages/runtime/src/persona_runtime/openrouter_subscription.py"

#: The composition roots that build tier registries today, each with the builder it
#: calls. All must be FOUND; finding more is fine (they are checked like the rest).
_KNOWN_SITES = frozenset(
    {
        ("packages/api/src/persona_api/app.py", "tier_registry_from_env"),
        ("packages/api/src/persona_api/services/model_tiers.py", "free_tier_registry_from_env"),
        ("packages/connectors/src/persona_connectors/service.py", "tier_registry_from_env"),
        ("packages/voice/src/persona_voice/agent/launcher.py", "tier_registry_from_env"),
        ("packages/voice/src/persona_voice/agent/launcher.py", "free_tier_registry_from_env"),
        ("packages/voice/src/persona_voice/agent/runner.py", "tier_registry_from_env"),
    }
)

#: A floor, not a count: high enough that a broken scan (wrong root, renamed package
#: layout) cannot pass as a clean one.
_MIN_MODULES_SCANNED = 200


def _repo_root() -> Path:
    """The worktree root, found by walking up to the directory holding ``packages/``."""
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "packages").is_dir() and (candidate / "pyproject.toml").is_file():
            return candidate
    msg = "could not locate the repository root from the test file"
    raise AssertionError(msg)


def _production_modules() -> dict[str, ast.Module]:
    root = _repo_root()
    paths = sorted(root.glob("packages/*/src/**/*.py"))
    assert len(paths) >= _MIN_MODULES_SCANNED, (
        f"only {len(paths)} modules scanned under {root}/packages/*/src; "
        "the scan is broken, not the code"
    )
    return {
        path.relative_to(root).as_posix(): ast.parse(path.read_text(encoding="utf-8"))
        for path in paths
    }


def _builder_names(tree: ast.Module) -> dict[str, str]:
    """Local name to builder, including every ``import ... as ...`` alias of one."""
    names = {builder: builder for builder in _BUILDERS}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in _BUILDERS:
                    names[alias.asname or alias.name] = alias.name
    return names


def _builder_called(call: ast.Call, names: dict[str, str]) -> str | None:
    if isinstance(call.func, ast.Name):
        return names.get(call.func.id)
    if isinstance(call.func, ast.Attribute) and call.func.attr in _BUILDERS:
        return call.func.attr
    return None


def _passes_a_resolved_mode(call: ast.Call) -> bool:
    """Whether the call names the mode explicitly, with a value that is not a literal."""
    for keyword in call.keywords:
        if keyword.arg == _MODE_KEYWORD:
            return not isinstance(keyword.value, ast.Constant)
    return False


def _builder_calls(tree: ast.Module) -> list[tuple[str, int, bool]]:
    """``(builder, line, passes a resolved mode)`` for every builder call in ``tree``."""
    names = _builder_names(tree)
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            builder = _builder_called(node, names)
            if builder is not None:
                calls.append((builder, node.lineno, _passes_a_resolved_mode(node)))
    return calls


def test_the_detector_sees_aliases_and_rejects_literal_modes() -> None:
    source = "\n".join(
        [
            "from persona_runtime.tier import tier_registry_from_env as build",
            "from persona_runtime import tier",
            "build()",
            "build(openrouter_subscription_mode=mode)",
            "tier.free_tier_registry_from_env(openrouter_subscription_mode=None)",
            "tier.free_tier_registry_from_env(openrouter_subscription_mode='paid')",
            "tier.tier_registry_from_env(**settings)",
            "unrelated(openrouter_subscription_mode='paid')",
        ]
    )

    assert _builder_calls(ast.parse(source)) == [
        ("tier_registry_from_env", 3, False),
        ("tier_registry_from_env", 4, True),
        ("free_tier_registry_from_env", 5, False),
        ("free_tier_registry_from_env", 6, False),
        ("tier_registry_from_env", 7, False),
    ]


def test_every_tier_registry_builder_call_passes_the_subscription_mode() -> None:
    found = [
        (module, builder, line, ok)
        for module, tree in _production_modules().items()
        for builder, line, ok in _builder_calls(tree)
    ]

    # Non-empty and complete FIRST: "every call complies" is vacuously true over a scan
    # that lost a package.
    missing_sites = _KNOWN_SITES - {(module, builder) for module, builder, _, _ in found}
    assert missing_sites == set(), f"known builder calls not found by the scan: {missing_sites}"
    unresolved = [f"{module}:{line}" for module, _, line, ok in found if not ok]
    assert unresolved == [], (
        f"these tier registry builds pass no resolved {_MODE_KEYWORD} (missing, or a "
        f"literal), so they ignore the OpenRouter free/paid mode every other process "
        f"applies: {unresolved}"
    )


def test_the_subscription_mode_resolver_is_defined_once_in_runtime() -> None:
    # One definition is what lets the api, the connector service and voice agree on
    # the mode from the same inputs; a second copy is how voice went without it.
    homes = [
        module
        for module, tree in _production_modules().items()
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == _RESOLVER
    ]

    assert homes == [_RESOLVER_HOME]

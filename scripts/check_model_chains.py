#!/usr/bin/env python
"""Every model-chain setting the source names is one ``CHAIN_ENV_NAMES`` knows (R9-213).

Each hosted process reads its own copy of every model list (voice composes its own
registry, D-V5-6), so the deploy has to hand every process the same lists, and it can only
do that for settings it knows exist. ``persona_runtime.tier.CHAIN_ENV_NAMES`` is that list,
derived from the prefix tables the registry builders iterate. This gate fails when the
source names a ``PERSONA_*_MODELS`` setting the list does not contain, which is how a new
chain would otherwise ship read by the code and set by nothing.

What it checks, in every ``.py`` under ``packages/<pkg>/src``:

* every string literal, including the literal parts of f-strings, except docstrings (a
  docstring describes a setting, it does not read one); comments are not code;
* every ``resolve_tier_config(...)`` call whose tier is a string literal or a module-level
  string constant (``_IMAGEGEN_TIER_NAME = "imagegen"`` style), because that call reads
  ``PERSONA_<TIER>_MODELS``; the tier must be one whose chain is known or allowed.

What it CANNOT see, stated plainly:

* a name assembled entirely at runtime, such as ``f"{prefix}MODELS"`` or
  ``f"PERSONA_{tier}_MODELS"``. Two places do that today. The registry builders in
  ``persona_runtime/tier.py`` are covered from the other side: ``CHAIN_ENV_NAMES`` is
  derived from the same prefix tables they iterate, and
  ``packages/runtime/tests/unit/test_chain_env_names.py`` proves the builders serve each
  tier from those six names alone. ``packages/core/src/persona/local_env.py`` (the
  ``PERSONA_DEV_CHEAP_TIERS`` switch) writes ``PERSONA_{tier}_MODELS`` for its own
  ``("FRONTIER", "MID", "SMALL")`` tuple: a known second list, dev-only, paid tiers only,
  and it cannot derive from the runtime tables because core must not import runtime.
* ``resolve_tier_config`` reached under another name (``import ... as``), through
  ``functools.partial``, or with a tier held in a local variable, a function argument or a
  constant imported from another module.

It proves itself on every run (the samples in ``FIXTURES`` go through the same
:func:`find_unlisted` that decides the real verdict), and it refuses to pass when an
expected package source directory is missing or when the scan finds nothing at all.

Exemptions are settings with ONE reader that are not a chat chain. Add one to ALLOWED with
a reason; never widen the pattern instead.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable


def _repo_root() -> Path:
    """The worktree being checked, asked of git rather than assumed (see check_flags_declared)."""
    import subprocess

    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
        )
        return Path(out.stdout.strip())
    except Exception:  # noqa: BLE001 - any git failure falls back to the script's own path
        return Path(__file__).resolve().parent.parent


ROOT = _repo_root()
PACKAGES = ("core", "runtime", "api", "voice", "connectors")
NAME = re.compile(r"\bPERSONA_[A-Z0-9_]+_MODELS\b")
RESOLVER = "resolve_tier_config"

#: One reader each, not a chat chain, so not something two processes must agree on.
ALLOWED: dict[str, str] = {
    "PERSONA_IMAGEGEN_MODELS": "image generation; read only by the api's image backend",
}

#: (source, the unlisted names the gate must report for it). Each shape it must catch or
#: must leave alone, run through the same :func:`find_unlisted` as the real scan, every run.
FIXTURES: tuple[tuple[str, list[str]], ...] = (
    ('value = os.environ.get("PERSONA_FOO_MODELS", "")\n', ["PERSONA_FOO_MODELS"]),
    ('value = env[f"PERSONA_FOO_MODELS"]\n', ["PERSONA_FOO_MODELS"]),
    ('message = f"set PERSONA_FOO_MODELS to {value}"\n', ["PERSONA_FOO_MODELS"]),
    ('"""A module docstring naming PERSONA_FOO_MODELS."""\nimport os\n', []),
    ('def read():\n    """Reads PERSONA_FOO_MODELS."""\n', []),
    ("# PERSONA_FOO_MODELS is only mentioned in a comment\n", []),
    ('value = os.environ.get("PERSONA_IMAGEGEN_MODELS", "")\n', []),
    ('value = os.environ.get("PERSONA_MID_MODELS", "")\n', []),
    ('resolution = resolve_tier_config("voice", env=snapshot)\n', ["PERSONA_VOICE_MODELS"]),
    ('resolution = credentials.resolve_tier_config(tier_name="voice")\n', ["PERSONA_VOICE_MODELS"]),
    ('_TIER = "voice"\nresolution = resolve_tier_config(_TIER)\n', ["PERSONA_VOICE_MODELS"]),
    ('_TIER: Final[str] = "imagegen"\nresolution = resolve_tier_config(tier_name=_TIER)\n', []),
    ('resolution = resolve_tier_config("mid", env=snapshot)\n', []),
    ("resolution = resolve_tier_config(tier_name, env=snapshot)\n", []),
)

for pkg in PACKAGES:
    sys.path.insert(0, str(ROOT / "packages" / pkg / "src"))


def _docstring_ids(tree: ast.AST) -> set[int]:
    """``id()`` of every docstring node: the first string statement of a module, class or def."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


def _module_string_constants(tree: ast.Module) -> dict[str, str]:
    """``NAME = "value"`` and ``NAME: T = "value"`` at module level, by name."""
    constants: dict[str, str] = {}
    for stmt in tree.body:
        value = stmt.value if isinstance(stmt, ast.Assign | ast.AnnAssign) else None
        if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
            continue
        targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
        for target in targets:
            if isinstance(target, ast.Name):
                constants[target.id] = value.value
    return constants


def _resolver_tier(node: ast.Call, constants: dict[str, str]) -> str | None:
    """The tier of a ``resolve_tier_config`` call when it is knowable, else ``None``."""
    func = node.func
    called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
    if called != RESOLVER:
        return None
    candidates = [*node.args[:1], *(kw.value for kw in node.keywords if kw.arg == "tier_name")]
    for arg in candidates:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
        if isinstance(arg, ast.Name) and arg.id in constants:
            return constants[arg.id]
    return None


def scan_source(source: str, filename: str = "<fixture>") -> list[tuple[str, int]]:
    """``(name, line)`` for every chain-shaped setting this source reads or names in code."""
    tree = ast.parse(source, filename=filename)
    skip = _docstring_ids(tree)
    constants = _module_string_constants(tree)
    hits: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            hits.extend((match.group(0), node.lineno) for match in NAME.finditer(node.value))
        elif isinstance(node, ast.Call) and (tier := _resolver_tier(node, constants)) is not None:
            hits.append((f"PERSONA_{tier.upper()}_MODELS", node.lineno))
    return hits


def find_unlisted(
    hits: Iterable[tuple[str, str, int]], known: Collection[str]
) -> list[tuple[str, str, int]]:
    """``(name, where, line)`` for every hit that is neither a known chain nor allowed.

    The ONE place the verdict is decided: :func:`main` reports exactly this list, and the
    self-test runs every fixture through it, so the check cannot be emptied unnoticed.
    """
    accepted = set(known) | set(ALLOWED)
    return [(name, where, line) for name, where, line in hits if name not in accepted]


def selftest(known: Collection[str]) -> int:
    """Fail loudly if the gate stops catching what it was built to catch."""
    broken = 0
    for source, expected in FIXTURES:
        hits = [(name, "<fixture>", line) for name, line in scan_source(source)]
        got = [name for name, _, _ in find_unlisted(hits, known)]
        if got != expected:
            print(f"   SELFTEST: expected {expected} from {source.strip()!r}, got {got}")
            broken += 1
    return broken


def source_files() -> tuple[list[Path], list[str]]:
    """Every source file to scan, plus a problem line per expected package that has none."""
    files: list[Path] = []
    problems: list[str] = []
    for pkg in PACKAGES:
        src = ROOT / "packages" / pkg / "src"
        found = sorted(src.rglob("*.py")) if src.is_dir() else []
        if not found:
            problems.append(f"packages/{pkg}/src is missing or holds no .py file")
        files.extend(found)
    return files, problems


def main() -> int:
    try:
        from persona_runtime.tier import CHAIN_ENV_NAMES
    except Exception as exc:  # noqa: BLE001 - a gate that cannot load its list must not pass
        print(f"cannot import persona_runtime.tier.CHAIN_ENV_NAMES: {exc!r}")
        return 1
    known = set(CHAIN_ENV_NAMES)
    if not known:
        print("CHAIN_ENV_NAMES is empty; the gate would pass on nothing.")
        return 1

    if broken := selftest(known):
        print(f"The model chain gate is not working: {broken} of {len(FIXTURES)} fixtures wrong.")
        return 1

    files, problems = source_files()
    if problems:
        for problem in problems:
            print(f"   {problem}")
        print("The scan would silently skip a package; refusing to pass.")
        return 1

    hits: list[tuple[str, str, int]] = []
    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        source = path.read_text(encoding="utf-8")
        hits.extend((name, rel, line) for name, line in scan_source(source, str(path)))
    scanned = {name for name, _, _ in hits}
    # A scan that sees nothing proves nothing: the source names these settings today (the
    # plan catalogue and the image factory among others), so an empty set means the scan broke.
    if not scanned:
        print("Scanned no PERSONA_*_MODELS names at all; the scan is broken, not clean.")
        return 1

    bad = find_unlisted(hits, known)
    print(
        f"Self-test {len(FIXTURES)} of {len(FIXTURES)} fixtures right. Checked {len(files)} files "
        f"in {len(PACKAGES)} packages: {len(scanned)} distinct PERSONA_*_MODELS names "
        f"({len(hits)} occurrences) against {len(known)} in CHAIN_ENV_NAMES and "
        f"{len(ALLOWED)} allowed."
    )
    if bad:
        print(f"\n{len(bad)} unlisted model-chain name(s):\n")
        for name, where, line in bad:
            print(f"   UNLISTED  {name:36s} {where}:{line}")
        print(
            "\nA chain the code reads must be one the deploy can set on every process. Read it\n"
            "through the tier prefix tables in persona_runtime/tier.py (so CHAIN_ENV_NAMES\n"
            "carries it), or, if exactly one process reads it and it is not a chat chain, add\n"
            "it to ALLOWED in this script with the reason."
        )
        return 1
    print("Model chain gate clean.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

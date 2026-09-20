"""``DeliveryRouter`` is constructed in exactly the places we have agreed to (R9-120, T1).

The owner ruling of 2026-09-21 attached a condition: one seam both callers enter, and no
second copy of the resolve-and-send logic. The seam itself was never the problem;
:class:`~persona_api.services.delivery_router.DeliveryRouter` has been the single routing
implementation since C0. The problem was that it was CONSTRUCTED in three places, each
with its own idea of which channels exist, and two of them hardcoded ``{"web": web}``.

Prose does not stop that recurring, so this pins the construction sites by name. A new
one fails here, and so does removing one without saying so, which is what makes the
frozen set a record of a decision rather than a high-water mark.

Two things about the shape of this guard, both of them defects this repo has shipped:

* It asserts the scan found files BEFORE it asserts anything about the result. A guard
  that walks zero files passes forever.
* It walks the AST rather than grepping. A regex over source misses an aliased import
  and matches the name in a docstring, and a guard that can be fooled by a comment is
  not a guard.
"""

from __future__ import annotations

import ast
from pathlib import Path

#: Every module allowed to call ``DeliveryRouter(...)``, with the reason it is allowed.
#:
#: ``services/origination_delivery.py``
#:     The one origination construction site. It is the only member now: T2 deleted
#:     ``connectors/composition.build_delivery_router``, whose return value the connector
#:     service threw away, and replaced its single call with a bind into the registry
#:     this seam reads.
_ALLOWED_CONSTRUCTION_SITES = frozenset(
    {
        "packages/api/src/persona_api/services/origination_delivery.py",
    }
)

#: A floor, not a count: it only has to be high enough that a broken scan (wrong root,
#: renamed package layout) cannot masquerade as a clean one.
_MIN_MODULES_SCANNED = 200


def _repo_root() -> Path:
    """The worktree root, found by walking up to the directory holding ``packages/``."""
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "packages").is_dir() and (candidate / "pyproject.toml").is_file():
            return candidate
    msg = "could not locate the repository root from the test file"
    raise AssertionError(msg)


def _constructs_delivery_router(tree: ast.AST) -> bool:
    """Whether this module calls ``DeliveryRouter(...)`` or ``x.DeliveryRouter(...)``."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "DeliveryRouter":
            return True
        if isinstance(func, ast.Attribute) and func.attr == "DeliveryRouter":
            return True
    return False


def test_delivery_router_is_constructed_only_where_we_agreed() -> None:
    """The frozen set of construction sites, asserted over a scan proven non-empty."""
    root = _repo_root()
    modules = sorted(root.glob("packages/*/src/**/*.py"))

    # Non-empty FIRST: everything below is vacuously true over an empty scan.
    assert len(modules) >= _MIN_MODULES_SCANNED, (
        f"only {len(modules)} modules scanned under {root}/packages/*/src; "
        "the scan is broken, not the code"
    )

    sites = {
        module.relative_to(root).as_posix()
        for module in modules
        if _constructs_delivery_router(ast.parse(module.read_text(encoding="utf-8")))
    }

    assert sites == set(_ALLOWED_CONSTRUCTION_SITES), (
        "the set of DeliveryRouter construction sites changed. Added sites mean a second "
        "place decides which channels exist, which is the R9-120 defect. Removed sites "
        "are good news that still has to be recorded here.\n"
        f"  unexpected: {sorted(sites - _ALLOWED_CONSTRUCTION_SITES)}\n"
        f"  missing:    {sorted(_ALLOWED_CONSTRUCTION_SITES - sites)}"
    )


def test_the_guard_can_actually_see_a_construction() -> None:
    """The detector finds a call it should find, so a green above means something.

    Without this, a typo in the AST walk would report zero construction sites and the
    frozen-set assertion would fail in a way that looks like a code change rather than a
    broken guard. Here it fails as itself.
    """
    plain = ast.parse("DeliveryRouter(deliverers={}, rls_engine=None)")
    dotted = ast.parse("mod.DeliveryRouter(deliverers={}, rls_engine=None)")
    unrelated = ast.parse("'DeliveryRouter is mentioned only in this string'")

    assert _constructs_delivery_router(plain)
    assert _constructs_delivery_router(dotted)
    assert not _constructs_delivery_router(unrelated)

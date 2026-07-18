"""The registry↔docs sync test (Spec M3, T1a — D-M3-5).

The one pricing truth has two halves — the code registry
(``persona.billing.pricing_registry``) the runtime reads, and the human table
(``docs/pricing/pricing-table.md``). They MUST agree on the set of paid surfaces:
a registry row without a docs row (or a docs row without a registry row) is
drift, and this test fails on it, matched on the ``(surface, sku)`` key.
"""

from __future__ import annotations

from pathlib import Path

from persona.billing.pricing_registry import registry_keys


def _pricing_table_path() -> Path:
    """Locate ``docs/pricing/pricing-table.md`` by ascending from this file.

    Robust across the worktree layout (``docs`` is a symlink to the main
    checkout) and CI — ``exists()`` follows the symlink.
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "docs" / "pricing" / "pricing-table.md"
        if candidate.exists():
            return candidate
    msg = "docs/pricing/pricing-table.md not found ascending from the test file"
    raise FileNotFoundError(msg)


def _docs_surface_keys(table: str) -> set[tuple[str, str]]:
    """Parse the ``## Surfaces`` markdown table into ``(surface, sku)`` keys."""
    start = table.index("## Surfaces")
    section = table[start:]
    end = section.find("\n## ", 1)
    if end != -1:
        section = section[:end]
    keys: set[tuple[str, str]] = set()
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        first = cells[0]
        if first == "surface" or set(first) <= set("-: "):  # header / separator row
            continue
        keys.add((cells[0], cells[2]))  # (surface, sku)
    return keys


def test_registry_and_docs_pricing_table_are_in_sync() -> None:
    docs_keys = _docs_surface_keys(_pricing_table_path().read_text(encoding="utf-8"))
    code_keys = registry_keys()
    missing_in_docs = code_keys - docs_keys
    orphan_in_docs = docs_keys - code_keys
    assert not missing_in_docs, f"registry rows missing a docs row: {sorted(missing_in_docs)}"
    assert not orphan_in_docs, f"docs rows with no registry row: {sorted(orphan_in_docs)}"


def test_docs_table_is_non_empty() -> None:
    # Guards the parser against silently matching an empty set against an empty set.
    assert _docs_surface_keys(_pricing_table_path().read_text(encoding="utf-8"))

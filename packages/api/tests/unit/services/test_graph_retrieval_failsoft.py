"""R4-C1-7 pin: graph retrieval failures degrade to MEMORYLESS, never turn-fatal.

Before the fix, any store error escaping the retrieval callable propagated out
of the detached chat worker and failed the whole turn — on community it was
EVERY turn (the schema has no graph tables); in cloud a transient Postgres
blip would do the same. Zero-graph is the loop's designed additive path, so an
empty :class:`GraphContext` is always the safe degradation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from persona_runtime.prompt import GraphContext

from persona_api.services.runtime_factory import RuntimeFactory


class _ExplodingStore:
    """Every read explodes — the community-missing-tables / Postgres-blip shape."""

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401 — total stub
        def _boom(*args: object, **kwargs: object) -> object:
            msg = "no such table: graph_nodes"
            raise RuntimeError(msg)

        return _boom


def test_graph_retrieval_failure_returns_empty_context(tmp_path: Path) -> None:
    factory = RuntimeFactory.__new__(RuntimeFactory)  # composition-free instance
    factory._graph_store = _ExplodingStore()  # noqa: SLF001 — the seam under test
    factory._audit_root = tmp_path  # noqa: SLF001
    factory._audit_logger = None  # noqa: SLF001

    retrieval = factory._build_graph_retrieval()  # noqa: SLF001
    assert retrieval is not None

    # flagged_nodes explodes inside the allowlist provider path; the store's
    # search explodes inside the retriever — either way the wrap must catch it.
    result = retrieval("what does the user enjoy?")
    assert isinstance(result, GraphContext)
    assert result.items == ()  # memoryless, not raised


def test_zero_graph_store_means_no_retrieval() -> None:
    """Community composition (edition-gated): no store ⇒ None ⇒ the loop's
    zero-graph additive path — the pre-K2 baseline behaviour."""
    factory = RuntimeFactory.__new__(RuntimeFactory)
    factory._graph_store = None  # noqa: SLF001
    assert factory._build_graph_retrieval() is None  # noqa: SLF001

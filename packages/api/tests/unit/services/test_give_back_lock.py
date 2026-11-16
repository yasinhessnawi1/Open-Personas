"""The give back locks the canonical FOR NO KEY UPDATE (R9-287 review LOW-2).

Shared by the forget and the persona erasure. ``with_for_update(key_share=True)`` without
``read`` is SQLAlchemy's spelling of FOR NO KEY UPDATE: the lock the canonical's metadata write
takes anyway, held from the read. This pins the statement the give back really runs.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from persona_api.services.erasure_graph import _give_back
from sqlalchemy.dialects import postgresql


class _Rows:
    def first(self) -> None:
        return None

    def all(self) -> list[Any]:
        return []

    def scalars(self) -> list[Any]:
        return []


class _Conn:
    def __init__(self) -> None:
        self.sql: list[str] = []

    def execute(self, stmt: Any, params: Any = None) -> _Rows:  # noqa: ANN401, ARG002
        self.sql.append(" ".join(str(stmt.compile(dialect=postgresql.dialect())).split()))
        return _Rows()


def test_the_canonical_is_read_for_no_key_update() -> None:
    record = {"canonical": "c1", "run_id": "r1", "state": "done"}
    member = SimpleNamespace(
        id="m1", concept_name="m1 name", metadata={"_k7_merge": json.dumps(record)}
    )
    conn = _Conn()

    _give_back(conn, "u1", [member], {"m1"})  # type: ignore[arg-type]

    canonical_reads = [
        s
        for s in conn.sql
        if s.startswith("SELECT graph_nodes.id, graph_nodes.concept_name, graph_nodes.content")
        and "graph_nodes.id = " in s
    ]
    assert canonical_reads, "the give back read the canonical"
    assert all(s.endswith("FOR NO KEY UPDATE") for s in canonical_reads)

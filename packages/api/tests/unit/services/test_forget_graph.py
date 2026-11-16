"""Forgetting a merged member: which of its source-less mirrors stay (R9-287).

The releaser's SQL runs in CI (tests/integration/test_forget_merged_member_postgres.py); this
pins its one decision of its own. The rest is the persona erasure's functions, unchanged.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from persona.graph.postgres import NodeRelease
from persona_api.services.forget_graph import (
    ForgetReleaser,
    merged_below,
    shared_legacy_mirrors,
)


def test_a_source_less_mirror_another_record_lists_is_kept() -> None:
    record = {"run_id": "r1", "mirrored_edges": ["e1", "e2", "e3"], "mirror_sources": {"e3": [7]}}
    others = [
        {"run_id": "r1", "mirrored_edges": ["e1"]},  # another record lists e1 without a source
        {"run_id": "r1", "mirror_sources": {"e2": [9]}},  # and e2 with one
    ]

    assert shared_legacy_mirrors(record, others) == {"e1", "e2"}


def test_a_mirror_only_this_record_lists_goes() -> None:
    record = {"run_id": "r1", "mirrored_edges": ["e1"]}

    assert shared_legacy_mirrors(record, [{"run_id": "r1", "mirrored_edges": ["e9"]}]) == set()


def test_a_sourced_mirror_is_not_this_rules_business() -> None:
    """A mirror with sources goes by the all-sources rule after the delete, not by id."""
    record = {"run_id": "r1", "mirrored_edges": ["e3"], "mirror_sources": {"e3": [7]}}

    assert shared_legacy_mirrors(record, [{"run_id": "r1", "mirrored_edges": ["e3"]}]) == set()


def test_a_listing_from_another_run_keeps_nothing() -> None:
    """Review LOW-3: the same mirror id from another run names that run's row, not this one."""
    record = {"run_id": "r1", "mirrored_edges": ["e1"]}

    assert shared_legacy_mirrors(record, [{"run_id": "r2", "mirrored_edges": ["e1"]}]) == set()
    assert shared_legacy_mirrors(record, [{"run_id": "r2", "mirror_sources": {"e1": [3]}}]) == set()


class _Rows:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def first(self) -> Any:  # noqa: ANN401
        return self.rows[0] if self.rows else None

    def all(self) -> list[Any]:
        return self.rows

    def scalars(self) -> list[Any]:
        return self.rows


class _Conn:
    """Answers the releaser's reads by table and records every statement it runs."""

    def __init__(
        self,
        node: Any,  # noqa: ANN401 - a Row stand-in
        *,
        edge_keys: list[int],
        records: list[Any] | None = None,
    ) -> None:
        self.node = node
        self.edge_keys = edge_keys
        self.records = records or []
        self.statements: list[str] = []

    def execute(self, stmt: Any, params: Any = None) -> _Rows:  # noqa: ANN401, ARG002
        sql = " ".join(str(stmt).split())
        self.statements.append(sql)
        if sql.startswith("SELECT graph_nodes.id, graph_nodes.concept_name"):
            return _Rows([] if self.node is None else [self.node])
        if sql.startswith("SELECT graph_edges.edge_key"):
            return _Rows(self.edge_keys)
        if sql.startswith("SELECT graph_nodes.id, graph_nodes.metadata"):
            return _Rows(self.records)
        return _Rows([])


def test_a_node_that_is_not_there_scans_nothing() -> None:
    """Review LOW-1: one read of the node, and nothing else."""
    conn = _Conn(None, edge_keys=[])

    release = ForgetReleaser().release(conn, "u1", "n1")  # type: ignore[arg-type]

    assert len(conn.statements) == 1
    assert release == NodeRelease()


def test_a_plain_node_no_record_names_skips_the_scans_after_the_delete() -> None:
    """Review LOW-1: never merged, nothing merged into it, its edges named by no record."""
    node = SimpleNamespace(id="n1", concept_name="n1", metadata={})
    conn = _Conn(node, edge_keys=[5])

    release = ForgetReleaser().release(conn, "u1", "n1")  # type: ignore[arg-type]
    before = len(conn.statements)
    release.after()

    assert release.members == ()
    assert len(conn.statements) == before, "no mirror or orphan scan"


def test_a_plain_node_whose_edge_a_record_names_still_scans_after_the_delete() -> None:
    """Its edge is the source of a mirror on some canonical: that mirror may now go."""
    import json  # noqa: PLC0415

    node = SimpleNamespace(id="n1", concept_name="n1", metadata={})
    lister = SimpleNamespace(
        id="m1",
        metadata={
            "_k7_merge": json.dumps(
                {"canonical": "c1", "run_id": "r1", "mirror_sources": {"c1::causal::n1": [5]}}
            )
        },
    )
    conn = _Conn(node, edge_keys=[5], records=[lister])

    release = ForgetReleaser().release(conn, "u1", "n1")  # type: ignore[arg-type]
    before = len(conn.statements)
    release.after()

    assert len(conn.statements) > before, "the mirror and orphan scans run"


class _CycleConn:
    """n1 > n2 > n3 > n2 and n2 > n1: cycles consolidation never writes, below and back."""

    def __init__(self) -> None:
        self.reads = 0

    def execute(self, stmt: Any, params: Any = None) -> _Rows:  # noqa: ANN401, ARG002
        self.reads += 1
        if self.reads > 10:
            msg = "the walk did not stop"
            raise AssertionError(msg)
        frontier = next(v for v in stmt.compile().params.values() if isinstance(v, list))
        below = {"n1": ["n2"], "n2": ["n1", "n3"], "n3": ["n2"]}
        return _Rows([m for node in frontier for m in below[node]])


def test_a_cycle_below_the_node_is_walked_once() -> None:
    """Round 2 LOW-6: each node is followed once, so a cycle ends the walk."""
    conn = _CycleConn()

    assert merged_below(conn, "u1", "n1") == ["n2", "n3"]  # type: ignore[arg-type]
    assert conn.reads == 3

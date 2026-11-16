"""Consolidation's merge act writes its record first, and every write holds the member's personas.

R9-284, D-R9-284-write-guard. A member's merge spans several transactions. Before this, the
mirrors and entity links were written first and the record (``merged_into`` with the mirror
ids) last, so an erasure landing in between left mirrors on the canonical that nothing could
trace back. Now the record goes first, in a transaction holding the member's personas; the
mirrors, the entity links and the alias accretion that follow hold them too. A recording fake
backend pins the order and the personas each write names (the reviewer's mutants M5 and M6
dropped them from the mirrors and from ``_accrete_aliases``).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from persona.audit import MemoryAuditLogger
from persona.graph.config import GraphSettings
from persona.graph.consolidation import ConsolidationPass
from persona.graph.models import ConceptNode, LinkType, NodeKind, NodeProvenance, TypedLink
from persona.schema.chunks import WriteSource

_NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
_P = "persona_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_Q = "persona_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def _node(node_id: str, *by: str | None) -> ConceptNode:
    return ConceptNode(
        id=node_id,
        node_kind=NodeKind.FACT,
        concept_name=node_id,
        content=f"about {node_id}",
        provenance=tuple(
            NodeProvenance(source=WriteSource.SYSTEM, persona_id=p, written_at=_NOW) for p in by
        ),
        created_at=_NOW,
    )


class _Backend:
    """Records each write with the personas it was asked to hold."""

    def __init__(
        self, canonical: ConceptNode, member: ConceptNode, *, open_ids: frozenset[str] = frozenset()
    ) -> None:
        self._nodes = {canonical.id: canonical, member.id: member}
        self._open = set(open_ids)
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.record: dict[str, Any] | None = None
        self.records: list[dict[str, Any]] = []
        self.already_linked: set[str] = set()

    def open_assertion_edges_keyed(self, _owner: str, node_id: str) -> list[tuple[TypedLink, int]]:
        if node_id != "m":
            return []
        edge = TypedLink(
            id="m::causal::x",
            src_node_id="m",
            dst_node_id="x",
            link_type=LinkType.CAUSAL,
            provenance=NodeProvenance(source=WriteSource.SYSTEM, persona_id=_Q, written_at=_NOW),
            created_at=_NOW,
        )
        return [(edge, 7)]

    def entities_for_node(self, _owner: str, node_id: str) -> list[str]:
        return ["ent_m"] if node_id == "m" else []

    def set_merged_into(self, _owner: str, _node_id: str, **kw: Any) -> None:  # noqa: ANN401
        self.record = json.loads(kw["metadata"]["_k7_merge"])
        self.records.append(self.record)
        self.calls.append(("set_merged_into", tuple(kw.get("guard_personas", ()))))

    def insert_edge_if_absent(self, _owner: str, link: TypedLink, **kw: Any) -> bool:  # noqa: ANN401
        self.calls.append((f"mirror {link.id}", tuple(kw.get("guard_personas", ()))))
        return link.id not in self._open

    def close_edges_incident(self, *_a: Any, **_k: Any) -> list[str]:  # noqa: ANN401
        self.calls.append(("close_edges_incident", ()))
        return []

    def associate_entities_returning(
        self,
        _owner: str,
        _node: str,
        ids: Any,  # noqa: ANN401
        **kw: Any,  # noqa: ANN401
    ) -> list[str]:
        self.calls.append((f"associate_entities {list(ids)}", tuple(kw.get("guard_personas", ()))))
        return [i for i in ids if i not in self.already_linked]

    def surrogate_for(self, *_a: Any) -> None:  # noqa: ANN401
        return None

    def bump_salience(self, *_a: Any, **_k: Any) -> None:  # noqa: ANN401
        return None

    def get_node(self, _owner: str, node_id: str) -> ConceptNode | None:
        return self._nodes.get(node_id)

    def get_embeddings(self, _owner: str, ids: Any) -> dict[str, list[float]]:  # noqa: ANN401
        return {i: [0.0] * 384 for i in ids}

    def update_node(self, _owner: str, node: ConceptNode, _emb: Any, **kw: Any) -> int:  # noqa: ANN401
        self.calls.append((f"update_node {node.id}", tuple(kw.get("guard_personas", ()))))
        self.updated = node
        return 1


class _Index:
    def remove(self, _surrogate: int) -> bool:
        return True


def _merge(open_ids: frozenset[str] = frozenset()) -> _Backend:
    canonical = _node("k", _Q)
    member = _node("m", _P, None, _P)
    backend = _Backend(canonical, member, open_ids=open_ids)
    consolidation = ConsolidationPass(
        backend=backend,  # type: ignore[arg-type]
        index=_Index(),  # type: ignore[arg-type]
        embedder=object(),  # type: ignore[arg-type]
        audit_logger=MemoryAuditLogger(),
        settings=GraphSettings(),
    )
    consolidation._merge_cluster(  # noqa: SLF001 - the merge act itself
        "u1", canonical, [member], run_id="r1", now=_NOW, epoch=1
    )
    return backend


def test_the_merge_record_is_written_before_any_mirror_or_entity_link() -> None:
    backend = _merge()

    names = [name for name, _ in backend.calls]
    assert names[0] == "set_merged_into"
    assert backend.record is not None
    assert backend.record["mirrored_edges"] == ["k::causal::x"], "precomputed in the record"
    assert backend.record["added_entities"] == ["ent_m"]
    assert backend.record["mirror_sources"] == {"k::causal::x": [7]}, "the source row (LOW-3)"
    assert names.index("mirror k::causal::x") > 0
    assert names.index("associate_entities ['ent_m']") > 0


def test_every_write_the_merge_makes_holds_the_members_personas() -> None:
    """Mutants M5 (mirrors) and M6 (_accrete_aliases) dropped these and survived."""
    backend = _merge()

    held = dict(backend.calls)
    # The record and the mirror also hold the source edge's asserter, _Q (round 5 H1).
    assert held["set_merged_into"] == (_P, _Q)
    assert held["mirror k::causal::x"] == (_P, _Q)
    assert held["associate_entities ['ent_m']"] == (_P,)
    assert held["update_node k"] == (_P,)


def test_a_mirror_never_overwrites_an_edge_the_canonical_already_has() -> None:
    """Review MEDIUM-1: another persona asserted k->x first; the record then keeps no mirror."""
    backend = _merge(open_ids=frozenset({"k::causal::x"}))

    assert backend.records[0]["mirrored_edges"] == ["k::causal::x"], "planned first"
    assert backend.records[-1]["mirrored_edges"] == [], "narrowed to the mirrors made"
    assert backend.records[-1]["mirror_sources"] == {}, "and to their sources (C8)"


def test_narrowing_keeps_exactly_the_sources_of_the_mirrors_made() -> None:
    """Reviewer's mutant C8 kept every planned source."""
    backend = _merge()

    assert backend.records[-1]["mirrored_edges"] == ["k::causal::x"]
    assert backend.records[-1]["mirror_sources"] == {"k::causal::x": [7]}


def test_the_record_is_pending_while_the_act_runs_and_done_at_its_end() -> None:
    """Round 5 MEDIUM-1: a member left pending is given back by the next run."""
    backend = _merge()

    assert [r["state"] for r in backend.records] == ["pending", "done"]
    names = [name for name, _ in backend.calls]
    last_record = len(names) - 1 - names[::-1].index("set_merged_into")
    assert last_record > names.index("associate_entities ['ent_m']"), "done after the act"


def test_the_accreted_alias_names_are_recorded_as_json() -> None:
    """Review HIGH-3: a name may hold a comma, so the reason lists the names as JSON."""
    backend = _merge()

    assert backend.updated.provenance[-1].reason == 'consolidation:r1 aliases:["m"]'


class _UnmergeBackend:
    """Two members of run r1 merged into k; both list the mirror k::causal::x."""

    def __init__(
        self, other_run: str, alive: frozenset[int] = frozenset({7}), other_canonical: str = "k"
    ) -> None:
        self._alive = set(alive)

        def record(run: str, canonical: str = "k") -> str:
            return json.dumps(
                {
                    "state": "done",
                    "run_id": run,
                    "canonical": canonical,
                    "mirrored_edges": ["k::causal::x"],
                    "added_entities": [],
                    "mirror_sources": {"k::causal::x": [7]},
                }
            )

        self.records = {"m": record("r1"), "n": record(other_run, other_canonical)}
        self.deleted: list[str] = []

    def get_node(self, _owner: str, node_id: str) -> ConceptNode:
        node = _node(node_id, _P)
        return node.model_copy(update={"metadata": {"_k7_merge": self.records[node_id]}})

    def merge_records_listing(self, _owner: str, **kw: Any) -> dict[str, str]:  # noqa: ANN401
        """Faithful to the SQL: every canonical, the run, and a listed mirror id."""
        out = {}
        for node_id, raw in self.records.items():
            record = json.loads(raw)
            if record["run_id"] == kw["run_id"] and set(kw["mirror_ids"]) & set(
                record["mirrored_edges"]
            ):
                out[node_id] = raw
        return out

    def existing_edge_keys(self, _owner: str, keys: Any) -> set[int]:  # noqa: ANN401
        return set(keys) & self._alive

    def delete_open_mirror(self, _owner: str, edge_id: str, **_k: Any) -> None:  # noqa: ANN401
        self.deleted.append(edge_id)

    def remove_entity_associations(self, *_a: Any) -> None:  # noqa: ANN401
        return None

    def reopen_edges_incident(self, *_a: Any, **_k: Any) -> None:  # noqa: ANN401
        return None

    def set_merged_into(self, *_a: Any, **_k: Any) -> None:  # noqa: ANN401
        return None

    def surrogate_for(self, *_a: Any) -> None:  # noqa: ANN401
        return None

    def get_embeddings(self, *_a: Any) -> dict[str, list[float]]:  # noqa: ANN401
        return {}


@pytest.mark.parametrize(
    ("other_run", "alive", "deleted"),
    [
        ("r1", frozenset({7}), []),
        ("r0", frozenset({7}), ["k::causal::x"]),
        ("r1", frozenset(), ["k::causal::x"]),
    ],
    ids=["shared", "another_run", "stale_listing"],
)
def test_unmerge_keeps_a_mirror_another_member_of_the_run_shares(
    other_run: str, alive: frozenset[int], deleted: list[str]
) -> None:
    """Round 5 MEDIUM-2: giving m back must not take n's share of the mirror.

    Round 6 M-1: n backs it only while a source n lists for it is still a row.
    """
    backend = _UnmergeBackend(other_run, alive)
    consolidation = ConsolidationPass(
        backend=backend,  # type: ignore[arg-type]
        index=_Index(),  # type: ignore[arg-type]
        embedder=object(),  # type: ignore[arg-type]
        audit_logger=MemoryAuditLogger(),
        settings=GraphSettings(),
    )

    assert consolidation._unmerge("u1", "m") is True  # noqa: SLF001 - the inverse itself

    assert backend.deleted == deleted


def test_a_member_of_another_cluster_in_the_run_shares_the_mirror_too() -> None:
    """Round 6 M-2: A1 into C1 and B2 into C2 both made C2::t::C1; B2's cluster fails."""
    backend = _UnmergeBackend("r1", other_canonical="c1")
    consolidation = ConsolidationPass(
        backend=backend,  # type: ignore[arg-type]
        index=_Index(),  # type: ignore[arg-type]
        embedder=object(),  # type: ignore[arg-type]
        audit_logger=MemoryAuditLogger(),
        settings=GraphSettings(),
    )

    assert consolidation._unmerge("u1", "m") is True  # noqa: SLF001 - the inverse itself

    assert backend.deleted == [], "A1's share keeps the mirror"


def test_only_the_entity_links_the_association_added_are_recorded() -> None:
    """Round 6 L-4: a give back must never remove a link the canonical had before."""
    fresh = _merge()
    assert fresh.records[0]["added_entities"] == [], "a pending record lists none"
    assert fresh.records[-1]["added_entities"] == ["ent_m"]

    canonical = _node("k", _Q)
    member = _node("m", _P, None, _P)
    backend = _Backend(canonical, member)
    backend.already_linked = {"ent_m"}  # linked meanwhile, by another path
    ConsolidationPass(
        backend=backend,  # type: ignore[arg-type]
        index=_Index(),  # type: ignore[arg-type]
        embedder=object(),  # type: ignore[arg-type]
        audit_logger=MemoryAuditLogger(),
        settings=GraphSettings(),
    )._merge_cluster("u1", canonical, [member], run_id="r1", now=_NOW, epoch=1)  # noqa: SLF001

    assert backend.records[-1]["added_entities"] == []


def test_both_record_writes_hold_the_sources_asserter() -> None:
    """Reviewer's C10: the fake kept only the last record write; the pending one went untested."""
    backend = _merge()

    held = [personas for name, personas in backend.calls if name == "set_merged_into"]

    assert held == [(_P, _Q), (_P, _Q)], "the pending write and the done write"

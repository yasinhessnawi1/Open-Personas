"""Erasing a persona takes it out of the user's graph, and leaves the rest (R9-284, ruling 4).

On a migrated Postgres, through the real graph backend: a node only the persona contributed to
goes with its edges, versions and entity links; a shared node keeps the other contributions;
the SELF node stays as dated stubs; an edge the persona asserted goes; an entity it established
goes when nothing links to it and otherwise loses the attribution. Runs in CI.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from persona.audit import MemoryAuditLogger
from persona.graph.config import GraphSettings
from persona.graph.consolidation import ConsolidationPass
from persona.graph.index import make_graph_index
from persona.graph.models import (
    CanonicalEntity,
    ConceptNode,
    EntityAlias,
    LinkType,
    NodeKind,
    NodeProvenance,
    TypedLink,
    make_self_node_id,
)
from persona.graph.postgres import PostgresGraphBackend
from persona.schema.chunks import WriteSource
from persona_api.services.erasure_graph import erase_persona_graph
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

_OWNER = "user_alice"
_PERSONA = "persona_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_OTHER = "persona_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
_SELF = make_self_node_id(_OWNER)
_NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


def _by(persona_id: str | None) -> NodeProvenance:
    return NodeProvenance(
        source=WriteSource.SYSTEM if persona_id else WriteSource.USER,
        persona_id=persona_id,
        written_at=_NOW,
        grounding=f"what {persona_id} heard" if persona_id else None,
    )


def _node(node_id: str, *by: str | None, kind: NodeKind = NodeKind.FACT) -> ConceptNode:
    return ConceptNode(
        id=node_id,
        node_kind=kind,
        concept_name=node_id,
        content=f"about {node_id}",
        provenance=tuple(_by(p) for p in by),
        created_at=_NOW,
    )


def _edge(edge_id: str, src: str, dst: str, by: str) -> TypedLink:
    return TypedLink(
        id=edge_id,
        src_node_id=src,
        dst_node_id=dst,
        link_type=LinkType.CAUSAL,
        provenance=_by(by),
        created_at=_NOW,
        valid_at=_NOW,
    )


@pytest.fixture
def graph(migrated_engine: Engine, embedder: Any) -> Engine:  # noqa: ANN401
    with migrated_engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES (:o, 'a@x.test')"), {"o": _OWNER})
        for pid in (_PERSONA, _OTHER):
            conn.execute(
                text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :o, 'x: 1')"),
                {"p": pid, "o": _OWNER},
            )
    backend = PostgresGraphBackend(engine=migrated_engine)
    vector = embedder.encode(["x"])[0]
    for node in (
        _node("only", _PERSONA, _PERSONA),
        _node("shared", _PERSONA, _OTHER),
        _node("theirs", _OTHER),
        _node("edited", _PERSONA),
        _node(_SELF, _PERSONA, kind=NodeKind.SELF),
    ):
        backend.insert_node(_OWNER, node, vector)
    for node_id in ("only", "shared", "edited", _SELF):
        backend.snapshot_node_version(
            _OWNER, node_id, valid_at=_NOW, invalid_at=_NOW, invalidated_by=None
        )
    # The user then edits a node only the persona had written: it is the user's now, and its
    # earlier version (the persona's alone) is not.
    backend.update_node(_OWNER, _node("edited", _PERSONA, None), vector)
    backend.upsert_edge(_OWNER, _edge("e_only", "only", "theirs", _OTHER))
    backend.upsert_edge(_OWNER, _edge("e_mine", "shared", "theirs", _PERSONA))
    backend.upsert_edge(_OWNER, _edge("e_theirs", "shared", "theirs", _OTHER))
    for entity_id, by in (("ent_gone", _PERSONA), ("ent_kept", _PERSONA), ("ent_theirs", _OTHER)):
        backend.insert_entity(
            _OWNER,
            CanonicalEntity(
                id=entity_id, canonical_name=entity_id, provenance=_by(by), created_at=_NOW
            ),
            vector,
        )
    # Review H-1: a surface the persona gave another persona's entity, and one from before
    # aliases carried a persona (left alone: whose it was is not known).
    backend.add_alias(_OWNER, "ent_theirs", EntityAlias(surface="my doctor", persona_id=_PERSONA))
    backend.add_alias(_OWNER, "ent_theirs", EntityAlias(surface="the GP"))
    for node_id, entity_id in (
        ("only", "ent_gone"),
        ("theirs", "ent_kept"),
        ("theirs", "ent_theirs"),
    ):
        backend.associate_entities(_OWNER, node_id, [entity_id])
    return migrated_engine


def _column(engine: Engine, sql: str) -> dict[str, Any]:
    with engine.connect() as conn:
        return {str(k): v for k, v in conn.execute(text(sql), {"o": _OWNER}).all()}


def _personas_in(trail: Any) -> list[str | None]:  # noqa: ANN401
    return [entry.get("persona_id") for entry in trail]


def test_the_persona_leaves_the_graph_and_everything_else_stays(graph: Engine) -> None:
    counts = erase_persona_graph(graph, owner_id=_OWNER, persona_id=_PERSONA)

    nodes = _column(graph, "SELECT id, provenance FROM graph_nodes WHERE owner_id = :o")
    assert set(nodes) == {"shared", "theirs", "edited", _SELF}
    assert _personas_in(nodes["shared"]) == [_OTHER]
    assert _personas_in(nodes["theirs"]) == [_OTHER]
    assert _personas_in(nodes["edited"]) == [None]
    assert nodes[_SELF] == [{"source": "system", "written_at": nodes[_SELF][0]["written_at"]}]

    versions = _column(
        graph, "SELECT node_id, provenance FROM graph_node_versions WHERE owner_id = :o"
    )
    assert set(versions) == {"shared", _SELF}
    assert _personas_in(versions["shared"]) == [_OTHER]
    assert _PERSONA not in str(versions[_SELF])

    edges = _column(graph, "SELECT id, 1 FROM graph_edges WHERE owner_id = :o")
    assert set(edges) == {"e_theirs"}

    entities = _column(graph, "SELECT id, provenance FROM graph_entities WHERE owner_id = :o")
    assert set(entities) == {"ent_kept", "ent_theirs"}
    assert entities["ent_kept"] is None
    assert entities["ent_theirs"]["persona_id"] == _OTHER
    aliases = _column(graph, "SELECT id, aliases FROM graph_entities WHERE owner_id = :o")
    assert [a["surface"] for a in aliases["ent_theirs"]] == ["the GP"]

    assert counts == {
        "nodes_deleted": 1,
        "nodes_stripped": 3,
        "canonicals_cleaned": 0,
        "members_unmerged": 0,
        "versions_deleted": 1,
        "versions_stripped": 2,
        "edges_deleted": 1,
        "mirrors_of_asserted_deleted": 0,
        "entities_deleted": 1,
        "entities_unattributed": 1,
        "entity_aliases_removed": 1,
    }


def test_running_it_again_finds_nothing(graph: Engine) -> None:
    erase_persona_graph(graph, owner_id=_OWNER, persona_id=_PERSONA)

    again = erase_persona_graph(graph, owner_id=_OWNER, persona_id=_PERSONA)

    assert set(again.values()) == {0}


def test_another_owners_graph_is_not_reached(graph: Engine) -> None:
    """The persona id names one owner's persona; every statement is owner scoped as well."""
    counts = erase_persona_graph(graph, owner_id="user_bob", persona_id=_PERSONA)

    assert set(counts.values()) == {0}
    assert len(_column(graph, "SELECT id, 1 FROM graph_nodes WHERE owner_id = :o")) == 5


# --- consolidation (review H3 and M1): built with the real ConsolidationPass merge act ------


@pytest.fixture
def consolidated(migrated_engine: Engine, embedder: Any) -> tuple[Engine, PostgresGraphBackend]:  # noqa: ANN401
    """Nodes, then the merges ConsolidationPass makes, with its real bookkeeping."""
    with migrated_engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES (:o, 'a@x.test')"), {"o": _OWNER})
        for pid in (_PERSONA, _OTHER):
            conn.execute(
                text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :o, 'x: 1')"),
                {"p": pid, "o": _OWNER},
            )
    backend = PostgresGraphBackend(engine=migrated_engine)
    vector = embedder.encode(["x"])[0]
    for node in (
        _node("p_canon", _PERSONA),
        _node("p_member", _PERSONA),
        _node(_SELF, _PERSONA, kind=NodeKind.SELF),
        _node("p_self_member", _PERSONA),
        _node("q_canon", _OTHER),
        _node("p_into_q", _PERSONA),
        _node("q_other", _OTHER),
        _node("p_canon2", _PERSONA),
        _node("q_member", _OTHER),
        _node("q_canon2", _OTHER),
        _node("q_member2", _OTHER),
        _node("q_x", _OTHER),
    ):
        backend.insert_node(_OWNER, node, vector)
    # Open assertion edges the merge act mirrors onto the canonical and closes on the member.
    backend.upsert_edge(_OWNER, _edge("e_pq", "p_into_q", "q_other", _OTHER))
    backend.upsert_edge(_OWNER, _edge("e_qm", "q_member", "q_other", _OTHER))
    # Review M-3: the erased persona asserted an edge on a member that stays.
    backend.upsert_edge(_OWNER, _edge("e_pm", "q_member2", "q_x", _PERSONA))
    settings = GraphSettings()
    consolidation = ConsolidationPass(
        backend=backend,
        index=make_graph_index(
            settings=settings,
            engine=migrated_engine,
            float32_fetch=backend.embeddings_by_surrogate,
        ),
        embedder=embedder,
        audit_logger=MemoryAuditLogger(),
        settings=settings,
    )
    for run, canonical, member in (
        ("r1", "p_canon", "p_member"),
        ("r2", _SELF, "p_self_member"),
        ("r3", "q_canon", "p_into_q"),
        ("r4", "p_canon2", "q_member"),
        ("r5", "q_canon2", "q_member2"),
    ):
        consolidation._merge_cluster(  # noqa: SLF001 - the merge act itself, as a run does it
            _OWNER,
            backend.get_node(_OWNER, canonical),
            [backend.get_node(_OWNER, member)],
            run_id=run,
            now=_NOW,
            epoch=1,
        )
    return migrated_engine, backend


def _open_edges(engine: Engine, node_id: str) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id FROM graph_edges WHERE owner_id = :o AND invalid_at IS NULL "
                "AND (src_node_id = :n OR dst_node_id = :n)"
            ),
            {"o": _OWNER, "n": node_id},
        )
        return {str(r[0]) for r in rows}


def test_consolidation_does_not_keep_a_node_only_the_persona_fed(
    consolidated: tuple[Engine, PostgresGraphBackend],
) -> None:
    """Review H3: merging added a SYSTEM consolidation entry; it is not provenance."""
    engine, backend = consolidated
    assert backend.get_node(_OWNER, "p_canon") is not None

    erase_persona_graph(engine, owner_id=_OWNER, persona_id=_PERSONA)

    for gone in ("p_canon", "p_member", "p_self_member", "p_into_q"):
        assert backend.get_node(_OWNER, gone) is None, gone


def test_the_self_node_keeps_nothing_the_persona_gave_it(
    consolidated: tuple[Engine, PostgresGraphBackend],
) -> None:
    engine, backend = consolidated

    erase_persona_graph(engine, owner_id=_OWNER, persona_id=_PERSONA)

    me = backend.get_node(_OWNER, _SELF)  # parses: the content hash matches the metadata
    assert me is not None
    assert [(p.source, p.persona_id, p.reason) for p in me.provenance] == [
        (WriteSource.SYSTEM, None, None)
    ]
    assert "p_self_member" not in me.metadata.get("_k7_aliases", "")


def test_a_canonical_that_stays_takes_back_what_the_personas_member_gave(
    consolidated: tuple[Engine, PostgresGraphBackend],
) -> None:
    engine, backend = consolidated
    assert _open_edges(engine, "q_canon"), "the merge mirrored the member's edge"

    erase_persona_graph(engine, owner_id=_OWNER, persona_id=_PERSONA)

    canonical = backend.get_node(_OWNER, "q_canon")
    assert canonical is not None
    assert "p_into_q" not in canonical.metadata.get("_k7_aliases", "")
    assert not any("p_into_q" in (p.reason or "") for p in canonical.provenance), (
        "the consolidation entry no longer lists the erased member (review H-3)"
    )
    with engine.connect() as conn:
        snapshots = conn.execute(
            text("SELECT provenance FROM graph_node_versions WHERE node_id = 'q_canon'")
        ).all()
    assert not any("p_into_q" in str(s[0]) for s in snapshots)
    assert _open_edges(engine, "q_canon") == set()
    assert [p.persona_id for p in canonical.provenance if p.persona_id] == [_OTHER]


def test_a_member_merged_into_an_erased_node_is_unmerged_not_stranded(
    consolidated: tuple[Engine, PostgresGraphBackend],
) -> None:
    """Review M1: another persona's member must not point at a node that no longer exists."""
    engine, backend = consolidated
    before = backend.get_node(_OWNER, "q_member")
    assert before is not None
    assert "_k7_merge" in before.metadata
    assert _open_edges(engine, "q_member") == set(), "the merge closed the member's edge"

    counts = erase_persona_graph(engine, owner_id=_OWNER, persona_id=_PERSONA)

    assert backend.get_node(_OWNER, "p_canon2") is None
    member = backend.get_node(_OWNER, "q_member")  # parses: the hash matches the metadata
    assert member is not None
    assert "_k7_merge" not in member.metadata
    assert member.content == before.content
    with engine.connect() as conn:
        merged_into = conn.execute(
            text("SELECT merged_into FROM graph_nodes WHERE owner_id = :o AND id = 'q_member'"),
            {"o": _OWNER},
        ).scalar_one()
    assert merged_into is None
    assert _open_edges(engine, "q_member") == {"e_qm"}
    assert counts["members_unmerged"] == 1


def test_the_backstop_finds_what_a_finished_erasure_left_and_nothing_else(graph: Engine) -> None:
    """Review H-2: a completed erasure the graph still names is found until it is gone."""
    from persona_api.services.erasure_graph import erased_personas_in_graph  # noqa: PLC0415
    from persona_api.services.erasure_ledger import ErasureLedger, SubjectKind  # noqa: PLC0415

    ledger = ErasureLedger(graph)
    for persona in (_PERSONA, _OTHER):
        done = ledger.claim(
            owner_id=_OWNER, subject_kind=SubjectKind.PERSONA, subject_id=persona, captured={}
        )
        ledger.complete(done.id)
    with graph.begin() as conn:  # only the erased persona's row is gone
        conn.execute(text("DELETE FROM personas WHERE id = :p"), {"p": _PERSONA})

    assert erased_personas_in_graph(graph) == [(_OWNER, _PERSONA)]

    erase_persona_graph(graph, owner_id=_OWNER, persona_id=_PERSONA)

    assert erased_personas_in_graph(graph) == []


def test_a_mirror_another_persona_reasserted_is_left_alone(
    consolidated: tuple[Engine, PostgresGraphBackend],
) -> None:
    """Review M-4: the same id, but now another persona's assertion, not the merge's mirror."""
    engine, backend = consolidated
    (mirror,) = _open_edges(engine, "q_canon")
    src, link_type, dst = mirror.split("::")
    backend.upsert_edge(_OWNER, _edge(mirror, src, dst, _OTHER))  # reasserted by another

    erase_persona_graph(engine, owner_id=_OWNER, persona_id=_PERSONA)

    assert _open_edges(engine, "q_canon") == {mirror}
    assert link_type


def test_the_mirror_of_an_edge_the_persona_asserted_on_a_kept_member_goes(
    consolidated: tuple[Engine, PostgresGraphBackend],
) -> None:
    """Review M-3: the persona's edge goes by the edge rule; its mirror must go with it."""
    engine, _backend = consolidated
    assert _open_edges(engine, "q_canon2") == {"q_canon2::causal::q_x"}

    counts = erase_persona_graph(engine, owner_id=_OWNER, persona_id=_PERSONA)

    assert _open_edges(engine, "q_canon2") == set()
    assert counts["mirrors_of_asserted_deleted"] == 1

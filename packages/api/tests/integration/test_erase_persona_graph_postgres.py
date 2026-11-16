"""Erasing a persona takes it out of the user's graph, and leaves the rest (R9-284, ruling 4).

On a migrated Postgres, through the real graph backend: a node only the persona contributed to
goes with its edges, versions and entity links; a shared node keeps the other contributions;
the SELF node stays as dated stubs; an edge the persona asserted goes; an entity it established
goes when nothing links to it and otherwise loses the attribution. Runs in CI.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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
    _compute_node_hash,
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
    backend = PostgresGraphBackend(engine=migrated_engine, persona_guard=False)
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
        "orphan_mirrors_deleted": 0,
        "merge_records_pruned": 0,
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
    backend = PostgresGraphBackend(engine=migrated_engine, persona_guard=False)
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
    # Review MEDIUM-3: the every-sweep look covers what completed since a moment.
    with graph.begin() as conn:
        conn.execute(
            text(
                "UPDATE erasures SET completed_at = now() - interval '2 days' WHERE subject_id = :p"
            ),
            {"p": _PERSONA},
        )
    a_day_ago = datetime.now(UTC) - timedelta(days=1)
    assert erased_personas_in_graph(graph, since=a_day_ago) == []
    assert erased_personas_in_graph(graph, since=a_day_ago - timedelta(days=2)) == [
        (_OWNER, _PERSONA)
    ]
    assert erased_personas_in_graph(graph) == [(_OWNER, _PERSONA)], "the full pass still does"

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
    assert "q_canon::causal::q_other" in _open_edges(engine, "q_canon")

    counts = erase_persona_graph(engine, owner_id=_OWNER, persona_id=_PERSONA)

    assert _open_edges(engine, "q_canon2") == set()
    # Since round 5 HIGH-2 the mirrors found from the seeds are counted here, so this also
    # counts the doomed member p_into_q's mirror on q_canon (its edge is a seed), which
    # _give_back used to remove uncounted.
    assert "q_canon::causal::q_other" not in _open_edges(engine, "q_canon")
    assert counts["mirrors_of_asserted_deleted"] == 2


# --- HIGH-1: an erasure interleaved with a merge act (D-R9-284-write-guard) ----------------


@pytest.mark.parametrize("after", ["set_merged_into", "insert_edge_if_absent"])
def test_an_erasure_in_the_middle_of_a_merge_leaves_no_mirror(
    migrated_engine: Engine,
    embedder: Any,  # noqa: ANN401
    after: str,
) -> None:
    """The record is written first, so an erasure landing after it, or after the first mirror,
    finds every mirror through it; and the merge's next guarded write is refused."""
    from persona.graph.errors import GraphPersonaGoneError  # noqa: PLC0415

    with migrated_engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES (:o, 'a@x.test')"), {"o": _OWNER})
        for pid in (_PERSONA, _OTHER):
            conn.execute(
                text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :o, 'x: 1')"),
                {"p": pid, "o": _OWNER},
            )
    backend = PostgresGraphBackend(engine=migrated_engine, persona_guard=True)
    vector = embedder.encode(["x"])[0]
    for node in (_node("i_canon", _OTHER), _node("i_member", _PERSONA), _node("i_x", _OTHER)):
        backend.insert_node(_OWNER, node, vector)
    backend.upsert_edge(_OWNER, _edge("e_ix", "i_member", "i_x", _OTHER))
    erased: list[bool] = []
    real = getattr(backend, after)

    def _then_erase(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        result = real(*args, **kwargs)
        if not erased:
            erased.append(True)
            with migrated_engine.begin() as conn:
                conn.execute(text("DELETE FROM personas WHERE id = :p"), {"p": _PERSONA})
            erase_persona_graph(migrated_engine, owner_id=_OWNER, persona_id=_PERSONA)
        return result

    setattr(backend, after, _then_erase)
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

    with pytest.raises(GraphPersonaGoneError):
        consolidation._merge_cluster(  # noqa: SLF001 - the merge act itself
            _OWNER,
            backend.get_node(_OWNER, "i_canon"),
            [backend.get_node(_OWNER, "i_member")],
            run_id="ri",
            now=_NOW,
            epoch=1,
        )

    assert erased == [True]
    assert backend.get_node(_OWNER, "i_member") is None
    with migrated_engine.connect() as conn:
        mirrors = conn.execute(
            text(
                "SELECT id FROM graph_edges WHERE owner_id = :o "
                "AND provenance->>'reason' = 'consolidation:ri'"
            ),
            {"o": _OWNER},
        ).all()
    assert mirrors == [], "no mirror of the erased persona's member survives"


def test_erasing_the_merged_member_leaves_an_edge_another_persona_asserted_first(
    migrated_engine: Engine,
    embedder: Any,  # noqa: ANN401
) -> None:
    """Review MEDIUM-1: the mirror's id was already another persona's open edge on the canonical.

    The merge does not overwrite it, so it never becomes a mirror, and erasing the member's
    persona leaves it.
    """
    with migrated_engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES (:o, 'a@x.test')"), {"o": _OWNER})
        for pid in (_PERSONA, _OTHER):
            conn.execute(
                text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :o, 'x: 1')"),
                {"p": pid, "o": _OWNER},
            )
    backend = PostgresGraphBackend(engine=migrated_engine, persona_guard=True)
    vector = embedder.encode(["x"])[0]
    for node in (_node("b_canon", _OTHER), _node("b_member", _PERSONA), _node("b_x", _OTHER)):
        backend.insert_node(_OWNER, node, vector)
    backend.upsert_edge(_OWNER, _edge("b_canon::causal::b_x", "b_canon", "b_x", _OTHER))
    backend.upsert_edge(_OWNER, _edge("e_bm", "b_member", "b_x", _OTHER))
    settings = GraphSettings()
    ConsolidationPass(
        backend=backend,
        index=make_graph_index(
            settings=settings,
            engine=migrated_engine,
            float32_fetch=backend.embeddings_by_surrogate,
        ),
        embedder=embedder,
        audit_logger=MemoryAuditLogger(),
        settings=settings,
    )._merge_cluster(  # noqa: SLF001 - the merge act itself
        _OWNER,
        backend.get_node(_OWNER, "b_canon"),
        [backend.get_node(_OWNER, "b_member")],
        run_id="rb",
        now=_NOW,
        epoch=1,
    )
    with migrated_engine.begin() as conn:
        conn.execute(text("DELETE FROM personas WHERE id = :p"), {"p": _PERSONA})

    erase_persona_graph(migrated_engine, owner_id=_OWNER, persona_id=_PERSONA)

    with migrated_engine.connect() as conn:
        kept = conn.execute(
            text(
                "SELECT provenance->>'persona_id' FROM graph_edges "
                "WHERE owner_id = :o AND id = 'b_canon::causal::b_x' AND invalid_at IS NULL"
            ),
            {"o": _OWNER},
        ).scalar_one()
    assert kept == _OTHER


def test_a_mirror_of_a_mirror_of_the_personas_edge_goes_too(
    migrated_engine: Engine,
    embedder: Any,  # noqa: ANN401
) -> None:
    """Review LOW-3: m's edge (the persona's) mirrored onto c1, and c1 later merged into c2."""
    with migrated_engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES (:o, 'a@x.test')"), {"o": _OWNER})
        for pid in (_PERSONA, _OTHER):
            conn.execute(
                text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :o, 'x: 1')"),
                {"p": pid, "o": _OWNER},
            )
    backend = PostgresGraphBackend(engine=migrated_engine, persona_guard=True)
    vector = embedder.encode(["x"])[0]
    for node_id in ("c_m", "c_1", "c_2", "c_x"):
        backend.insert_node(_OWNER, _node(node_id, _OTHER), vector)
    backend.upsert_edge(_OWNER, _edge("e_cm", "c_m", "c_x", _PERSONA))
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
    for run, canonical, member in (("rc1", "c_1", "c_m"), ("rc2", "c_2", "c_1")):
        consolidation._merge_cluster(  # noqa: SLF001 - the merge act itself
            _OWNER,
            backend.get_node(_OWNER, canonical),
            [backend.get_node(_OWNER, member)],
            run_id=run,
            now=_NOW,
            epoch=1,
        )
    assert _open_edges(migrated_engine, "c_2") == {"c_2::causal::c_x"}, "the second mirror"
    with migrated_engine.begin() as conn:
        conn.execute(text("DELETE FROM personas WHERE id = :p"), {"p": _PERSONA})

    counts = erase_persona_graph(migrated_engine, owner_id=_OWNER, persona_id=_PERSONA)

    assert _open_edges(migrated_engine, "c_2") == set()
    assert counts["mirrors_of_asserted_deleted"] >= 1


def test_a_doomed_members_mirrors_go_down_the_whole_chain(
    migrated_engine: Engine,
    embedder: Any,  # noqa: ANN401
) -> None:
    """Round 5 HIGH-2: doomed A merged into C, C merged into D, then P (A's only persona) goes.

    A's record and A's edge A->X go with A (the edge by cascade), so the erasure reads them
    first: D keeps no open mirror of P's assertion, and C's closed mirror row goes too.
    """
    with migrated_engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES (:o, 'a@x.test')"), {"o": _OWNER})
        for pid in (_PERSONA, _OTHER):
            conn.execute(
                text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :o, 'x: 1')"),
                {"p": pid, "o": _OWNER},
            )
    backend = PostgresGraphBackend(engine=migrated_engine, persona_guard=True)
    vector = embedder.encode(["x"])[0]
    backend.insert_node(_OWNER, _node("w_a", _PERSONA), vector)
    for node_id in ("w_c", "w_d", "w_x"):
        backend.insert_node(_OWNER, _node(node_id, _OTHER), vector)
    backend.upsert_edge(_OWNER, _edge("w_a::causal::w_x", "w_a", "w_x", _PERSONA))
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
    for run, canonical, member in (("rw1", "w_c", "w_a"), ("rw2", "w_d", "w_c")):
        consolidation._merge_cluster(  # noqa: SLF001 - the merge act itself
            _OWNER,
            backend.get_node(_OWNER, canonical),
            [backend.get_node(_OWNER, member)],
            run_id=run,
            now=_NOW,
            epoch=1,
        )
    assert _open_edges(migrated_engine, "w_d") == {"w_d::causal::w_x"}
    with migrated_engine.begin() as conn:
        conn.execute(text("DELETE FROM personas WHERE id = :p"), {"p": _PERSONA})

    erase_persona_graph(migrated_engine, owner_id=_OWNER, persona_id=_PERSONA)

    assert backend.get_node(_OWNER, "w_a") is None
    assert _open_edges(migrated_engine, "w_d") == set()
    with migrated_engine.connect() as conn:
        left = conn.execute(
            text(
                "SELECT id FROM graph_edges WHERE owner_id = :o "
                "AND id IN ('w_c::causal::w_x', 'w_d::causal::w_x')"
            ),
            {"o": _OWNER},
        ).all()
    assert left == [], "the closed mirror on C and the open one on D both go"


def test_a_mirror_two_members_share_goes_with_the_last_of_its_sources(
    migrated_engine: Engine,
    embedder: Any,  # noqa: ANN401
) -> None:
    """Round 5 MEDIUM-2: A (P's) and B (Q's) both mirror onto C->X in one run.

    Erasing P keeps the mirror, since B's source survives; erasing Q then removes it.
    """
    with migrated_engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES (:o, 'a@x.test')"), {"o": _OWNER})
        for pid in (_PERSONA, _OTHER):
            conn.execute(
                text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :o, 'x: 1')"),
                {"p": pid, "o": _OWNER},
            )
    backend = PostgresGraphBackend(engine=migrated_engine, persona_guard=True)
    vector = embedder.encode(["x"])[0]
    backend.insert_node(_OWNER, _node("v_a", _PERSONA), vector)
    backend.insert_node(_OWNER, _node("v_b", _OTHER), vector)
    backend.insert_node(_OWNER, _node("v_c", None), vector)
    backend.insert_node(_OWNER, _node("v_x", None), vector)
    backend.upsert_edge(_OWNER, _edge("v_a::causal::v_x", "v_a", "v_x", _PERSONA))
    backend.upsert_edge(_OWNER, _edge("v_b::causal::v_x", "v_b", "v_x", _OTHER))
    settings = GraphSettings()
    ConsolidationPass(
        backend=backend,
        index=make_graph_index(
            settings=settings,
            engine=migrated_engine,
            float32_fetch=backend.embeddings_by_surrogate,
        ),
        embedder=embedder,
        audit_logger=MemoryAuditLogger(),
        settings=settings,
    )._merge_cluster(  # noqa: SLF001 - the merge act itself
        _OWNER,
        backend.get_node(_OWNER, "v_c"),
        [backend.get_node(_OWNER, "v_a"), backend.get_node(_OWNER, "v_b")],
        run_id="rv",
        now=_NOW,
        epoch=1,
    )
    assert _open_edges(migrated_engine, "v_c") == {"v_c::causal::v_x"}

    for persona, left in ((_PERSONA, {"v_c::causal::v_x"}), (_OTHER, set())):
        with migrated_engine.begin() as conn:
            conn.execute(text("DELETE FROM personas WHERE id = :p"), {"p": persona})
        erase_persona_graph(migrated_engine, owner_id=_OWNER, persona_id=persona)
        assert _open_edges(migrated_engine, "v_c") == left, persona


def test_unmerge_leaves_a_foreign_edge_on_the_mirror_id(
    migrated_engine: Engine,
    embedder: Any,  # noqa: ANN401
) -> None:
    """Reviewer's C10: the pending record lists C->X, but C->X is the other persona's own edge.

    The merge stopped after the record (before narrowing it), so giving the member back walks a
    list naming an edge the merge never made; that edge stays, with its owner's provenance.
    """
    with migrated_engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES (:o, 'a@x.test')"), {"o": _OWNER})
        for pid in (_PERSONA, _OTHER):
            conn.execute(
                text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :o, 'x: 1')"),
                {"p": pid, "o": _OWNER},
            )
    backend = PostgresGraphBackend(engine=migrated_engine, persona_guard=True)
    vector = embedder.encode(["x"])[0]
    for node in (_node("f_c", _OTHER), _node("f_m", _PERSONA), _node("f_x", _OTHER)):
        backend.insert_node(_OWNER, node, vector)
    backend.upsert_edge(_OWNER, _edge("f_c::causal::f_x", "f_c", "f_x", _OTHER))
    backend.upsert_edge(_OWNER, _edge("f_m::causal::f_x", "f_m", "f_x", _PERSONA))
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

    def _stop(*_a: Any, **_k: Any) -> list[str]:  # noqa: ANN401
        raise RuntimeError("the merge stops after the record")

    real_close = backend.close_edges_incident
    backend.close_edges_incident = _stop  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        consolidation._merge_cluster(  # noqa: SLF001 - the merge act itself
            _OWNER,
            backend.get_node(_OWNER, "f_c"),
            [backend.get_node(_OWNER, "f_m")],
            run_id="rf",
            now=_NOW,
            epoch=1,
        )
    backend.close_edges_incident = real_close  # type: ignore[method-assign]

    assert consolidation.unmerge(_OWNER, "f_m") is True

    with migrated_engine.connect() as conn:
        owner = conn.execute(
            text(
                "SELECT provenance->>'persona_id' FROM graph_edges WHERE owner_id = :o "
                "AND id = 'f_c::causal::f_x' AND invalid_at IS NULL"
            ),
            {"o": _OWNER},
        ).scalar_one()
    assert owner == _OTHER


def test_a_legacy_comma_reason_loses_only_the_doomed_name(
    migrated_engine: Engine,
    embedder: Any,  # noqa: ANN401
) -> None:
    """Reviewer's E9: through erase_persona_graph, with the canonical's names as the vocabulary.

    The canonical's consolidation entry predates JSON reasons: "aliases:Bergen, Norway,Tromso".
    Erasing the persona of the member "Bergen, Norway" leaves "Tromso" there, and in the list.
    """
    import json  # noqa: PLC0415

    with migrated_engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES (:o, 'a@x.test')"), {"o": _OWNER})
        for pid in (_PERSONA, _OTHER):
            conn.execute(
                text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :o, 'x: 1')"),
                {"p": pid, "o": _OWNER},
            )
    backend = PostgresGraphBackend(engine=migrated_engine, persona_guard=False)
    vector = embedder.encode(["x"])[0]
    legacy = NodeProvenance(
        source=WriteSource.SYSTEM,
        written_at=_NOW,
        reason="consolidation:r1 aliases:Bergen, Norway,Tromso",
    )
    canonical = _node("l_k", _OTHER).model_copy(
        update={
            "provenance": (_by(_OTHER), legacy),
            "metadata": {"_k7_aliases": json.dumps(["Bergen, Norway", "Tromso"])},
        }
    )
    backend.insert_node(_OWNER, canonical, vector)
    for node_id, name, persona in (("l_m", "Bergen, Norway", _PERSONA), ("l_t", "Tromso", _OTHER)):
        backend.insert_node(
            _OWNER, _node(node_id, persona).model_copy(update={"concept_name": name}), vector
        )
        record = {"run_id": "r1", "canonical": "l_k", "mirrored_edges": [], "added_entities": []}
        metadata = {"_k7_merge": json.dumps(record)}
        backend.set_merged_into(
            _OWNER,
            node_id,
            merged_into="l_k",
            metadata=metadata,
            content_hash=_compute_node_hash(name, f"about {node_id}", metadata),
        )
    with migrated_engine.begin() as conn:
        conn.execute(text("DELETE FROM personas WHERE id = :p"), {"p": _PERSONA})

    erase_persona_graph(migrated_engine, owner_id=_OWNER, persona_id=_PERSONA)

    kept = backend.get_node(_OWNER, "l_k")
    assert kept is not None
    reasons = [p.reason for p in kept.provenance if p.reason]
    assert reasons == ['consolidation:r1 aliases:["Tromso"]']
    assert json.loads(kept.metadata["_k7_aliases"]) == ["Tromso"]


def test_a_co_members_stale_listing_does_not_keep_a_mirror(
    migrated_engine: Engine,
    embedder: Any,  # noqa: ANN401
) -> None:
    """Round 6 M-1: A (edge eA, P's) and B (edge eB, Q's) share C->X; B is left pending.

    Erasing P deletes eA and drops C->X from A's record; giving B back then removes C->X,
    which nothing backs any more.
    """
    import json  # noqa: PLC0415

    with migrated_engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES (:o, 'a@x.test')"), {"o": _OWNER})
        for pid in (_PERSONA, _OTHER):
            conn.execute(
                text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :o, 'x: 1')"),
                {"p": pid, "o": _OWNER},
            )
    backend = PostgresGraphBackend(engine=migrated_engine, persona_guard=True)
    vector = embedder.encode(["x"])[0]
    for node_id in ("s_a", "s_b", "s_c", "s_x"):
        backend.insert_node(_OWNER, _node(node_id, _OTHER), vector)
    backend.upsert_edge(_OWNER, _edge("s_a::causal::s_x", "s_a", "s_x", _PERSONA))
    backend.upsert_edge(_OWNER, _edge("s_b::causal::s_x", "s_b", "s_x", _OTHER))
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
    consolidation._merge_cluster(  # noqa: SLF001 - the merge act itself
        _OWNER,
        backend.get_node(_OWNER, "s_c"),
        [backend.get_node(_OWNER, "s_a"), backend.get_node(_OWNER, "s_b")],
        run_id="rs",
        now=_NOW,
        epoch=1,
    )
    b = backend.get_node(_OWNER, "s_b")
    assert b is not None
    crashed = {**json.loads(b.metadata["_k7_merge"]), "state": "pending"}
    metadata = {**b.metadata, "_k7_merge": json.dumps(crashed, sort_keys=True)}
    backend.set_merged_into(
        _OWNER,
        "s_b",
        merged_into="s_c",
        metadata=metadata,
        content_hash=_compute_node_hash(b.concept_name, b.content, metadata),
    )
    assert _open_edges(migrated_engine, "s_c") == {"s_c::causal::s_x"}

    with migrated_engine.begin() as conn:
        conn.execute(text("DELETE FROM personas WHERE id = :p"), {"p": _PERSONA})
    erase_persona_graph(migrated_engine, owner_id=_OWNER, persona_id=_PERSONA)

    assert _open_edges(migrated_engine, "s_c") == {"s_c::causal::s_x"}, "B's source backs it"
    a = backend.get_node(_OWNER, "s_a")
    assert a is not None
    record = json.loads(a.metadata["_k7_merge"])
    assert "s_c::causal::s_x" not in record["mirrored_edges"], "A's stale listing is dropped"

    assert backend.pending_merges(_OWNER) == ["s_b"]
    assert consolidation.unmerge(_OWNER, "s_b") is True

    assert _open_edges(migrated_engine, "s_c") == set(), "nothing backs C->X any more"


def test_a_mirror_two_clusters_made_survives_one_clusters_give_back(
    migrated_engine: Engine,
    embedder: Any,  # noqa: ANN401
) -> None:
    """Round 6 M-2: A1 (edge C2->A1) into C1 and B2 (edge B2->C1) into C2, one run.

    Both make C2::causal::C1; giving B2 back (its cluster failed) leaves A1's mirror.
    """
    with migrated_engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES (:o, 'a@x.test')"), {"o": _OWNER})
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :o, 'x: 1')"),
            {"p": _OTHER, "o": _OWNER},
        )
    backend = PostgresGraphBackend(engine=migrated_engine, persona_guard=True)
    vector = embedder.encode(["x"])[0]
    for node_id in ("t_c1", "t_a1", "t_c2", "t_b2"):
        backend.insert_node(_OWNER, _node(node_id, _OTHER), vector)
    backend.upsert_edge(_OWNER, _edge("t_c2::causal::t_a1", "t_c2", "t_a1", _OTHER))
    backend.upsert_edge(_OWNER, _edge("t_b2::causal::t_c1", "t_b2", "t_c1", _OTHER))
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
    for canonical, member in (("t_c1", "t_a1"), ("t_c2", "t_b2")):
        consolidation._merge_cluster(  # noqa: SLF001 - the merge act itself
            _OWNER,
            backend.get_node(_OWNER, canonical),
            [backend.get_node(_OWNER, member)],
            run_id="rt",
            now=_NOW,
            epoch=1,
        )
    assert "t_c2::causal::t_c1" in _open_edges(migrated_engine, "t_c1")

    assert consolidation.unmerge(_OWNER, "t_b2") is True

    assert "t_c2::causal::t_c1" in _open_edges(migrated_engine, "t_c1"), "A1's mirror stays"


def test_a_mirror_whose_sources_went_by_another_path_is_pruned(
    migrated_engine: Engine,
    embedder: Any,  # noqa: ANN401
) -> None:
    """Round 6 M-1 (c): the source row went outside the erasure; the next graph step cleans up.

    A user's forget cannot make this shape on its own: forgetting the member takes its record
    with it, and forgetting the other end takes the mirror too (they share that node). So the
    source row is removed here directly, as any path outside the erasure (or a delete from
    before R9-284) leaves it. The next graph step of the owner, for any persona, deletes the
    mirror nothing backs and drops it from the record.
    """
    import json  # noqa: PLC0415

    with migrated_engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES (:o, 'a@x.test')"), {"o": _OWNER})
        for pid in (_PERSONA, _OTHER):
            conn.execute(
                text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :o, 'x: 1')"),
                {"p": pid, "o": _OWNER},
            )
    backend = PostgresGraphBackend(engine=migrated_engine, persona_guard=True)
    vector = embedder.encode(["x"])[0]
    for node_id in ("o_c", "o_m", "o_x"):
        backend.insert_node(_OWNER, _node(node_id, _OTHER), vector)
    backend.upsert_edge(_OWNER, _edge("o_m::causal::o_x", "o_m", "o_x", _PERSONA))
    settings = GraphSettings()
    ConsolidationPass(
        backend=backend,
        index=make_graph_index(
            settings=settings,
            engine=migrated_engine,
            float32_fetch=backend.embeddings_by_surrogate,
        ),
        embedder=embedder,
        audit_logger=MemoryAuditLogger(),
        settings=settings,
    )._merge_cluster(  # noqa: SLF001 - the merge act itself
        _OWNER,
        backend.get_node(_OWNER, "o_c"),
        [backend.get_node(_OWNER, "o_m")],
        run_id="ro",
        now=_NOW,
        epoch=1,
    )
    assert _open_edges(migrated_engine, "o_c") == {"o_c::causal::o_x"}
    with migrated_engine.begin() as conn:  # the source row goes, not through the erasure
        conn.execute(
            text("DELETE FROM graph_edges WHERE owner_id = :o AND id = 'o_m::causal::o_x'"),
            {"o": _OWNER},
        )

    unrelated = "persona_" + "c" * 32  # names nothing in this graph
    counts = erase_persona_graph(migrated_engine, owner_id=_OWNER, persona_id=unrelated)

    assert _open_edges(migrated_engine, "o_c") == set(), "the orphan mirror is gone"
    member = backend.get_node(_OWNER, "o_m")
    assert member is not None
    record = json.loads(member.metadata["_k7_merge"])
    assert record["mirrored_edges"] == []
    assert record["mirror_sources"] == {}
    assert counts["orphan_mirrors_deleted"] == 1
    assert counts["merge_records_pruned"] == 1


def test_a_refused_done_write_leaves_no_entity_link_on_the_canonical(
    migrated_engine: Engine,
    embedder: Any,  # noqa: ANN401
) -> None:
    """Round 7 HIGH-1: P's row goes between the pending record and the done one.

    The done write holds P and is refused; the entity links commit with it, so none lands on
    the canonical, and erasing P leaves no link to the entity only P's member carried.
    """
    from persona.graph.errors import GraphPersonaGoneError  # noqa: PLC0415

    with migrated_engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES (:o, 'a@x.test')"), {"o": _OWNER})
        for pid in (_PERSONA, _OTHER):
            conn.execute(
                text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :o, 'x: 1')"),
                {"p": pid, "o": _OWNER},
            )
    backend = PostgresGraphBackend(engine=migrated_engine, persona_guard=True)
    vector = embedder.encode(["x"])[0]
    backend.insert_node(_OWNER, _node("h_k", _OTHER), vector)
    backend.insert_node(_OWNER, _node("h_m", _PERSONA), vector)
    backend.insert_entity(
        _OWNER,
        CanonicalEntity(
            id="h_ent", canonical_name="h_ent", provenance=_by(_PERSONA), created_at=_NOW
        ),
        vector,
    )
    backend.associate_entities(_OWNER, "h_m", ["h_ent"])
    consolidation = ConsolidationPass(
        backend=backend,
        index=make_graph_index(
            settings=GraphSettings(),
            engine=migrated_engine,
            float32_fetch=backend.embeddings_by_surrogate,
        ),
        embedder=embedder,
        audit_logger=MemoryAuditLogger(),
        settings=GraphSettings(),
    )
    real_close = backend.close_edges_incident

    def _close_then_delete_p(*args: Any, **kwargs: Any) -> list[str]:  # noqa: ANN401
        closed = real_close(*args, **kwargs)
        with migrated_engine.begin() as conn:
            conn.execute(text("DELETE FROM personas WHERE id = :p"), {"p": _PERSONA})
        return closed

    backend.close_edges_incident = _close_then_delete_p  # type: ignore[method-assign]
    with pytest.raises(GraphPersonaGoneError):
        consolidation._merge_cluster(  # noqa: SLF001 - the merge act itself
            _OWNER,
            backend.get_node(_OWNER, "h_k"),
            [backend.get_node(_OWNER, "h_m")],
            run_id="rh",
            now=_NOW,
            epoch=1,
        )
    backend.close_edges_incident = real_close  # type: ignore[method-assign]

    assert backend.entities_for_node(_OWNER, "h_k") == [], "no link landed with the refusal"

    erase_persona_graph(migrated_engine, owner_id=_OWNER, persona_id=_PERSONA)

    with migrated_engine.connect() as conn:
        links = conn.execute(
            text("SELECT node_id FROM graph_node_entities WHERE owner_id = :o"), {"o": _OWNER}
        ).all()
    assert links == []


def test_a_reopened_fact_does_not_block_the_erasure(
    migrated_engine: Engine,
    embedder: Any,  # noqa: ANN401
) -> None:
    """Round 8 MEDIUM-B: M's edge was closed by the merge, then a writer opened it again.

    Erasing the canonical's persona unmerges M and reopens its closed edges; the closed row
    whose id is open again is left closed, so the step completes and the id stays single.
    """
    with migrated_engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES (:o, 'a@x.test')"), {"o": _OWNER})
        for pid in (_PERSONA, _OTHER):
            conn.execute(
                text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :o, 'x: 1')"),
                {"p": pid, "o": _OWNER},
            )
    backend = PostgresGraphBackend(engine=migrated_engine, persona_guard=True)
    vector = embedder.encode(["x"])[0]
    backend.insert_node(_OWNER, _node("d_k", _PERSONA), vector)
    backend.insert_node(_OWNER, _node("d_m", _OTHER), vector)
    backend.insert_node(_OWNER, _node("d_x", _OTHER), vector)
    backend.upsert_edge(_OWNER, _edge("d_m::causal::d_x", "d_m", "d_x", _OTHER))
    ConsolidationPass(
        backend=backend,
        index=make_graph_index(
            settings=GraphSettings(),
            engine=migrated_engine,
            float32_fetch=backend.embeddings_by_surrogate,
        ),
        embedder=embedder,
        audit_logger=MemoryAuditLogger(),
        settings=GraphSettings(),
    )._merge_cluster(  # noqa: SLF001 - the merge act itself
        _OWNER,
        backend.get_node(_OWNER, "d_k"),
        [backend.get_node(_OWNER, "d_m")],
        run_id="rd",
        now=_NOW,
        epoch=1,
    )
    backend.upsert_edge(_OWNER, _edge("d_m::causal::d_x", "d_m", "d_x", _OTHER))  # reopened
    with migrated_engine.begin() as conn:
        conn.execute(text("DELETE FROM personas WHERE id = :p"), {"p": _PERSONA})

    counts = erase_persona_graph(migrated_engine, owner_id=_OWNER, persona_id=_PERSONA)

    assert counts["members_unmerged"] == 1
    assert backend.get_node(_OWNER, "d_k") is None
    with migrated_engine.connect() as conn:
        open_rows = conn.execute(
            text(
                "SELECT count(*) FROM graph_edges WHERE owner_id = :o "
                "AND id = 'd_m::causal::d_x' AND invalid_at IS NULL"
            ),
            {"o": _OWNER},
        ).scalar_one()
    assert open_rows == 1

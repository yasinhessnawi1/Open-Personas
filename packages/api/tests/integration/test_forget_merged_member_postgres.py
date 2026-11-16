"""Forgetting a merged member releases its copies on the canonical, on Postgres (R9-287).

Two members, M1 and M2, merged into a canonical C: C carries a mirror of each member's edge,
their entity links (one shared, one M1's own) and their names as aliases. Forgetting M1 (the
node delete, with the api's releaser) removes M1's mirror, M1's own entity link and M1's name,
leaves M2's mirror, the shared entity link and M2's name, and no merge record lists the
removed mirror. Runs in CI.
"""

from __future__ import annotations

import json
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
    LinkType,
    NodeKind,
    NodeProvenance,
    TypedLink,
)
from persona.graph.postgres import PostgresGraphBackend
from persona.schema.chunks import WriteSource
from persona_api.services.forget_graph import ForgetReleaser
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

_OWNER = "user_forget"
_PERSONA = "persona_ffffffffffffffffffffffffffffffff"
_NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


def _by() -> NodeProvenance:
    return NodeProvenance(source=WriteSource.SYSTEM, persona_id=_PERSONA, written_at=_NOW)


def _node(node_id: str, name: str) -> ConceptNode:
    return ConceptNode(
        id=node_id,
        node_kind=NodeKind.FACT,
        concept_name=name,
        content=f"about {node_id}",
        provenance=(_by(),),
        created_at=_NOW,
    )


def _edge(src: str, dst: str) -> TypedLink:
    return TypedLink(
        id=f"{src}::causal::{dst}",
        src_node_id=src,
        dst_node_id=dst,
        link_type=LinkType.CAUSAL,
        provenance=_by(),
        created_at=_NOW,
        valid_at=_NOW,
    )


def _merged(engine: Engine, embedder: Any) -> PostgresGraphBackend:  # noqa: ANN401
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES (:o, 'f@x.test')"), {"o": _OWNER})
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :o, 'x: 1')"),
            {"p": _PERSONA, "o": _OWNER},
        )
    backend = PostgresGraphBackend(
        engine=engine, persona_guard=True, node_releaser=ForgetReleaser()
    )
    vector = embedder.encode(["x"])[0]
    for node_id, name in (
        ("f_c", "the canonical"),
        ("f_m1", "first name"),
        ("f_m2", "second name"),
        ("f_x", "x"),
        ("f_y", "y"),
    ):
        backend.insert_node(_OWNER, _node(node_id, name), vector)
    backend.upsert_edge(_OWNER, _edge("f_m1", "f_x"))
    backend.upsert_edge(_OWNER, _edge("f_m2", "f_y"))
    for entity_id in ("f_shared", "f_own"):
        backend.insert_entity(
            _OWNER,
            CanonicalEntity(
                id=entity_id, canonical_name=entity_id, provenance=_by(), created_at=_NOW
            ),
            vector,
        )
    backend.associate_entities(_OWNER, "f_m1", ["f_shared", "f_own"])
    backend.associate_entities(_OWNER, "f_m2", ["f_shared"])
    consolidation = ConsolidationPass(
        backend=backend,
        index=make_graph_index(
            settings=GraphSettings(), engine=engine, float32_fetch=backend.embeddings_by_surrogate
        ),
        embedder=embedder,
        audit_logger=MemoryAuditLogger(),
        settings=GraphSettings(),
    )
    consolidation._merge_cluster(  # noqa: SLF001 - the merge act itself, as a run does it
        _OWNER,
        backend.get_node(_OWNER, "f_c"),
        [backend.get_node(_OWNER, "f_m1"), backend.get_node(_OWNER, "f_m2")],
        run_id="fr1",
        now=_NOW,
        epoch=1,
    )
    return backend


def _open_edges_from_canonical(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        return {
            str(dst)
            for dst in conn.execute(
                text(
                    "SELECT dst_node_id FROM graph_edges WHERE owner_id = :o "
                    "AND src_node_id = 'f_c' AND invalid_at IS NULL"
                ),
                {"o": _OWNER},
            ).scalars()
        }


def _mirror_id(engine: Engine, dst: str) -> str:
    with engine.connect() as conn:
        return str(
            conn.execute(
                text(
                    "SELECT id FROM graph_edges WHERE owner_id = :o "
                    "AND src_node_id = 'f_c' AND dst_node_id = :d"
                ),
                {"o": _OWNER, "d": dst},
            ).scalar_one()
        )


def _entities_of_canonical(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        return set(
            conn.execute(
                text(
                    "SELECT entity_id FROM graph_node_entities "
                    "WHERE owner_id = :o AND node_id = 'f_c'"
                ),
                {"o": _OWNER},
            ).scalars()
        )


def _records(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT metadata FROM graph_nodes WHERE owner_id = :o AND merged_into IS NOT NULL"
            ),
            {"o": _OWNER},
        ).scalars()
        return [json.loads(m["_k7_merge"]) for m in rows if m and "_k7_merge" in m]


def _aliases(engine: Engine) -> list[str]:
    with engine.connect() as conn:
        meta = conn.execute(
            text("SELECT metadata FROM graph_nodes WHERE owner_id = :o AND id = 'f_c'"),
            {"o": _OWNER},
        ).scalar_one()
    return json.loads((meta or {}).get("_k7_aliases", "[]"))


def test_forgetting_a_member_releases_only_its_copies(
    migrated_engine: Engine,
    embedder: Any,  # noqa: ANN401
) -> None:
    backend = _merged(migrated_engine, embedder)
    assert _open_edges_from_canonical(migrated_engine) == {"f_x", "f_y"}, "both mirrored"
    assert _entities_of_canonical(migrated_engine) == {"f_shared", "f_own"}
    assert {"first name", "second name"} <= set(_aliases(migrated_engine))
    removed = _mirror_id(migrated_engine, "f_x")
    kept = _mirror_id(migrated_engine, "f_y")

    assert backend.delete_node(_OWNER, "f_m1") is not None

    assert _open_edges_from_canonical(migrated_engine) == {"f_y"}, "M1's mirror is gone"
    assert _entities_of_canonical(migrated_engine) == {"f_shared"}, "M2 still carries it"
    aliases = _aliases(migrated_engine)
    assert "first name" not in aliases
    assert "second name" in aliases
    listed = [
        mirror
        for record in _records(migrated_engine)
        for mirror in [*record.get("mirrored_edges", []), *(record.get("mirror_sources") or {})]
    ]
    assert removed not in listed, "no record lists the removed mirror"
    assert kept in listed, "M2's record still lists its own mirror"


def test_forgetting_a_node_never_merged_is_the_plain_delete(
    migrated_engine: Engine,
    embedder: Any,  # noqa: ANN401
) -> None:
    backend = _merged(migrated_engine, embedder)

    assert backend.delete_node(_OWNER, "f_y") is not None

    # Its own edge M2->Y goes by cascade, and with it the mirror C->Y, whose only source it
    # was (the all-sources rule); M1's mirror stays.
    assert _open_edges_from_canonical(migrated_engine) == {"f_x"}
    assert _entities_of_canonical(migrated_engine) == {"f_shared", "f_own"}


# ===== D-R9-287-forget-canonical: what was merged into the node goes with it =====


def _nodes(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        return set(
            conn.execute(
                text("SELECT id FROM graph_nodes WHERE owner_id = :o"), {"o": _OWNER}
            ).scalars()
        )


def _open(engine: Engine, src: str, dst: str) -> bool:
    with engine.connect() as conn:
        return (
            conn.execute(
                text(
                    "SELECT 1 FROM graph_edges WHERE owner_id = :o AND src_node_id = :s "
                    "AND dst_node_id = :d AND invalid_at IS NULL"
                ),
                {"o": _OWNER, "s": src, "d": dst},
            ).first()
            is not None
        )


def _listed_anywhere(engine: Engine) -> set[str]:
    return {
        str(mirror)
        for record in _records(engine)
        for mirror in [*record.get("mirrored_edges", []), *(record.get("mirror_sources") or {})]
    }


def test_forgetting_a_canonical_takes_its_members_with_it(
    migrated_engine: Engine,
    embedder: Any,  # noqa: ANN401
) -> None:
    backend = _merged(migrated_engine, embedder)

    assert backend.delete_node(_OWNER, "f_c") is not None

    assert _nodes(migrated_engine) == {"f_x", "f_y"}, "the canonical and both members are gone"
    assert _records(migrated_engine) == []


def test_forgetting_a_canonical_with_a_pending_member_leaves_nothing_to_resurrect(
    migrated_engine: Engine,
    embedder: Any,  # noqa: ANN401
) -> None:
    """A pending member would be given back to life by the next run; it goes instead."""
    from persona.graph.models import _compute_node_hash  # noqa: PLC0415

    backend = _merged(migrated_engine, embedder)
    m2 = backend.get_node(_OWNER, "f_m2")
    assert m2 is not None
    crashed = {**json.loads(m2.metadata["_k7_merge"]), "state": "pending"}
    metadata = {**m2.metadata, "_k7_merge": json.dumps(crashed, sort_keys=True)}
    backend.set_merged_into(
        _OWNER,
        "f_m2",
        merged_into="f_c",
        metadata=metadata,
        content_hash=_compute_node_hash(m2.concept_name, m2.content, metadata),
        expected_content_hash=m2.content_hash,
        expected_provenance=m2.provenance,
    )
    assert backend.pending_merges(_OWNER) == ["f_m2"]

    backend.delete_node(_OWNER, "f_c")

    assert "f_m2" not in _nodes(migrated_engine)
    assert backend.pending_merges(_OWNER) == []


def test_forgetting_the_middle_of_a_chain_takes_everything_below_and_nothing_else(
    migrated_engine: Engine,
    embedder: Any,  # noqa: ANN401
) -> None:
    """A merged into B, B merged into C; forget B. A and B go; C, X and Z stay, and so do the
    edges among what stays. C's mirror of A's edge (copied through B) goes, and no record lists
    it."""
    with migrated_engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES (:o, 'f@x.test')"), {"o": _OWNER})
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :o, 'x: 1')"),
            {"p": _PERSONA, "o": _OWNER},
        )
    backend = PostgresGraphBackend(
        engine=migrated_engine, persona_guard=True, node_releaser=ForgetReleaser()
    )
    vector = embedder.encode(["x"])[0]
    for node_id, name in (
        ("ch_a", "a name"),
        ("ch_b", "b name"),
        ("ch_c", "c name"),
        ("ch_x", "x"),
        ("ch_z", "z"),
    ):
        backend.insert_node(_OWNER, _node(node_id, name), vector)
    backend.upsert_edge(_OWNER, _edge("ch_a", "ch_x"))
    backend.upsert_edge(_OWNER, _edge("ch_z", "ch_x"))
    backend.upsert_edge(_OWNER, _edge("ch_c", "ch_z"))
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
    for run, canonical, member in (("cr1", "ch_b", "ch_a"), ("cr2", "ch_c", "ch_b")):
        consolidation._merge_cluster(  # noqa: SLF001 - the merge act itself
            _OWNER,
            backend.get_node(_OWNER, canonical),
            [backend.get_node(_OWNER, member)],
            run_id=run,
            now=_NOW,
            epoch=1,
        )
    assert _open(migrated_engine, "ch_c", "ch_x"), "A's edge reached C through B"
    with migrated_engine.connect() as conn:
        mirror_on_c = str(
            conn.execute(
                text(
                    "SELECT id FROM graph_edges WHERE owner_id = :o "
                    "AND src_node_id = 'ch_c' AND dst_node_id = 'ch_x'"
                ),
                {"o": _OWNER},
            ).scalar_one()
        )

    assert backend.delete_node(_OWNER, "ch_b") is not None

    assert _nodes(migrated_engine) == {"ch_c", "ch_x", "ch_z"}
    assert not _open(migrated_engine, "ch_c", "ch_x"), "the mirror copied through B is gone"
    assert mirror_on_c not in _listed_anywhere(migrated_engine)
    assert _open(migrated_engine, "ch_z", "ch_x"), "an edge it never contained stays"
    assert _open(migrated_engine, "ch_c", "ch_z"), "the canonical's own edge stays"
    with migrated_engine.connect() as conn:
        meta = conn.execute(
            text("SELECT metadata FROM graph_nodes WHERE owner_id = :o AND id = 'ch_c'"),
            {"o": _OWNER},
        ).scalar_one()
    assert "b name" not in json.loads((meta or {}).get("_k7_aliases", "[]"))

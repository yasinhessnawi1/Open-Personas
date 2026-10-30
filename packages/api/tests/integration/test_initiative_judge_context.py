"""F2 on real Postgres: the judge's CONTEXT for a cited node (owner ruling 2026-10-30).

The A5-R-1 re-run's B7 shape: a June note says the user must prepare slides for the
Nordlys Consulting review; a later note says the user left Nordlys. Canonical entity
resolution links the two through "Nordlys Consulting" (the association table, as
production writes it), so the judge sees the job change as CONTEXT. The context passes
the scan's own gate: a wellbeing-tagged note and a note learned from an unsynthesised
(gated) conversation are linked too, and never shown.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from persona.audit import MemoryAuditLogger
from persona.graph import build_graph_store
from persona.graph.models import (
    CanonicalEntity,
    ConceptNode,
    LinkType,
    NodeKind,
    NodeProvenance,
    TypedLink,
    make_edge_id,
    make_entity_id,
)
from persona.graph.postgres import PostgresGraphBackend
from persona.schema.chunks import WriteSource
from persona_api.initiative.pipeline_wiring import ApiGroundingSource
from persona_api.initiative.wellbeing_gate import ConversationWellbeingGate
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from persona_api.tasks.store import CheckpointStore, TaskStore
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 9, 26, 5, 0, tzinfo=UTC)
_OWNER = "own_judgectx"
_PERSONA = "pers_judgectx"
_SLIDES = f"{_OWNER}::node::00000001"
_LEFT = f"{_OWNER}::node::00000002"
_NEWJOB = f"{_OWNER}::node::00000003"
_TAGGED = f"{_OWNER}::node::00000004"
_FRESH = f"{_OWNER}::node::00000005"
_DIM = 384


class _Emb:
    model_name = "test-embedder"
    dim = _DIM

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] + [0.0] * (_DIM - 1) for _ in texts]


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:  # noqa: ARG001 (ordering)
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (non-superuser role) not set; skipping RLS test")
    engine = make_rls_engine(app_url.replace("+asyncpg", "+psycopg"))
    yield engine
    engine.dispose()


def _node(
    node_id: str,
    content: str,
    *,
    days_ago: int,
    wellbeing: str | None = None,
    source: str | None = None,
) -> ConceptNode:
    at = _NOW - timedelta(days=days_ago)
    return ConceptNode(
        id=node_id,
        node_kind=NodeKind.CIRCUMSTANCE if wellbeing else NodeKind.FACT,
        concept_name=node_id.rsplit("::", 1)[-1],
        content=content,
        wellbeing_category=wellbeing,
        provenance=(
            NodeProvenance(
                source=WriteSource.PERSONA_SELF,
                persona_id=_PERSONA,
                interaction_id=source,
                interaction_kind="conversation" if source else None,
                written_at=at,
            ),
        ),
        created_at=at,
    )


def _seed(super_engine: Engine) -> None:
    with super_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:u, :e)"),
            {"u": _OWNER, "e": f"{_OWNER}@x.test"},
        )
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :u, 'name: A')"),
            {"p": _PERSONA, "u": _OWNER},
        )
        # An unsynthesised conversation: a user turn and no synthesis marker (gated for
        # the scan, so for the judge's context too).
        conn.execute(
            text("INSERT INTO conversations (id, owner_id, persona_id) VALUES (:c, :u, :p)"),
            {"c": "conv-fresh", "u": _OWNER, "p": _PERSONA},
        )
        conn.execute(
            text(
                "INSERT INTO messages (conversation_id, role, content, created_at) "
                "VALUES ('conv-fresh', 'user', 'Nordlys keeps calling me.', :t)"
            ),
            {"t": _NOW},
        )
    backend = PostgresGraphBackend(engine=super_engine)
    vec = [1.0] + [0.0] * (_DIM - 1)
    nodes = [
        _node(
            _SLIDES, "In June the user noted they must prepare the Nordlys slides.", days_ago=110
        ),
        _node(_LEFT, "The user worked at Nordlys Consulting until 31 August.", days_ago=26),
        _node(_NEWJOB, "The user started at Fjord Analytics on 1 September.", days_ago=25),
        _node(
            _TAGGED,
            "Leaving Nordlys made the user anxious.",
            days_ago=20,
            wellbeing="mental_health",
        ),
        _node(_FRESH, "Nordlys keeps calling the user.", days_ago=1, source="conv-fresh"),
    ]
    for node in nodes:
        backend.insert_node_if_absent(_OWNER, node, vec)
    nordlys = make_entity_id(_OWNER, 1)
    backend.insert_entity(
        _OWNER,
        CanonicalEntity(id=nordlys, canonical_name="Nordlys Consulting", created_at=_NOW),
        vec,
    )
    for node_id in (_SLIDES, _LEFT, _TAGGED, _FRESH):
        backend.associate_entities(_OWNER, node_id, [nordlys])
    backend.upsert_edge(
        _OWNER,
        TypedLink(
            id=make_edge_id(_LEFT, _NEWJOB, LinkType.TEMPORAL),
            src_node_id=_LEFT,
            dst_node_id=_NEWJOB,
            link_type=LinkType.TEMPORAL,
            provenance=NodeProvenance(source=WriteSource.SYSTEM, written_at=_NOW),
            created_at=_NOW,
        ),
    )


def _source(app_engine: Engine, *, wired: bool = True) -> ApiGroundingSource:
    store = build_graph_store(engine=app_engine, embedder=_Emb(), audit_logger=MemoryAuditLogger())
    gate = ConversationWellbeingGate(store, app_engine)
    if not wired:
        return ApiGroundingSource(app_engine, TaskStore(app_engine), CheckpointStore(app_engine))
    return ApiGroundingSource(
        app_engine, TaskStore(app_engine), CheckpointStore(app_engine), gate=gate
    )


def _as_owner(fn: Any) -> Any:  # noqa: ANN401
    token = current_user_id.set(_OWNER)
    try:
        return fn()
    finally:
        current_user_id.reset(token)


def test_the_slides_note_shows_the_job_change_and_nothing_gated(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed(migrated_engine)
    source = _source(app_engine)
    notes = _as_owner(lambda: source.node_context(_OWNER, _SLIDES, limit=4))
    assert [n.content for n in notes] == ["The user worked at Nordlys Consulting until 31 August."]
    assert notes[0].written_on == date(2026, 8, 31)


def test_the_newest_come_first_and_the_limit_holds(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed(migrated_engine)
    source = _source(app_engine)
    notes = _as_owner(lambda: source.node_context(_OWNER, _LEFT, limit=4))
    assert [n.content for n in notes] == [
        "The user started at Fjord Analytics on 1 September.",
        "In June the user noted they must prepare the Nordlys slides.",
    ]
    one = _as_owner(lambda: source.node_context(_OWNER, _LEFT, limit=1))
    assert [n.content for n in one] == ["The user started at Fjord Analytics on 1 September."]


def test_a_source_without_the_graph_shows_no_context(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed(migrated_engine)
    assert (
        _as_owner(lambda: _source(app_engine, wired=False).node_context(_OWNER, _SLIDES, limit=4))
        == []
    )


_HUB = f"{_OWNER}::node::00000100"


def _edge(backend: PostgresGraphBackend, src: str, dst: str, link: LinkType) -> None:
    backend.upsert_edge(
        _OWNER,
        TypedLink(
            id=make_edge_id(src, dst, link),
            src_node_id=src,
            dst_node_id=dst,
            link_type=link,
            provenance=NodeProvenance(source=WriteSource.SYSTEM, written_at=_NOW),
            created_at=_NOW,
        ),
    )


def _seed_hub(super_engine: Engine) -> PostgresGraphBackend:
    """A cited hub with twelve older temporal links, created first (so the oldest edges
    are the first ones any created_at-ascending read returns), then the newest note."""
    _seed(super_engine)
    backend = PostgresGraphBackend(engine=super_engine)
    vec = [1.0] + [0.0] * (_DIM - 1)
    backend.insert_node_if_absent(
        _OWNER,
        _node(_HUB, "The user's lease on Toftes gate runs to 31 December.", days_ago=200),
        vec,
    )
    for i in range(12):
        nid = f"{_OWNER}::node::{200 + i:08d}"
        backend.insert_node_if_absent(
            _OWNER, _node(nid, f"Old routine note {i} about the flat.", days_ago=150 - i), vec
        )
        _edge(backend, _HUB, nid, LinkType.TEMPORAL)
    newest = f"{_OWNER}::node::00000300"
    backend.insert_node_if_absent(
        _OWNER, _node(newest, "The user moved out of Toftes gate on 1 September.", days_ago=2), vec
    )
    _edge(backend, newest, _HUB, LinkType.TEMPORAL)  # pointing INTO the hub: both directions
    return backend


def test_the_newest_note_is_shown_even_past_ten_edges(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    """Review MEDIUM-1: thirteen linked notes, the newest contradicts the cited one; it
    must be the first context note, not crowded out by the ten oldest edges."""
    _seed_hub(migrated_engine)
    notes = _as_owner(lambda: _source(app_engine).node_context(_OWNER, _HUB, limit=2))
    assert [n.content for n in notes] == [
        "The user moved out of Toftes gate on 1 September.",
        "Old routine note 11 about the flat.",
    ]


def test_the_context_shows_causal_and_entity_links_and_hides_the_rest(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    """Review LOW-2: a causal link and an entity association are shown, and so is a note
    written directly (no interaction behind it: the scan's reader shows those too); a
    SELF note, a note learned from a DISCLOSED conversation, and a merged member (its
    edge created after the merge) are never shown."""
    _seed(migrated_engine)
    backend = PostgresGraphBackend(engine=migrated_engine)
    vec = [1.0] + [0.0] * (_DIM - 1)
    hub = f"{_OWNER}::node::00000400"
    causal = f"{_OWNER}::node::00000401"
    direct = f"{_OWNER}::node::00000402"
    self_note = f"{_OWNER}::node::00000403"
    disclosed = f"{_OWNER}::node::00000404"
    flagged = f"{_OWNER}::node::00000405"
    merged = f"{_OWNER}::node::00000406"
    survivor = f"{_OWNER}::node::00000407"
    with migrated_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO conversations (id, owner_id, persona_id) VALUES (:c, :u, :p)"),
            {"c": "conv-disclosed", "u": _OWNER, "p": _PERSONA},
        )
    seeds = [
        _node(hub, "The user is training for the Oslo marathon.", days_ago=40),
        _node(causal, "The user's knee injury led to a paused training plan.", days_ago=5),
        _node(direct, "The marathon registration closes on 1 October.", days_ago=4),
        _node(
            flagged,
            "The user disclosed panic attacks.",
            days_ago=3,
            wellbeing="mental_health",
            source="conv-disclosed",
        ),
        _node(
            disclosed,
            "The user skipped the marathon club this week.",
            days_ago=3,
            source="conv-disclosed",
        ),
        _node(merged, "The user runs with Kari on Sundays.", days_ago=2),
        _node(survivor, "The user runs with Kari every Sunday.", days_ago=2),
    ]
    for node in seeds:
        backend.insert_node_if_absent(_OWNER, node, vec)
    backend.insert_node_if_absent(
        _OWNER,
        ConceptNode(
            id=self_note,
            node_kind=NodeKind.SELF,
            concept_name="self",
            content="I care about running.",
            provenance=(NodeProvenance(source=WriteSource.PERSONA_SELF, written_at=_NOW),),
            created_at=_NOW,
        ),
        vec,
    )
    _edge(backend, hub, causal, LinkType.CAUSAL)
    for other in (self_note, disclosed):
        _edge(backend, hub, other, LinkType.TEMPORAL)
    with migrated_engine.begin() as conn:
        conn.execute(
            text("UPDATE graph_nodes SET merged_into = :s WHERE id = :m"),
            {"s": survivor, "m": merged},
        )
    _edge(backend, hub, merged, LinkType.TEMPORAL)  # an edge to the member after the merge
    marathon = make_entity_id(_OWNER, 2)
    backend.insert_entity(
        _OWNER, CanonicalEntity(id=marathon, canonical_name="Oslo marathon", created_at=_NOW), vec
    )
    for node_id in (hub, direct):
        backend.associate_entities(_OWNER, node_id, [marathon])
    notes = _as_owner(lambda: _source(app_engine).node_context(_OWNER, hub, limit=4))
    assert [n.content for n in notes] == [  # the direct (causal) link ranks first
        "The user's knee injury led to a paused training plan.",
        "The marathon registration closes on 1 October.",
    ]


def test_a_closed_edge_is_not_followed_and_ties_break_by_id(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed(migrated_engine)
    backend = PostgresGraphBackend(engine=migrated_engine)
    vec = [1.0] + [0.0] * (_DIM - 1)
    hub, closed, low, high = (f"{_OWNER}::node::{i:08d}" for i in (500, 501, 502, 503))
    backend.insert_node_if_absent(
        _OWNER, _node(hub, "The user rents a cabin in Hemsedal.", days_ago=30), vec
    )
    backend.insert_node_if_absent(
        _OWNER, _node(closed, "The cabin lease ended early.", days_ago=1), vec
    )
    backend.insert_node_if_absent(
        _OWNER, _node(low, "The cabin has a wood stove.", days_ago=5), vec
    )
    backend.insert_node_if_absent(
        _OWNER, _node(high, "The cabin sleeps six people.", days_ago=5), vec
    )
    for other in (closed, low, high):
        _edge(backend, hub, other, LinkType.TEMPORAL)
    with migrated_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE graph_edges SET invalid_at = valid_at + interval '1 second' "
                "WHERE dst_node_id = :d"
            ),
            {"d": closed},
        )
    notes = _as_owner(lambda: _source(app_engine).node_context(_OWNER, hub, limit=1))
    assert [n.content for n in notes] == ["The cabin sleeps six people."]  # same age: higher id


def test_direct_links_rank_ahead_of_entity_siblings(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    """Re-review LOW-1: a hub entity ("Oslo") links the cited note to many newer siblings;
    the one temporal link, older and contradicting, still comes first, and the entity
    siblings only fill the remaining slots."""
    _seed(migrated_engine)
    backend = PostgresGraphBackend(engine=migrated_engine)
    vec = [1.0] + [0.0] * (_DIM - 1)
    hub = f"{_OWNER}::node::00000600"
    moved = f"{_OWNER}::node::00000601"
    backend.insert_node_if_absent(
        _OWNER, _node(hub, "The user rents a flat in Oslo on Toftes gate.", days_ago=60), vec
    )
    backend.insert_node_if_absent(
        _OWNER, _node(moved, "The user moved out of the Toftes gate flat.", days_ago=20), vec
    )
    _edge(backend, hub, moved, LinkType.TEMPORAL)
    oslo = make_entity_id(_OWNER, 3)
    backend.insert_entity(
        _OWNER, CanonicalEntity(id=oslo, canonical_name="Oslo", created_at=_NOW), vec
    )
    backend.associate_entities(_OWNER, hub, [oslo])
    backend.associate_entities(_OWNER, moved, [oslo])  # linked both ways: still direct
    for i in range(5):
        sibling = f"{_OWNER}::node::{610 + i:08d}"
        backend.insert_node_if_absent(
            _OWNER, _node(sibling, f"An Oslo restaurant note {i}.", days_ago=5 - i), vec
        )
        backend.associate_entities(_OWNER, sibling, [oslo])
    notes = _as_owner(lambda: _source(app_engine).node_context(_OWNER, hub, limit=2))
    assert [n.content for n in notes] == [
        "The user moved out of the Toftes gate flat.",
        "An Oslo restaurant note 4.",
    ]

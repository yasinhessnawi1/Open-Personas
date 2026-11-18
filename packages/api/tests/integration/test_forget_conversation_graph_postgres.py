"""Forgetting a conversation's memory takes the facts learned from it out of the graph (R9-304).

On a migrated Postgres, through the real graph backend, the forget runs the way the route runs
it: ``chat_service.delete_conversation`` with the graph step, as ``persona_app`` under row level
security. A fact only the conversation supports goes, with its edges, versions and entity
links; a fact another conversation also supports keeps that one's entries; the SELF node keeps
dated stubs; an edge the conversation asserted goes; an entity it established goes when nothing
links to it, else loses the citation. Merges (R9-287's rules): a duplicate from this conversation
merged into a fact that stays gives back its mirror, and a duplicate another conversation
supports, merged into a fact that goes, is unmerged and stays. The conversation's row goes in
the same commit. Then the guard: a write that cites the forgotten conversation is refused, and
one in flight when the forget starts is waited for and then removed. Runs in CI.
"""

from __future__ import annotations

import functools
import os
import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from _graph_versions import snapshot_node_version
from persona.audit import MemoryAuditLogger
from persona.graph.config import GraphSettings
from persona.graph.consolidation import ConsolidationPass
from persona.graph.errors import GraphInteractionGoneError
from persona.graph.index import make_graph_index
from persona.graph.models import (
    CanonicalEntity,
    ConceptNode,
    LinkType,
    NodeKind,
    NodeProvenance,
    TypedLink,
    make_self_node_id,
)
from persona.graph.postgres import PostgresGraphBackend
from persona.schema.chunks import WriteSource
from persona_api.errors import ConversationNotFoundError
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from persona_api.services import chat_service
from persona_api.services import forget_conversation_graph as fcg
from persona_api.services.forget_graph import ForgetReleaser
from sqlalchemy import event, text

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

_OWNER = "user_conv_graph"
_PERSONA = "persona_cccccccccccccccccccccccccccccccc"
_C = "conv_forgotten_0000000000000000000000"
_D = "conv_kept_00000000000000000000000000000"
_SELF = make_self_node_id(_OWNER)
_NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


def _from(conversation: str) -> NodeProvenance:
    return NodeProvenance(
        source=WriteSource.PERSONA_SELF,
        persona_id=_PERSONA,
        interaction_id=conversation,
        interaction_kind="conversation",
        written_at=_NOW,
        grounding=f"what was said in {conversation}",
    )


def _node(node_id: str, *sources: str, kind: NodeKind = NodeKind.FACT) -> ConceptNode:
    return ConceptNode(
        id=node_id,
        node_kind=kind,
        concept_name=node_id,
        content=f"about {node_id}",
        provenance=tuple(_from(s) for s in sources),
        created_at=_NOW,
    )


def _edge(edge_id: str, src: str, dst: str, by: str) -> TypedLink:
    return TypedLink(
        id=edge_id,
        src_node_id=src,
        dst_node_id=dst,
        link_type=LinkType.CAUSAL,
        provenance=_from(by),
        created_at=_NOW,
        valid_at=_NOW,
    )


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:
    """``persona_app`` under RLS, the role the request path deletes with."""
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    with migrated_engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES (:o, 'cg@x.test')"), {"o": _OWNER})
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :o, 'x: 1')"),
            {"p": _PERSONA, "o": _OWNER},
        )
        for conversation in (_C, _D):
            conn.execute(
                text("INSERT INTO conversations (id, owner_id, persona_id) VALUES (:c, :o, :p)"),
                {"c": conversation, "o": _OWNER, "p": _PERSONA},
            )
    engine = make_rls_engine(app_url)
    token = current_user_id.set(_OWNER)
    yield engine
    current_user_id.reset(token)
    engine.dispose()


def _merge(
    engine: Engine,
    backend: PostgresGraphBackend,
    embedder: Any,  # noqa: ANN401
    canonical: str,
    member: str,
    run: str,
) -> None:
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
        backend.get_node(_OWNER, canonical),
        [backend.get_node(_OWNER, member)],
        run_id=run,
        now=_NOW,
        epoch=1,
    )


@pytest.fixture
def graph(app_engine: Engine, migrated_engine: Engine, embedder: Any) -> PostgresGraphBackend:  # noqa: ANN401
    """The owner's graph, seeded through the guarded backend (the conversations exist)."""
    backend = PostgresGraphBackend(
        engine=app_engine, persona_guard=True, node_releaser=ForgetReleaser()
    )
    vector = embedder.encode(["x"])[0]
    for node in (
        _node("only_c", _C),
        _node("theirs", _D),
        _node(_SELF, _C, kind=NodeKind.SELF),
        _node("canon_d", _D),
        _node("member_c", _C),
        _node("canon_c", _C),
        _node("member_d", _D),
    ):
        backend.insert_node(_OWNER, node, vector)
    # "shared" was learned in C, snapshotted, then extended from D: its first version is C's.
    backend.insert_node(_OWNER, _node("shared", _C), vector)
    snapshot_node_version(
        migrated_engine, _OWNER, "shared", valid_at=_NOW, invalid_at=_NOW, invalidated_by=None
    )
    backend.update_node(
        _OWNER,
        _node("shared", _C, _D),
        vector,
        expected_content_hash=_node("shared", _C).content_hash,
        expected_provenance=_node("shared", _C).provenance,
    )
    backend.upsert_edge(_OWNER, _edge("e_c", "theirs", "shared", _C))
    backend.upsert_edge(_OWNER, _edge("e_d", "shared", "theirs", _D))
    backend.upsert_edge(_OWNER, _edge("e_member", "member_c", "theirs", _D))
    for entity_id, linked in (("ent_gone", "only_c"), ("ent_kept", "theirs")):
        backend.insert_entity(
            _OWNER,
            CanonicalEntity(
                id=entity_id, canonical_name=entity_id, provenance=_from(_C), created_at=_NOW
            ),
            vector,
        )
        backend.associate_entities(_OWNER, linked, [entity_id])
    _merge(app_engine, backend, embedder, "canon_d", "member_c", "run_d")
    _merge(app_engine, backend, embedder, "canon_c", "member_d", "run_c")
    return backend


def _forget(engine: Engine, conversation: str = _C) -> dict[str, int]:
    return chat_service.delete_conversation(
        rls_engine=engine,
        conversation_id=conversation,
        forget_memory=True,
        persona_id=_PERSONA,
        graph_forget=functools.partial(
            fcg.forget_conversation_graph, owner_id=_OWNER, conversation_id=conversation
        ),
    )


def _rows(engine: Engine, sql: str) -> dict[str, Any]:
    with engine.connect() as conn:
        return {str(k): v for k, v in conn.execute(text(sql), {"o": _OWNER}).all()}


def _cited(trail: Any) -> list[str | None]:  # noqa: ANN401
    return [entry.get("interaction_id") for entry in trail]


def test_the_conversation_leaves_the_graph_and_everything_else_stays(
    app_engine: Engine, migrated_engine: Engine, graph: PostgresGraphBackend
) -> None:
    assert graph.get_node(_OWNER, "member_c") is not None
    mirrored = _rows(
        migrated_engine,
        "SELECT dst_node_id, 1 FROM graph_edges "
        "WHERE owner_id = :o AND src_node_id = 'canon_d' AND invalid_at IS NULL",
    )
    assert "theirs" in mirrored, "the member's edge was mirrored onto its canonical"

    counts = _forget(app_engine)

    nodes = _rows(migrated_engine, "SELECT id, provenance FROM graph_nodes WHERE owner_id = :o")
    assert set(nodes) == {"shared", "theirs", _SELF, "canon_d", "member_d"}
    assert _cited(nodes["shared"]) == [_D]
    assert _cited(nodes["theirs"]) == [_D]
    assert nodes[_SELF] == [{"source": "persona_self", "written_at": nodes[_SELF][0]["written_at"]}]
    assert _C not in str(nodes), "no trail cites the conversation"

    merged = _rows(migrated_engine, "SELECT id, merged_into FROM graph_nodes WHERE owner_id = :o")
    assert merged["member_d"] is None, "another conversation's duplicate is unmerged and stays"

    versions = _rows(
        migrated_engine, "SELECT node_id, provenance FROM graph_node_versions WHERE owner_id = :o"
    )
    assert "shared" not in versions, "a snapshot only the conversation fed goes"

    edges = _rows(
        migrated_engine,
        "SELECT id, 1 FROM graph_edges WHERE owner_id = :o AND invalid_at IS NULL",
    )
    assert "e_c" not in edges, "an edge the conversation asserted goes"
    assert "e_d" in edges
    mirrored = _rows(
        migrated_engine,
        "SELECT dst_node_id, 1 FROM graph_edges "
        "WHERE owner_id = :o AND src_node_id = 'canon_d' AND invalid_at IS NULL",
    )
    assert "theirs" not in mirrored, "the forgotten duplicate's mirror is given back"

    entities = _rows(
        migrated_engine, "SELECT id, provenance FROM graph_entities WHERE owner_id = :o"
    )
    assert set(entities) == {"ent_kept"}
    assert entities["ent_kept"] is None, "still linked, so only the citation goes"

    conversations = _rows(migrated_engine, "SELECT id, 1 FROM conversations WHERE owner_id = :o")
    assert set(conversations) == {_D}, "the row went in the same commit"
    assert counts["nodes_deleted"] == 3  # only_c, member_c, canon_c
    assert counts["nodes_stripped"] == 2  # shared, SELF
    assert counts["members_unmerged"] == 1


def test_the_preview_counts_what_the_forget_then_does(
    app_engine: Engine,
    graph: PostgresGraphBackend,  # noqa: ARG001 - seeds the graph
) -> None:
    preview = fcg.preview(app_engine, owner_id=_OWNER, conversation_id=_C)

    # only_c and canon_c go; shared stays without C. member_c hides behind canon_d; SELF is you.
    assert preview == fcg.ConversationGraphPreview(facts_removed=2, facts_kept=1)
    # D alone fed theirs and canon_d; shared keeps C; member_d hides behind canon_c.
    assert fcg.preview(app_engine, owner_id=_OWNER, conversation_id=_D).facts_removed == 2


def test_a_second_delete_finds_nothing_and_changes_nothing(
    app_engine: Engine,
    migrated_engine: Engine,
    graph: PostgresGraphBackend,  # noqa: ARG001
) -> None:
    _forget(app_engine)
    before = _rows(migrated_engine, "SELECT id, provenance FROM graph_nodes WHERE owner_id = :o")

    with pytest.raises(ConversationNotFoundError):
        _forget(app_engine)

    assert (
        _rows(migrated_engine, "SELECT id, provenance FROM graph_nodes WHERE owner_id = :o")
        == before
    )


def test_a_write_citing_the_forgotten_conversation_is_refused(
    app_engine: Engine,
    migrated_engine: Engine,
    graph: PostgresGraphBackend,
    embedder: Any,  # noqa: ANN401
) -> None:
    _forget(app_engine)
    vector = embedder.encode(["late"])[0]

    with pytest.raises(GraphInteractionGoneError):
        graph.insert_node(_OWNER, _node("late", _C), vector)
    with pytest.raises(GraphInteractionGoneError):
        graph.upsert_edge(_OWNER, _edge("e_late", "theirs", "shared", _C))
    graph.insert_node(_OWNER, _node("still_learning", _D), vector)

    nodes = _rows(migrated_engine, "SELECT id, 1 FROM graph_nodes WHERE owner_id = :o")
    assert "late" not in nodes
    assert "still_learning" in nodes


def test_a_write_in_flight_is_waited_for_and_then_removed(
    app_engine: Engine,
    migrated_engine: Engine,
    graph: PostgresGraphBackend,
    embedder: Any,  # noqa: ANN401
) -> None:
    """The writer holds the conversation's row from before its INSERT until it commits.

    It is paused after the INSERT. The forget, started then, must wait on the row (it cannot
    see the uncommitted node, so going ahead would leave it behind); once the writer commits,
    the forget goes on and removes the node it wrote.
    """
    vector = embedder.encode(["racing"])[0]
    written, release = threading.Event(), threading.Event()

    @event.listens_for(app_engine, "after_execute")
    def _pause_after_the_write(_conn: Any, clause: Any, *_args: Any) -> None:  # noqa: ANN401
        is_the_write = str(clause).lstrip().upper().startswith("INSERT INTO GRAPH_NODES")
        if is_the_write and not written.is_set():
            written.set()
            release.wait(timeout=60)

    errors: list[BaseException] = []

    def _in_owner_scope(work: Any) -> threading.Thread:  # noqa: ANN401
        def _run() -> None:
            token = current_user_id.set(_OWNER)
            try:
                work()
            except BaseException as exc:  # noqa: BLE001 - reported to the test
                errors.append(exc)
            finally:
                current_user_id.reset(token)

        thread = threading.Thread(target=_run)
        thread.start()
        return thread

    writer = _in_owner_scope(lambda: graph.insert_node(_OWNER, _node("racing", _C), vector))
    assert written.wait(timeout=60), "the writer never wrote"
    forget = _in_owner_scope(lambda: _forget(app_engine))
    try:
        forget.join(timeout=2)
        assert forget.is_alive(), "the forget waits for the write in flight"
    finally:
        release.set()
        writer.join(timeout=60)
        forget.join(timeout=60)
        event.remove(app_engine, "after_execute", _pause_after_the_write)

    assert errors == []
    nodes = _rows(migrated_engine, "SELECT id, 1 FROM graph_nodes WHERE owner_id = :o")
    assert "racing" not in nodes, "what the writer committed first, the forget removed"
    assert "only_c" not in nodes

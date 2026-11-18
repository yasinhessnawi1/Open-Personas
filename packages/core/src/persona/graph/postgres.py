"""Postgres + pgvector transport for the knowledge graph (Spec K0, T3).

The graph analogue of :class:`persona.stores.postgres.PostgresBackend`: a dumb SQL
transport over the three :mod:`persona.graph._schema` tables. It does NOT own the
embedder (unlike the Spec 07 backend) — the merge engine (T6) embeds a node's
content ONCE and passes the vector here, so extend-vs-create matching and storage
share one embedding. Policy/merge/audit live above it (T6/T8); this is transport
only.

Decisions in force:

- **D-K0-3:** durable string ``id`` is the identity; the ``BIGINT IDENTITY``
  ``surrogate`` is the turbovec index key — assigned by Postgres on insert and
  returned so the store can sync the index.
- **D-K0-4:** the accumulation ``provenance`` trail is stored as a JSONB array;
  ``update_node`` (extend) replaces content/embedding/trail in place — no
  ``superseded_by`` chunk-chain.
- **scope:** every method filters ``owner_id`` in ``WHERE`` (correctness) AND
  relies on RLS (the migration's direct ``owner_id`` policy) for tenant isolation
  in prod. Dim mismatches fail fast at the boundary as ``GraphIndexError``.
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy import (
    BigInteger,
    Text,
    any_,
    cast,
    delete,
    func,
    literal,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError, OperationalError

from persona.graph._schema import (
    EMBEDDING_DIM,
    graph_consolidation_markers,
    graph_edges,
    graph_entities,
    graph_node_entities,
    graph_node_versions,
    graph_nodes,
)
from persona.graph.errors import (
    GraphError,
    GraphIndexError,
    GraphInteractionGoneError,
    GraphLinkTargetGoneError,
    GraphMergeConflictError,
    GraphNodeBusyError,
    GraphPersonaGoneError,
    GraphStaleWriteError,
)
from persona.graph.maintenance_lock import graph_maintenance_held, take_graph_maintenance_lock
from persona.graph.models import (
    CanonicalEntity,
    ConceptNode,
    EntityAlias,
    LinkType,
    NodeKind,
    NodeProvenance,
    NodeVersion,
    TypedLink,
    _compute_node_hash,
    make_edge_id,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Sequence
    from contextlib import AbstractContextManager

    from sqlalchemy import Connection, Engine

__all__ = [
    "DELETE_LOCK_TIMEOUT",
    "NodeRelease",
    "NodeReleaser",
    "PostgresGraphBackend",
    "RefuseNodeDeletes",
]

#: How long a node delete waits for a running consolidation's graph lock (R9-287).
DELETE_LOCK_TIMEOUT = "15s"

# Postgres' "lock not available" (the lock timeout ran out) and "deadlock detected": the
# delete met a consolidation and lost; trying again is the answer. Anything else is not.
_BUSY_SQLSTATES = frozenset({"55P03", "40P01"})
_LOCK_TIMEOUT_SHAPE = re.compile(r"[1-9][0-9]{0,5}(ms|s)")


def _nothing_after() -> None:
    return None


@dataclass(frozen=True)
class NodeRelease:
    """What a node delete takes with it, as its releaser decided (R9-287).

    ``members`` are the nodes merged into the deleted one, directly or down a chain
    (D-R9-287-forget-canonical): duplicates of the fact the user forgets, deleted in the same
    statement. ``after`` runs once they are gone (their edges by cascade).
    """

    members: tuple[str, ...] = ()
    after: Callable[[], object] = _nothing_after


class NodeReleaser(Protocol):
    """What a node delete releases besides the node: what consolidation copied from it (R9-287).

    Called inside the delete's transaction, after the owner's graph maintenance lock is held,
    before any row goes. The api supplies it; the graph's own tests pass ``None``.
    """

    def release(self, conn: Connection, owner_id: str, node_id: str) -> NodeRelease: ...


class RefuseNodeDeletes:
    """The releaser of a process that never deletes a node (R9-287 review MEDIUM-1).

    Fail closed: a delete through it is refused in its own transaction, before anything is
    removed, rather than deleting a node and leaving what consolidation copied from it.
    """

    def release(self, conn: Connection, owner_id: str, node_id: str) -> NodeRelease:
        del conn, owner_id
        msg = "this process does not delete graph nodes"
        raise GraphError(msg, context={"node_id": node_id})


_DEFAULT_EMBEDDING_MODEL = "bge-small-en-v1.5"

# R9-284: a graph write holds the persona it names (see PostgresGraphBackend._hold_personas).
_HOLD_PERSONA = text("SELECT 1 FROM personas WHERE id = :p AND owner_id = :o FOR KEY SHARE")
# R9-304: and the conversation each entry it adds was learned from.
_HOLD_CONVERSATION = text(
    "SELECT 1 FROM conversations WHERE id = :c AND owner_id = :o FOR KEY SHARE"
)
# The kind an entry's interaction id must have to name a ``conversations`` row. A run's or a
# task leg's id names no conversation; an entry with no kind predates the field (R-K5-OPEN-CONV)
# and is only ever on a node already, never added by a current writer.
_CONVERSATION_KIND = "conversation"


def cited_conversations(entries: Iterable[NodeProvenance | None]) -> list[str]:
    """The conversations ``entries`` were learned from, once each, in a stable order (R9-304)."""
    return sorted(
        {
            p.interaction_id
            for p in entries
            if p is not None and p.interaction_id and p.interaction_kind == _CONVERSATION_KIND
        }
    )


#: Postgres' "foreign key violation": a row the write references is gone.
_FOREIGN_KEY_VIOLATION = "23503"


@contextmanager
def _link_target_gone(node_id: str) -> Iterator[None]:
    """A link write whose node (or entity) went meanwhile raises GraphLinkTargetGoneError."""
    try:
        yield
    except IntegrityError as exc:
        if getattr(exc.orig, "sqlstate", None) != _FOREIGN_KEY_VIOLATION:
            raise
        raise GraphLinkTargetGoneError(
            "a link names a node removed meanwhile", context={"node_id": node_id[:120]}
        ) from exc


def added_entries(
    trail: Sequence[NodeProvenance], expected: Sequence[NodeProvenance]
) -> list[NodeProvenance]:
    """The entries of ``trail`` that the trail the writer read did not have (R9-304)."""
    read = list(expected)
    return [p for p in trail if p not in read]


def trail_is(expected: Sequence[NodeProvenance]) -> Any:  # noqa: ANN401 - SQL
    """The row's trail is the one the writer read (D-R9-284-node-cas, the trail half).

    The content hash spans name, content and metadata, not the trail, and the erasure can
    rewrite a trail alone (a consolidation entry's alias names name no persona, so the write
    guard does not see them). Compared as jsonb, so key order does not matter, with null
    fields stripped on both sides: a row written before a field was added lacks the key that
    the writer's parsed copy has as null.
    """
    read = [p.model_dump(mode="json", exclude_none=True) for p in expected]
    return func.jsonb_strip_nulls(graph_nodes.c.provenance) == func.jsonb_strip_nulls(
        literal(read, JSONB)
    )


def _without_nulls(value: object) -> object:
    """``jsonb_strip_nulls`` in Python: object fields whose value is null go, recursively."""
    if isinstance(value, dict):
        return {k: _without_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_without_nulls(v) for v in value]
    return value


def trail_matches(stored: object, expected: Sequence[NodeProvenance]) -> bool:
    """:func:`trail_is` for a row already read (the supersede's locked row)."""
    read = [p.model_dump(mode="json", exclude_none=True) for p in expected]
    return _without_nulls(stored or []) == _without_nulls(read)


def _refuse_if_stale(conn: Connection, owner_id: str, node_id: str, exists: bool | None) -> None:
    """A CAS that wrote nothing: raise if the row is there (it changed), else it is absent."""
    if exists is None:
        exists = (
            conn.execute(
                select(graph_nodes.c.id).where(
                    graph_nodes.c.id == node_id, graph_nodes.c.owner_id == owner_id
                )
            ).first()
            is not None
        )
    if exists:
        raise GraphStaleWriteError(
            "the node changed since it was read", context={"node_id": node_id}
        )


def keep_merge_record(metadata: dict[str, str], current: object) -> dict[str, str]:
    """``metadata`` with the row's merge record in place of the caller's (round 9 MEDIUM-1).

    Only ``_k7_merge``: other ``_k7_*`` keys (the alias list) are the caller's to write.
    """
    kept = {k: v for k, v in metadata.items() if k != "_k7_merge"}
    if isinstance(current, dict) and isinstance(current.get("_k7_merge"), str):
        kept["_k7_merge"] = current["_k7_merge"]
    return kept


def reopen_statement(owner_id: str, node_id: str, invalidated_by: str) -> Any:  # noqa: ANN401
    """Re-open the node's edges closed under ``invalidated_by``, but never a second open row.

    A writer may have opened a new row with the same id since (K2's typed links do not look at
    merged endpoints); re-opening the old one too would break the one-open-row-per-id index
    and fail the whole statement, every time (R9-284 round 8 MEDIUM-B). That row is left
    closed: the fact is open already. Shared by unmerge and the erasure's unmerge of members.
    """
    open_twin = graph_edges.alias("open_twin")
    newest = graph_edges.alias("newest")
    return (
        update(graph_edges)
        .where(
            graph_edges.c.owner_id == owner_id,
            graph_edges.c.invalidated_by == invalidated_by,
            or_(graph_edges.c.src_node_id == node_id, graph_edges.c.dst_node_id == node_id),
            # Only the newest row per id closed under this tag (round 9 LOW-3): two rows of one
            # fact closed under the same tag would otherwise both reopen, and break the index.
            graph_edges.c.edge_key
            == select(func.max(newest.c.edge_key))
            .where(
                newest.c.owner_id == owner_id,
                newest.c.id == graph_edges.c.id,
                newest.c.invalidated_by == invalidated_by,
            )
            .scalar_subquery(),
            ~select(open_twin.c.id)
            .where(
                open_twin.c.owner_id == owner_id,
                open_twin.c.id == graph_edges.c.id,
                open_twin.c.invalid_at.is_(None),
            )
            .exists(),
        )
        .values(invalid_at=None, invalidated_by=None, invalidated_at=None)
    )


class PostgresGraphBackend:
    """Transport over a Postgres + pgvector engine for the graph (D-K0-3).

    One instance owns one engine/pool; rows are partitioned by ``owner_id``. The
    caller (the API composition root, or a test) owns the engine lifecycle and any
    RLS ``set_config`` plumbing — the transport just issues parameterised
    statements. Vectors are supplied pre-computed (the store embeds once).
    """

    def __init__(
        self,
        *,
        engine: Engine,
        persona_guard: bool,
        node_releaser: NodeReleaser | None,
        delete_lock_timeout: str = DELETE_LOCK_TIMEOUT,
    ) -> None:
        if _LOCK_TIMEOUT_SHAPE.fullmatch(delete_lock_timeout) is None:
            msg = "delete_lock_timeout must look like '15s' or '200ms'"
            raise ValueError(msg)
        self._delete_lock_timeout = delete_lock_timeout
        self._engine = engine
        # R9-287: required like ``persona_guard``, so no construction can forget it. ``None``
        # only where nothing deletes a node through this backend (the graph's own tests).
        self._node_releaser = node_releaser
        # R9-284: hold each named persona's row in the write's own transaction. Off where the
        # schema has no ``personas`` table (the graph's own tests); the api turns it on. It
        # holds each cited conversation's row too (R9-304), from the same schema.
        self._persona_guard = persona_guard

    def _hold_personas(
        self,
        conn: Connection,
        owner_id: str,
        persona_ids: Iterable[str | None],
        *,
        cites: Iterable[NodeProvenance | None] = (),
    ) -> None:
        """Hold the row of every persona this write names, or refuse the write (R9-284).

        ``FOR KEY SHARE`` on the owner's persona row, in the same transaction as the write: an
        erasure's row delete waits for this write to commit (and its graph step then removes
        what it wrote), and a write after the delete finds no row and raises
        :class:`GraphPersonaGoneError`. A no-op with the guard off.

        ``cites`` are the provenance entries the write adds. Each conversation they cite is
        held the same way, after the personas (R9-304): forgetting a conversation's memory
        removes its facts in the transaction that deletes its row, so a synthesis still in its
        model call either lands first and is removed, or finds no row and raises
        :class:`GraphInteractionGoneError`. Only added entries: an older entry may name a
        conversation deleted without forgetting it, and that must not lock the node.
        """
        if not self._persona_guard:
            return
        for persona_id in sorted({p for p in persona_ids if p}):
            held = conn.execute(_HOLD_PERSONA, {"p": persona_id, "o": owner_id}).first()
            if held is None:
                raise GraphPersonaGoneError(
                    "a graph write names a persona that no longer exists",
                    context={"persona_id": persona_id[:80]},
                )
        for conversation_id in cited_conversations(cites):
            held = conn.execute(_HOLD_CONVERSATION, {"c": conversation_id, "o": owner_id}).first()
            if held is None:
                raise GraphInteractionGoneError(
                    "a graph write cites a conversation that no longer exists",
                    context={"interaction_id": conversation_id[:80]},
                )

    # ===== nodes ===========================================================

    def insert_node(
        self,
        owner_id: str,
        node: ConceptNode,
        embedding: Sequence[float],
        *,
        embedding_model: str = _DEFAULT_EMBEDDING_MODEL,
    ) -> int:
        """Insert a node row; return the Postgres-assigned ``surrogate`` (index key)."""
        row = self._node_to_row(owner_id, node, embedding, embedding_model)
        stmt = pg_insert(graph_nodes).values(**row).returning(graph_nodes.c.surrogate)
        with self._engine.begin() as conn:
            self._hold_personas(
                conn, owner_id, (p.persona_id for p in node.provenance), cites=node.provenance
            )
            surrogate = conn.execute(stmt).scalar_one()
        return int(surrogate)

    def insert_node_if_absent(
        self,
        owner_id: str,
        node: ConceptNode,
        embedding: Sequence[float],
        *,
        embedding_model: str = _DEFAULT_EMBEDDING_MODEL,
    ) -> int | None:
        """Insert a node only if its id is free; return the surrogate, or ``None`` on conflict.

        The race-safe get-or-create primitive (Spec K6): ``ON CONFLICT (id) DO
        NOTHING`` so two concurrent inserts of the same reserved id (the self node)
        collapse to a single row — the winner gets a ``surrogate``, the loser gets
        ``None`` (and re-reads the winner). Distinct from :meth:`insert_node`, which
        assumes a fresh id and raises on a duplicate.
        """
        row = self._node_to_row(owner_id, node, embedding, embedding_model)
        stmt = (
            pg_insert(graph_nodes)
            .values(**row)
            .on_conflict_do_nothing(index_elements=["id"])
            .returning(graph_nodes.c.surrogate)
        )
        with self._engine.begin() as conn:
            self._hold_personas(
                conn, owner_id, (p.persona_id for p in node.provenance), cites=node.provenance
            )
            surrogate = conn.execute(stmt).scalar_one_or_none()
        return None if surrogate is None else int(surrogate)

    def update_node(
        self,
        owner_id: str,
        node: ConceptNode,
        embedding: Sequence[float],
        *,
        expected_content_hash: str,
        expected_provenance: Sequence[NodeProvenance],
        embedding_model: str = _DEFAULT_EMBEDDING_MODEL,
        guard_personas: Sequence[str] = (),
    ) -> int | None:
        """Replace a node's mutable fields in place (the extend path, D-K0-4).

        Keeps ``id``/``owner_id``/``surrogate``/``created_at``; refreshes content,
        embedding, metadata, wellbeing tag, content_hash, and the provenance trail.
        Returns the ``surrogate`` (for index replace) or ``None`` if absent.

        A compare-and-set (D-R9-284-node-cas): the row must still have
        ``expected_content_hash`` and ``expected_provenance``, the hash and trail the caller
        read, or :class:`GraphStaleWriteError` is raised and nothing is written; the caller
        re-reads and retries. The trail is compared because the hash does not cover it.

        The merge record (``_k7_merge``) is lifecycle state, like ``merged_into``: the row's
        is written, whatever the caller's metadata says (round 9 MEDIUM-1). Under the CAS a
        stale copy can no longer drop it, but a fresh read can still carry foreign metadata: a
        version restore writes the snapshot's, which may hold an old record or none. So the
        row's record is read (no lock: if the row changes before the write, the CAS refuses)
        and kept, and the content hash is recomputed over the metadata actually written.
        """
        self._check_dim(embedding, node.id)
        with self._engine.begin() as conn:
            self._hold_personas(
                conn,
                owner_id,
                [*(p.persona_id for p in node.provenance), *guard_personas],
                cites=added_entries(node.provenance, expected_provenance),
            )
            current = conn.execute(
                select(graph_nodes.c.metadata).where(
                    graph_nodes.c.id == node.id, graph_nodes.c.owner_id == owner_id
                )
            ).scalar_one_or_none()
            metadata = keep_merge_record(dict(node.metadata), current)
            content_hash = (
                node.content_hash
                if metadata == dict(node.metadata)
                else _compute_node_hash(node.concept_name, node.content, metadata)
            )
            surrogate = conn.execute(
                self._update_node_statement(
                    owner_id, node, embedding, embedding_model, metadata, content_hash
                ).where(
                    graph_nodes.c.content_hash == expected_content_hash,
                    trail_is(expected_provenance),
                )
            ).scalar_one_or_none()
            if surrogate is None:
                _refuse_if_stale(conn, owner_id, node.id, current is not None)
        return None if surrogate is None else int(surrogate)

    def append_provenance(
        self,
        owner_id: str,
        node_id: str,
        entry: NodeProvenance,
        *,
        expected_content_hash: str,
        expected_provenance: Sequence[NodeProvenance],
    ) -> bool:
        """Append one entry to a node's trail, and nothing else (R9-304 review round 2).

        A restatement's support entry: content, metadata, embedding and embedding model are
        not written, so the content hash stays and no version is taken. Under the node CAS
        (the hash and trail the caller read, :class:`GraphStaleWriteError` if the row
        changed) and the write guard (the entry's persona and conversation are held first).
        ``False`` if the node is gone. ``updated_at`` advances, as for any node write.
        """
        # Appended in SQL, under the CAS on the trail as read (review round 3).
        appended = graph_nodes.c.provenance.concat(
            func.jsonb_build_array(literal(entry.model_dump(mode="json"), JSONB))
        )
        with self._engine.begin() as conn:
            self._hold_personas(conn, owner_id, [entry.persona_id], cites=[entry])
            written = conn.execute(
                update(graph_nodes)
                .where(
                    graph_nodes.c.id == node_id,
                    graph_nodes.c.owner_id == owner_id,
                    graph_nodes.c.content_hash == expected_content_hash,
                    trail_is(expected_provenance),
                )
                .values(provenance=appended, updated_at=datetime.now(UTC))
                .returning(graph_nodes.c.id)
            ).first()
            if written is None:
                _refuse_if_stale(conn, owner_id, node_id, None)
                return False
        return True

    def _update_node_statement(
        self,
        owner_id: str,
        node: ConceptNode,
        embedding: Sequence[float],
        embedding_model: str,
        metadata: dict[str, str],
        content_hash: str,
    ) -> Any:  # noqa: ANN401 - an UPDATE
        return (
            update(graph_nodes)
            .where(graph_nodes.c.id == node.id, graph_nodes.c.owner_id == owner_id)
            .values(
                node_kind=str(node.node_kind),
                concept_name=node.concept_name,
                content=node.content,
                metadata=metadata,
                wellbeing_category=node.wellbeing_category,
                embedding=list(embedding),
                embedding_model=embedding_model,
                content_hash=content_hash,
                provenance=[p.model_dump(mode="json") for p in node.provenance],
                # Advance the dirty-neighbourhood watermark (K7-D-4). salience /
                # last_evidence_epoch / merged_into are lifecycle state and are
                # deliberately NOT touched by a content update.
                updated_at=datetime.now(UTC),
            )
            .returning(graph_nodes.c.surrogate)
        )

    def get_node(self, owner_id: str, node_id: str) -> ConceptNode | None:
        stmt = select(graph_nodes).where(
            graph_nodes.c.id == node_id, graph_nodes.c.owner_id == owner_id
        )
        with self._engine.connect() as conn:
            row = conn.execute(stmt).mappings().one_or_none()
        return None if row is None else self._row_to_node(dict(row))

    def get_nodes_by_surrogates(
        self, owner_id: str, surrogates: Sequence[int]
    ) -> dict[int, ConceptNode]:
        """Hydrate nodes for index-search results (surrogate → node)."""
        if not surrogates:
            return {}
        stmt = select(graph_nodes).where(
            graph_nodes.c.owner_id == owner_id,
            graph_nodes.c.surrogate.in_(list(surrogates)),
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return {int(r["surrogate"]): self._row_to_node(dict(r)) for r in rows}

    def surrogate_for(self, owner_id: str, node_id: str) -> int | None:
        stmt = select(graph_nodes.c.surrogate).where(
            graph_nodes.c.id == node_id, graph_nodes.c.owner_id == owner_id
        )
        with self._engine.connect() as conn:
            val = conn.execute(stmt).scalar_one_or_none()
        return None if val is None else int(val)

    def surrogates_for_owner(self, owner_id: str) -> list[int]:
        """All of the user's node surrogates — the dense-search allowlist (criterion 6)."""
        stmt = select(graph_nodes.c.surrogate).where(graph_nodes.c.owner_id == owner_id)
        with self._engine.connect() as conn:
            return [int(r[0]) for r in conn.execute(stmt)]

    def surrogates_for_nodes(self, owner_id: str, node_ids: Sequence[str]) -> list[int]:
        """Surrogates for the given node-ids, owner-scoped (the K4-subtraction allowlist)."""
        if not node_ids:
            return []
        stmt = select(graph_nodes.c.surrogate).where(
            graph_nodes.c.owner_id == owner_id, graph_nodes.c.id.in_(list(node_ids))
        )
        with self._engine.connect() as conn:
            return [int(r[0]) for r in conn.execute(stmt)]

    def flagged_nodes(self, owner_id: str) -> list[ConceptNode]:
        """The owner's wellbeing-tagged nodes (K4 gate-eligible flagged set; criterion 5).

        Every node whose ``wellbeing_category`` is set, hydrated with its full
        provenance trail so K4 can compute recency. Owner-scoped (RLS in prod).
        """
        stmt = select(graph_nodes).where(
            graph_nodes.c.owner_id == owner_id,
            graph_nodes.c.wellbeing_category.isnot(None),
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._row_to_node(dict(r)) for r in rows]

    def node_ids_for_owner(self, owner_id: str) -> list[str]:
        """All of the owner's durable node ids — the K4 positive-allowlist enumeration."""
        stmt = select(graph_nodes.c.id).where(graph_nodes.c.owner_id == owner_id)
        with self._engine.connect() as conn:
            return [str(r[0]) for r in conn.execute(stmt)]

    def recent_nodes(self, owner_id: str, *, limit: int) -> list[ConceptNode]:
        """The A5 noticing pool (Spec A5, A5-D-X-reads; the ``flagged_nodes`` shape).

        Salience DESC then ``updated_at`` DESC (K7's evidence signal as the pool
        ORDERING — never a retrieval gate), excluding at the read: the SELF
        anchor, merged members, and every wellbeing-tagged node (the initiative
        subject-exclusion, A5-D-X-k4-initiative-side). Owner-scoped (RLS in prod).
        """
        stmt = (
            select(graph_nodes)
            .where(
                graph_nodes.c.owner_id == owner_id,
                graph_nodes.c.node_kind != NodeKind.SELF.value,
                graph_nodes.c.merged_into.is_(None),
                graph_nodes.c.wellbeing_category.is_(None),
            )
            .order_by(graph_nodes.c.salience.desc(), graph_nodes.c.updated_at.desc())
            .limit(limit)
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._row_to_node(dict(r)) for r in rows]

    def delete_node(self, owner_id: str, node_id: str) -> int | None:
        """Delete a node (edges cascade); return its ``surrogate`` for index removal.

        :meth:`delete_node_and_members`, keeping only the node's own surrogate.
        """
        return self.delete_node_and_members(owner_id, node_id).get(node_id)

    def delete_node_and_members(self, owner_id: str, node_id: str) -> dict[str, int]:
        """Delete a node and what was merged into it; ``{deleted id: surrogate}`` (R9-287).

        Empty when the node was not there. Every id the DELETE returned is in it, so the store
        can audit each node that went (round 2 LOW-2).

        With a node releaser (R9-287) the delete runs under the owner's graph maintenance lock,
        waiting at most :data:`DELETE_LOCK_TIMEOUT` for a running consolidation
        (:class:`GraphNodeBusyError` after that), and releases what consolidation copied from
        the node (its mirrors, entity links and names on the canonical) in the same transaction.
        Every node merged into it, directly or down a chain, goes in the same statement
        (D-R9-287-forget-canonical). A member left the dense index when it was merged, and a
        stale entry is benign.
        """
        try:
            with self._engine.begin() as conn:
                release = NodeRelease()
                if self._node_releaser is not None:
                    if conn.dialect.name == "postgresql":
                        # The shape is checked in __init__: digits and a unit, nothing else.
                        conn.execute(
                            text(f"SET LOCAL lock_timeout = '{self._delete_lock_timeout}'")
                        )
                    take_graph_maintenance_lock(conn, owner_id)
                    release = self._node_releaser.release(conn, owner_id, node_id)
                ids = [node_id, *(m for m in release.members if m != node_id)]
                gone: dict[str, int] = {
                    str(row[0]): int(row[1])
                    for row in conn.execute(
                        delete(graph_nodes)
                        .where(
                            graph_nodes.c.owner_id == owner_id,
                            # One array parameter, however many members (review nit).
                            graph_nodes.c.id == any_(literal(ids, ARRAY(Text))),
                        )
                        .returning(graph_nodes.c.id, graph_nodes.c.surrogate)
                    ).all()
                }
                release.after()
        except OperationalError as exc:
            if getattr(exc.orig, "sqlstate", None) not in _BUSY_SQLSTATES:
                raise
            raise GraphNodeBusyError(
                "the graph is being reorganised", context={"node_id": node_id}
            ) from exc
        return gone

    # ===== dense + sparse retrieval (the K1 legs' SQL) =====================

    def dense_query(
        self,
        owner_id: str,
        query_vector: Sequence[float],
        top_k: int,
        *,
        allowed_surrogates: Sequence[int] | None = None,
        exclude_self: bool = False,
    ) -> list[ConceptNode]:
        """Exact pgvector cosine search, allowlist-scoped (the pgvector dense leg).

        ``allowed_surrogates=None`` → all the user's nodes; an empty sequence →
        no candidates (returns ``[]``) — isolation never relies on ``None``
        (design call #3). Returned nodes carry ``distance``.

        **Consolidated nodes are never returned** (``merged_into IS NULL``, K7-D-4 /
        K7-D-X-read-surface — a merged node left the retrievable set). ``exclude_self``
        additionally drops the ``NodeKind.SELF`` anchor (K7-D-7): the merge engine
        passes it for the extend-target top-1 so the anchor is never an extend/evolve
        target; semantic-link wiring leaves it off (SELF may still be a link end).
        """
        self._check_dim(query_vector, "<query>")
        if allowed_surrogates is not None and len(allowed_surrogates) == 0:
            return []
        q_vec = list(query_vector)
        distance = graph_nodes.c.embedding.cosine_distance(q_vec).label("distance")
        stmt = select(graph_nodes, distance).where(
            graph_nodes.c.owner_id == owner_id,
            graph_nodes.c.merged_into.is_(None),
        )
        if exclude_self:
            stmt = stmt.where(graph_nodes.c.node_kind != str(NodeKind.SELF))
        if allowed_surrogates is not None:
            stmt = stmt.where(graph_nodes.c.surrogate.in_(list(allowed_surrogates)))
        stmt = stmt.order_by(distance).limit(top_k)
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._row_to_node(dict(r), distance=float(r["distance"])) for r in rows]

    def fts_query(self, owner_id: str, query: str, top_k: int) -> list[ConceptNode]:
        """Postgres FTS (BM25-class) over node content — the K1 sparse leg (crit 3)."""
        tsquery = func.websearch_to_tsquery("english", query)
        rank = func.ts_rank(graph_nodes.c.fts, tsquery)
        stmt = (
            select(graph_nodes)
            .where(graph_nodes.c.owner_id == owner_id, graph_nodes.c.fts.op("@@")(tsquery))
            .order_by(rank.desc())
            .limit(top_k)
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._row_to_node(dict(r)) for r in rows]

    def count_nodes(self, owner_id: str) -> int:
        """Number of the user's ``::node::`` fact nodes (the next ``make_node_id`` index).

        Excludes the K6 ``SELF`` node (Spec K6): it carries a reserved ``::self`` id
        outside the monotonic ``::node::{index}`` scheme, so counting it would push
        fact ids past their true index (harmless gaps, but the two id schemes are
        cleaner kept independent). Only fact-kind nodes participate in this count.
        """
        stmt = (
            select(func.count())
            .select_from(graph_nodes)
            .where(
                graph_nodes.c.owner_id == owner_id,
                graph_nodes.c.node_kind != str(NodeKind.SELF),
            )
        )
        with self._engine.connect() as conn:
            return int(conn.execute(stmt).scalar_one())

    def seed_nodes(self, owner_id: str, *, limit: int) -> list[ConceptNode]:
        """The first-paint seed window: the owner's most-recent nodes (K5-D-8, B1-refined).

        Ordered by ``created_at`` DESC — "what you've been thinking about lately" — served by
        the ``(owner_id, created_at)`` index as a bounded backward index-scan (O(limit), flat
        regardless of graph size). Recency is the v1 product choice (the seed is recoverable —
        the user explores outward); the degree-"anchor" alternative is a recorded planned option
        (a materialised degree column on the write path) — see decisions.md B1. Degree-for-sizing
        stays free per-window via :meth:`edges_among`.
        """
        if limit <= 0:
            return []
        stmt = (
            select(graph_nodes)
            .where(
                graph_nodes.c.owner_id == owner_id,
                graph_nodes.c.merged_into.is_(None),  # consolidated-away nodes stay hidden (K7-D-4)
            )
            .order_by(graph_nodes.c.created_at.desc())
            .limit(limit)
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._row_to_node(dict(r)) for r in rows]

    def edges_among(self, owner_id: str, node_ids: Sequence[str]) -> list[TypedLink]:
        """The stored typed edges whose BOTH endpoints lie in ``node_ids``.

        The induced sub-graph of a window (K5-D-2) in one query — semantic,
        temporal, and causal edges (which are materialised in ``graph_edges``).
        ENTITY links are resolved on-the-fly (D-K0-9) and are NOT returned here;
        the detail panel surfaces them via :meth:`neighbors`. RLS-scoped read.
        """
        ids = list(node_ids)
        if len(ids) < 2:  # noqa: PLR2004 — an edge needs two distinct endpoints in the set
            return []
        stmt = select(graph_edges).where(
            graph_edges.c.owner_id == owner_id,
            graph_edges.c.src_node_id.in_(ids),
            graph_edges.c.dst_node_id.in_(ids),
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._row_to_link(dict(r)) for r in rows]

    def entity_edges_among(self, owner_id: str, node_ids: Sequence[str]) -> list[TypedLink]:
        """Synthesised ENTITY links among a node set (D-K12-A; K12 thread A).

        The seed window historically showed only ``edges_among``'s materialised
        semantic/temporal/causal edges — entity relations (captured in the
        ``graph_node_entities`` join table) never surfaced there, only on the
        focus/detail path (:meth:`neighbors`/:meth:`entity_neighbors`, D-K0-9).
        This expands that same join table across ``node_ids``: for every pair
        that shares >=1 canonical entity, one on-the-fly ``TypedLink(link_type=
        ENTITY)`` — mirroring the edge shape :meth:`~persona.graph.store.
        PostgresGraphStore.neighbors` already synthesises (deterministic
        ``make_edge_id``, no ``graph_edges`` row). ``DISTINCT`` on the node-id
        pair collapses multiple shared entities to one edge; ``node_id <
        node_id`` picks a single direction per pair (no duplicate reverse edge).
        Both endpoints must be live (``merged_into IS NULL``, K7-D-4) — a
        consolidated-away node contributes no entity edge. RLS-scoped; a read
        (CQS).
        """
        ids = list(node_ids)
        if len(ids) < 2:  # noqa: PLR2004 — an edge needs two distinct endpoints in the set
            return []
        mine = graph_node_entities.alias("mine")
        theirs = graph_node_entities.alias("theirs")
        src_nodes = graph_nodes.alias("src_nodes")
        dst_nodes = graph_nodes.alias("dst_nodes")
        stmt = (
            select(mine.c.node_id.label("src"), theirs.c.node_id.label("dst"))
            .distinct()
            .select_from(mine.join(theirs, mine.c.entity_id == theirs.c.entity_id))
            .join(src_nodes, src_nodes.c.id == mine.c.node_id)
            .join(dst_nodes, dst_nodes.c.id == theirs.c.node_id)
            .where(
                mine.c.owner_id == owner_id,
                theirs.c.owner_id == owner_id,
                src_nodes.c.owner_id == owner_id,
                dst_nodes.c.owner_id == owner_id,
                mine.c.node_id.in_(ids),
                theirs.c.node_id.in_(ids),
                mine.c.node_id < theirs.c.node_id,
                src_nodes.c.merged_into.is_(None),
                dst_nodes.c.merged_into.is_(None),
            )
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        now = datetime.now(UTC)
        return [
            TypedLink(
                id=make_edge_id(str(src), str(dst), LinkType.ENTITY),
                src_node_id=str(src),
                dst_node_id=str(dst),
                link_type=LinkType.ENTITY,
                created_at=now,
            )
            for src, dst in rows
        ]

    def next_node_index(self, owner_id: str) -> int:
        """The next collision-free ``make_node_id`` index for the owner (Spec K7, K7-D-9).

        **Fixes the latent delete→create id collision** the Phase-1 audit flagged:
        ``count_nodes`` (create → 0,1,2; delete id 1; count → 2; next create reuses
        index 2 → collides with the live node 2). The fix is ``MAX(live index) + 1``:
        we always allocate strictly above every LIVE ``::node::`` id, so a deleted
        id's number is only ever reused when it is genuinely gone — two live nodes can
        never share an id (also PK-guarded). ``split_part(id, '::', -1)`` reads the
        trailing index segment (robust to an ``owner_id`` containing ``::``). The K6
        ``{owner}::self`` anchor is the only id excluded — by its SHAPE (see the
        WHERE clause), never by node_kind (R4-C1-12, which cost dead-lettered jobs).

        **K5 coordination (state.md cross-spec item 1):** K7 implemented this
        allocator; R4 corrected the exclusion. One canonical allocator, no double-fix.
        """
        stmt = select(
            func.coalesce(
                func.max(func.split_part(graph_nodes.c.id, "::", -1).cast(BigInteger)),
                -1,
            )
            + 1
        ).where(
            graph_nodes.c.owner_id == owner_id,
            # Exclude by ID SHAPE, not node_kind (R4-C1-12). Only the K6 anchor
            # ``{owner}::self`` must be skipped (its trailing segment is the literal
            # ``self`` — un-castable). Excluding by node_kind was WRONG: a SELF-KIND
            # node can carry a numeric ``{owner}::node::NNNNN`` id (synthesis/entity
            # mis-classifying a person as self), and dropping it from the MAX made the
            # allocator re-hand-out its index → INSERT PK collision that dead-lettered
            # every synthesis job. ``::node::`` ids have ``node`` as the 2nd-to-last
            # segment; the ``::self`` anchor does not — so this counts every numeric
            # node (any kind) and only ever skips the anchor.
            func.split_part(graph_nodes.c.id, "::", -2) == "node",
        )
        with self._engine.connect() as conn:
            return int(conn.execute(stmt).scalar_one())

    def is_merged(self, owner_id: str, node_id: str) -> bool:
        """Whether ``node_id`` has been consolidated into a canonical (K7-D-4/-7).

        A merged node must never be an extend/evolve target; ``get_node`` still
        returns it (K5 inspection / restore), so the merge engine checks this before
        evolving an explicitly-named target.
        """
        stmt = select(graph_nodes.c.merged_into).where(
            graph_nodes.c.id == node_id, graph_nodes.c.owner_id == owner_id
        )
        with self._engine.connect() as conn:
            val = conn.execute(stmt).scalar_one_or_none()
        return val is not None

    # ===== node versions (Spec K7, K7-D-1) ================================

    def supersede_node(
        self,
        owner_id: str,
        node: ConceptNode,
        embedding: Sequence[float],
        *,
        expected_content_hash: str,
        expected_provenance: Sequence[NodeProvenance],
        valid_at: datetime,
        invalid_at: datetime,
        invalidated_by: str | None,
        embedding_model: str = _DEFAULT_EMBEDDING_MODEL,
        guard_personas: Sequence[str] = (),
    ) -> int | None:
        """Snapshot the node as a closed version and write ``node`` over it, in one commit.

        The supersede (K7-D-1) under the node CAS (R9-284 round 11): the personas are held,
        the row is locked ``FOR NO KEY UPDATE``, its hash and trail are checked against the
        caller's read (:class:`GraphStaleWriteError` on a mismatch, before anything is
        written), the version is inserted from the locked row, and the update runs. A lost
        CAS leaves no version behind, and a version is never taken of a row an erasure has
        since rewritten. Returns the version key, or ``None`` if the node is gone.
        """
        self._check_dim(embedding, node.id)
        with self._engine.begin() as conn:
            self._hold_personas(
                conn,
                owner_id,
                [
                    *(p.persona_id for p in node.provenance),
                    *(p.persona_id for p in expected_provenance),
                    *guard_personas,
                ],
                cites=added_entries(node.provenance, expected_provenance),
            )
            row = (
                conn.execute(
                    select(graph_nodes)
                    .where(graph_nodes.c.id == node.id, graph_nodes.c.owner_id == owner_id)
                    .with_for_update(key_share=True)
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            if row["content_hash"] != expected_content_hash or not trail_matches(
                row["provenance"], expected_provenance
            ):
                raise GraphStaleWriteError(
                    "the node changed since it was read", context={"node_id": node.id}
                )
            version_key = int(
                conn.execute(
                    pg_insert(graph_node_versions)
                    .values(
                        owner_id=owner_id,
                        node_id=node.id,
                        node_kind=row["node_kind"],
                        concept_name=row["concept_name"],
                        content=row["content"],
                        metadata=dict(row["metadata"]),
                        wellbeing_category=row["wellbeing_category"],
                        embedding=list(row["embedding"]),
                        embedding_model=row["embedding_model"],
                        content_hash=row["content_hash"],
                        provenance=row["provenance"],
                        valid_at=valid_at,
                        invalid_at=invalid_at,
                        invalidated_by=invalidated_by,
                        invalidated_at=datetime.now(UTC),
                    )
                    .returning(graph_node_versions.c.version_key)
                ).scalar_one()
            )
            metadata = keep_merge_record(dict(node.metadata), row["metadata"])
            content_hash = (
                node.content_hash
                if metadata == dict(node.metadata)
                else _compute_node_hash(node.concept_name, node.content, metadata)
            )
            surrogate = conn.execute(
                self._update_node_statement(
                    owner_id, node, embedding, embedding_model, metadata, content_hash
                ).where(
                    graph_nodes.c.content_hash == expected_content_hash,
                    trail_is(expected_provenance),
                )
            ).scalar_one_or_none()
            if surrogate is None:
                # The locked row passed the check, so this is not expected; refuse inside
                # the transaction so the version inserted above rolls back with it.
                raise GraphStaleWriteError(
                    "the node changed since it was read", context={"node_id": node.id}
                )
        return version_key

    def latest_version_invalid_at(self, owner_id: str, node_id: str) -> datetime | None:
        """The max ``invalid_at`` across a node's versions — when its CURRENT account began.

        ``None`` when the node has never been superseded (its current account's
        world-time origin is then the creation provenance's ``written_at``).
        """
        stmt = select(func.max(graph_node_versions.c.invalid_at)).where(
            graph_node_versions.c.owner_id == owner_id,
            graph_node_versions.c.node_id == node_id,
        )
        with self._engine.connect() as conn:
            val = conn.execute(stmt).scalar_one_or_none()
        return None if val is None else _as_utc(val)

    def get_node_versions(self, owner_id: str, node_id: str) -> list[NodeVersion]:
        """A node's window-closed prior accounts, oldest→newest (K7-D-1 point-in-time)."""
        stmt = (
            select(graph_node_versions)
            .where(
                graph_node_versions.c.owner_id == owner_id,
                graph_node_versions.c.node_id == node_id,
            )
            .order_by(graph_node_versions.c.valid_at, graph_node_versions.c.version_key)
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._row_to_version(dict(r)) for r in rows]

    def read_version_for_restore(
        self, owner_id: str, version_id: str
    ) -> tuple[NodeVersion, list[float]] | None:
        """A version row + its preserved float32 embedding (the byte-exact restore source)."""
        try:
            version_key = int(version_id)
        except (TypeError, ValueError):
            return None
        stmt = select(graph_node_versions).where(
            graph_node_versions.c.version_key == version_key,
            graph_node_versions.c.owner_id == owner_id,
        )
        with self._engine.connect() as conn:
            row = conn.execute(stmt).mappings().one_or_none()
        if row is None:
            return None
        return self._row_to_version(dict(row)), [float(x) for x in row["embedding"]]

    # ===== embeddings (rerank source + rebuild) ===========================

    def get_embeddings(self, owner_id: str, node_ids: Sequence[str]) -> dict[str, list[float]]:
        """Durable float32 embeddings by node-id (the K1 rerank source, crit 2)."""
        if not node_ids:
            return {}
        stmt = select(graph_nodes.c.id, graph_nodes.c.embedding).where(
            graph_nodes.c.owner_id == owner_id, graph_nodes.c.id.in_(list(node_ids))
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return {str(r[0]): [float(x) for x in r[1]] for r in rows}

    def embeddings_by_surrogate(self, surrogates: Sequence[int]) -> dict[int, list[float]]:
        """Float32 embeddings keyed by surrogate — the turbovec exact-rerank source (T7).

        Owner-agnostic: the surrogates are already the user's (the ANN allowlist was
        the user's set); RLS scopes in prod.
        """
        if not surrogates:
            return {}
        stmt = select(graph_nodes.c.surrogate, graph_nodes.c.embedding).where(
            graph_nodes.c.surrogate.in_(list(surrogates))
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return {int(r[0]): [float(x) for x in r[1]] for r in rows}

    def iter_embeddings(self, owner_id: str) -> list[tuple[int, list[float]]]:
        """All ``(surrogate, embedding)`` for the user — the index-rebuild source (crit 9)."""
        stmt = select(graph_nodes.c.surrogate, graph_nodes.c.embedding).where(
            graph_nodes.c.owner_id == owner_id
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return [(int(r[0]), [float(x) for x in r[1]]) for r in rows]

    # ===== edges ===========================================================

    def upsert_edge(
        self, owner_id: str, link: TypedLink, *, guard_personas: Sequence[str] = ()
    ) -> None:
        """Insert or replace the OPEN edge for a fact-id (idempotent assertion, D-K0-2, K7-D-1).

        The conflict target is the partial unique index ``(id) WHERE invalid_at IS
        NULL`` — one open edge per fact. A window-closed edge with the same ``id``
        coexists as a separate row (it is outside the partial index), so re-asserting
        a fact after it was closed opens a NEW open row (K7-D-1) without touching the
        closed history. ``edge_key`` (IDENTITY) is never written and never updated.
        """
        row = self._link_to_row(owner_id, link)
        stmt = pg_insert(graph_edges).values(**row)
        update_cols = {
            c.name: stmt.excluded[c.name] for c in graph_edges.c if c.name not in ("id", "edge_key")
        }
        stmt = stmt.on_conflict_do_update(
            index_elements=["id"],
            index_where=graph_edges.c.invalid_at.is_(None),
            set_=update_cols,
        )
        asserted_by = link.provenance.persona_id if link.provenance is not None else None
        with self._engine.begin() as conn:
            self._hold_personas(
                conn, owner_id, [asserted_by, *guard_personas], cites=[link.provenance]
            )
            with _link_target_gone(link.src_node_id):
                conn.execute(stmt)

    def delete_links_from(self, owner_id: str, node_id: str, link_type: LinkType) -> None:
        """Delete a node's OUTGOING edges of one type (semantic-link re-eval on extend, D-K0-2).

        Only ever called with ``LinkType.SEMANTIC`` — semantic wiring is derived and
        recomputable (K7-D-1.3), so it is hard-deleted-and-reformed on evolve, NOT
        window-closed. Assertion edges (temporal/causal) are never touched here.
        """
        stmt = delete(graph_edges).where(
            graph_edges.c.owner_id == owner_id,
            graph_edges.c.src_node_id == node_id,
            graph_edges.c.link_type == str(link_type),
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def delete_links_incident(self, owner_id: str, node_id: str, link_type: LinkType) -> None:
        """Delete a node's edges of one type in BOTH directions (K5-D-7 correction re-eval).

        On a user correction the node's embedding changes, so every semantic edge
        *incident* to it is stale — including INBOUND ones (``dst = node``) that a
        neighbour scored against the OLD embedding (the half-edge a one-sided
        ``delete_links_from`` would leave behind). Clearing both directions, then
        re-forming the node's outgoing links from the fresh embedding, keeps the
        symmetric semantic neighbourhood honest. RLS-scoped.
        """
        stmt = delete(graph_edges).where(
            graph_edges.c.owner_id == owner_id,
            graph_edges.c.link_type == str(link_type),
            or_(
                graph_edges.c.src_node_id == node_id,
                graph_edges.c.dst_node_id == node_id,
            ),
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def invalidate_edge(
        self,
        owner_id: str,
        edge_id: str,
        *,
        invalidated_by: str | None,
        invalid_at: datetime | None = None,
    ) -> bool:
        """Window-close the OPEN edge for a fact-id — never delete (Spec K7, K7-D-1, §0).

        Sets ``invalid_at`` (world-time end), ``invalidated_by`` (provenance-of-closure)
        and ``invalidated_at`` (system bookkeeping, K7-D-10) on the single open row for
        ``edge_id`` (owner-scoped, ``invalid_at IS NULL``). The row PERSISTS — a
        subsequent :meth:`upsert_edge` of the same fact opens a NEW row sharing the
        ``id`` (the two coexist, one closed one open). Idempotent: closing an
        already-closed / absent / other-owner edge affects zero rows and returns
        ``False``. ``invalid_at`` defaults to now; it is clamped strictly after
        ``valid_at`` (the DDL window CHECK) so an immediate open→close stays valid.
        """
        end_ts = invalid_at if invalid_at is not None else datetime.now(UTC)
        closed = func.greatest(end_ts, graph_edges.c.valid_at + text("interval '1 microsecond'"))
        stmt = (
            update(graph_edges)
            .where(
                graph_edges.c.owner_id == owner_id,
                graph_edges.c.id == edge_id,
                graph_edges.c.invalid_at.is_(None),
            )
            .values(
                invalid_at=closed,
                invalidated_by=invalidated_by,
                invalidated_at=datetime.now(UTC),
            )
            .returning(graph_edges.c.edge_key)
        )
        with self._engine.begin() as conn:
            return conn.execute(stmt).scalar_one_or_none() is not None

    def neighbors(
        self,
        owner_id: str,
        node_id: str,
        *,
        link_types: set[LinkType] | None = None,
        limit: int,
        as_of: datetime | None = None,
    ) -> list[tuple[TypedLink, ConceptNode]]:
        """Typed one-hop traversal in BOTH directions (K1 §2; crit 7 / K7-D-1).

        Returns ``(edge, neighbour-node)``. ``link_types=None`` traverses all four
        types; bounded by ``limit`` (K1-D-3 anti-flooding). **Traverses OPEN edges by
        default** (``invalid_at IS NULL``) — a window-closed edge is invisible to
        current-state reads. With an ``as_of`` world-time, returns exactly the edges
        whose window covers that instant (``valid_at <= as_of AND (invalid_at IS NULL
        OR invalid_at > as_of)``, the Zep predicate) — a closed edge is visible inside
        its window, invisible after.
        """
        conds = [
            graph_edges.c.owner_id == owner_id,
            or_(graph_edges.c.src_node_id == node_id, graph_edges.c.dst_node_id == node_id),
        ]
        if link_types is not None:
            conds.append(graph_edges.c.link_type.in_([str(lt) for lt in link_types]))
        if as_of is None:
            conds.append(graph_edges.c.invalid_at.is_(None))
        else:
            conds.append(graph_edges.c.valid_at <= as_of)
            conds.append(or_(graph_edges.c.invalid_at.is_(None), graph_edges.c.invalid_at > as_of))
        edge_stmt = (
            select(graph_edges).where(*conds).order_by(graph_edges.c.created_at).limit(limit)
        )
        with self._engine.connect() as conn:
            edge_rows = [dict(r) for r in conn.execute(edge_stmt).mappings().all()]
            other_ids = [
                r["dst_node_id"] if r["src_node_id"] == node_id else r["src_node_id"]
                for r in edge_rows
            ]
            nodes: dict[str, ConceptNode] = {}
            if other_ids:
                node_rows = (
                    conn.execute(
                        select(graph_nodes).where(
                            graph_nodes.c.owner_id == owner_id, graph_nodes.c.id.in_(other_ids)
                        )
                    )
                    .mappings()
                    .all()
                )
                nodes = {str(r["id"]): self._row_to_node(dict(r)) for r in node_rows}
        out: list[tuple[TypedLink, ConceptNode]] = []
        for r, other in zip(edge_rows, other_ids, strict=True):
            neighbour = nodes.get(other)
            if neighbour is not None:
                out.append((self._row_to_link(r), neighbour))
        return out

    # ===== entities (the canonical registry) ==============================

    def insert_entity(
        self,
        owner_id: str,
        entity: CanonicalEntity,
        name_embedding: Sequence[float],
        *,
        embedding_model: str = _DEFAULT_EMBEDDING_MODEL,  # noqa: ARG002 — symmetry; not stored
    ) -> None:
        self._check_dim(name_embedding, entity.id)
        row = {
            "id": entity.id,
            "owner_id": owner_id,
            "canonical_name": entity.canonical_name,
            "aliases": [a.model_dump(mode="json") for a in entity.aliases],
            "name_embedding": list(name_embedding),
            "provenance": None
            if entity.provenance is None
            else entity.provenance.model_dump(mode="json"),
            "created_at": entity.created_at,
        }
        named = [
            entity.provenance.persona_id if entity.provenance else None,
            *(a.persona_id for a in entity.aliases),
        ]
        with self._engine.begin() as conn:
            self._hold_personas(conn, owner_id, named, cites=[entity.provenance])
            conn.execute(pg_insert(graph_entities).values(**row))

    def get_entity(self, owner_id: str, entity_id: str) -> CanonicalEntity | None:
        stmt = select(graph_entities).where(
            graph_entities.c.id == entity_id, graph_entities.c.owner_id == owner_id
        )
        with self._engine.connect() as conn:
            row = conn.execute(stmt).mappings().one_or_none()
        return None if row is None else self._row_to_entity(dict(row))

    def add_alias(self, owner_id: str, entity_id: str, alias: EntityAlias) -> None:
        """Append a confirmed surface form to an entity's alias set (K2's MERGE callback).

        One UPDATE appends in SQL, in the transaction that holds the alias's persona (R9-284
        review MEDIUM-2): reading the list on one connection and writing it back on another
        could rewrite an alias an erasure had just removed, or lose a concurrent append. An
        alias already in the set, field for field, is not appended again. A missing entity is
        a no-op, as before.
        """
        new = func.jsonb_build_array(literal(alias.model_dump(mode="json"), JSONB))
        stmt = (
            update(graph_entities)
            .where(
                graph_entities.c.id == entity_id,
                graph_entities.c.owner_id == owner_id,
                ~graph_entities.c.aliases.contains(new),
            )
            .values(aliases=graph_entities.c.aliases.concat(new))
        )
        with self._engine.begin() as conn:
            self._hold_personas(conn, owner_id, [alias.persona_id])
            conn.execute(stmt)

    def find_entity_by_text(self, owner_id: str, normalized_name: str) -> CanonicalEntity | None:
        """Exact (case/whitespace-insensitive) lookup by canonical name OR alias surface.

        The deterministic resolver's first step (research §2 "exact alias lookup →
        fuzzy → embedding"): a registered alias mention ("my doctor") is NOT near
        the entity's name embedding, so it can't be found by cosine — only by this
        exact-text match. ``normalized_name`` is the caller's
        lower+whitespace-collapsed mention. Matched case-insensitively here via
        ``lower(btrim(...))`` over the canonical name and every alias surface.

        v0.1 uses a correlated ``jsonb_array_elements`` scan over the per-user
        alias arrays (small N per owner); a normalized-alias GIN index is the
        v0.2 push-down if registries grow large.
        """
        sql = text(
            "SELECT id, owner_id, canonical_name, aliases, provenance, created_at "
            "FROM graph_entities "
            "WHERE owner_id = :owner AND ("
            "  lower(btrim(canonical_name)) = :norm "
            "  OR EXISTS (SELECT 1 FROM jsonb_array_elements(aliases) AS e "
            "             WHERE lower(btrim(e->>'surface')) = :norm)"
            ") LIMIT 1"
        )
        params = {"owner": owner_id, "norm": normalized_name}
        with self._engine.connect() as conn:
            row = conn.execute(sql, params).mappings().one_or_none()
        return None if row is None else self._row_to_entity(dict(row))

    def count_entities(self, owner_id: str) -> int:
        """Number of canonical entities for the user (the next ``make_entity_id`` index)."""
        stmt = (
            select(func.count())
            .select_from(graph_entities)
            .where(graph_entities.c.owner_id == owner_id)
        )
        with self._engine.connect() as conn:
            return int(conn.execute(stmt).scalar_one())

    def entity_candidates(
        self, owner_id: str, name_vector: Sequence[float], top_k: int
    ) -> list[tuple[CanonicalEntity, float]]:
        """Cosine candidate-gen over canonical-name embeddings (the resolve blocking step, T5)."""
        self._check_dim(name_vector, "<name>")
        n_vec = list(name_vector)
        distance = graph_entities.c.name_embedding.cosine_distance(n_vec).label("distance")
        stmt = (
            select(graph_entities, distance)
            .where(graph_entities.c.owner_id == owner_id)
            .order_by(distance)
            .limit(top_k)
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [(self._row_to_entity(dict(r)), float(r["distance"])) for r in rows]

    # ===== node ↔ entity associations (T6b) ===============================

    def associate_entities(
        self,
        owner_id: str,
        node_id: str,
        entity_ids: Sequence[str],
        *,
        guard_personas: Sequence[str] = (),
    ) -> None:
        """Record that ``node_id`` concerns each entity (idempotent on the PK)."""
        self.associate_entities_returning(
            owner_id, node_id, entity_ids, guard_personas=guard_personas
        )

    def associate_entities_returning(
        self,
        owner_id: str,
        node_id: str,
        entity_ids: Sequence[str],
        *,
        guard_personas: Sequence[str] = (),
    ) -> list[str]:
        """:meth:`associate_entities`, returning the links this call inserted (round 6 L-4).

        A link the node already had is not returned: consolidation records only these, so a
        give back never removes a link the canonical had before the merge.
        """
        if not entity_ids:
            return []
        now = datetime.now(UTC)
        rows = [
            {"owner_id": owner_id, "node_id": node_id, "entity_id": eid, "created_at": now}
            for eid in dict.fromkeys(entity_ids)  # de-dupe, preserve order
        ]
        stmt = (
            pg_insert(graph_node_entities)
            .values(rows)
            .on_conflict_do_nothing(index_elements=["node_id", "entity_id"])
            .returning(graph_node_entities.c.entity_id)
        )
        with self._engine.begin() as conn, _link_target_gone(node_id):
            self._hold_personas(conn, owner_id, guard_personas)
            return [str(e) for e in conn.execute(stmt).scalars()]

    def associate_entities_recording(
        self,
        owner_id: str,
        node_id: str,
        entity_ids: Sequence[str],
        *,
        member_id: str,
        merged_into: str,
        expected_content_hash: str,
        expected_provenance: Sequence[NodeProvenance],
        record: Callable[[list[str]], tuple[dict[str, str], str]],
        guard_personas: Sequence[str] = (),
    ) -> list[str]:
        """Link ``node_id`` to the entities and write ``member_id``'s record, in one commit.

        Consolidation's done write (R9-284 round 7 HIGH-1): ``record`` is given the links
        the insert actually made and returns the member's metadata and content hash, so the
        record lists exactly those. One transaction holding ``guard_personas``: if the guard
        refuses, or anything fails, neither the links nor the record land.

        The record write is a compare-and-set (round 8 LOW-1, D-R9-284-node-cas): the member
        must still be merged into ``merged_into`` and have ``expected_content_hash``, the hash
        of its pending record. Otherwise :class:`GraphMergeConflictError` is raised and the
        links roll back with it.
        """
        now = datetime.now(UTC)
        rows = [
            {"owner_id": owner_id, "node_id": node_id, "entity_id": eid, "created_at": now}
            for eid in dict.fromkeys(entity_ids)
        ]
        with self._engine.begin() as conn:
            self._hold_personas(conn, owner_id, guard_personas)
            added: list[str] = []
            if rows:
                added = [
                    str(e)
                    for e in conn.execute(
                        pg_insert(graph_node_entities)
                        .values(rows)
                        .on_conflict_do_nothing(index_elements=["node_id", "entity_id"])
                        .returning(graph_node_entities.c.entity_id)
                    ).scalars()
                ]
            metadata, content_hash = record(added)
            written = conn.execute(
                update(graph_nodes)
                .where(
                    graph_nodes.c.id == member_id,
                    graph_nodes.c.owner_id == owner_id,
                    graph_nodes.c.merged_into == merged_into,
                    graph_nodes.c.content_hash == expected_content_hash,
                    trail_is(expected_provenance),
                )
                .values(
                    merged_into=merged_into,
                    metadata=metadata,
                    content_hash=content_hash,
                    updated_at=datetime.now(UTC),
                )
            ).rowcount
            if written != 1:
                raise GraphMergeConflictError(
                    "the member changed since its pending merge record",
                    context={"node_id": member_id},
                )
        return added

    def nodes_for_entity(self, owner_id: str, entity_id: str) -> list[ConceptNode]:
        """All nodes that concern ``entity_id`` (criterion 2 — the entity thread)."""
        stmt = (
            select(graph_nodes)
            .join(
                graph_node_entities,
                (graph_node_entities.c.node_id == graph_nodes.c.id)
                & (graph_node_entities.c.owner_id == graph_nodes.c.owner_id),
            )
            .where(
                graph_node_entities.c.owner_id == owner_id,
                graph_node_entities.c.entity_id == entity_id,
                graph_nodes.c.merged_into.is_(None),  # merged members leave the thread (K7-D-4)
            )
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._row_to_node(dict(r)) for r in rows]

    def entities_for_node(self, owner_id: str, node_id: str) -> list[str]:
        """The canonical-entity ids a node concerns."""
        stmt = select(graph_node_entities.c.entity_id).where(
            graph_node_entities.c.owner_id == owner_id,
            graph_node_entities.c.node_id == node_id,
        )
        with self._engine.connect() as conn:
            return [str(r[0]) for r in conn.execute(stmt)]

    def entity_neighbors(self, owner_id: str, node_id: str) -> list[ConceptNode]:
        """Sibling nodes sharing ≥1 entity with ``node_id`` (on-the-fly ENTITY traversal).

        Resolves entity links through the association table (node→entities→sibling
        nodes) WITHOUT materialised node↔node entity edges — no O(n²) explosion.
        Excludes ``node_id`` itself; distinct.
        """
        mine = graph_node_entities.alias("mine")
        theirs = graph_node_entities.alias("theirs")
        stmt = (
            select(graph_nodes)
            .distinct()
            .join(theirs, theirs.c.node_id == graph_nodes.c.id)
            .join(mine, mine.c.entity_id == theirs.c.entity_id)
            .where(
                mine.c.owner_id == owner_id,
                theirs.c.owner_id == owner_id,
                graph_nodes.c.owner_id == owner_id,
                mine.c.node_id == node_id,
                theirs.c.node_id != node_id,
                graph_nodes.c.merged_into.is_(None),  # merged siblings are invisible (K7-D-4)
            )
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._row_to_node(dict(r)) for r in rows]

    # ===== consolidation (Spec K7, K7-D-4) ================================

    def get_marker(self, owner_id: str) -> tuple[int, datetime | None]:
        """Get-or-create the per-owner consolidation marker → ``(current_epoch, watermark)``."""
        ins = (
            pg_insert(graph_consolidation_markers)
            .values(owner_id=owner_id)
            .on_conflict_do_nothing(index_elements=["owner_id"])
        )
        stmt = select(
            graph_consolidation_markers.c.current_epoch,
            graph_consolidation_markers.c.watermark,
        ).where(graph_consolidation_markers.c.owner_id == owner_id)
        with self._engine.begin() as conn:
            conn.execute(ins)
            row = conn.execute(stmt).one()
        return int(row[0]), (None if row[1] is None else _as_utc(row[1]))

    def advance_marker(
        self, owner_id: str, *, epoch: int, watermark: datetime | None, last_run_at: datetime
    ) -> None:
        """Advance the marker after a pass (ordinal epoch + dirty watermark + run time).

        ``watermark`` may be ``None``: a run that skipped its merges before any run set one
        still records its epoch (R9-284 round 9 LOW-1).
        """
        stmt = (
            update(graph_consolidation_markers)
            .where(graph_consolidation_markers.c.owner_id == owner_id)
            .values(current_epoch=epoch, watermark=watermark, last_run_at=last_run_at)
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def dirty_candidates(self, owner_id: str, watermark: datetime | None) -> list[ConceptNode]:
        """Consolidation candidates: non-merged, non-SELF, dirty-since-watermark (K7-D-4).

        Ordered by evidence (provenance-trail length) DESC, then oldest ``created_at``,
        then min ``id`` — the deterministic seed/canonical survivorship order (K7-D-4.3).
        """
        conds = [
            graph_nodes.c.owner_id == owner_id,
            graph_nodes.c.merged_into.is_(None),
            graph_nodes.c.node_kind != str(NodeKind.SELF),
        ]
        if watermark is not None:
            conds.append(graph_nodes.c.updated_at > watermark)
        stmt = (
            select(graph_nodes)
            .where(*conds)
            .order_by(
                func.jsonb_array_length(graph_nodes.c.provenance).desc(),
                graph_nodes.c.created_at.asc(),
                graph_nodes.c.id.asc(),
            )
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._row_to_node(dict(r)) for r in rows]

    def band_semantic_neighbors(
        self, owner_id: str, node_id: str, *, floor: float, ceil: float
    ) -> list[ConceptNode]:
        """Non-merged, non-SELF nodes joined to ``node_id`` by an OPEN band semantic edge.

        The near-dup cluster substrate (K7-D-3): open ``semantic`` edges whose weight
        is in ``[floor, ceil)`` (the existing config band). The pass then re-verifies
        similarity-to-seed from stored embeddings (K7-D-4.2 star-clustering).
        """
        conds = [
            graph_edges.c.owner_id == owner_id,
            graph_edges.c.link_type == str(LinkType.SEMANTIC),
            graph_edges.c.invalid_at.is_(None),
            graph_edges.c.weight >= floor,
            graph_edges.c.weight < ceil,
            or_(graph_edges.c.src_node_id == node_id, graph_edges.c.dst_node_id == node_id),
        ]
        with self._engine.connect() as conn:
            edge_rows = conn.execute(
                select(graph_edges.c.src_node_id, graph_edges.c.dst_node_id).where(*conds)
            ).all()
            other_ids = {(r[1] if r[0] == node_id else r[0]) for r in edge_rows} - {node_id}
            if not other_ids:
                return []
            node_rows = (
                conn.execute(
                    select(graph_nodes).where(
                        graph_nodes.c.owner_id == owner_id,
                        graph_nodes.c.id.in_(other_ids),
                        graph_nodes.c.merged_into.is_(None),
                        graph_nodes.c.node_kind != str(NodeKind.SELF),
                    )
                )
                .mappings()
                .all()
            )
        return [self._row_to_node(dict(r)) for r in node_rows]

    def open_assertion_edges(self, owner_id: str, node_id: str) -> list[TypedLink]:
        """A node's OPEN temporal/causal edges (both directions) — the mirror source (K7-D-4.4)."""
        conds = [
            graph_edges.c.owner_id == owner_id,
            graph_edges.c.invalid_at.is_(None),
            graph_edges.c.link_type.in_([str(LinkType.TEMPORAL), str(LinkType.CAUSAL)]),
            or_(graph_edges.c.src_node_id == node_id, graph_edges.c.dst_node_id == node_id),
        ]
        with self._engine.connect() as conn:
            rows = conn.execute(select(graph_edges).where(*conds)).mappings().all()
        return [self._row_to_link(dict(r)) for r in rows]

    def open_assertion_edges_keyed(
        self, owner_id: str, node_id: str
    ) -> list[tuple[TypedLink, int]]:
        """A node's OPEN temporal/causal edges with each row's ``edge_key``, in one query.

        ``edge_key`` names the exact row a mirror copies (R9-284 LOW-3); read with the edge,
        not by a second query that could see a different row (round 5 LOW-1).
        """
        conds = [
            graph_edges.c.owner_id == owner_id,
            graph_edges.c.invalid_at.is_(None),
            graph_edges.c.link_type.in_([str(LinkType.TEMPORAL), str(LinkType.CAUSAL)]),
            or_(graph_edges.c.src_node_id == node_id, graph_edges.c.dst_node_id == node_id),
        ]
        with self._engine.connect() as conn:
            rows = conn.execute(select(graph_edges).where(*conds)).mappings().all()
        return [(self._row_to_link(dict(r)), int(r["edge_key"])) for r in rows]

    def close_edges_incident(
        self, owner_id: str, node_id: str, *, invalidated_by: str
    ) -> list[str]:
        """Window-close ALL open edges incident to ``node_id``; return their fact-ids (K7-D-4.4)."""
        now = datetime.now(UTC)
        closed = func.greatest(now, graph_edges.c.valid_at + text("interval '1 microsecond'"))
        stmt = (
            update(graph_edges)
            .where(
                graph_edges.c.owner_id == owner_id,
                graph_edges.c.invalid_at.is_(None),
                or_(graph_edges.c.src_node_id == node_id, graph_edges.c.dst_node_id == node_id),
            )
            .values(invalid_at=closed, invalidated_by=invalidated_by, invalidated_at=now)
            .returning(graph_edges.c.id)
        )
        with self._engine.begin() as conn:
            return [str(r[0]) for r in conn.execute(stmt)]

    def reopen_edges_incident(self, owner_id: str, node_id: str, *, invalidated_by: str) -> None:
        """Re-open the edges incident to ``node_id`` closed under ``invalidated_by`` (unmerge)."""
        with self._engine.begin() as conn:
            conn.execute(reopen_statement(owner_id, node_id, invalidated_by))

    def maintenance_lock(self, owner_id: str) -> AbstractContextManager[None]:
        """The owner's graph maintenance lock, held on its own session (D-R9-284-graph-mutex)."""
        return graph_maintenance_held(self._engine, owner_id)

    def pending_merges(self, owner_id: str) -> list[str]:
        """Members whose merge record is still ``pending``: their act stopped part way (R9-284).

        Filtered in SQL by the record's text, then read in Python, so a record that is not JSON
        is never cast; one without a state is done.
        """
        record = graph_nodes.c.metadata["_k7_merge"].astext
        stmt = (
            select(graph_nodes.c.id, record)
            .where(
                graph_nodes.c.owner_id == owner_id,
                graph_nodes.c.merged_into.is_not(None),
                record.contains('"state": "pending"'),
            )
            .order_by(graph_nodes.c.id)
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        pending = []
        for node_id, raw in rows:
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if isinstance(parsed, dict) and parsed.get("state") == "pending":
                pending.append(str(node_id))
        return pending

    def insert_edge_if_absent(
        self, owner_id: str, link: TypedLink, *, guard_personas: Sequence[str] = ()
    ) -> bool:
        """Open ``link`` unless an open edge with its id exists; return whether it is now ``link``.

        Consolidation's mirror (R9-284 review MEDIUM-1): an id is ``src::type::dst``, so an
        edge another persona (or the user) already asserted on the canonical has the mirror's
        id, and upserting the mirror would overwrite it with SYSTEM provenance; that is ``False``.
        An open edge that already is this merge's mirror (another member of the same run made
        it, round 5 MEDIUM-2) is ``True``: the caller adds its own source to the mirror.
        """
        row = self._link_to_row(owner_id, link)
        stmt = (
            pg_insert(graph_edges)
            .values(**row)
            .on_conflict_do_nothing(
                index_elements=["id"], index_where=graph_edges.c.invalid_at.is_(None)
            )
            .returning(graph_edges.c.id)
        )
        asserted_by = link.provenance.persona_id if link.provenance is not None else None
        reason = link.provenance.reason if link.provenance is not None else None
        same_mirror = select(graph_edges.c.id).where(
            graph_edges.c.owner_id == owner_id,
            graph_edges.c.id == link.id,
            graph_edges.c.invalid_at.is_(None),
            cast(graph_edges.c.provenance, JSONB).contains({"source": "system", "reason": reason}),
        )
        with self._engine.begin() as conn:
            self._hold_personas(
                conn, owner_id, [asserted_by, *guard_personas], cites=[link.provenance]
            )
            if conn.execute(stmt).first() is not None:
                return True
            return reason is not None and conn.execute(same_mirror).first() is not None

    def existing_edge_keys(self, owner_id: str, edge_keys: Sequence[int]) -> set[int]:
        """Which of ``edge_keys`` are still rows of the owner's edges."""
        if not edge_keys:
            return set()
        stmt = select(graph_edges.c.edge_key).where(
            graph_edges.c.owner_id == owner_id, graph_edges.c.edge_key.in_(list(edge_keys))
        )
        with self._engine.connect() as conn:
            return {int(k) for k in conn.execute(stmt).scalars()}

    def merge_records_listing(
        self, owner_id: str, *, run_id: str, mirror_ids: Sequence[str]
    ) -> dict[str, str]:
        """``{member id: raw record}`` for each merged member of ``run_id`` listing a mirror id.

        Across every canonical (round 6 M-2). Narrowed in SQL by the record's text, then
        decided in Python on the parsed record, so a record that is not JSON is never cast.
        """
        if not mirror_ids:
            return {}
        record = graph_nodes.c.metadata["_k7_merge"].astext
        stmt = (
            select(graph_nodes.c.id, record)
            .where(
                graph_nodes.c.owner_id == owner_id,
                graph_nodes.c.merged_into.is_not(None),
                record.contains(run_id, autoescape=True),
                or_(*(record.contains(m, autoescape=True) for m in mirror_ids)),
            )
            .order_by(graph_nodes.c.id)
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        wanted = set(mirror_ids)
        out: dict[str, str] = {}
        for node_id, raw in rows:
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(parsed, dict) or parsed.get("run_id") != run_id:
                continue
            if wanted & {str(m) for m in parsed.get("mirrored_edges", [])}:
                out[str(node_id)] = str(raw)
        return out

    def delete_open_mirror(self, owner_id: str, edge_id: str, *, tag: str) -> None:
        """Remove the OPEN row for ``edge_id`` only while it is still the merge's mirror."""
        stmt = delete(graph_edges).where(
            graph_edges.c.owner_id == owner_id,
            graph_edges.c.id == edge_id,
            graph_edges.c.invalid_at.is_(None),
            cast(graph_edges.c.provenance, JSONB).contains({"source": "system", "reason": tag}),
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def delete_open_edge(self, owner_id: str, edge_id: str) -> None:
        """Remove the OPEN row for ``edge_id`` (unmerge: drop a consolidation-mirrored edge)."""
        stmt = delete(graph_edges).where(
            graph_edges.c.owner_id == owner_id,
            graph_edges.c.id == edge_id,
            graph_edges.c.invalid_at.is_(None),
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def entity_carriers(
        self, owner_id: str, canonical_id: str, entity_ids: Sequence[str], *, excluding: str
    ) -> dict[str, str]:
        """``{entity: a member merged into the canonical that links it}``, but ``excluding``.

        The first such member by id. For a give back that must keep a link another member
        still carries (R9-284 round 8 LOW-4).
        """
        if not entity_ids:
            return {}
        stmt = (
            select(graph_node_entities.c.entity_id, graph_nodes.c.id)
            .join(graph_nodes, graph_nodes.c.id == graph_node_entities.c.node_id)
            .where(
                graph_node_entities.c.owner_id == owner_id,
                graph_nodes.c.owner_id == owner_id,
                graph_nodes.c.merged_into == canonical_id,
                graph_nodes.c.id != excluding,
                graph_node_entities.c.entity_id.in_(list(entity_ids)),
            )
            .order_by(graph_nodes.c.id)
        )
        out: dict[str, str] = {}
        with self._engine.connect() as conn:
            for entity_id, member_id in conn.execute(stmt).all():
                out.setdefault(str(entity_id), str(member_id))
        return out

    def remove_entity_associations(
        self, owner_id: str, node_id: str, entity_ids: Sequence[str]
    ) -> None:
        """Drop specific ``(node_id, entity_id)`` associations (unmerge: undo the copy)."""
        if not entity_ids:
            return
        stmt = delete(graph_node_entities).where(
            graph_node_entities.c.owner_id == owner_id,
            graph_node_entities.c.node_id == node_id,
            graph_node_entities.c.entity_id.in_(list(entity_ids)),
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def set_merged_into(
        self,
        owner_id: str,
        node_id: str,
        *,
        merged_into: str | None,
        metadata: dict[str, str],
        content_hash: str,
        expected_content_hash: str,
        expected_provenance: Sequence[NodeProvenance],
        guard_personas: Sequence[str] = (),
    ) -> None:
        """Set/clear ``merged_into`` + bookkeeping ``metadata``/``content_hash`` (K7-D-4.4).

        A compare-and-set on ``expected_content_hash`` (D-R9-284-node-cas): a row that changed
        since the caller read it raises :class:`GraphStaleWriteError`; a missing row is a
        no-op.

        Targeted update — does NOT touch ``content``/``embedding`` (§0 byte-equality) or
        ``salience``/``last_evidence_epoch``. Advances ``updated_at`` so a re-scan sees
        the change (idempotency converges: a merged node is then excluded from candidacy).
        """
        stmt = (
            update(graph_nodes)
            .where(
                graph_nodes.c.id == node_id,
                graph_nodes.c.owner_id == owner_id,
                graph_nodes.c.content_hash == expected_content_hash,
                trail_is(expected_provenance),
            )
            .values(
                merged_into=merged_into,
                metadata=metadata,
                content_hash=content_hash,
                updated_at=datetime.now(UTC),
            )
        )
        with self._engine.begin() as conn:
            self._hold_personas(conn, owner_id, guard_personas)
            if conn.execute(stmt).rowcount != 1:
                _refuse_if_stale(conn, owner_id, node_id, None)

    def merged_members(self, owner_id: str, canonical_id: str) -> list[ConceptNode]:
        """The nodes consolidated into ``canonical_id`` (K5 inspection / unmerge scope)."""
        stmt = select(graph_nodes).where(
            graph_nodes.c.owner_id == owner_id, graph_nodes.c.merged_into == canonical_id
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._row_to_node(dict(r)) for r in rows]

    # ===== salience by evidence (Spec K7, K7-D-6) =========================
    # Every statement here moves salience by an ordinal EVIDENCE epoch — NEVER a
    # clock. The no-wallclock structural test asserts none of these methods read
    # now()/datetime.now/func.now. Clamping is SQL GREATEST/LEAST so each is a single
    # statement; ``updated_at`` is deliberately untouched (salience is not a content
    # change → a bump must not make a node a fresh consolidation candidate).

    def current_epoch(self, owner_id: str) -> int:
        """The owner's ordinal evidence epoch (the consolidation-run counter), 0 if unset."""
        stmt = select(graph_consolidation_markers.c.current_epoch).where(
            graph_consolidation_markers.c.owner_id == owner_id
        )
        with self._engine.connect() as conn:
            val = conn.execute(stmt).scalar_one_or_none()
        return 0 if val is None else int(val)

    def bump_salience(
        self,
        owner_id: str,
        node_id: str,
        *,
        delta: float,
        epoch: int,
        floor: float,
        cap: float,
    ) -> None:
        """Move one node's salience by ``delta`` (clamped) and stamp its evidence epoch.

        A corroboration (``+δ_c``) / contradiction (``−δ_x``) event — a single clamped
        UPDATE; ``last_evidence_epoch`` records that this node saw evidence at ``epoch``
        (resetting its disuse clock). SELF is never salient (K7-D-7), so it is excluded.
        """
        clamped = func.greatest(floor, func.least(cap, graph_nodes.c.salience + delta))
        stmt = (
            update(graph_nodes)
            .where(
                graph_nodes.c.owner_id == owner_id,
                graph_nodes.c.id == node_id,
                graph_nodes.c.node_kind != str(NodeKind.SELF),
            )
            .values(salience=clamped, last_evidence_epoch=epoch)
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def record_recall(
        self,
        owner_id: str,
        node_ids: Sequence[str],
        *,
        delta: float,
        epoch: int,
        floor: float,
        cap: float,
    ) -> int:
        """Reinforce recalled nodes (``+δ_r``) in ONE statement (K7-D-6 recall path).

        The off-the-token-path recall bump: a single clamped UPDATE over the injected
        node ids (SELF excluded), stamping ``last_evidence_epoch``. Returns the row
        count. The store wraps this fail-soft; the caller invokes it AFTER K3's
        injection selection, never on the reply stream.
        """
        if not node_ids:
            return 0
        clamped = func.greatest(floor, func.least(cap, graph_nodes.c.salience + delta))
        stmt = (
            update(graph_nodes)
            .where(
                graph_nodes.c.owner_id == owner_id,
                graph_nodes.c.id.in_(list(node_ids)),
                graph_nodes.c.node_kind != str(NodeKind.SELF),
            )
            .values(salience=clamped, last_evidence_epoch=epoch)
        )
        with self._engine.begin() as conn:
            return conn.execute(stmt).rowcount

    def apply_disuse_decay(
        self,
        owner_id: str,
        node_kind: str,
        *,
        current_epoch: int,
        grace: int,
        delta: float,
        floor: float,
    ) -> None:
        """Decay one kind's idle-beyond-grace nodes by one step (K7-D-6 disuse, per pass).

        Idleness is ``current_epoch − last_evidence_epoch`` (ordinal epochs, not time).
        Does NOT reset ``last_evidence_epoch`` — an idle node keeps decaying each pass
        until an evidence event stamps it. Callers skip ``TRAIT`` (``delta == 0``) and
        SELF. Merged nodes are left alone (they are outside retrieval).
        """
        clamped = func.greatest(floor, graph_nodes.c.salience - delta)
        stmt = (
            update(graph_nodes)
            .where(
                graph_nodes.c.owner_id == owner_id,
                graph_nodes.c.node_kind == node_kind,
                graph_nodes.c.merged_into.is_(None),
                (current_epoch - graph_nodes.c.last_evidence_epoch) > grace,
            )
            .values(salience=clamped)
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def get_salience(self, owner_id: str, node_id: str) -> tuple[float, int] | None:
        """``(salience, last_evidence_epoch)`` for a node — the read used by tests/K9."""
        stmt = select(graph_nodes.c.salience, graph_nodes.c.last_evidence_epoch).where(
            graph_nodes.c.owner_id == owner_id, graph_nodes.c.id == node_id
        )
        with self._engine.connect() as conn:
            row = conn.execute(stmt).one_or_none()
        return None if row is None else (float(row[0]), int(row[1]))

    # ===== row <-> model ===================================================

    @staticmethod
    def _check_dim(vector: Sequence[float], ref: str) -> None:
        if len(vector) != EMBEDDING_DIM:
            raise GraphIndexError(
                "embedding dimension mismatch",
                context={"expected": str(EMBEDDING_DIM), "got": str(len(vector)), "ref": ref},
            )

    def _node_to_row(
        self, owner_id: str, node: ConceptNode, embedding: Sequence[float], embedding_model: str
    ) -> dict[str, Any]:
        self._check_dim(embedding, node.id)
        return {
            "id": node.id,
            "owner_id": owner_id,
            "node_kind": str(node.node_kind),
            "concept_name": node.concept_name,
            "content": node.content,
            "metadata": dict(node.metadata),
            "wellbeing_category": node.wellbeing_category,
            "embedding": list(embedding),
            "embedding_model": embedding_model,
            "content_hash": node.content_hash,
            "provenance": [p.model_dump(mode="json") for p in node.provenance],
            "created_at": node.created_at,
            # updated_at starts at insert time (K7-D-4 dirty watermark). salience /
            # last_evidence_epoch / merged_into take their server defaults.
            "updated_at": node.created_at,
        }

    def _row_to_node(self, row: dict[str, Any], *, distance: float | None = None) -> ConceptNode:
        trail = tuple(NodeProvenance.model_validate(p) for p in row["provenance"])
        return ConceptNode(
            id=str(row["id"]),
            node_kind=NodeKind(row["node_kind"]),
            concept_name=str(row["concept_name"]),
            content=str(row["content"]),
            metadata=_as_str_dict(row.get("metadata")),
            wellbeing_category=row.get("wellbeing_category"),
            distance=distance,
            content_hash=str(row["content_hash"]),
            provenance=trail,
            created_at=_as_utc(row["created_at"]),
        )

    def _link_to_row(self, owner_id: str, link: TypedLink) -> dict[str, Any]:
        # valid_at defaults to created_at (K7-D-1 backfill semantics) when a caller
        # leaves it unset — old TypedLink constructors stay byte-compatible. edge_key
        # is IDENTITY-assigned by Postgres and is never written here.
        return {
            "id": link.id,
            "owner_id": owner_id,
            "src_node_id": link.src_node_id,
            "dst_node_id": link.dst_node_id,
            "link_type": str(link.link_type),
            "weight": link.weight,
            "provenance": None
            if link.provenance is None
            else link.provenance.model_dump(mode="json"),
            "created_at": link.created_at,
            "valid_at": link.valid_at if link.valid_at is not None else link.created_at,
            "invalid_at": link.invalid_at,
            "invalidated_by": link.invalidated_by,
            "invalidated_at": link.invalidated_at,
        }

    def _row_to_link(self, row: dict[str, Any]) -> TypedLink:
        prov = row.get("provenance")
        return TypedLink(
            id=str(row["id"]),
            src_node_id=str(row["src_node_id"]),
            dst_node_id=str(row["dst_node_id"]),
            link_type=LinkType(row["link_type"]),
            weight=None if row.get("weight") is None else float(row["weight"]),
            provenance=None if prov is None else NodeProvenance.model_validate(prov),
            created_at=_as_utc(row["created_at"]),
            valid_at=None if row.get("valid_at") is None else _as_utc(row["valid_at"]),
            invalid_at=None if row.get("invalid_at") is None else _as_utc(row["invalid_at"]),
            invalidated_by=row.get("invalidated_by"),
            invalidated_at=None
            if row.get("invalidated_at") is None
            else _as_utc(row["invalidated_at"]),
        )

    def _row_to_version(self, row: dict[str, Any]) -> NodeVersion:
        trail = tuple(NodeProvenance.model_validate(p) for p in row["provenance"])
        return NodeVersion(
            version_id=str(row["version_key"]),
            node_id=str(row["node_id"]),
            node_kind=NodeKind(row["node_kind"]),
            concept_name=str(row["concept_name"]),
            content=str(row["content"]),
            metadata=_as_str_dict(row.get("metadata")),
            wellbeing_category=row.get("wellbeing_category"),
            content_hash=str(row["content_hash"]),
            provenance=trail,
            valid_at=_as_utc(row["valid_at"]),
            invalid_at=_as_utc(row["invalid_at"]),
            invalidated_by=row.get("invalidated_by"),
            invalidated_at=None
            if row.get("invalidated_at") is None
            else _as_utc(row["invalidated_at"]),
        )

    def _row_to_entity(self, row: dict[str, Any]) -> CanonicalEntity:
        prov = row.get("provenance")
        aliases = tuple(EntityAlias.model_validate(a) for a in (row.get("aliases") or []))
        return CanonicalEntity(
            id=str(row["id"]),
            canonical_name=str(row["canonical_name"]),
            aliases=aliases,
            provenance=None if prov is None else NodeProvenance.model_validate(prov),
            created_at=_as_utc(row["created_at"]),
        )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _as_str_dict(value: Any) -> dict[str, str]:  # noqa: ANN401 — JSONB comes back as Any
    if value is None:
        return {}
    return {str(k): str(v) for k, v in dict(value).items()}

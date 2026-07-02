"""The user-scoped graph store — assembly + same-path index sync (Spec K0, T8).

``PostgresGraphStore`` is the concrete :class:`~persona.graph.protocol.GraphStore`:
the K1+K2 surface that composes the Postgres transport (T3), the dense index (T7,
pgvector default / turbovec opt-in), the merge engine (T6), an embedder, and the
audit logger. It owns the cross-cutting guarantees:

- **Same-path index sync (criterion 8):** every ``merge``/``delete_node`` writes
  Postgres (the authoritative source, inside the merge engine / transport) AND
  updates the index in the *same call*. **Postgres is written first**; if the
  index update then fails, it RAISES (``GraphIndexError``) rather than silently
  drifting — Postgres is intact and the index is recoverable via
  :meth:`rebuild_index`. For pgvector the index ops are no-ops (the table IS the
  index → atomic). A stale turbovec entry after a delete is benign (hydration
  drops surrogates absent from Postgres); a missing add surfaces as the raise.
- **Exactly one ``AuditEvent`` per mutation (Spec 01):** ``merge`` → one ``WRITE``,
  ``delete_node`` → one ``DELETE``; reads emit none.
- **Allowlist scoping from owner_id (criterion 6):** ``search_dense`` ALWAYS passes
  the user's surrogate set (∩ any K4 subtraction) to the index — isolation never
  relies on ``None``. Postgres RLS is the second layer (pgvector).
- **Rebuildable from Postgres (criterion 9):** :meth:`rebuild_index` re-syncs the
  user's vectors from the durable embeddings — also the cold-start mechanism
  (pgvector under ``turbovec_calibration_min`` nodes → rebuild turbovec once).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from persona.audit import AuditAction, AuditEvent
from persona.graph.errors import NodeMergeError
from persona.graph.models import (
    ConceptNode,
    LinkType,
    NodeKind,
    NodeProvenance,
    TypedLink,
    make_edge_id,
    make_self_node_id,
)
from persona.graph.protocol import MergeAction
from persona.schema.chunks import WriteSource

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy import Engine

    from persona.audit import AuditLogger
    from persona.graph.config import GraphSettings
    from persona.graph.protocol import GraphIndex, KnowledgeCandidate, MergeOutcome
    from persona.stores.embedder import Embedder

#: Placeholder ``concept_name`` for a self node whose user has not set a name yet
#: (Spec K6 — the name is optional/skippable; the anchor still exists, generically
#: labelled, until a name is captured and synced in).
_UNNAMED_SELF_LABEL = "the user"

__all__ = ["PostgresGraphStore", "build_graph_store"]


class _StoreBackend(Protocol):
    """The transport surface the store composes (PostgresGraphBackend satisfies it)."""

    def surrogate_for(self, owner_id: str, node_id: str) -> int | None: ...
    def get_embeddings(self, owner_id: str, node_ids: Sequence[str]) -> dict[str, list[float]]: ...
    def get_nodes_by_surrogates(
        self, owner_id: str, surrogates: Sequence[int]
    ) -> dict[int, ConceptNode]: ...
    def get_node(self, owner_id: str, node_id: str) -> ConceptNode | None: ...
    def delete_node(self, owner_id: str, node_id: str) -> int | None: ...
    def insert_node_if_absent(
        self, owner_id: str, node: ConceptNode, embedding: Sequence[float]
    ) -> int | None: ...
    def update_node(
        self, owner_id: str, node: ConceptNode, embedding: Sequence[float]
    ) -> int | None: ...
    def surrogates_for_owner(self, owner_id: str) -> list[int]: ...
    def surrogates_for_nodes(self, owner_id: str, node_ids: Sequence[str]) -> list[int]: ...
    def flagged_nodes(self, owner_id: str) -> list[ConceptNode]: ...
    def node_ids_for_owner(self, owner_id: str) -> list[str]: ...
    def fts_query(self, owner_id: str, query: str, top_k: int) -> list[ConceptNode]: ...
    def neighbors(
        self, owner_id: str, node_id: str, *, link_types: set[LinkType] | None, limit: int
    ) -> list[tuple[TypedLink, ConceptNode]]: ...
    def entity_neighbors(self, owner_id: str, node_id: str) -> list[ConceptNode]: ...
    def iter_embeddings(self, owner_id: str) -> list[tuple[int, list[float]]]: ...


class _MergeRunner(Protocol):
    def merge(self, owner_id: str, candidate: KnowledgeCandidate) -> MergeOutcome: ...


class PostgresGraphStore:
    """Concrete ``GraphStore``: transport + index + merge engine + audit (T8)."""

    def __init__(
        self,
        *,
        backend: _StoreBackend,
        index: GraphIndex,
        merge_engine: _MergeRunner,
        embedder: Embedder,
        audit_logger: AuditLogger,
        settings: GraphSettings | None = None,
    ) -> None:
        from persona.graph.config import GraphSettings as _Settings

        self._backend = backend
        self._index = index
        self._merge = merge_engine
        self._embedder = embedder
        self._audit = audit_logger
        self._settings = settings or _Settings()

    # ===== write (K2) ======================================================

    def merge(self, owner_id: str, candidate: KnowledgeCandidate) -> MergeOutcome:
        outcome = self._merge.merge(owner_id, candidate)  # Postgres (authoritative)
        self._emit_audit(
            owner_id,
            AuditAction.WRITE,
            source=candidate.provenance.source,
            node_id=outcome.node_id,
            provenance=candidate.provenance,
            metadata={"action": outcome.action.value},
        )
        # Same-path index sync — raises (not silent) on failure; Postgres is intact.
        surrogate = self._backend.surrogate_for(owner_id, outcome.node_id)
        embedding = self._backend.get_embeddings(owner_id, [outcome.node_id]).get(outcome.node_id)
        if surrogate is None or embedding is None:  # pragma: no cover - defensive
            raise NodeMergeError(
                "merged node not found for index sync",
                context={"node_id": outcome.node_id, "owner_id": owner_id},
            )
        if outcome.action is MergeAction.CREATED:
            self._index.add(surrogate=surrogate, vector=embedding)
        else:
            self._index.replace(surrogate=surrogate, vector=embedding)
        return outcome

    def delete_node(self, owner_id: str, node_id: str) -> bool:
        surrogate = self._backend.delete_node(owner_id, node_id)  # Postgres (authoritative)
        if surrogate is None:
            return False
        self._emit_audit(owner_id, AuditAction.DELETE, source=WriteSource.USER, node_id=node_id)
        self._index.remove(surrogate)  # same path; raises on failure (stale entry benign)
        return True

    # ===== the user's self node (Spec K6) =================================

    def get_self_node(self, owner_id: str) -> ConceptNode | None:
        """Read the user's central self node (Spec K6), or ``None`` if not yet created."""
        return self._backend.get_node(owner_id, make_self_node_id(owner_id))

    def get_or_create_self_node(
        self, owner_id: str, *, display_name: str | None = None
    ) -> ConceptNode:
        """Ensure the user's central self node exists, sync its name, and return it (K6).

        The single central per-user node (``NodeKind.SELF``, reserved id
        ``{owner_id}::self``), named with the user's name — the anchor everything the
        persona learns about them can connect to (Option C / K6-D-2: an ordinary
        node, no forced star). Idempotent and race-safe (K6-D-5): the reserved id
        makes concurrent first-writes collapse to one node via the unique
        constraint (the loser re-reads the winner), never a duplicate or a crash.

        ``display_name`` is the caller-resolved name (the runtime supplies it from
        the ``users`` table — K6-D-6). ``None`` means "ensure it exists, do NOT touch
        the name" (e.g. a graph write with no name in hand must not clobber a name
        already set). A name change (K6-D-9) updates ``concept_name`` and APPENDS a
        provenance entry recording the prior name (``superseded_content``) — no
        silent overwrite (K0-D-4). Same-path index sync (the K0 invariant): the node
        lives in Postgres AND the dense index; intentional retrieval treatment of the
        self node is the deferred K1/K3 follow-on (K6-D-10).
        """
        self_id = make_self_node_id(owner_id)
        existing = self._backend.get_node(owner_id, self_id)
        desired_name = display_name if display_name is not None else _UNNAMED_SELF_LABEL

        if existing is not None:
            # Rename only when the caller knows a name AND it actually changed.
            if display_name is None or existing.concept_name == desired_name:
                return existing
            return self._rename_self_node(owner_id, existing, desired_name)
        return self._create_self_node(owner_id, self_id, desired_name)

    def _create_self_node(self, owner_id: str, self_id: str, name: str) -> ConceptNode:
        now = datetime.now(UTC)
        node = ConceptNode(
            id=self_id,
            node_kind=NodeKind.SELF,
            concept_name=name,
            content=name,
            provenance=(
                NodeProvenance(
                    source=WriteSource.SYSTEM, written_at=now, reason="self node created"
                ),
            ),
            created_at=now,
        )
        vector = self._embedder.encode([node.content])[0]
        surrogate = self._backend.insert_node_if_absent(owner_id, node, vector)
        if surrogate is None:
            # Lost the create race — another writer won; return the persisted winner.
            winner = self._backend.get_node(owner_id, self_id)
            if winner is None:  # pragma: no cover - the conflict proves a row exists
                raise NodeMergeError(
                    "self node vanished after insert conflict", context={"owner_id": owner_id}
                )
            return winner
        self._index.add(surrogate=surrogate, vector=list(vector))
        self._emit_audit(
            owner_id,
            AuditAction.WRITE,
            source=WriteSource.SYSTEM,
            node_id=self_id,
            provenance=node.provenance[0],
            metadata={"action": "self_created"},
        )
        return node

    def _rename_self_node(self, owner_id: str, existing: ConceptNode, new_name: str) -> ConceptNode:
        now = datetime.now(UTC)
        provenance = (
            *existing.provenance,
            NodeProvenance(
                source=WriteSource.USER,
                written_at=now,
                reason="name updated",
                superseded_content=existing.concept_name,
            ),
        )
        renamed = ConceptNode(
            id=existing.id,
            node_kind=NodeKind.SELF,
            concept_name=new_name,
            content=new_name,
            metadata=existing.metadata,
            wellbeing_category=existing.wellbeing_category,
            provenance=provenance,
            created_at=existing.created_at,
        )
        vector = self._embedder.encode([renamed.content])[0]
        surrogate = self._backend.update_node(owner_id, renamed, vector)
        if surrogate is None:  # pragma: no cover - existing was just read
            raise NodeMergeError("self node vanished before rename", context={"owner_id": owner_id})
        self._index.replace(surrogate=surrogate, vector=list(vector))
        self._emit_audit(
            owner_id,
            AuditAction.WRITE,
            source=WriteSource.USER,
            node_id=existing.id,
            provenance=provenance[-1],
            metadata={"action": "self_renamed"},
        )
        return renamed

    # ===== read: the K1 legs ==============================================

    def get_node(self, owner_id: str, node_id: str) -> ConceptNode | None:
        return self._backend.get_node(owner_id, node_id)

    def search_dense(
        self,
        owner_id: str,
        query: str,
        top_k: int,
        *,
        allowlist: set[str] | None = None,
    ) -> list[ConceptNode]:
        vector = self._embedder.encode([query])[0]
        allowed = self._effective_allowlist(owner_id, allowlist)
        hits = self._index.search(query_vector=vector, top_k=top_k, allowlist=allowed)
        nodes = self._backend.get_nodes_by_surrogates(owner_id, [s for s, _ in hits])
        out: list[ConceptNode] = []
        for surrogate, score in hits:
            node = nodes.get(surrogate)
            if node is not None:
                out.append(node.model_copy(update={"distance": 1.0 - score}))
        return out

    def search_fts(self, owner_id: str, query: str, top_k: int) -> list[ConceptNode]:
        return self._backend.fts_query(owner_id, query, top_k)

    def flagged_nodes(self, owner_id: str) -> list[ConceptNode]:
        """The owner's wellbeing-tagged nodes — the K4 gate-eligible flagged read.

        Returns every node carrying a ``wellbeing_category`` (with its full
        provenance trail, so K4 can compute recency). RLS-scoped like all
        Postgres access; a read (CQS — no writes).
        """
        return self._backend.flagged_nodes(owner_id)

    def node_ids_for_owner(self, owner_id: str) -> list[str]:
        """The owner's full node-id set — the K4 positive-allowlist enumeration.

        Returns every durable node-id the owner holds, the basis K4 subtracts
        the flagged set from to build the positive allowlist. RLS-scoped; a read
        (CQS — no writes).
        """
        return self._backend.node_ids_for_owner(owner_id)

    def neighbors(
        self,
        owner_id: str,
        node_id: str,
        *,
        link_types: set[LinkType] | None = None,
        limit: int,
    ) -> list[tuple[TypedLink, ConceptNode]]:
        types = link_types if link_types is not None else set(LinkType)
        out: list[tuple[TypedLink, ConceptNode]] = []
        edge_types = types - {LinkType.ENTITY}
        if edge_types:
            out.extend(
                self._backend.neighbors(owner_id, node_id, link_types=edge_types, limit=limit)
            )
        if LinkType.ENTITY in types and len(out) < limit:
            for node in self._backend.entity_neighbors(owner_id, node_id)[: limit - len(out)]:
                # ENTITY links are resolved on-the-fly (D-K0-9) — synthesise the edge.
                edge = TypedLink(
                    id=make_edge_id(node_id, node.id, LinkType.ENTITY),
                    src_node_id=node_id,
                    dst_node_id=node.id,
                    link_type=LinkType.ENTITY,
                    created_at=datetime.now(UTC),
                )
                out.append((edge, node))
        return out[:limit]

    def get_embeddings(self, owner_id: str, node_ids: Sequence[str]) -> dict[str, list[float]]:
        return self._backend.get_embeddings(owner_id, node_ids)

    # ===== lifecycle =======================================================

    def rebuild_index(self, owner_id: str) -> None:
        """Re-sync the user's vectors into the index from Postgres (criterion 9 / cold-start).

        Per-owner re-add over the shared index (drop the user's entries, re-add from
        the durable embeddings). No-op on pgvector (the table IS the index); on
        turbovec this both repairs drift and performs the cold-start warm-up
        (pgvector → turbovec once the user crosses ``turbovec_calibration_min``).
        """
        items = self._backend.iter_embeddings(owner_id)
        for surrogate, _ in items:
            self._index.remove(surrogate)
        for surrogate, vector in items:
            self._index.add(surrogate=surrogate, vector=vector)

    # ===== helpers =========================================================

    def _effective_allowlist(self, owner_id: str, allowlist: set[str] | None) -> list[int]:
        """The user's surrogate set (∩ any K4 subtraction) — never ``None`` to the index."""
        if allowlist is None:
            return self._backend.surrogates_for_owner(owner_id)
        return self._backend.surrogates_for_nodes(owner_id, list(allowlist))

    def _emit_audit(
        self,
        owner_id: str,
        action: AuditAction,
        *,
        source: WriteSource,
        node_id: str,
        provenance: NodeProvenance | None = None,
        metadata: dict[str, str] | None = None,
    ) -> None:
        event = AuditEvent(
            timestamp=datetime.now(UTC),
            persona_id=owner_id,  # the scope id (CSA-1): owner_id rides the persona_id slot
            action=action,
            store="knowledge_graph",
            source=source,
            written_by=provenance.persona_id if provenance is not None else None,
            reason=provenance.reason if provenance is not None else None,
            chunk_ids=[node_id],
            metadata=metadata or {},
        )
        self._audit.emit(event)


def build_graph_store(
    *,
    engine: Engine,
    embedder: Embedder,
    audit_logger: AuditLogger,
    settings: GraphSettings | None = None,
) -> PostgresGraphStore:
    """Composition root: wire the transport, index, merge engine, and store.

    Wires the turbovec rerank's ``float32_fetch`` to the transport's
    ``embeddings_by_surrogate`` (the mandatory-rerank + criterion-6 source). The
    index backend is config-selected (pgvector default).
    """
    from persona.graph.config import GraphSettings as _Settings
    from persona.graph.index import make_graph_index
    from persona.graph.merge import MergeEngine
    from persona.graph.postgres import PostgresGraphBackend

    resolved = settings or _Settings()
    backend = PostgresGraphBackend(engine=engine)
    index = make_graph_index(
        settings=resolved, engine=engine, float32_fetch=backend.embeddings_by_surrogate
    )
    merge_engine = MergeEngine(backend=backend, embedder=embedder, settings=resolved)
    return PostgresGraphStore(
        backend=backend,
        index=index,
        merge_engine=merge_engine,
        embedder=embedder,
        audit_logger=audit_logger,
        settings=resolved,
    )

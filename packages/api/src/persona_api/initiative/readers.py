"""API implementations of the scan's two read Protocols (Spec A5, T6).

The runtime scanner is DB-free (Protocols in, candidates out); these adapters
bind it to the real stores. Every read is owner-scoped (RLS via
``rls_connection`` / the RLS-built graph store), and the initiative
subject-exclusion holds at EVERY graph surface: the noticing pool is the
``recent_nodes`` read (SELF/merged/wellbeing excluded in SQL), and the lens
expansion applies the SAME exclusion to neighbour targets — a wellbeing-tagged
node cannot enter scan context through a link either (criterion 6, scan side;
the scanner's own drop is the third layer behind these two).

R9-249 review (HIGH-1, MEDIUM-1): the exclusion follows an item's SOURCE too. A
pool node or lens target learned from a gated conversation (disclosed, or not yet
synthesised) is dropped even when it carries no tag of its own, and so is a task
whose id or originating conversation is gated. Each reader computes its gated set
ONCE per scan (one flagged read, one freshness query) from the provenance and
origins it already holds.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.graph.models import LinkType, NodeKind
from persona.logging import get_logger
from persona_runtime.initiative import ScanLink, ScanNode, ScanTask
from persona_runtime.initiative.grounding import ContextNote
from sqlalchemy import text

from persona_api.db.engine import rls_connection
from persona_api.initiative.wellbeing_gate import ConversationWellbeingGate, node_sources
from persona_api.tasks.reader import APITaskStateReader

if TYPE_CHECKING:
    from persona.graph.models import ConceptNode, TypedLink
    from persona.graph.protocol import GraphStore
    from persona.tasks import Task
    from sqlalchemy import Engine

    from persona_api.tasks.store import CheckpointStore, TaskStore

__all__ = ["ApiScanGraphReader", "ApiScanTaskReader"]

_log = get_logger("api.initiative.readers")

#: The three scan lenses (spec §2): temporal links ARE approaching dates, entity
#: threads ARE open loops, causal links ARE situations in motion. SEMANTIC is
#: K0's similarity fabric — navigation, not noticing — and is deliberately out.
_LENS_TYPES: set[LinkType] = {LinkType.TEMPORAL, LinkType.ENTITY, LinkType.CAUSAL}
_LENS_LIMIT = 3  # per-node expansion bound (anti-flooding; the pool is the driver)


#: The judge's context read (F2): how many of the newest admissible linked notes it reads
#: before the interaction gate. Tagged, SELF and merged notes are already filtered in SQL,
#: so only notes learned from a gated interaction can be dropped after; past this many of
#: those in a row the context comes back short, never older.
_CONTEXT_FETCH = 50

#: One hop from the cited node by the scan's lenses, newest note first (review MEDIUM-1:
#: ``neighbors`` reads edges oldest first, so a busy node's newest notes never surfaced).
#: Temporal and causal links are open edges; ENTITY links are the canonical association,
#: exactly as the graph store resolves them for the scan. Merged members, tagged and SELF
#: notes are excluded here (review LOW-1: a merged member keeps any edge made after the
#: merge, so the filter must be on the node). A direct link (temporal or causal) ranks
#: ahead of an entity sibling (re-review LOW-1): a hub entity ("Oslo") can link a note to
#: many newer siblings, which must not crowd out the one link that contradicts it.
_CONTEXT_SQL = text(
    """
    WITH linked AS (
        SELECT CASE WHEN e.src_node_id = :node THEN e.dst_node_id ELSE e.src_node_id END
            AS id,
            0 AS rank
        FROM graph_edges e
        WHERE e.owner_id = :owner
          AND (e.src_node_id = :node OR e.dst_node_id = :node)
          AND e.link_type IN ('temporal', 'causal')
          AND e.invalid_at IS NULL
        UNION
        SELECT other.node_id, 1 AS rank
        FROM graph_node_entities mine
        JOIN graph_node_entities other
          ON other.entity_id = mine.entity_id AND other.owner_id = mine.owner_id
        WHERE mine.owner_id = :owner AND mine.node_id = :node AND other.node_id <> :node
    )
    , ranked AS (SELECT id, MIN(rank) AS rank FROM linked GROUP BY id)
    SELECT g.id, g.content, g.created_at, g.provenance
    FROM graph_nodes g
    JOIN ranked ON ranked.id = g.id
    WHERE g.owner_id = :owner
      AND g.merged_into IS NULL
      AND g.wellbeing_category IS NULL
      AND g.node_kind <> :self_kind
    ORDER BY ranked.rank, g.created_at DESC, g.id DESC
    LIMIT :fetch
    """
)


def _row_sources(provenance: object) -> set[str]:
    """The interactions a stored node was learned from (its provenance JSON).

    Anything but a list raises (re-review LOW-2): reading it as "no sources" would skip
    the interaction gate; the error makes the judge's context CONTEXT_UNAVAILABLE.
    """
    if not isinstance(provenance, list):
        msg = f"unreadable node provenance: {type(provenance).__name__}"
        raise ValueError(msg)
    if not all(isinstance(p, dict) for p in provenance):  # third review LOW-3
        msg = "unreadable node provenance entry (not an object)"
        raise ValueError(msg)
    return {
        str(p["interaction_id"])
        for p in provenance
        if isinstance(p, dict) and p.get("interaction_id") is not None
    }


def judge_context(
    engine: Engine, gate: ConversationWellbeingGate, owner_id: str, node_id: str, *, limit: int
) -> list[ContextNote]:
    """The newest notes linked to ``node_id`` by the scan's lenses, for the judge (F2).

    The scan reader's exclusions apply: no wellbeing-tagged, SELF or merged note, and no
    note learned from a gated interaction (``include_unsynthesised`` as the reader asks
    it); a note written directly, with no interaction behind it, is shown, as the scan
    shows it. Newest first, ties broken by id, at most ``limit``.
    """
    with rls_connection(engine, owner_id) as conn:
        rows = conn.execute(
            _CONTEXT_SQL,
            {
                "owner": owner_id,
                "node": node_id,
                "self_kind": NodeKind.SELF.value,
                "fetch": _CONTEXT_FETCH,
            },
        ).all()
    sources = {src for row in rows for src in _row_sources(row.provenance)}
    gated = gate.gated(owner_id, sources, include_unsynthesised=True)
    kept = [row for row in rows if not _row_sources(row.provenance) & gated]
    return [
        ContextNote(written_on=row.created_at.date(), content=str(row.content))
        for row in kept[:limit]
    ]


def _admissible_target(node: ConceptNode) -> bool:
    """The pool read's subject exclusion, applied to lens targets.

    Wellbeing-tagged and SELF targets are dropped here; MERGED members need no
    check — consolidation window-closes their edges (K7-D-4.4), so open-edge
    traversal (the ``neighbors`` default) structurally cannot reach them.
    """
    return node.wellbeing_category is None and node.node_kind is not NodeKind.SELF


def _task_origins(task: Task) -> set[str]:
    """The task itself, its runs, and its originating conversation (where recorded)."""
    return {task.id, *task.run_ids} | ({task.conversation_id} if task.conversation_id else set())


class ApiScanGraphReader:
    """``recent_nodes`` pool + typed-lens ``neighbors`` expansion (a ``ScanGraphReader``)."""

    def __init__(self, store: GraphStore, gate: ConversationWellbeingGate) -> None:
        """Bind the RLS-built graph store (R5-D-2 audit parity) and the shared gate."""
        self._store = store
        self._gate = gate

    def noticing_pool(self, owner_id: str, *, limit: int) -> list[ScanNode]:
        """The pool with lens links; subject-safe at the read, the expansion and the source."""
        expanded: list[tuple[ConceptNode, list[tuple[TypedLink, ConceptNode]]]] = []
        for node in self._store.recent_nodes(owner_id, limit=limit):
            targets = [
                (edge, target)
                for edge, target in self._store.neighbors(
                    owner_id, node.id, link_types=_LENS_TYPES, limit=_LENS_LIMIT
                )
                if _admissible_target(target)
            ]
            expanded.append((node, targets))
        sources = {
            src
            for node, targets in expanded
            for item in (node, *(t for _, t in targets))
            for src in node_sources(item)
        }
        gated = self._gate.gated(owner_id, sources, include_unsynthesised=True)
        out: list[ScanNode] = []
        for node, targets in expanded:
            if node_sources(node) & gated:
                _log.info("scan pool node learned from a gated interaction; dropped")
                continue
            links = [
                ScanLink(
                    link_type=edge.link_type.value,
                    target_id=target.id,
                    target_content=target.content,
                )
                for edge, target in targets
                if not node_sources(target) & gated
            ]
            out.append(
                ScanNode(
                    id=node.id,
                    kind=node.node_kind.value,
                    content=node.content,
                    wellbeing_category=node.wellbeing_category,
                    links=tuple(links),
                )
            )
        return out


class ApiScanTaskReader:
    """Task history over the A4 reader: active + the additive recent-terminal read."""

    def __init__(
        self, tasks: TaskStore, checkpoints: CheckpointStore, gate: ConversationWellbeingGate
    ) -> None:
        """Bind the RLS task stores and the shared gate; a per-owner reader per call."""
        self._tasks = tasks
        self._checkpoints = checkpoints
        self._gate = gate

    def task_history(self, owner_id: str, *, limit: int) -> list[ScanTask]:
        """Active tasks first, then recent terminals; conclusions-only summaries.

        A task is dropped when its id or one of its runs is gated (a flagged node learned
        from its run) or its originating conversation is (``Task.conversation_id``,
        where recorded).
        """
        reader = APITaskStateReader(self._tasks, self._checkpoints, owner_id)
        active = reader.list_active()
        terminal = reader.list_recent_terminal(limit=max(0, limit - len(active)))
        chosen = [*active, *terminal][:limit]
        origins = {t.id: _task_origins(t) for t in chosen}
        gated = self._gate.gated(
            owner_id, set().union(*origins.values()), include_unsynthesised=True
        )
        out: list[ScanTask] = []
        for task in chosen:
            if origins[task.id] & gated:
                _log.info("scan task tied to a gated interaction; dropped")
                continue
            summary = ""
            checkpoint = reader.get_latest_checkpoint(task.id)
            if checkpoint is not None and checkpoint.progress_conclusions:
                summary = checkpoint.progress_conclusions[-1]
            out.append(
                ScanTask(
                    id=task.id,
                    goal=task.contract.goal,
                    status=task.state.value,
                    summary=summary,
                )
            )
        return out

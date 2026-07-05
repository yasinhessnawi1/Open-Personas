"""API implementations of the scan's three read Protocols (Spec A5, T6).

The runtime scanner is DB-free (Protocols in, candidates out); these adapters
bind it to the real stores. Every read is owner-scoped (RLS via
``rls_connection`` / the RLS-built graph store), and the initiative
subject-exclusion holds at EVERY graph surface: the noticing pool is the
``recent_nodes`` read (SELF/merged/wellbeing excluded in SQL), and the lens
expansion applies the SAME exclusion to neighbour targets — a wellbeing-tagged
node cannot enter scan context through a link either (criterion 6, scan side;
the scanner's own drop is the third layer behind these two).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.graph.models import LinkType, NodeKind
from persona.logging import get_logger
from persona_runtime.initiative import ScanConversation, ScanLink, ScanNode, ScanTask
from sqlalchemy import select

from persona_api.db.engine import rls_connection
from persona_api.db.models import conversations as conversations_t
from persona_api.tasks.reader import APITaskStateReader

if TYPE_CHECKING:
    from persona.graph.models import ConceptNode
    from persona.graph.protocol import GraphStore
    from sqlalchemy import Engine

    from persona_api.tasks.store import CheckpointStore, TaskStore

__all__ = ["ApiScanConversationReader", "ApiScanGraphReader", "ApiScanTaskReader"]

_log = get_logger("api.initiative.readers")

#: The three scan lenses (spec §2): temporal links ARE approaching dates, entity
#: threads ARE open loops, causal links ARE situations in motion. SEMANTIC is
#: K0's similarity fabric — navigation, not noticing — and is deliberately out.
_LENS_TYPES: set[LinkType] = {LinkType.TEMPORAL, LinkType.ENTITY, LinkType.CAUSAL}
_LENS_LIMIT = 3  # per-node expansion bound (anti-flooding; the pool is the driver)


def _admissible_target(node: ConceptNode) -> bool:
    """The pool read's subject exclusion, applied to lens targets.

    Wellbeing-tagged and SELF targets are dropped here; MERGED members need no
    check — consolidation window-closes their edges (K7-D-4.4), so open-edge
    traversal (the ``neighbors`` default) structurally cannot reach them.
    """
    return node.wellbeing_category is None and node.node_kind is not NodeKind.SELF


class ApiScanGraphReader:
    """``recent_nodes`` pool + typed-lens ``neighbors`` expansion (a ``ScanGraphReader``)."""

    def __init__(self, store: GraphStore) -> None:
        """Bind the RLS-built graph store (the worker's, R5-D-2 audit parity)."""
        self._store = store

    def noticing_pool(self, owner_id: str, *, limit: int) -> list[ScanNode]:
        """The pool with lens links; subject-safe at both the read and the expansion."""
        out: list[ScanNode] = []
        for node in self._store.recent_nodes(owner_id, limit=limit):
            links: list[ScanLink] = []
            for edge, target in self._store.neighbors(
                owner_id, node.id, link_types=_LENS_TYPES, limit=_LENS_LIMIT
            ):
                if _admissible_target(target):
                    links.append(
                        ScanLink(
                            link_type=edge.link_type.value,
                            target_id=target.id,
                            target_content=target.content,
                        )
                    )
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


class ApiScanConversationReader:
    """Recent compacted summaries over the ``conversations`` table (ruling 3 seams)."""

    def __init__(self, engine: Engine) -> None:
        """Bind the ``persona_app`` RLS engine."""
        self._engine = engine

    def recent_summaries(self, owner_id: str, *, limit: int) -> list[ScanConversation]:
        """Newest-first non-empty compacted summaries (context; grounds by id only)."""
        stmt = (
            select(conversations_t.c.id, conversations_t.c.compacted_summary)
            .where(conversations_t.c.compacted_summary != "")
            .order_by(conversations_t.c.updated_at.desc())
            .limit(limit)
        )
        with rls_connection(self._engine, owner_id) as conn:
            rows = conn.execute(stmt).all()
        return [ScanConversation(id=str(r[0]), summary=str(r[1])) for r in rows]


class ApiScanTaskReader:
    """Task history over the A4 reader: active + the additive recent-terminal read."""

    def __init__(self, tasks: TaskStore, checkpoints: CheckpointStore) -> None:
        """Bind the RLS task stores; a per-owner reader is built per call."""
        self._tasks = tasks
        self._checkpoints = checkpoints

    def task_history(self, owner_id: str, *, limit: int) -> list[ScanTask]:
        """Active tasks first, then recent terminals; conclusions-only summaries."""
        reader = APITaskStateReader(self._tasks, self._checkpoints, owner_id)
        active = reader.list_active()
        terminal = reader.list_recent_terminal(limit=max(0, limit - len(active)))
        out: list[ScanTask] = []
        for task in [*active, *terminal][:limit]:
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

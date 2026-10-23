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

from persona_api.initiative.wellbeing_gate import ConversationWellbeingGate, node_sources
from persona_api.tasks.reader import APITaskStateReader

if TYPE_CHECKING:
    from persona.graph.models import ConceptNode, TypedLink
    from persona.graph.protocol import GraphStore
    from persona.tasks import Task

    from persona_api.tasks.store import CheckpointStore, TaskStore

__all__ = ["ApiScanGraphReader", "ApiScanTaskReader"]

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

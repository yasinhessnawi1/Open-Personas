"""Turning the files a leg produced into checkpoint pointers (the A2 pointer half, D-A2-1).

The checkpoint carries bulk **by reference**: a leg writes ``report.md``, the next leg reads
``ARTIFACTS: - workspace: report.md`` in its reconstruction and opens the file rather than
inheriting its bytes. That is what keeps a checkpoint a few KB while the detail stays whole.

The reference type (:class:`~persona.tasks.checkpoint.ArtifactPointer`), the renderer, the
completion report and the task workspace were all built around this, and the field stayed
empty because nothing ever built a pointer. The producer data was already there:
:attr:`persona.schema.tools.ToolResult.artifacts` carries every durable byte-output a tool
persisted (Spec 28). This module is the one-line translation between the two, plus the bound
that stops a hundred-leg task from reciting a hundred legs of files.

Pure: no I/O, no clock. The runtime extracts a run's artifacts through
``persona_runtime.legs.ledger.artifacts_from_run``; the api's approval resolver translates a
replayed action's artifacts through here directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from persona.tasks.checkpoint import ArtifactPointer

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from persona.schema.tools import PersistedArtifact

__all__ = [
    "MAX_ARTIFACT_POINTERS",
    "WORKSPACE_POINTER_KIND",
    "merge_artifact_pointers",
    "pointers_from_artifacts",
]

#: The reference class every persisted tool output gets. ``ref`` is the workspace-relative
#: path (:attr:`persona.schema.tools.PersistedArtifact.workspace_path`), which is exactly what
#: the workspace route already serves and what a later leg's file tools already take.
WORKSPACE_POINTER_KIND: Final = "workspace"

#: How many pointers a checkpoint carries. The pointers sit outside the token budget by
#: design (they are references, not content), so nothing else bounds them: a task running for
#: weeks would otherwise recite every file it ever wrote into every leg's context. Newest win
#: when the list overflows, because the file a leg needs next is the one just written; the
#: older ones are still in the workspace and still in the run records.
MAX_ARTIFACT_POINTERS: Final = 50


def pointers_from_artifacts(artifacts: Iterable[PersistedArtifact]) -> tuple[ArtifactPointer, ...]:
    """The workspace pointers for a run's persisted byte-outputs, in order, deduplicated.

    Args:
        artifacts: The :class:`~persona.schema.tools.PersistedArtifact` values a tool surfaced.

    Returns:
        One :data:`WORKSPACE_POINTER_KIND` pointer per distinct workspace path.
    """
    seen: dict[str, None] = {}
    for artifact in artifacts:
        path = artifact.workspace_path.strip()
        if path:
            seen.setdefault(path, None)
    return tuple(ArtifactPointer(kind=WORKSPACE_POINTER_KIND, ref=path) for path in seen)


def merge_artifact_pointers(
    prior: Sequence[ArtifactPointer], fresh: Sequence[ArtifactPointer]
) -> tuple[ArtifactPointer, ...]:
    """What earlier legs produced, then this leg's, each file once, bounded.

    A file re-written under the same path keeps its ORIGINAL position: it is the same
    artifact, and a checkpoint that re-ordered its pointers every leg would make the diff
    between two checkpoints unreadable.

    Args:
        prior: The pointers the previous checkpoint carried.
        fresh: The pointers this leg produced.

    Returns:
        The merged, deduplicated pointers, capped at :data:`MAX_ARTIFACT_POINTERS` (newest kept).
    """
    seen: dict[tuple[str, str], ArtifactPointer] = {}
    for pointer in (*prior, *fresh):
        seen.setdefault((pointer.kind, pointer.ref), pointer)
    merged = tuple(seen.values())
    return merged[-MAX_ARTIFACT_POINTERS:] if len(merged) > MAX_ARTIFACT_POINTERS else merged

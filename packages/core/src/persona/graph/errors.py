"""Domain exceptions raised by the knowledge-graph store (Spec K0).

A small family under an intermediate :class:`GraphError` parent — introduced
here because K0 lands four leaf subclasses at once (the D-03-1 convention:
add the intermediate parent when the fourth subclass arrives, so callers can
catch all graph failures with one ``except GraphError``). All carry the
standard ``context: dict[str, str]`` keyword so log lines + audit events get
structured data.
"""

from __future__ import annotations

from persona.errors import PersonaError

__all__ = [
    "EntityResolutionError",
    "GraphError",
    "GraphIndexError",
    "GraphProtectedNodeError",
    "GraphRebuildError",
    "NodeMergeError",
]


class GraphError(PersonaError):
    """Base for every knowledge-graph store error (Spec K0).

    Subclass of :class:`persona.errors.PersonaError`, so the project-wide
    ``except PersonaError`` still catches graph failures; the intermediate
    parent lets callers narrow to ``except GraphError`` for the graph surface.
    """


class EntityResolutionError(GraphError):
    """Raised when canonical-entity resolution cannot produce a verdict (K0-D-9).

    The deterministic resolver returns a three-way verdict
    (``MERGE`` / ``SEPARATE`` / ``AMBIGUOUS``) on the happy path; this is
    reserved for genuinely-broken inputs (e.g. a mention with no usable
    surface form, or a registry row whose name embedding is the wrong
    dimension). ``context`` conventionally carries ``{"mention": ...}`` and,
    where relevant, ``{"reason": ...}``.
    """


class NodeMergeError(GraphError):
    """Raised when the merge engine cannot complete a canonicalise→extend/create path (K0-D-1/4).

    Covers the unrecoverable merge cases: an extend target that vanished
    mid-operation, a contradiction/update with no resolvable prior version,
    or a candidate that fails the boundary schema. Conservative-by-default
    (no silent overwrite, D-K0-4) — when merge cannot proceed safely it
    raises rather than guessing. ``context`` conventionally carries the
    candidate concept and the failing step.
    """


class GraphIndexError(GraphError):
    """Raised when the dense-index layer fails (K0-D-6/7).

    Covers an index add/replace/remove that cannot keep the same-path sync
    invariant (criterion 8), a dimension mismatch at the index boundary, or
    an allowlist/search-kernel failure. The pgvector and turbovec adapters
    both raise this at their boundary so callers depend on the domain type,
    not on a backend-specific exception. ``context`` conventionally carries
    the backend and the offending node-id/surrogate.
    """


class GraphProtectedNodeError(GraphError):
    """Raised when a K7 lifecycle op targets a structurally-protected node (K7-D-7).

    The ``NodeKind.SELF`` anchor (``{owner}::self``, Spec K6) sits OUTSIDE every K7
    lifecycle operation and the merge target space: it can never be evolved,
    consolidated, decayed, or true-deleted — a rename flows through K6-D-9's
    provenance-append path instead, and deletion is refused outright (the anchor is
    not deletable). This closes the Phase-1 gap where SELF was reachable as a
    merge/evolve/delete target. ``context`` conventionally carries the offending
    ``node_id`` and the attempted operation.
    """


class GraphRebuildError(GraphError):
    """Raised when rebuilding the dense index from Postgres fails (criterion 9).

    The index is derived and always rebuildable from the durable float32
    embeddings; this surfaces when that safety operation itself cannot
    complete (e.g. a read of ``(surrogate, embedding)`` returns an
    inconsistent shape). ``context`` conventionally carries the backend and
    the node count read.
    """

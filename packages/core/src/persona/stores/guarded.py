"""What one guarded upsert writes, for a transport with no transaction (R9-271).

A versioned write reads its chain, decides, and only then lands, and a forget can commit in
between. So the write tells the transport what it read: ``supersedes`` names each version it
replaces, with the version number it read and the id of the version replacing it, and
``requires`` names the evidence it was built from. The transport refuses the whole call when any
of that is no longer true, and nothing of it lands.

Postgres keeps this inside one transaction with row locks (:mod:`persona.stores.postgres`).
Chroma, and the in-memory transports the tests use, call :func:`guarded_batch` while holding
whatever excludes their deletes, then write the batch it returns in one call. The batch carries
each superseded version again with its link moved, because a transport that can only replace whole
rows has to move a link that way, in the same call as the new version, or a crash between two
calls would tear the chain (Spec K13).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.errors import ChainConflictError, ForgottenEvidenceError
from persona.stores.versioning import link_supersedes

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from persona.schema.chunks import PersonaChunk

__all__ = ["check_requires_kinds", "guarded_batch", "missing_evidence"]


def check_requires_kinds(requires: Sequence[str], requires_kinds: Sequence[str]) -> None:
    """Refuse evidence named with nowhere to look for it: that is a wiring mistake, not data."""
    if requires and not requires_kinds:
        msg = "a write that cites evidence must say which kinds of memory it can be in"
        raise ValueError(msg)


def missing_evidence(
    read: Callable[[str, list[str]], list[PersonaChunk]],
    requires: Sequence[str],
    requires_kinds: Sequence[str],
) -> int:
    """How many of the cited ids are on disk in none of ``requires_kinds``."""
    unfound = set(requires)
    for kind in requires_kinds:
        if not unfound:
            break
        unfound -= {c.id for c in read(kind, sorted(unfound))}
    return len(unfound)


def guarded_batch(
    read: Callable[[str, list[str]], list[PersonaChunk]],
    *,
    persona_id: str,
    store_kind: str,
    chunks: list[PersonaChunk],
    supersedes: Mapping[str, tuple[int, str]] | None,
    requires: Sequence[str],
    requires_kinds: Sequence[str],
) -> list[PersonaChunk]:
    """Check what the write read, and return the rows one upsert must write, links first.

    ``read(store_kind, ids)`` returns the chunks among ``ids`` that are on disk for this persona.

    Raises :class:`ForgottenEvidenceError` when a cited id is gone, and
    :class:`ChainConflictError` when a superseded version is gone, has another number, or has
    already been superseded. Both are raised before anything is written, and neither carries a
    word of memory text: these reach logs.
    """
    check_requires_kinds(requires, requires_kinds)
    if requires:
        missing = missing_evidence(read, requires, requires_kinds)
        if missing:
            raise ForgottenEvidenceError(
                "the evidence this memory was built from has been forgotten",
                context={"persona_id": persona_id, "store": store_kind, "missing": str(missing)},
            )
    if not supersedes:
        return list(chunks)
    on_disk = {c.id: c for c in read(store_kind, sorted(supersedes))}
    linked: list[PersonaChunk] = []
    for prior_id in sorted(supersedes):
        version, new_id = supersedes[prior_id]
        prior = on_disk.get(prior_id)
        prov = prior.provenance if prior is not None else None
        if prior is None or prov is None or prov.version != version or prov.superseded_by:
            raise ChainConflictError(
                "the version this write replaces has moved or gone since it was read",
                context={"persona_id": persona_id, "store": store_kind},
            )
        linked.append(link_supersedes(prior, new_id))
    return [*linked, *chunks]

"""Pure-function helpers for the versioned, append-only update model.

See ``docs/specs/spec_01/spec_01_core.md`` §5.2.1 and architecture v0.3 §4.3.
The four typed stores share these helpers; the helpers know nothing about
ChromaDB or any other transport.

Vocabulary recap:
- ``logical_id``: the stable identifier that groups all versions of "the
  same fact". Equal to the chunk's ``id`` on first write (D-01-8).
- ``version``: monotonic per ``logical_id`` starting at 1.
- ``superseded_by``: pointer from version N to version N+1's ``id``. The
  current (head) version of a chain has ``superseded_by=None``.
- A chain is the list of all versions for one ``logical_id``, oldest first.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from persona.errors import BrokenVersionChainError

if TYPE_CHECKING:
    from collections.abc import Iterable

    from persona.schema.chunks import PersonaChunk

__all__ = [
    "ChainDefect",
    "ChainDiagnosis",
    "ChainRewrite",
    "compute_next_version",
    "current_version",
    "current_view",
    "diagnose_chain",
    "link_supersedes",
    "plan_excision",
    "plan_raced_resolution",
    "plan_relink",
    "settled_chains",
    "validate_chain",
]


def _provenance_or_raise(chunk: PersonaChunk) -> str:
    """Return ``chunk.provenance.logical_id`` or raise if provenance is absent.

    Identity-store chunks have no provenance and never pass through these
    helpers; if one does, the caller has wired the policy table wrong.
    """
    if chunk.provenance is None:
        msg = "expected chunk with provenance; got identity-store chunk"
        raise BrokenVersionChainError(msg, context={"chunk_id": chunk.id})
    return chunk.provenance.logical_id


def compute_next_version(existing: Iterable[PersonaChunk], logical_id: str) -> int:
    """Return the version number for an append to ``logical_id``'s chain.

    Args:
        existing: All chunks the store currently holds for this persona.
            We filter by ``logical_id`` inside.
        logical_id: The logical chain being appended to. If no existing
            chunk has this ``logical_id``, the new version is 1.

    Returns:
        ``max(existing.version) + 1`` for the matching chain, or 1 if the
        chain is empty.
    """
    versions = [
        c.provenance.version
        for c in existing
        if c.provenance is not None and c.provenance.logical_id == logical_id
    ]
    return max(versions) + 1 if versions else 1


def current_version(existing: Iterable[PersonaChunk], logical_id: str) -> PersonaChunk | None:
    """Return the head (non-superseded) chunk in a logical chain, or None.

    A chain may have at most one head at any time. If the input contains
    multiple non-superseded versions for the same ``logical_id``, that is a
    broken chain — raise rather than silently pick one.
    """
    heads = [
        c
        for c in existing
        if c.provenance is not None
        and c.provenance.logical_id == logical_id
        and c.provenance.superseded_by is None
    ]
    if not heads:
        return None
    if len(heads) > 1:
        msg = "multiple head versions in chain"
        raise BrokenVersionChainError(
            msg,
            context={"logical_id": logical_id, "heads": ",".join(c.id for c in heads)},
        )
    return heads[0]


def link_supersedes(prev: PersonaChunk, new_id: str) -> PersonaChunk:
    """Return a copy of ``prev`` with ``superseded_by`` set to ``new_id``.

    Frozen Pydantic models cannot be mutated in place, so we use
    ``model_copy``. The provenance sub-model is replaced wholesale because
    it is also frozen.
    """
    _ = _provenance_or_raise(prev)
    assert prev.provenance is not None  # narrowing for type checker
    new_provenance = prev.provenance.model_copy(update={"superseded_by": new_id})
    return prev.model_copy(update={"provenance": new_provenance})


def validate_chain(chain: list[PersonaChunk]) -> None:
    """Raise :class:`BrokenVersionChainError` if ``chain`` is malformed.

    Two of the invariants are about the caller rather than the data (every chunk has
    provenance, and they all share one ``logical_id``); a failure there means a store was
    wired wrong, so it raises with the developer-facing message it always had.

    Everything else is a DATA defect, and the message a data defect raises is read by a person
    whose memory has stopped working (Spec K13, D-K13-9). It says what happened and what to do
    about it, and :func:`diagnose_chain` is what decides which of those it is. An empty chain
    is valid: a logical id nothing has written to has nothing to check.
    """
    if not chain:
        return

    # 1. Provenance on every chunk.
    for c in chain:
        if c.provenance is None:
            msg = "chain member missing provenance"
            raise BrokenVersionChainError(msg, context={"chunk_id": c.id})

    # 2. Single logical_id.
    logical_ids = {c.provenance.logical_id for c in chain if c.provenance is not None}
    if len(logical_ids) != 1:
        msg = "chain spans multiple logical_ids"
        raise BrokenVersionChainError(msg, context={"logical_ids": ",".join(sorted(logical_ids))})

    # 3. Everything the data itself can get wrong.
    diagnosis = diagnose_chain(chain)
    if diagnosis.defect is not ChainDefect.NONE:
        raise BrokenVersionChainError(
            diagnosis.detail,
            context={
                "logical_id": diagnosis.logical_id,
                "defect": str(diagnosis.defect),
                "repairable": "yes" if diagnosis.repairable else "no",
                "affected": ",".join(diagnosis.offending_ids),
                "what_to_do": _what_to_do(diagnosis),
            },
        )


def _what_to_do(diagnosis: ChainDiagnosis) -> str:
    if diagnosis.repairable:
        return "run `persona repair` on this persona"
    if diagnosis.resolvable:
        return "run `persona repair --resolve-raced` on this persona"
    return "this one needs a person to look; `persona repair` will not touch it"


# ----- diagnosis and repair (Spec K13, T3) ----------------------------------
#
# A chain can be damaged in more than one way, and the ways are not equally repairable. The
# damage this spec exists for is a crash between the two writes a versioned update used to make
# (now one write, see ``TypedStore.write``): the prior head was left pointing at a chunk that
# was never written. Some other shapes look similar and are NOT that, and guessing at those
# would turn a diagnosis into a second defect, so they are refused by name instead.
#
# Everything here is a pure function over one chain. The classification is STRUCTURAL: it asks
# whether a pointer names a row that is present and which version it is, never what an error
# message happened to say. Three of the shapes below produce the identical message from
# ``validate_chain``, so the message was never a classifier.


class ChainDefect(StrEnum):
    """What is wrong with one logical chain, if anything."""

    NONE = "none"
    #: One or more versions point at a chunk that is not in the chain. This is the crash
    #: signature: the link was committed and the chunk it names never was.
    DANGLING_LINK = "dangling_link"
    #: More than one version claims to be current. A reader sees the same memory twice and
    #: the next write to the chain raises.
    MULTIPLE_HEADS = "multiple_heads"
    #: A version number is missing from the sequence. Something deleted a row out of the
    #: middle of the chain; nothing can bring it back.
    MISSING_VERSION = "missing_version"
    #: Two rows claim the same version number.
    DUPLICATE_VERSION = "duplicate_version"
    #: Two writers read the chain and both wrote the next version (R9-205). The one duplicate
    #: shape that can be resolved: see :func:`plan_raced_resolution`.
    RACED_HEADS = "raced_heads"
    #: A version points at a chunk that IS in the chain, but not the one that follows it.
    CROSSED_LINK = "crossed_link"


class ChainDiagnosis(BaseModel):
    """What one chain is suffering from, and whether it can be put right.

    ``detail`` is written for the person who runs ``persona repair`` and reads the output, so
    it says what happened and what they can do, not what the invariant was called.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    logical_id: str
    defect: ChainDefect
    repairable: bool
    detail: str
    #: Can be put right, but only when an operator asks for it by name (``--resolve-raced``).
    #: Kept apart from ``repairable`` so a plain ``--apply`` can never renumber a chain.
    resolvable: bool = False
    #: The ids whose ``superseded_by`` is part of the problem (empty for a healthy chain).
    offending_ids: tuple[str, ...] = ()
    #: How many versions the chain holds right now.
    version_count: int = 0


def _sorted_by_version(chain: Iterable[PersonaChunk]) -> list[PersonaChunk]:
    return sorted(chain, key=lambda c: c.provenance.version if c.provenance else 0)


def diagnose_chain(chain: list[PersonaChunk]) -> ChainDiagnosis:
    """Classify one logical chain by its structure. Pure; reads nothing, writes nothing.

    An empty chain and a chain of one unsuperseded version are both healthy. Everything else
    is decided in a fixed order, worst first, so a chain with two problems is reported as the
    one that blocks the repair rather than the one that permits it.
    """
    if not chain:
        return ChainDiagnosis(
            logical_id="", defect=ChainDefect.NONE, repairable=False, detail="nothing to check"
        )

    ordered = _sorted_by_version(chain)
    logical_id = next(
        (c.provenance.logical_id for c in ordered if c.provenance is not None), ordered[0].id
    )
    present = {c.id for c in ordered}
    versions = [c.provenance.version for c in ordered if c.provenance is not None]
    count = len(ordered)

    # The one exception to the refusal below, checked first because it is a strict subset of it.
    raced = _raced_rows(ordered)
    if raced:
        return ChainDiagnosis(
            logical_id=logical_id,
            defect=ChainDefect.RACED_HEADS,
            repairable=False,
            resolvable=True,
            detail=(
                "Two saves of this memory ran at the same moment, so two versions both call "
                "themselves the newest one. Both are on disk and both are readable, and the "
                "chain refuses new saves until this is settled. Resolving keeps both, in the "
                "order they were saved, and leaves the later one current."
            ),
            offending_ids=tuple(c.id for c in raced),
            version_count=count,
        )

    # Worst first: a chain missing a version, or holding the same version twice, cannot be
    # rebuilt from its version order, because its version order is not trustworthy.
    duplicates = sorted({v for v in versions if versions.count(v) > 1})
    if duplicates:
        return ChainDiagnosis(
            logical_id=logical_id,
            defect=ChainDefect.DUPLICATE_VERSION,
            repairable=False,
            detail=(
                f"Two saved versions both call themselves version {duplicates[0]}, so there is "
                "no way to tell which came first. Nothing here is lost: both are on disk and "
                "both are readable. Repairing it automatically would mean picking one and "
                "hiding the other, so this needs a person to look."
            ),
            offending_ids=tuple(
                c.id for c in ordered if c.provenance and c.provenance.version in duplicates
            ),
            version_count=count,
        )

    expected = list(range(1, len(versions) + 1))
    if versions != expected:
        missing = sorted(set(expected) - set(versions))
        return ChainDiagnosis(
            logical_id=logical_id,
            defect=ChainDefect.MISSING_VERSION,
            repairable=False,
            detail=(
                f"This memory's saved history skips a version (missing {missing}). A version "
                "was deleted from the middle of it, and nothing can bring it back. The current "
                "version is fine and still readable; renumbering what is left would rewrite "
                "the record of what actually happened, so this is left alone."
            ),
            offending_ids=tuple(c.id for c in ordered),
            version_count=count,
        )

    # Everything below compares the links a chain HAS against the links its version order
    # says it should have. That comparison is only meaningful now that the version sequence
    # has been checked, which is why the two refusals above come first.
    wrong = plan_relink(ordered)
    by_id = {c.id: c for c in ordered}

    def _actual(chunk_id: str) -> str | None:
        prov = by_id[chunk_id].provenance
        return prov.superseded_by if prov is not None else None

    # A pointer that names a chunk which IS here, but is not the one that follows it, was not
    # made by a crash. Rebuilding the links from the version order would throw that meaning
    # away, so it is refused rather than guessed at.
    crossed = [i for i in wrong if _actual(i) is not None and _actual(i) in present]
    if crossed:
        return ChainDiagnosis(
            logical_id=logical_id,
            defect=ChainDefect.CROSSED_LINK,
            repairable=False,
            detail=(
                "This memory's versions are linked in an order that does not match their "
                "version numbers. Every version is present and readable, so nothing is lost, "
                "but an interrupted save cannot produce this and repairing it would mean "
                "guessing which order was meant. If you need the history back, the version "
                "numbers are the record of what the order was."
            ),
            offending_ids=tuple(sorted(crossed)),
            version_count=count,
        )

    dangling = [i for i in wrong if _actual(i) is not None and _actual(i) not in present]
    if dangling:
        return ChainDiagnosis(
            logical_id=logical_id,
            defect=ChainDefect.DANGLING_LINK,
            repairable=True,
            detail=(
                "A saved version points forward at a newer version that was never written, "
                "which is what an interrupted save leaves behind. No content is missing: "
                "every version that was written is still here. Repairing relinks them in "
                "version order, which makes this memory's history readable again."
            ),
            offending_ids=tuple(sorted(dangling)),
            version_count=count,
        )

    if wrong:
        # What is left is a row that should point at its successor and points at nothing:
        # more than one version calls itself current.
        return ChainDiagnosis(
            logical_id=logical_id,
            defect=ChainDefect.MULTIPLE_HEADS,
            repairable=True,
            detail=(
                "More than one version of this memory is marked as the current one, so it can "
                "come back twice in the same answer. Every version is present. Repairing "
                "relinks them in version order and leaves the newest as the current one."
            ),
            offending_ids=tuple(sorted(wrong)),
            version_count=count,
        )

    return ChainDiagnosis(
        logical_id=logical_id,
        defect=ChainDefect.NONE,
        repairable=False,
        detail="healthy",
        version_count=count,
    )


def _raced_rows(ordered: list[PersonaChunk]) -> list[PersonaChunk]:
    """The rows that raced, when ``ordered`` is EXACTLY the raced shape, else nothing.

    Exactly: every duplicate is at the top version, every top row is unsuperseded, and
    everything below is a complete, correctly linked chain whose last row points at one of the
    top rows. Each clause is what makes the version order the truth up to the race, so a chain
    that fails any of them stays a refused ``DUPLICATE_VERSION`` for a person to look at.
    """
    provenances = [c.provenance for c in ordered]
    if any(p is None for p in provenances):
        return []
    top = max(c.provenance.version for c in ordered if c.provenance is not None)
    rows = [c for c in ordered if c.provenance is not None and c.provenance.version == top]
    below = [c for c in ordered if c.provenance is not None and c.provenance.version != top]
    if len(rows) < 2 or any(_link(c) is not None for c in rows):
        return []
    if [c.provenance.version for c in below if c.provenance is not None] != list(range(1, top)):
        return []
    if any(_link(c) != following.id for c, following in zip(below, below[1:], strict=False)):
        return []
    if below and _link(below[-1]) not in {c.id for c in rows}:
        return []
    return rows


def _link(chunk: PersonaChunk) -> str | None:
    return chunk.provenance.superseded_by if chunk.provenance is not None else None


class ChainRewrite(BaseModel):
    """A chain's new shape: the links to write and the versions to renumber."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    links: dict[str, str | None]
    versions: dict[str, int]


def plan_raced_resolution(chain: list[PersonaChunk]) -> ChainRewrite:
    """The order a raced chain's top rows go in, and what that makes of its links and versions.

    The prior head's ``superseded_by`` was overwritten by each writer in turn, so the row it
    names committed last, and that is a fact the database recorded rather than a clock reading.
    The others go by ``written_at``, then id so the answer is the same on every run. The first
    row keeps the contested version and the rest take the numbers after it.

    Raises:
        BrokenVersionChainError: ``chain`` is not exactly the raced shape.
    """
    ordered = _sorted_by_version(chain)
    raced = _raced_rows(ordered)
    if not raced:
        msg = "this chain is not the raced shape, so it has no raced resolution"
        raise BrokenVersionChainError(msg, context={"chunks": str(len(chain))})

    raced_ids = {c.id for c in raced}
    below = [c for c in ordered if c.id not in raced_ids]
    prior = below[-1] if below else None
    committed_last = _link(prior) if prior is not None else None

    def written_key(chunk: PersonaChunk) -> tuple[object, str]:
        assert chunk.provenance is not None  # _raced_rows checked every row has provenance
        return (chunk.provenance.written_at, chunk.id)

    earlier = sorted((c for c in raced if c.id != committed_last), key=written_key)
    settled = [*earlier, *(c for c in raced if c.id == committed_last)]

    assert settled[0].provenance is not None
    first_version = settled[0].provenance.version
    links: dict[str, str | None] = {}
    if prior is not None:
        links[prior.id] = settled[0].id
    for chunk, following in zip(settled, [*settled[1:], None], strict=True):
        links[chunk.id] = following.id if following is not None else None
    return ChainRewrite(
        links=links,
        versions={c.id: first_version + i for i, c in enumerate(settled)},
    )


def plan_excision(chain: list[PersonaChunk], doomed_ids: Iterable[str]) -> ChainRewrite:
    """What the rest of a chain has to become once ``doomed_ids`` are gone.

    Taking a version out of a chain leaves a hole and a link to a row that is not there, which
    :func:`diagnose_chain` calls ``MISSING_VERSION`` and refuses for good. So the survivors are
    closed up: renumbered from 1 in version order, each linked to the next, the newest current.
    Only rows that would change appear in the result.

    The chain does not have to be healthy going in. Two heads on a transport with no index come
    out as one, in version order, then ``written_at``, then id.
    """
    doomed = set(doomed_ids)

    def order(chunk: PersonaChunk) -> tuple[int, object, str]:
        assert chunk.provenance is not None  # every row of a chain has provenance
        return (chunk.provenance.version, chunk.provenance.written_at, chunk.id)

    survivors = sorted((c for c in chain if c.id not in doomed), key=order)
    links: dict[str, str | None] = {}
    versions: dict[str, int] = {}
    for position, chunk in enumerate(survivors):
        following = survivors[position + 1].id if position + 1 < len(survivors) else None
        if _link(chunk) != following:
            links[chunk.id] = following
        if chunk.provenance is not None and chunk.provenance.version != position + 1:
            versions[chunk.id] = position + 1
    return ChainRewrite(links=links, versions=versions)


def plan_relink(chain: list[PersonaChunk]) -> dict[str, str | None]:
    """The links a repairable chain SHOULD have, for the rows that do not have them.

    The links are fully determined by the version order: version N points at version N+1, and
    the newest version points at nothing. That is only a safe thing to assert because
    :func:`diagnose_chain` refuses every shape where the version order is not the truth, so
    this function must never be called on a diagnosis that is not ``repairable``.

    Returns a map of chunk id to its correct ``superseded_by`` (``None`` meaning current),
    containing only the rows that are currently wrong. An empty map means nothing to do.
    """
    ordered = _sorted_by_version(chain)
    wanted: dict[str, str | None] = {}
    for i, chunk in enumerate(ordered):
        correct = ordered[i + 1].id if i + 1 < len(ordered) else None
        actual = chunk.provenance.superseded_by if chunk.provenance is not None else None
        if actual != correct:
            wanted[chunk.id] = correct
    return wanted


def settled_chains(chunks: Iterable[PersonaChunk]) -> list[PersonaChunk]:
    """The same chunks, with every settleable chain that has two heads settled.

    For copying a store with no head index into one that has it (the community import). The
    source may hold a chain two saves raced, nobody can run a repair inside an import, and the
    index would refuse the copy. This changes pointers on the copies only, and only for the
    shapes ``persona repair`` itself would settle. Anything else comes back as it was, and the
    index refuses it, which is the honest outcome for a shape nobody can settle without guessing.
    """
    materialised = list(chunks)
    chains: dict[str, list[PersonaChunk]] = {}
    for chunk in materialised:
        if chunk.provenance is not None:
            chains.setdefault(chunk.provenance.logical_id, []).append(chunk)

    settled: dict[str, PersonaChunk] = {}
    for chain in chains.values():
        diagnosis = diagnose_chain(chain)
        if diagnosis.resolvable:
            plan = plan_raced_resolution(chain)
            links, versions = plan.links, plan.versions
        elif diagnosis.defect is ChainDefect.MULTIPLE_HEADS:
            links, versions = plan_relink(chain), {}
        else:
            continue
        for chunk in chain:
            if chunk.id in links and chunk.provenance is not None:
                update: dict[str, object] = {"superseded_by": links[chunk.id]}
                if chunk.id in versions:
                    update["version"] = versions[chunk.id]
                provenance = chunk.provenance.model_copy(update=update)
                settled[chunk.id] = chunk.model_copy(update={"provenance": provenance})
    return [settled.get(c.id, c) for c in materialised]


def current_view(chunks: Iterable[PersonaChunk]) -> list[PersonaChunk]:
    """The rows a reader should see, with a chain that points nowhere still readable.

    Ordinarily the current view is every chunk with no ``superseded_by``. A chain broken by an
    interrupted save has NO such row: its newest surviving version points at a chunk that was
    never written, so the node vanishes from every read while its rows sit on disk. This
    restores exactly that case: **when a chain has no current version at all, its highest
    version is treated as the current one.**

    Deliberately no wider than that. Ignoring every dangling pointer would also un-supersede
    old versions in chains that still have a perfectly good current version, and the reader
    would get the same memory twice, which is worse than the problem being fixed. This is a
    read, so it never writes the repair back (``persona repair`` does that, on purpose and with
    an audit trail); input order is preserved.
    """
    materialised = list(chunks)
    current = [
        c for c in materialised if c.provenance is None or c.provenance.superseded_by is None
    ]
    covered = {c.provenance.logical_id for c in current if c.provenance is not None}

    orphaned: dict[str, PersonaChunk] = {}
    for chunk in materialised:
        prov = chunk.provenance
        if prov is None or prov.logical_id in covered:
            continue
        best = orphaned.get(prov.logical_id)
        if best is None or prov.version > (best.provenance.version if best.provenance else 0):
            orphaned[prov.logical_id] = chunk
    if not orphaned:
        return current

    rescued = {c.id for c in orphaned.values()}
    return [c for c in materialised if c in current or c.id in rescued]

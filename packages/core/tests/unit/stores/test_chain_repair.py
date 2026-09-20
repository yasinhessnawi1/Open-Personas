"""A broken version chain is named, readable, and repairable, and the rest is refused (K13, T3).

T2 closed the window that makes a chain break. This is about the chains that might already be
broken: a store written by an older release, or, the case we cannot see from here, a self
hoster's database. Production has none, which is the reason this is written as prevention and
not as a cleanup: the diagnosis's ordinary answer is "nothing is wrong" and the tests below
say that out loud rather than only testing the damaged paths.

Four behaviours, each with its own reason for existing:

1. **Diagnosis is structural.** Three of the shapes below produce the identical message from
   the old validator, so a classifier that read the message would conflate them.
2. **A chain that points nowhere is readable again.** The worst shape leaves a node with no
   current version at all: invisible to every read while its rows sit on disk. That is a read
   fix, not a repair, so it works before anybody runs anything.
3. **The repair rebuilds links from the version order**, which is only honest while the
   version order is trustworthy.
4. **Everything else is refused by name.** A chain whose pointers disagree with its version
   numbers, or which is missing a version, is not what a crash produces, and guessing at it
   would turn a diagnosis into a second defect.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from persona.audit import AuditAction, MemoryAuditLogger
from persona.errors import BrokenVersionChainError
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource
from persona.stores.self_facts import SelfFactsStore
from persona.stores.versioning import (
    ChainDefect,
    current_view,
    diagnose_chain,
    plan_relink,
)
from tests.unit.stores.test_versioned_store_integrity import _IdKeyedBackend

_NOW = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)
_PERSONA = "p1"
_KIND = "self_facts"
_LOGICAL = "p1::self_facts::0000"


def _v(version: int, *, superseded_by: str | None, chunk_id: str | None = None) -> PersonaChunk:
    return PersonaChunk(
        id=chunk_id or (_LOGICAL if version == 1 else f"{_LOGICAL}::v{version:04d}"),
        text=f"version {version}",
        created_at=_NOW,
        provenance=ChunkProvenance(
            source=WriteSource.USER,
            logical_id=_LOGICAL,
            version=version,
            superseded_by=superseded_by,
            written_at=_NOW,
        ),
    )


def _seed(backend: _IdKeyedBackend, chain: list[PersonaChunk]) -> None:
    backend.upsert(persona_id=_PERSONA, store_kind=_KIND, chunks=chain)


def _store(backend: _IdKeyedBackend) -> SelfFactsStore:
    return SelfFactsStore(backend=backend, audit_logger=MemoryAuditLogger())


# --- 1. the six shapes, told apart by structure -------------------------------------------


def test_a_healthy_chain_is_healthy() -> None:
    """The ordinary answer, and the one almost every real chain gives."""
    chain = [_v(1, superseded_by=f"{_LOGICAL}::v0002"), _v(2, superseded_by=None)]
    assert diagnose_chain(chain).defect is ChainDefect.NONE


def test_one_version_on_its_own_is_healthy() -> None:
    assert diagnose_chain([_v(1, superseded_by=None)]).defect is ChainDefect.NONE


def test_an_interrupted_save_with_nothing_after_it_is_a_dangling_link() -> None:
    """The worst shape: the link was written, the chunk it names never was."""
    diagnosis = diagnose_chain([_v(1, superseded_by="never-written")])
    assert diagnosis.defect is ChainDefect.DANGLING_LINK
    assert diagnosis.repairable
    assert diagnosis.offending_ids == (_LOGICAL,)


def test_an_interrupted_save_followed_by_a_retry_is_still_a_dangling_link() -> None:
    """The shape that accumulates silently: one head, so every ordinary read looks fine."""
    chain = [_v(1, superseded_by="never-written"), _v(2, superseded_by=None, chunk_id="retry")]
    diagnosis = diagnose_chain(chain)
    assert diagnosis.defect is ChainDefect.DANGLING_LINK
    assert diagnosis.repairable


def test_two_current_versions_are_multiple_heads() -> None:
    diagnosis = diagnose_chain([_v(1, superseded_by=None), _v(2, superseded_by=None)])
    assert diagnosis.defect is ChainDefect.MULTIPLE_HEADS
    assert diagnosis.repairable


def test_a_deleted_middle_version_is_refused() -> None:
    """Nothing can bring the missing version back, and renumbering would rewrite the record."""
    chain = [_v(1, superseded_by=f"{_LOGICAL}::v0002"), _v(3, superseded_by=None)]
    diagnosis = diagnose_chain(chain)
    assert diagnosis.defect is ChainDefect.MISSING_VERSION
    assert not diagnosis.repairable


def test_two_rows_claiming_one_version_are_refused() -> None:
    chain = [_v(1, superseded_by=None), _v(1, superseded_by=None, chunk_id="twin")]
    diagnosis = diagnose_chain(chain)
    assert diagnosis.defect is ChainDefect.DUPLICATE_VERSION
    assert not diagnosis.repairable


def test_a_link_that_points_at_the_wrong_present_version_is_refused() -> None:
    """THE refusal that matters: the target exists, so this is not an interrupted save.

    Rebuilding the links from the version order would be a guess about what somebody meant,
    and the repair's whole licence to act is that it is not guessing.
    """
    chain = [
        _v(1, superseded_by=f"{_LOGICAL}::v0003"),
        _v(2, superseded_by=f"{_LOGICAL}::v0003"),
        _v(3, superseded_by=None),
    ]
    diagnosis = diagnose_chain(chain)
    assert diagnosis.defect is ChainDefect.CROSSED_LINK
    assert not diagnosis.repairable
    assert diagnosis.offending_ids == (_LOGICAL,)


def test_the_newest_version_pointing_backwards_is_refused() -> None:
    """A cycle reads as perfectly healthy to a check that only looks for missing targets."""
    chain = [_v(1, superseded_by=f"{_LOGICAL}::v0002"), _v(2, superseded_by=_LOGICAL)]
    assert diagnose_chain(chain).defect is ChainDefect.CROSSED_LINK


def test_the_refusals_say_what_a_person_should_do() -> None:
    """Refusing without explaining is just a dead end with better manners."""
    refused = [
        diagnose_chain([_v(1, superseded_by=f"{_LOGICAL}::v0002"), _v(3, superseded_by=None)]),
        diagnose_chain([_v(1, superseded_by=None), _v(1, superseded_by=None, chunk_id="twin")]),
    ]
    for diagnosis in refused:
        assert not diagnosis.repairable
        assert len(diagnosis.detail) > 80, diagnosis.detail
        # It has to say the content is safe, because that is the reader's first question.
        assert "lost" in diagnosis.detail or "readable" in diagnosis.detail


# --- 2. a chain that points nowhere is readable again -------------------------------------


def test_a_chain_with_no_current_version_is_still_read() -> None:
    """The node was invisible: no unsuperseded row, so every read skipped it."""
    backend = _IdKeyedBackend()
    _seed(backend, [_v(1, superseded_by="never-written")])

    visible = _store(backend).get_all(_PERSONA)

    assert [c.id for c in visible] == [_LOGICAL], (
        "a chain whose only version points at a chunk that was never written is invisible "
        "to every read; its rows are on disk and nothing can reach them"
    )


def test_the_tolerance_does_not_resurrect_an_old_version() -> None:
    """The narrowness is the point: a wider rule returns the same memory twice."""
    backend = _IdKeyedBackend()
    _seed(
        backend,
        [_v(1, superseded_by="never-written"), _v(2, superseded_by=None, chunk_id="retry")],
    )

    visible = _store(backend).get_all(_PERSONA)

    assert [c.id for c in visible] == ["retry"], (
        "the chain has a perfectly good current version, so ignoring the older version's "
        "dangling pointer would hand the reader two copies of one memory"
    )


def test_the_tolerance_is_a_read_and_writes_nothing() -> None:
    """CQS. The repair is a separate, audited, deliberate act."""
    backend = _IdKeyedBackend()
    _seed(backend, [_v(1, superseded_by="never-written")])
    backend.upsert_batches.clear()

    _store(backend).get_all(_PERSONA)

    assert backend.upsert_batches == []
    rows = backend.get_all(persona_id=_PERSONA, store_kind=_KIND)
    assert rows[0].provenance is not None
    assert rows[0].provenance.superseded_by == "never-written", "the read repaired the row"


def test_history_still_refuses_a_broken_chain_and_names_the_repair() -> None:
    """Read tolerance makes the node readable; it does not pretend the history is fine."""
    backend = _IdKeyedBackend()
    _seed(backend, [_v(1, superseded_by="never-written")])

    with pytest.raises(BrokenVersionChainError) as caught:
        _store(backend).history(_PERSONA, _LOGICAL)

    message = str(caught.value)
    assert "persona repair" in message, f"the error does not say what to do: {message}"
    assert "defect=dangling_link" in message


# --- 3. the repair ------------------------------------------------------------------------


def test_repair_relinks_an_interrupted_save() -> None:
    backend = _IdKeyedBackend()
    _seed(
        backend,
        [_v(1, superseded_by="never-written"), _v(2, superseded_by=None, chunk_id="retry")],
    )
    store = _store(backend)

    repaired = store.repair(_PERSONA)

    assert repaired == 1
    chain = store.history(_PERSONA, _LOGICAL)
    assert [c.id for c in chain] == [_LOGICAL, "retry"]


def test_repair_makes_the_lonely_broken_chain_current_again() -> None:
    backend = _IdKeyedBackend()
    _seed(backend, [_v(1, superseded_by="never-written")])
    store = _store(backend)

    assert store.repair(_PERSONA) == 1

    rows = backend.get_all(persona_id=_PERSONA, store_kind=_KIND)
    assert rows[0].provenance is not None
    assert rows[0].provenance.superseded_by is None
    store.history(_PERSONA, _LOGICAL)  # no longer raises


def test_repair_leaves_a_healthy_store_alone() -> None:
    """The commonest run. It must be a no-op, not a rewrite that happens to match."""
    backend = _IdKeyedBackend()
    _seed(backend, [_v(1, superseded_by=f"{_LOGICAL}::v0002"), _v(2, superseded_by=None)])
    before = backend.get_all(persona_id=_PERSONA, store_kind=_KIND)

    assert _store(backend).repair(_PERSONA) == 0
    assert backend.get_all(persona_id=_PERSONA, store_kind=_KIND) == before


def test_repair_refuses_what_it_cannot_honestly_fix() -> None:
    """The refusal has to hold at the store level, not only in the classifier."""
    backend = _IdKeyedBackend()
    _seed(backend, [_v(1, superseded_by=f"{_LOGICAL}::v0002"), _v(3, superseded_by=None)])
    before = backend.get_all(persona_id=_PERSONA, store_kind=_KIND)

    assert _store(backend).repair(_PERSONA) == 0, "repair touched a chain it cannot justify"
    assert backend.get_all(persona_id=_PERSONA, store_kind=_KIND) == before


def test_repair_emits_one_audit_event_per_chain() -> None:
    backend = _IdKeyedBackend()
    _seed(backend, [_v(1, superseded_by="never-written")])
    audit = MemoryAuditLogger()
    store = SelfFactsStore(backend=backend, audit_logger=audit)

    store.repair(_PERSONA, written_by="cli.repair")

    events = [e for e in audit.events if e.action is AuditAction.REPAIR]
    assert len(events) == 1
    assert events[0].logical_ids == [_LOGICAL]
    assert events[0].written_by == "cli.repair"
    assert events[0].metadata["defect"] == "dangling_link"


def test_repair_never_writes_a_chunk() -> None:
    """The reason ``persona repair`` needs no model: it moves pointers, it does not save text."""
    backend = _IdKeyedBackend()
    _seed(backend, [_v(1, superseded_by="never-written")])
    backend.upsert_batches.clear()

    _store(backend).repair(_PERSONA)

    assert backend.upsert_batches == [], (
        "the repair went through upsert, which re-embeds every chunk it writes"
    )


def test_diagnose_reports_what_it_checked_even_when_all_is_well() -> None:
    """A maintenance command that says nothing when nothing is wrong reads like it did not run."""
    backend = _IdKeyedBackend()
    _seed(backend, [_v(1, superseded_by=f"{_LOGICAL}::v0002"), _v(2, superseded_by=None)])

    diagnosis = _store(backend).diagnose(_PERSONA)

    assert diagnosis.healthy
    assert diagnosis.chains_checked == 1
    assert diagnosis.findings == ()


# --- 4. the link plan -----------------------------------------------------------------------


def test_the_plan_only_touches_the_rows_that_are_wrong() -> None:
    chain = [
        _v(1, superseded_by=f"{_LOGICAL}::v0002"),
        _v(2, superseded_by="never-written"),
        _v(3, superseded_by=None, chunk_id=f"{_LOGICAL}::v0003"),
    ]
    assert plan_relink(chain) == {f"{_LOGICAL}::v0002": f"{_LOGICAL}::v0003"}


def test_current_view_passes_unversioned_rows_through() -> None:
    """Identity chunks carry no provenance and belong to no chain."""
    plain = PersonaChunk(id="identity::0000", text="a name", created_at=_NOW)
    assert current_view([plain]) == [plain]


# --- 5. one indexed chain read, not a full store scan (Spec K13, T5) -----------------------


class _CountingBackend(_IdKeyedBackend):
    """Records which read the STORE asked for, so a scan cannot hide behind a right answer.

    Each read is served straight from the bucket rather than by delegating to its sibling,
    because the base fake implements ``get_by_logical_ids`` and ``recent`` in terms of
    ``get_all``: delegating would log reads this fake made of itself and blame them on the
    store. The real transports serve all three independently, which is the point.
    """

    def __init__(self) -> None:
        super().__init__()
        self.reads: list[str] = []

    def get_all(self, *, persona_id: str, store_kind: str) -> list[PersonaChunk]:
        self.reads.append("get_all")
        return list(self._bucket(persona_id, store_kind).values())

    def get_by_logical_ids(
        self, *, persona_id: str, store_kind: str, logical_ids: list[str]
    ) -> list[PersonaChunk]:
        self.reads.append("get_by_logical_ids")
        wanted = set(logical_ids)
        return [
            c
            for c in self._bucket(persona_id, store_kind).values()
            if (c.provenance.logical_id if c.provenance is not None else c.id) in wanted
        ]

    def recent(self, *, persona_id: str, store_kind: str, limit: int) -> list[PersonaChunk]:
        self.reads.append("recent")
        return list(self._bucket(persona_id, store_kind).values())[:limit]


def _chain_of_three(backend: _IdKeyedBackend) -> None:
    _seed(
        backend,
        [
            _v(1, superseded_by=f"{_LOGICAL}::v0002"),
            _v(2, superseded_by=f"{_LOGICAL}::v0003"),
            _v(3, superseded_by=None),
        ],
    )


def test_history_reads_one_chain_and_not_the_whole_store() -> None:
    """A full scan ships every embedding in the store to answer a question about one fact."""
    backend = _CountingBackend()
    _chain_of_three(backend)
    store = _store(backend)
    backend.reads.clear()

    store.history(_PERSONA, _LOGICAL)

    assert backend.reads == ["get_by_logical_ids"], (
        f"history took {backend.reads}; get_all reads every row in the store, and on Postgres "
        "that means every 384 dimension vector, to build one chain"
    )


def test_rollback_performs_one_chain_read() -> None:
    """Acceptance 4. Rollback used to pay for the scan twice: its own, after its caller's."""
    backend = _CountingBackend()
    _chain_of_three(backend)
    store = _store(backend)
    backend.reads.clear()

    store.rollback(_PERSONA, _LOGICAL, to_version=1, source=WriteSource.USER)

    assert backend.reads == ["get_by_logical_ids"], f"rollback took {backend.reads}"


def test_history_still_returns_the_whole_chain_in_order() -> None:
    """The cheaper read has to be the same read."""
    backend = _CountingBackend()
    _chain_of_three(backend)

    chain = _store(backend).history(_PERSONA, _LOGICAL)

    assert [c.provenance.version for c in chain if c.provenance] == [1, 2, 3]


def test_recent_keeps_its_backend_push_down() -> None:
    """Acceptance 5, a guard rather than a change: K8 pushed ``recent`` down and an outside
    review still reported it as a disguised full scan, because it had been one in the last
    published release. It must not quietly become one again."""
    backend = _CountingBackend()
    _chain_of_three(backend)
    store = _store(backend)
    backend.reads.clear()

    store.recent(_PERSONA, limit=2)

    assert backend.reads == ["recent"], (
        f"recent() took {backend.reads}: it is supposed to be pushed down to the transport "
        "(ORDER BY ... LIMIT on Postgres), not served by materialising the store"
    )

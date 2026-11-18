"""A forget racing a write must not put back what it took (R9-271).

Every versioned write reads its chain, decides, and only then lands. A forget can commit inside
that gap, and so can a forget of the evidence a derived write (a core block, a gist) was built
from during a model call that takes seconds. These tests run each interleaving deterministically
through a gate: the transport runs it once, at the start of the next write, which is after the
writer has read and checked everything it is going to check in Python.

The transport here models the contract the real ones keep (``Backend.upsert`` with
``supersedes`` and ``requires``): the versions a write supersedes are compared with what the
writer read, the evidence it cites is checked, and nothing of a refused call lands. Both shapes
run, with the head index (Postgres) and without it (Chroma), because the head index is not what
makes any of this safe.

The core refresh's own sweep after the write is switched off where a test says so. It is a
second line, and these tests are about the first one: what lands must already be right.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from persona.audit import AuditAction, MemoryAuditLogger
from persona.errors import ChainConflictError, ForgottenEvidenceError
from persona.recall.core_memory import CoreBlock, core_block_to_chunk, refresh_core_block
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource, mint_chunk_id
from persona.stores.core_memory import CORE_MEMORY_KIND, CoreMemoryStore, core_logical_id
from persona.stores.engine import EpisodicConsolidationEngine
from persona.stores.episodic import EpisodicStore
from persona.stores.lifecycle import EpisodicSettings
from persona.stores.pyramid import GIST_KIND, EpisodicPyramid
from persona.stores.self_facts import SelfFactsStore
from persona.stores.summarizer import StubSummarizer
from persona.stores.versioning import validate_chain

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

_NOW = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)
_PERSONA = "p1"
_CORE = core_logical_id(_PERSONA)
_FACT = "fact-chain"

_BOTH_SHAPES = pytest.mark.parametrize("head_index", [True, False], ids=["postgres", "chroma"])


class _Transport:
    """In-memory ``memory_chunks`` keeping the append contract, with a gate on the next write."""

    def __init__(self, *, head_index: bool) -> None:
        self.rows: dict[str, tuple[str, str, PersonaChunk]] = {}
        self.head_index = head_index
        #: Runs once, at the start of the next write of ``gate_kind``.
        self.gate: Callable[[], None] | None = None
        self.gate_kind: str | None = None
        self.writes = 0
        #: Every write of this kind is refused as a lost race, as a mapped deadlock would be.
        self.conflict_kind: str | None = None

    # -- the gate ------------------------------------------------------------

    def gate_next_write(self, kind: str, action: Callable[[], None]) -> None:
        self.gate, self.gate_kind = action, kind

    def _open_gate(self, store_kind: str) -> None:
        if self.gate is not None and self.gate_kind == store_kind:
            action, self.gate = self.gate, None
            action()

    # -- writes --------------------------------------------------------------

    def _land(self, staged: dict[str, tuple[str, str, PersonaChunk]]) -> None:
        if self.head_index:
            heads: set[tuple[str, str, str]] = set()
            for owner, kind, chunk in staged.values():
                prov = chunk.provenance
                if prov is None or prov.superseded_by is not None:
                    continue
                key = (owner, kind, prov.logical_id)
                if key in heads:
                    raise ChainConflictError("another writer landed on this version chain first")
                heads.add(key)
        self.rows = staged

    def upsert(
        self,
        *,
        persona_id: str,
        store_kind: str,
        chunks: list[PersonaChunk],
        supersedes: dict[str, tuple[int, str]] | None = None,
        requires: Sequence[str] = (),
        requires_kinds: Sequence[str] = (),
    ) -> None:
        self._open_gate(store_kind)
        self.writes += 1
        if store_kind == self.conflict_kind:
            raise ChainConflictError("another writer landed on this version chain first")
        present = {
            chunk_id
            for chunk_id, (owner, kind, _) in self.rows.items()
            if owner == persona_id and kind in requires_kinds
        }
        if set(requires) - present:
            raise ForgottenEvidenceError("cited evidence is gone")
        staged = dict(self.rows)
        for prior_id, (version, new_id) in (supersedes or {}).items():
            row = staged.get(prior_id)
            if row is None or row[0] != persona_id or row[1] != store_kind:
                raise ChainConflictError("the version this write supersedes is gone")
            prov = row[2].provenance
            if prov is None or prov.version != version or prov.superseded_by is not None:
                raise ChainConflictError("the version this write supersedes has moved")
            moved = prov.model_copy(update={"superseded_by": new_id})
            staged[prior_id] = (row[0], row[1], row[2].model_copy(update={"provenance": moved}))
        for chunk in chunks:
            staged[chunk.id] = (persona_id, store_kind, chunk)
        self._land(staged)

    def excise_versions(
        self,
        *,
        persona_id: str,
        store_kind: str,
        logical_id: str,
        expected: dict[str, tuple[int, str | None]],
        doomed_ids: list[str],
        links: dict[str, str | None],
        versions: dict[str, int],
    ) -> None:
        if self._versions_of(persona_id, store_kind, logical_id) != expected:
            raise ChainConflictError("the chain moved since the plan was made")
        staged = {k: v for k, v in self.rows.items() if k not in doomed_ids}
        for chunk_id in {*links, *versions}:
            owner, kind, chunk = staged[chunk_id]
            assert chunk.provenance is not None
            update: dict[str, object] = {}
            if chunk_id in links:
                update["superseded_by"] = links[chunk_id]
            if chunk_id in versions:
                update["version"] = versions[chunk_id]
            moved = chunk.provenance.model_copy(update=update)
            staged[chunk_id] = (owner, kind, chunk.model_copy(update={"provenance": moved}))
        self._land(staged)

    def delete_documents(self, *, persona_id: str, store_kind: str, ids: list[str]) -> None:
        for chunk_id in ids:
            row = self.rows.get(chunk_id)
            if row is not None and row[0] == persona_id and row[1] == store_kind:
                del self.rows[chunk_id]

    def delete_persona(self, persona_id: str, store_kind: str) -> None:
        for chunk in self._of(persona_id, store_kind):
            del self.rows[chunk.id]

    # -- reads ---------------------------------------------------------------

    def _of(self, persona_id: str, store_kind: str) -> list[PersonaChunk]:
        return [c for p, k, c in self.rows.values() if p == persona_id and k == store_kind]

    def _versions_of(
        self, persona_id: str, store_kind: str, logical_id: str
    ) -> dict[str, tuple[int, str | None]]:
        return {
            c.id: (c.provenance.version, c.provenance.superseded_by)
            for c in self.get_by_logical_ids(
                persona_id=persona_id, store_kind=store_kind, logical_ids=[logical_id]
            )
            if c.provenance is not None
        }

    def get_all(self, *, persona_id: str, store_kind: str) -> list[PersonaChunk]:
        return self._of(persona_id, store_kind)

    def get_by_ids(self, *, persona_id: str, store_kind: str, ids: list[str]) -> list[PersonaChunk]:
        return [c for c in self._of(persona_id, store_kind) if c.id in ids]

    def get_by_logical_ids(
        self, *, persona_id: str, store_kind: str, logical_ids: list[str]
    ) -> list[PersonaChunk]:
        return [
            c
            for c in self._of(persona_id, store_kind)
            if c.provenance is not None and c.provenance.logical_id in logical_ids
        ]

    def recent(self, *, persona_id: str, store_kind: str, limit: int) -> list[PersonaChunk]:
        rows = sorted(self._of(persona_id, store_kind), key=lambda c: (c.created_at, c.id))
        return list(reversed(rows))[:limit]

    def set_bands(self, *, persona_id: str, store_kind: str, bands: dict[str, int]) -> None:
        _ = (persona_id, store_kind, bands)

    def band_histogram(self, *, persona_id: str, store_kind: str) -> dict[int, int]:
        return {0: len(self._of(persona_id, store_kind))}

    # -- what the tests look at -----------------------------------------------

    def texts(self, store_kind: str) -> list[str]:
        return [c.text for c in self._of(_PERSONA, store_kind)]

    def chain(self, store_kind: str, logical_id: str) -> list[PersonaChunk]:
        rows = self.get_by_logical_ids(
            persona_id=_PERSONA, store_kind=store_kind, logical_ids=[logical_id]
        )
        return sorted(rows, key=lambda c: c.provenance.version if c.provenance else 0)


def _evidence(transport: _Transport, *ids: str, kind: str = "episodic") -> list[PersonaChunk]:
    chunks = []
    for index, chunk_id in enumerate(ids):
        created = _NOW - timedelta(hours=3) + timedelta(minutes=index)
        chunk = PersonaChunk(
            id=chunk_id,
            text=f"evidence {chunk_id}",
            created_at=created,
            provenance=ChunkProvenance(
                source=WriteSource.SYSTEM, logical_id=chunk_id, written_at=created
            ),
        )
        transport.rows[chunk_id] = (_PERSONA, kind, chunk)
        chunks.append(chunk)
    return chunks


def _cited(transport: _Transport) -> set[str]:
    return {
        source
        for c in transport.chain(CORE_MEMORY_KIND, _CORE)
        for source in c.metadata.get("source_ids", "").split(",")
        if source
    }


def _forget(transport: _Transport, audit: MemoryAuditLogger, *ids: str) -> Callable[[], None]:
    return lambda: EpisodicStore(backend=transport, audit_logger=audit).remove_documents(  # type: ignore[arg-type]
        _PERSONA, list(ids)
    )


def _core(transport: _Transport) -> tuple[CoreMemoryStore, MemoryAuditLogger]:
    audit = MemoryAuditLogger()
    return CoreMemoryStore(backend=transport, audit_logger=audit), audit


def _block(store: CoreMemoryStore, text: str, *sources: str) -> None:
    chunk = core_block_to_chunk(CoreBlock(text=text, source_ids=sources), _PERSONA, now=_NOW)
    store.write(_PERSONA, [chunk], source=WriteSource.SYSTEM)


async def _refresh(store: CoreMemoryStore, sources: list[PersonaChunk]) -> CoreBlock | None:
    return await refresh_core_block(
        _PERSONA,
        summarizer=StubSummarizer(),
        sources=sources,
        store=store,
        target_tokens=50,
        now=_NOW,
    )


@pytest.fixture
def no_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Switch off the refresh's after-the-write sweep, so only what lands is judged."""
    monkeypatch.setattr(CoreMemoryStore, "orphaned_citations", lambda *_: [])


# --- R1: the prior head must not come back ------------------------------------------------


def _fact(chunk_id: str, text: str) -> PersonaChunk:
    return PersonaChunk(
        id=chunk_id,
        text=text,
        created_at=_NOW,
        provenance=ChunkProvenance(source=WriteSource.SYSTEM, logical_id=_FACT, written_at=_NOW),
    )


@_BOTH_SHAPES
def test_a_forget_between_the_read_and_the_write_does_not_bring_the_old_version_back(
    head_index: bool,
) -> None:
    transport = _Transport(head_index=head_index)
    audit = MemoryAuditLogger()
    store = SelfFactsStore(backend=transport, audit_logger=audit)  # type: ignore[arg-type]
    store.write(_PERSONA, [_fact("f1", "the thing to forget")], force=True)
    transport.gate_next_write("self_facts", lambda: store.remove_documents(_PERSONA, ["f1"]))

    store.write(_PERSONA, [_fact("f1", "a later, unrelated update")], force=True)

    assert "the thing to forget" not in transport.texts("self_facts")
    chain = transport.chain("self_facts", _FACT)
    validate_chain(chain)
    assert [(c.text, c.provenance.version) for c in chain if c.provenance] == [
        ("a later, unrelated update", 1)
    ], "the writer's own text lands, as the first version of a chain that starts again"


@_BOTH_SHAPES
def test_a_forget_of_another_chain_costs_the_writer_nothing(head_index: bool) -> None:
    transport = _Transport(head_index=head_index)
    audit = MemoryAuditLogger()
    store = SelfFactsStore(backend=transport, audit_logger=audit)  # type: ignore[arg-type]
    store.write(_PERSONA, [_fact("f1", "first")], force=True)
    other = _fact("g1", "another fact").model_copy(
        update={
            "provenance": ChunkProvenance(
                source=WriteSource.SYSTEM, logical_id="other", written_at=_NOW
            )
        }
    )
    store.write(_PERSONA, [other], force=True)
    transport.gate_next_write("self_facts", lambda: store.remove_documents(_PERSONA, ["g1"]))
    writes = transport.writes

    store.write(_PERSONA, [_fact("f1", "second")], force=True)

    assert transport.writes == writes + 1, "an unrelated forget must not cost a retry"
    assert [c.text for c in transport.chain("self_facts", _FACT)] == ["first", "second"]


@_BOTH_SHAPES
@pytest.mark.asyncio
@pytest.mark.usefixtures("no_sweep")
async def test_a_forget_that_takes_the_head_inside_the_write_leaves_one_clean_chain(
    head_index: bool,
) -> None:
    transport = _Transport(head_index=head_index)
    store, audit = _core(transport)
    sources = _evidence(transport, "a", "x", "b")
    _block(store, "summary of a", "a")
    _block(store, "summary of x", "x")
    transport.gate_next_write(CORE_MEMORY_KIND, _forget(transport, audit, "x"))

    block = await _refresh(store, [sources[2]])

    assert block is not None
    assert "summary of x" not in transport.texts(CORE_MEMORY_KIND)
    assert "x" not in _cited(transport)
    chain = transport.chain(CORE_MEMORY_KIND, _CORE)
    validate_chain(chain)
    assert [c.text for c in chain] == ["summary of a", block.text]


@_BOTH_SHAPES
@pytest.mark.asyncio
@pytest.mark.usefixtures("no_sweep")
async def test_a_forget_of_the_whole_chain_inside_the_write_brings_none_of_it_back(
    head_index: bool,
) -> None:
    transport = _Transport(head_index=head_index)
    store, audit = _core(transport)
    sources = _evidence(transport, "x", "b")
    _block(store, "summary of x", "x")
    transport.gate_next_write(CORE_MEMORY_KIND, _forget(transport, audit, "x"))

    block = await _refresh(store, [sources[1]])

    assert block is not None
    assert [c.text for c in transport.chain(CORE_MEMORY_KIND, _CORE)] == [block.text]


# --- R1b: a write over a head the forget renumbered ---------------------------------------


@_BOTH_SHAPES
@pytest.mark.asyncio
async def test_a_write_over_a_renumbered_head_leaves_no_gap_in_the_history(
    head_index: bool,
) -> None:
    transport = _Transport(head_index=head_index)
    store, audit = _core(transport)
    sources = _evidence(transport, "a", "b", "c", "d")
    _block(store, "first", "a")
    _block(store, "second", "b")
    _block(store, "third", "c")
    transport.gate_next_write(CORE_MEMORY_KIND, _forget(transport, audit, "b"))

    block = await _refresh(store, [sources[3]])

    assert block is not None
    history = store.history(_PERSONA, _CORE)  # raised BrokenVersionChainError before R9-271
    assert [(c.text, c.provenance.version) for c in history if c.provenance] == [
        ("first", 1),
        ("third", 2),
        (block.text, 3),
    ]


# --- a rollback to a summary whose evidence a forget takes while it lands (round 2) -------


@_BOTH_SHAPES
def test_a_rollback_racing_a_forget_of_its_evidence_puts_nothing_back(head_index: bool) -> None:
    """The rollback checks its target, then a forget takes the evidence and the target with it.

    The forget renumbers the head as it closes the chain, so the rollback's write is stale and
    goes round again. The second round must still refuse a summary of what was just forgotten,
    rather than append its text as the new head (PR #39 review, round 1).
    """
    transport = _Transport(head_index=head_index)
    store, audit = _core(transport)
    _evidence(transport, "x", "a")
    _block(store, "summary of x", "x")
    _block(store, "summary of a", "a")
    transport.gate_next_write(CORE_MEMORY_KIND, _forget(transport, audit, "x"))

    with pytest.raises(ForgottenEvidenceError):
        store.rollback(_PERSONA, _CORE, 1, source=WriteSource.SYSTEM)

    assert transport.texts(CORE_MEMORY_KIND) == ["summary of a"]
    assert "x" not in _cited(transport)
    validate_chain(transport.chain(CORE_MEMORY_KIND, _CORE))


@_BOTH_SHAPES
def test_a_rollback_whose_evidence_stays_still_lands_after_a_lost_race(head_index: bool) -> None:
    """The guard must not cost a legitimate rollback: an unrelated forget only makes it retry."""
    transport = _Transport(head_index=head_index)
    store, audit = _core(transport)
    _evidence(transport, "a", "b", "y")
    _block(store, "summary of a", "a")
    _block(store, "summary of y", "y")
    _block(store, "summary of b", "b")
    transport.gate_next_write(CORE_MEMORY_KIND, _forget(transport, audit, "y"))

    store.rollback(_PERSONA, _CORE, 1, source=WriteSource.SYSTEM)

    current = store.current(_PERSONA)
    assert current is not None
    assert current.text == "summary of a"
    validate_chain(transport.chain(CORE_MEMORY_KIND, _CORE))


# --- R2: a block built from evidence forgotten before it landed ----------------------------


@_BOTH_SHAPES
@pytest.mark.asyncio
@pytest.mark.usefixtures("no_sweep")
async def test_a_block_built_from_evidence_forgotten_before_the_write_never_lands(
    head_index: bool,
) -> None:
    transport = _Transport(head_index=head_index)
    store, audit = _core(transport)
    sources = _evidence(transport, "a", "x")
    transport.gate_next_write(CORE_MEMORY_KIND, _forget(transport, audit, "x"))

    block = await _refresh(store, sources)

    assert block is None
    assert transport.chain(CORE_MEMORY_KIND, _CORE) == [], "a block citing forgotten x landed"
    assert store.current(_PERSONA) is None
    core_writes = [
        e for e in audit.events if e.store == CORE_MEMORY_KIND and e.action is AuditAction.WRITE
    ]
    assert core_writes == []


@_BOTH_SHAPES
@pytest.mark.asyncio
@pytest.mark.usefixtures("no_sweep")
async def test_the_older_block_stays_current_when_the_new_one_is_refused(
    head_index: bool,
) -> None:
    transport = _Transport(head_index=head_index)
    store, audit = _core(transport)
    sources = _evidence(transport, "a", "x")
    _block(store, "summary of a", "a")
    transport.gate_next_write(CORE_MEMORY_KIND, _forget(transport, audit, "x"))

    assert await _refresh(store, sources) is None

    current = store.current(_PERSONA)
    assert current is not None
    assert current.text == "summary of a"
    validate_chain(transport.chain(CORE_MEMORY_KIND, _CORE))


@_BOTH_SHAPES
@pytest.mark.asyncio
@pytest.mark.usefixtures("no_sweep")
async def test_a_refresh_whose_own_evidence_is_untouched_still_lands(head_index: bool) -> None:
    transport = _Transport(head_index=head_index)
    store, audit = _core(transport)
    sources = _evidence(transport, "a", "b", "y")
    transport.gate_next_write(CORE_MEMORY_KIND, _forget(transport, audit, "y"))

    block = await _refresh(store, sources[:2])

    assert block is not None
    assert store.current(_PERSONA) is not None
    assert _cited(transport) == {"a", "b"}


@_BOTH_SHAPES
@pytest.mark.asyncio
async def test_a_block_citing_a_row_in_another_store_lands(head_index: bool) -> None:
    """The guard looks where a citation can be, the same kinds the sweep looks in."""
    transport = _Transport(head_index=head_index)
    store, _ = _core(transport)
    sources = _evidence(transport, "a")
    identity = _evidence(transport, "identity-row", kind="identity")

    block = await _refresh(store, [*sources, *identity])

    assert block is not None
    assert _cited(transport) == {"a", "identity-row"}


def test_a_write_naming_evidence_on_a_store_that_cites_none_is_refused() -> None:
    transport = _Transport(head_index=True)
    store = SelfFactsStore(backend=transport, audit_logger=MemoryAuditLogger())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="cites no evidence"):
        store.write(_PERSONA, [_fact("f1", "text")], force=True, requires=["a"])
    assert transport.rows == {}


# --- R2b: a gist built from evidence forgotten during its model call ----------------------

_SETTINGS = EpisodicSettings(
    min_chunks_per_run=2,
    cluster_gap_minutes=45.0,
    cluster_split_threshold=0.70,
    max_cluster_chunks=40,
    gist_target_tokens=64,
    engine_max_batch=500,
)


class _ForgetsDuringTheCall:
    """A summarizer a forget lands inside, once."""

    def __init__(self, forget: Callable[[], None] | None) -> None:
        self._forget = forget

    async def summarize(self, content: str, *, target_tokens: int) -> str:
        _ = target_tokens
        if self._forget is not None:
            forget, self._forget = self._forget, None
            forget()
        return f"gist of: {content[:40]}"


class _Merges:
    def __init__(self) -> None:
        self.candidates: list[Any] = []

    def merge(self, owner_id: str, candidate: Any) -> object:  # noqa: ANN401
        _ = owner_id
        self.candidates.append(candidate)
        return object()


def _raw(transport: _Transport, count: int) -> list[str]:
    ids = []
    for index in range(count):
        chunk_id = mint_chunk_id(_PERSONA, "episodic")
        created = _NOW - timedelta(hours=3) + timedelta(minutes=index)
        transport.rows[chunk_id] = (
            _PERSONA,
            "episodic",
            PersonaChunk(
                id=chunk_id,
                text=f"USER: remember {index}\nASSISTANT: noted",
                created_at=created,
                provenance=ChunkProvenance(
                    source=WriteSource.SYSTEM, logical_id=chunk_id, written_at=created
                ),
            ),
        )
        ids.append(chunk_id)
    return ids


def _engine(
    transport: _Transport,
    summarizer: Any,  # noqa: ANN401
) -> tuple[EpisodicConsolidationEngine, EpisodicPyramid, _Merges]:
    audit = MemoryAuditLogger()
    pyramid = EpisodicPyramid(backend=transport, audit_logger=audit)  # type: ignore[arg-type]
    merges = _Merges()
    engine = EpisodicConsolidationEngine(
        backend=transport,  # type: ignore[arg-type]
        pyramid=pyramid,
        summarizer=summarizer,
        graph=merges,  # type: ignore[arg-type]
        settings=_SETTINGS,
    )
    return engine, pyramid, merges


@_BOTH_SHAPES
def test_a_gist_whose_member_was_forgotten_during_the_model_call_is_not_written(
    head_index: bool,
) -> None:
    transport = _Transport(head_index=head_index)
    ids = _raw(transport, 3)
    audit = MemoryAuditLogger()
    engine, pyramid, merges = _engine(
        transport, _ForgetsDuringTheCall(_forget(transport, audit, ids[1]))
    )

    report = asyncio.run(engine.run("u1", _PERSONA, now=_NOW))

    assert pyramid.gists(_PERSONA) == [], "a gist over forgotten evidence landed"
    assert merges.candidates == [], "a candidate built from forgotten evidence reached the graph"
    assert report.gists_written == 0
    assert [s.reason for s in report.skipped] == ["evidence forgotten"]


@_BOTH_SHAPES
def test_the_next_pass_gists_what_remains(head_index: bool) -> None:
    transport = _Transport(head_index=head_index)
    ids = _raw(transport, 3)
    audit = MemoryAuditLogger()
    engine, pyramid, _ = _engine(
        transport, _ForgetsDuringTheCall(_forget(transport, audit, ids[1]))
    )
    asyncio.run(engine.run("u1", _PERSONA, now=_NOW))

    report = asyncio.run(engine.run("u1", _PERSONA, now=_NOW))

    assert report.gists_written == 1
    assert [g.member_ids for g in pyramid.gists(_PERSONA)] == [(ids[0], ids[2])]
    assert GIST_KIND in {k for _, k, _ in transport.rows.values()}


@_BOTH_SHAPES
def test_a_gist_write_that_loses_a_race_skips_its_window_and_the_run_goes_on(
    head_index: bool,
) -> None:
    """A lost race on one gist (or a deadlock Postgres broke) must not abort the whole pass."""
    transport = _Transport(head_index=head_index)
    _raw(transport, 3)
    transport.conflict_kind = GIST_KIND
    engine, pyramid, merges = _engine(transport, _ForgetsDuringTheCall(None))

    report = asyncio.run(engine.run("u1", _PERSONA, now=_NOW))

    assert pyramid.gists(_PERSONA) == []
    assert merges.candidates == []
    assert [s.reason for s in report.skipped] == ["gist write conflict"]

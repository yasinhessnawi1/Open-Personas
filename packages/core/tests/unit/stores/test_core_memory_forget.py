"""A derived summary must not outlive its evidence (R9-209, K8-D-14 applied to the core block).

The core block is a summary written from cited source ids, and it is versioned. Forget deleted
the evidence and left the summary: the block went on reading out forgotten material in the prompt
of every turn, and when all the evidence was gone the refresh found nothing to summarise and kept
the old block for good. Gists have honoured K8-D-14 since K8. The core block never did.

So a forget deletes every version that cites a forgotten id. The care is all in what that leaves:

- A chain with a version taken out of the middle is ``MISSING_VERSION``, which ``history`` raises
  on and ``persona repair`` refuses forever. So the survivors are re-linked and renumbered in the
  same operation, and the chain comes out of a forget valid.
- The head can be one of the doomed. The newest survivor becomes current, because a chain must
  have exactly one head. **That is what the persona shows until the next pass rebuilds: an older
  summary that cites nothing forgotten, or, with no survivor, no block at all.** Pinned below.
- A refresh can be in flight, holding sources a forget is about to delete, and the model call
  takes seconds. It checks its sources before it writes and again after. The forget deletes the
  evidence first and the block second, so whichever way they interleave, the block is gone.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from persona.audit import AuditAction, MemoryAuditLogger
from persona.errors import ChainConflictError, ForgottenEvidenceError
from persona.recall.core_memory import (
    CoreBlock,
    core_block_to_chunk,
    read_core_block,
    refresh_core_block,
)
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource
from persona.stores.core_memory import CORE_MEMORY_KIND, CoreMemoryStore, core_logical_id
from persona.stores.episodic import EpisodicStore
from persona.stores.summarizer import StubSummarizer
from persona.stores.versioning import ChainDefect, diagnose_chain, validate_chain

if TYPE_CHECKING:
    from collections.abc import Callable

_NOW = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
_PERSONA = "p1"
_CHAIN = core_logical_id(_PERSONA)


class _Backend:
    """In-memory ``memory_chunks`` that records what touched the core chain."""

    def __init__(self) -> None:
        self.rows: dict[str, tuple[str, str, PersonaChunk]] = {}
        self.excisions = 0
        self.before_next_core_write: Callable[[], None] | None = None
        #: Runs inside the next excise, after the forget has read the chain (R2-M1 b).
        self.before_next_excise: Callable[[], None] | None = None
        #: Model the head index on excise: a plan that would leave two heads is refused whole.
        self.head_index = False
        self.always_conflict = False

    def _of(self, persona_id: str, store_kind: str) -> list[PersonaChunk]:
        return [c for p, k, c in self.rows.values() if p == persona_id and k == store_kind]

    def upsert(self, *, persona_id: str, store_kind: str, chunks: list[PersonaChunk]) -> None:
        if store_kind == CORE_MEMORY_KIND and self.before_next_core_write is not None:
            hook, self.before_next_core_write = self.before_next_core_write, None
            hook()
        for chunk in chunks:
            self.rows[chunk.id] = (persona_id, store_kind, chunk)

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

    def delete_documents(self, *, persona_id: str, store_kind: str, ids: list[str]) -> None:
        for chunk_id in ids:
            row = self.rows.get(chunk_id)
            if row is not None and row[0] == persona_id and row[1] == store_kind:
                del self.rows[chunk_id]

    def delete_persona(self, persona_id: str, store_kind: str) -> None:
        for chunk in self._of(persona_id, store_kind):
            del self.rows[chunk.id]

    def recent(self, *, persona_id: str, store_kind: str, limit: int) -> list[PersonaChunk]:
        return self._of(persona_id, store_kind)[:limit]

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
        hook, self.before_next_excise = self.before_next_excise, None
        if hook is not None:
            hook()
        self.excisions += 1
        if self.always_conflict:
            raise ChainConflictError("another writer landed on this version chain first")
        found = {
            c.id: (c.provenance.version, c.provenance.superseded_by)
            for c in self.get_by_logical_ids(
                persona_id=persona_id, store_kind=store_kind, logical_ids=[logical_id]
            )
            if c.provenance is not None
        }
        if found != expected:
            raise ChainConflictError("the chain moved since the plan was made")
        before = dict(self.rows)
        self.delete_documents(persona_id=persona_id, store_kind=store_kind, ids=doomed_ids)
        for chunk_id in {*links, *versions}:
            owner, kind, chunk = self.rows[chunk_id]
            assert chunk.provenance is not None
            update: dict[str, object] = {}
            if chunk_id in links:
                update["superseded_by"] = links[chunk_id]
            if chunk_id in versions:
                update["version"] = versions[chunk_id]
            provenance = chunk.provenance.model_copy(update=update)
            self.rows[chunk_id] = (
                owner,
                kind,
                chunk.model_copy(update={"provenance": provenance}),
            )
        heads = [
            c
            for p, k, c in self.rows.values()
            if p == persona_id
            and k == store_kind
            and c.provenance
            and not c.provenance.superseded_by
        ]
        if self.head_index and len(heads) > 1:
            self.rows = before  # one transaction: nothing of it landed
            raise ChainConflictError("another writer landed on this version chain first")


def _store(backend: _Backend | None = None) -> tuple[CoreMemoryStore, _Backend, MemoryAuditLogger]:
    backend = backend or _Backend()
    audit = MemoryAuditLogger()
    return CoreMemoryStore(backend=backend, audit_logger=audit), backend, audit  # type: ignore[arg-type]


def _block(store: CoreMemoryStore, text: str, *sources: str) -> None:
    chunk = core_block_to_chunk(CoreBlock(text=text, source_ids=sources), _PERSONA, now=_NOW)
    store.write(_PERSONA, [chunk], source=WriteSource.SYSTEM)


def _evidence(backend: _Backend, *ids: str) -> None:
    for chunk_id in ids:
        chunk = PersonaChunk(
            id=chunk_id,
            text=f"evidence {chunk_id}",
            created_at=_NOW,
            provenance=ChunkProvenance(
                source=WriteSource.SYSTEM, logical_id=chunk_id, written_at=_NOW
            ),
        )
        backend.rows[chunk_id] = (_PERSONA, "episodic", chunk)


def _chain(backend: _Backend) -> list[PersonaChunk]:
    rows = [c for p, k, c in backend.rows.values() if k == CORE_MEMORY_KIND and p == _PERSONA]
    return sorted(rows, key=lambda c: c.provenance.version if c.provenance else 0)


def _three_blocks() -> tuple[CoreMemoryStore, _Backend, MemoryAuditLogger]:
    store, backend, audit = _store()
    _block(store, "first", "a")
    _block(store, "second", "b")
    _block(store, "third", "c")
    return store, backend, audit


@pytest.mark.parametrize(
    ("forgotten", "left"),
    [
        ("a", ["second", "third"]),
        ("b", ["first", "third"]),
        ("c", ["first", "second"]),
    ],
    ids=["oldest", "middle", "head"],
)
def test_the_chain_comes_out_valid_whichever_version_went(forgotten: str, left: list[str]) -> None:
    store, backend, _ = _three_blocks()

    assert store.forget_cited(_PERSONA, [forgotten]) == 1

    chain = _chain(backend)
    assert [c.text for c in chain] == left
    validate_chain(chain)  # a hole is MISSING_VERSION, which nothing could ever repair
    assert [c.provenance.version for c in chain if c.provenance] == [1, 2]
    assert store.history(_PERSONA, _CHAIN) == chain
    assert diagnose_chain(chain).defect is ChainDefect.NONE


def test_exactly_one_head_is_left_and_it_is_the_newest_survivor() -> None:
    store, backend, _ = _three_blocks()
    store.forget_cited(_PERSONA, ["c"])
    heads = [c for c in _chain(backend) if c.provenance and c.provenance.superseded_by is None]
    assert [c.text for c in heads] == ["second"]


def test_a_version_citing_several_forgotten_ids_goes_once() -> None:
    store, backend, _ = _store()
    _block(store, "first", "a", "b")
    _block(store, "second", "b", "c")
    _block(store, "third", "d")

    assert store.forget_cited(_PERSONA, ["a", "b", "c"]) == 2

    assert [c.text for c in _chain(backend)] == ["third"]
    validate_chain(_chain(backend))


def test_a_block_citing_nothing_forgotten_is_left_exactly_as_it_was() -> None:
    store, backend, audit = _three_blocks()
    before = _chain(backend)
    events = len(audit.events)

    assert store.forget_cited(_PERSONA, ["something-else"]) == 0

    assert _chain(backend) == before
    assert backend.excisions == 0, "a forget that touches no block must not touch the chain"
    assert len(audit.events) == events


def test_no_block_yet_is_nothing_to_do() -> None:
    store, backend, _ = _store()
    assert store.forget_cited(_PERSONA, ["a"]) == 0
    assert backend.excisions == 0


def test_a_chain_that_was_already_broken_comes_out_healthy() -> None:
    """Chroma has no head index, so two heads can be here when a forget arrives."""
    store, backend, _ = _store()
    _block(store, "first", "a")
    _block(store, "second", "b")
    head = _chain(backend)[-1]
    assert head.provenance is not None
    racer = PersonaChunk(
        id="racer",
        text="third",
        metadata={"source_ids": "c"},
        created_at=_NOW,
        provenance=head.provenance.model_copy(update={"superseded_by": None}),
    )
    backend.rows["racer"] = (_PERSONA, CORE_MEMORY_KIND, racer)

    store.forget_cited(_PERSONA, ["a"])

    validate_chain(_chain(backend))


def test_every_version_going_leaves_no_chain_and_no_block() -> None:
    store, backend, _ = _store()
    _block(store, "first", "a")
    _block(store, "second", "a", "b")

    assert store.forget_cited(_PERSONA, ["a"]) == 2

    assert _chain(backend) == []
    assert store.current(_PERSONA) is None


def test_the_audit_records_ids_and_counts_and_never_a_word_of_text() -> None:
    store, backend, audit = _three_blocks()
    doomed = _chain(backend)[1].id

    store.forget_cited(_PERSONA, ["b"])

    event = audit.events[-1]
    assert event.action is AuditAction.REMOVE_DOCUMENTS
    assert event.store == CORE_MEMORY_KIND
    assert event.chunk_ids == [doomed]
    assert event.logical_ids == [_CHAIN]
    assert event.metadata == {"forgotten_cited": "1", "versions_left": "2"}
    dumped = event.model_dump_json()
    for word in ("first", "second", "third"):
        assert word not in dumped


def _episodic(backend: _Backend, audit: MemoryAuditLogger) -> EpisodicStore:
    return EpisodicStore(backend=backend, audit_logger=audit)


def test_forgetting_through_the_door_reaches_the_block() -> None:
    store, backend, audit = _three_blocks()
    _evidence(backend, "a", "b", "c")

    _episodic(backend, audit).remove_documents(_PERSONA, ["c"])

    assert "c" not in backend.rows
    assert [c.text for c in _chain(backend)] == ["first", "second"]
    assert store.current(_PERSONA).text == "second"  # type: ignore[union-attr]


def test_the_door_cascades_on_every_version_of_the_fact_not_only_the_one_named() -> None:
    """A block cites a physical id, and that version may since have been edited."""
    store, backend, audit = _store()
    episodic = _episodic(backend, audit)
    first = PersonaChunk(
        id="fact",
        text="the first wording",
        created_at=_NOW,
        provenance=ChunkProvenance(source=WriteSource.SYSTEM, logical_id="fact", written_at=_NOW),
    )
    episodic.write(_PERSONA, [first], source=WriteSource.SYSTEM)
    _block(store, "summary of the first wording", "fact")
    edited = first.model_copy(update={"id": "fact-edit"})
    episodic.write(_PERSONA, [edited], source=WriteSource.SYSTEM)

    episodic.remove_documents(_PERSONA, ["fact-edit"])

    assert _chain(backend) == []


def test_the_evidence_goes_before_the_block() -> None:
    """The order that makes a refresh in flight lose, whichever way it interleaves."""
    store, backend, audit = _three_blocks()
    _evidence(backend, "a", "b", "c")
    seen: list[bool] = []
    real = backend.excise_versions

    def recording(**kwargs: Any) -> None:  # noqa: ANN401 - the transport's keywords
        seen.append("c" in backend.rows)
        real(**kwargs)

    backend.excise_versions = recording  # type: ignore[method-assign]
    _episodic(backend, audit).remove_documents(_PERSONA, ["c"])
    assert seen == [False]
    assert store.current(_PERSONA) is not None


def test_wiping_the_episodic_store_retires_the_block() -> None:
    store, backend, audit = _three_blocks()
    _evidence(backend, "a", "b", "c")

    _episodic(backend, audit).delete(_PERSONA)

    assert _chain(backend) == []
    assert store.current(_PERSONA) is None


def test_the_persona_shows_the_newest_clean_summary_until_the_next_pass() -> None:
    """Pinned for the owner: it may briefly know less, and it never says the forgotten thing."""
    store, backend, audit = _three_blocks()
    _evidence(backend, "a", "b", "c")

    _episodic(backend, audit).remove_documents(_PERSONA, ["c"])

    shown = read_core_block(store, _PERSONA)
    assert shown is not None
    assert shown.text == "second"
    assert "c" not in shown.source_ids


def test_the_persona_shows_no_block_when_no_version_survives() -> None:
    """Pinned: no block at all, which renders nothing, byte-identical to before K9 (K9-D-11)."""
    store, backend, audit = _store()
    _block(store, "only", "a")
    _evidence(backend, "a")

    _episodic(backend, audit).remove_documents(_PERSONA, ["a"])

    assert read_core_block(store, _PERSONA) is None


async def _refresh(store: CoreMemoryStore, sources: list[PersonaChunk]) -> CoreBlock | None:
    return await refresh_core_block(
        _PERSONA,
        summarizer=StubSummarizer(),
        sources=sources,
        store=store,
        target_tokens=50,
        now=_NOW,
    )


def _sources(backend: _Backend, *ids: str) -> list[PersonaChunk]:
    _evidence(backend, *ids)
    return [backend.rows[i][2] for i in ids]


@pytest.mark.asyncio
async def test_the_next_pass_rebuilds_from_what_remains_and_the_chain_carries_on() -> None:
    store, backend, audit = _three_blocks()
    _evidence(backend, "a", "b", "c")
    _episodic(backend, audit).remove_documents(_PERSONA, ["c"])

    await _refresh(store, _sources(backend, "d"))

    chain = _chain(backend)
    validate_chain(chain)
    assert [c.provenance.version for c in chain if c.provenance] == [1, 2, 3]
    assert read_core_block(store, _PERSONA).source_ids == ("d",)  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_with_all_the_evidence_gone_the_old_block_does_not_stay() -> None:
    """recall/core_memory.py used to return here and leave the block in the prompt for good."""
    store, backend, _ = _three_blocks()

    assert await _refresh(store, []) is None

    assert _chain(backend) == []
    assert store.current(_PERSONA) is None


@pytest.mark.asyncio
async def test_a_first_refresh_with_nothing_to_summarise_is_quiet() -> None:
    store, backend, audit = _store()
    assert await _refresh(store, []) is None
    assert backend.excisions == 0
    assert audit.events == []


class _ForgetsDuringTheModelCall:
    """A summarizer whose model call a forget lands inside."""

    def __init__(self, forget: Callable[[], None]) -> None:
        self._forget = forget

    async def summarize(self, content: str, *, target_tokens: int) -> str:
        _ = (content, target_tokens)
        self._forget()
        return "a summary of something just forgotten"


@pytest.mark.asyncio
async def test_a_forget_during_the_model_call_stops_the_block_landing() -> None:
    store, backend, audit = _store()
    sources = _sources(backend, "a", "b")

    block = await refresh_core_block(
        _PERSONA,
        summarizer=_ForgetsDuringTheModelCall(
            lambda: _episodic(backend, audit).remove_documents(_PERSONA, ["a"])
        ),
        sources=sources,
        store=store,
        target_tokens=50,
        now=_NOW,
    )

    assert block is None
    assert _chain(backend) == [], "a block citing a deleted id landed"
    core_writes = [
        e for e in audit.events if e.store == CORE_MEMORY_KIND and e.action is AuditAction.WRITE
    ]
    assert core_writes == [], "a block already known to be stale must not be written at all"


@pytest.mark.asyncio
async def test_a_forget_between_the_check_and_the_write_is_caught_after_the_write() -> None:
    """The window the first check cannot see: the second check is what closes it."""
    store, backend, audit = _store()
    _block(store, "older", "z")
    sources = _sources(backend, "a", "b", "z")
    backend.before_next_core_write = lambda: _episodic(backend, audit).remove_documents(
        _PERSONA, ["a"]
    )

    block = await _refresh(store, sources)

    assert block is None
    assert [c.text for c in _chain(backend)] == ["older"]
    validate_chain(_chain(backend))


def test_a_rollback_to_a_block_citing_forgotten_evidence_is_refused() -> None:
    """Rare now, because a forget deletes such a version. Restored data can still hold one."""
    store, backend, audit = _store()
    _block(store, "older", "gone")
    _block(store, "newer", "here")
    _evidence(backend, "here")
    events = len(audit.events)

    with pytest.raises(ForgottenEvidenceError) as caught:
        store.rollback(_PERSONA, _CHAIN, 1, source=WriteSource.SYSTEM)

    assert len(_chain(backend)) == 2, "a refused rollback must not land"
    assert len(audit.events) == events
    assert "gone" not in str(caught.value) or "1" in str(caught.value)
    assert "older" not in str(caught.value)


def test_a_rollback_to_a_block_whose_evidence_is_all_there_lands() -> None:
    store, backend, _ = _store()
    _block(store, "older", "a")
    _block(store, "newer", "b")
    _evidence(backend, "a", "b")

    store.rollback(_PERSONA, _CHAIN, 1, source=WriteSource.SYSTEM)

    assert store.current(_PERSONA).text == "older"  # type: ignore[union-attr]


def test_the_guard_comes_after_the_chain_error() -> None:
    store, _, _ = _store()
    _block(store, "only", "gone")
    with pytest.raises(Exception, match="does not exist"):
        store.rollback(_PERSONA, _CHAIN, 9, source=WriteSource.SYSTEM)


def test_a_citation_in_another_store_counts_as_evidence() -> None:
    """Older blocks could cite more than episodic rows. A sweep must not take those for gone."""
    store, backend, _ = _store()
    _block(store, "older", "identity-row")
    chunk = PersonaChunk(id="identity-row", text="a line", created_at=_NOW)
    backend.rows["identity-row"] = (_PERSONA, "identity", chunk)

    assert store.missing_sources(_PERSONA, ["identity-row", "nowhere"]) == ["nowhere"]


def test_the_sweep_finds_only_what_cites_nothing_that_exists() -> None:
    store, backend, _ = _store()
    _block(store, "older", "gone-1", "here")
    _block(store, "newer", "here", "gone-2")
    _evidence(backend, "here")

    assert store.orphaned_citations(_PERSONA) == ["gone-1", "gone-2"]


def test_the_sweep_finds_nothing_on_a_clean_chain_and_changes_nothing() -> None:
    store, backend, audit = _store()
    _block(store, "only", "here")
    _evidence(backend, "here")
    events = len(audit.events)

    assert store.orphaned_citations(_PERSONA) == []
    assert backend.excisions == 0
    assert len(audit.events) == events


# --- the round 2 review, finding R2-M1 ------------------------------------------------


@pytest.mark.asyncio
async def test_a_forget_inside_the_write_cannot_resurrect_the_older_block() -> None:
    """(a) The older block cited x, this pass summarises only a, and x is forgotten mid write.

    The write carries the prior head's text back as the superseded linked copy, so checking
    only the new block's own sources let a row citing x survive the forget.
    """
    store, backend, audit = _store()
    _evidence(backend, "x", "a")
    _block(store, "older summary of x", "x")
    sources = [backend.rows["a"][2]]
    backend.before_next_core_write = lambda: _episodic(backend, audit).remove_documents(
        _PERSONA, ["x"]
    )

    block = await _refresh(store, sources)

    assert store.orphaned_citations(_PERSONA) == [], "a row citing forgotten x survived"
    assert block is not None, "the new block cites nothing forgotten, so it is the current one"
    chain = _chain(backend)
    validate_chain(chain)
    assert [c.text for c in chain] == [block.text]
    assert "older summary of x" not in {c.text for c in chain}


def _refresh_commits_between(store: CoreMemoryStore, backend: _Backend) -> None:
    """A refresh commits after the forget read the chain and before it excised."""
    backend.before_next_excise = lambda: _block(store, "a newer block", "kept")


def test_a_forget_planned_on_a_stale_read_re_reads_and_finishes() -> None:
    """(b) The stale plan would make the old head's survivor current beside the new head."""
    store, backend, _ = _store()
    backend.head_index = True
    _evidence(backend, "kept")
    _block(store, "first", "kept")
    _block(store, "second", "gone")
    _refresh_commits_between(store, backend)

    assert store.forget_cited(_PERSONA, ["gone"]) == 1

    chain = _chain(backend)
    validate_chain(chain)
    assert [c.text for c in chain] == ["first", "a newer block"]
    assert store.orphaned_citations(_PERSONA) == []
    assert backend.excisions == 2, "the stale plan, then one re-read"


def test_a_forget_on_a_chain_that_stays_busy_says_so_and_leaves_it_whole() -> None:
    store, backend, _ = _store()
    _block(store, "first", "gone")
    backend.always_conflict = True
    before = _chain(backend)

    with pytest.raises(ChainConflictError):
        store.forget_cited(_PERSONA, ["gone"])

    assert backend.excisions == 3
    assert _chain(backend) == before


def test_a_stale_plan_that_leaves_the_head_alone_replans_too() -> None:
    """L-A on the model: a refresh appends after the read, and the plan dooms a middle version."""
    store, backend, _ = _store()
    _evidence(backend, "kept")
    for cited in ("kept", "gone", "kept", "kept"):
        _block(store, f"cites {cited}", cited)
    _refresh_commits_between(store, backend)

    assert store.forget_cited(_PERSONA, ["gone"]) == 1

    chain = _chain(backend)
    validate_chain(chain)
    assert [c.provenance.version for c in chain if c.provenance] == [1, 2, 3, 4]
    assert chain[-1].text == "a newer block"
    assert backend.excisions == 2


def test_retire_re_reads_when_a_refresh_lands_first_and_takes_the_whole_chain() -> None:
    """A whole-persona delete must not fail because a refresh got in (R9-284)."""
    store, backend, _ = _store()
    backend.head_index = True
    _block(store, "first", "kept")
    _block(store, "second", "kept")
    _refresh_commits_between(store, backend)

    assert store.retire(_PERSONA) == 3

    assert _chain(backend) == []
    assert backend.excisions == 2, "the stale plan, then one re-read"


def test_a_retire_on_a_chain_that_stays_busy_says_so_and_leaves_it_whole() -> None:
    store, backend, _ = _store()
    _block(store, "first", "kept")
    backend.always_conflict = True
    before = _chain(backend)

    with pytest.raises(ChainConflictError):
        store.retire(_PERSONA)

    assert backend.excisions == 3
    assert _chain(backend) == before


def test_a_whole_store_delete_takes_the_evidence_before_what_was_written_from_it() -> None:
    """Raw, then gists, then the block: K8-D-14's order, the one a refresh in flight relies on."""
    store, backend, audit = _three_blocks()
    _evidence(backend, "a", "b", "c")
    order: list[str] = []
    real_delete, real_excise = backend.delete_persona, backend.excise_versions

    def delete_persona(persona_id: str, store_kind: str) -> None:
        order.append(store_kind)
        real_delete(persona_id, store_kind)

    def excise_versions(**kwargs: Any) -> None:  # noqa: ANN401
        order.append(str(kwargs["store_kind"]))
        real_excise(**kwargs)

    backend.delete_persona = delete_persona  # type: ignore[method-assign]
    backend.excise_versions = excise_versions  # type: ignore[method-assign]

    counts = _episodic(backend, audit).delete_by_kind(_PERSONA)

    assert order == ["episodic", "episodic_gist", CORE_MEMORY_KIND]
    assert counts == {"episodic": 3, "episodic_gist": 0, CORE_MEMORY_KIND: 3}

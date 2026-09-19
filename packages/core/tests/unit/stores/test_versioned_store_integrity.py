"""The versioned stores keep every version, and recall still finds the current one.

Two defects in the same contract, found by the phase-1 sweep and reproduced against main.

**An update destroyed the version it superseded.** ``TypedStore.write`` kept whatever ``id`` the
caller handed it, and the backends upsert by chunk id (``INSERT ... ON CONFLICT (id) DO UPDATE``),
so an update whose chunk reused the first write's id overwrote version 1 in place. One physical
row survived, carrying ``version=2``, and ``history()`` then raised ``BrokenVersionChainError``
(``got=2 expected=1``) on the chain it was asked to read. "Versioned, append-only" is the
headline promise of self_facts, worldview and episodic, and ``history()`` and ``rollback()`` are
built on it.

**Recall dropped current chunks on the floor.** ``query()`` asked the backend for ``top_k``
rows and filtered superseded ones out of the ANSWER, so every superseded version inside the
window silently cost a result. A logical chain edited more times than ``top_k`` could fill the
whole window with its own dead versions and return nothing current. This is not hypothetical on
self_facts: ``persona.autonomy`` appends a versioned chain under ``logical_id="autonomy"`` there,
so the dead versions accumulate for every persona that learns.

The two mask each other, which is why they are fixed together: while an update overwrote its
predecessor, chains never accumulated superseded rows, so the recall loss stayed rare.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from persona.audit import MemoryAuditLogger
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource, make_chunk_id
from persona.stores.self_facts import SelfFactsStore

_NOW = datetime(2026, 9, 19, 9, 0, tzinfo=UTC)
_PERSONA = "p1"
_KIND = "self_facts"


class _IdKeyedBackend:
    """In-memory backend keyed by chunk id, like the production upsert.

    Postgres (``postgres.py``) and Chroma both write ``ON CONFLICT (id) DO UPDATE``, so keying a
    dict by ``chunk.id`` is the faithful shape: two chunks sharing an id are ONE row.
    """

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, PersonaChunk]] = {}

    def _bucket(self, persona_id: str, store_kind: str) -> dict[str, PersonaChunk]:
        return self.rows.setdefault((persona_id, store_kind), {})

    def upsert(self, *, persona_id: str, store_kind: str, chunks: list[PersonaChunk]) -> None:
        bucket = self._bucket(persona_id, store_kind)
        for chunk in chunks:
            bucket[chunk.id] = chunk

    def get_all(self, *, persona_id: str, store_kind: str) -> list[PersonaChunk]:
        return list(self._bucket(persona_id, store_kind).values())

    def get_by_logical_ids(
        self, *, persona_id: str, store_kind: str, logical_ids: list[str]
    ) -> list[PersonaChunk]:
        wanted = set(logical_ids)
        return [
            c
            for c in self.get_all(persona_id=persona_id, store_kind=store_kind)
            if (c.provenance.logical_id if c.provenance is not None else c.id) in wanted
        ]

    def query(
        self,
        *,
        persona_id: str,
        store_kind: str,
        text: str,  # noqa: ARG002
        top_k: int,
        where: dict[str, Any] | None = None,  # noqa: ARG002, ANN401
    ) -> list[PersonaChunk]:
        # Insertion order stands in for "nearest neighbours": every version of one fact has
        # near-identical text, so a real ANN returns them together and the oldest are as close
        # as the newest. This is the worst case the fix has to survive, and the realistic one.
        rows = self.get_all(persona_id=persona_id, store_kind=store_kind)
        return rows[:top_k]

    def count(self, *, persona_id: str, store_kind: str, include_superseded: bool = False) -> int:  # noqa: ARG002
        return len(self.get_all(persona_id=persona_id, store_kind=store_kind))

    def recent(self, *, persona_id: str, store_kind: str, limit: int) -> list[PersonaChunk]:
        return self.get_all(persona_id=persona_id, store_kind=store_kind)[:limit]

    def delete(self, *, persona_id: str, store_kind: str | None = None) -> None: ...

    def remove_documents(self, *, persona_id: str, store_kind: str, doc_ids: list[str]) -> None: ...


def _store(backend: _IdKeyedBackend) -> SelfFactsStore:
    return SelfFactsStore(backend=backend, audit_logger=MemoryAuditLogger())


def _chunk(text: str, *, chunk_id: str, logical_id: str | None = None) -> PersonaChunk:
    """A write. ``logical_id`` set means "this is an update to that chain"."""
    provenance = None
    if logical_id is not None:
        provenance = ChunkProvenance(
            source=WriteSource.USER, logical_id=logical_id, version=1, written_at=_NOW
        )
    return PersonaChunk(id=chunk_id, text=text, created_at=_NOW, provenance=provenance)


def _edit(store: SelfFactsStore, chunk_id: str, text: str) -> None:
    """The update a caller makes when it reuses the id it first wrote under."""
    store.write(
        _PERSONA,
        [_chunk(text, chunk_id=chunk_id, logical_id=chunk_id)],
        source=WriteSource.USER,
        reason="the user corrected it",
    )


# --- defect 1: an update must not destroy the version it supersedes ----------------------


def test_an_update_keeps_the_version_it_supersedes() -> None:
    backend = _IdKeyedBackend()
    store = _store(backend)
    first = make_chunk_id(_PERSONA, _KIND, 0)

    store.write(_PERSONA, [_chunk("I like tea", chunk_id=first)], source=WriteSource.USER)
    _edit(store, first, "I like coffee")

    rows = backend.get_all(persona_id=_PERSONA, store_kind=_KIND)
    assert len(rows) == 2, f"the update overwrote its predecessor; rows={[r.id for r in rows]}"


def test_history_reads_the_whole_chain_after_an_update() -> None:
    """``history()`` raised BrokenVersionChainError because version 1 was gone."""
    backend = _IdKeyedBackend()
    store = _store(backend)
    first = make_chunk_id(_PERSONA, _KIND, 0)

    store.write(_PERSONA, [_chunk("I like tea", chunk_id=first)], source=WriteSource.USER)
    _edit(store, first, "I like coffee")

    chain = store.history(_PERSONA, first)
    assert [c.provenance.version for c in chain if c.provenance] == [1, 2]
    assert chain[0].text == "I like tea"
    assert chain[1].text == "I like coffee"


def test_the_superseded_version_points_at_its_successor() -> None:
    backend = _IdKeyedBackend()
    store = _store(backend)
    first = make_chunk_id(_PERSONA, _KIND, 0)

    store.write(_PERSONA, [_chunk("I like tea", chunk_id=first)], source=WriteSource.USER)
    _edit(store, first, "I like coffee")

    chain = store.history(_PERSONA, first)
    head = chain[-1]
    assert chain[0].provenance is not None
    assert chain[0].provenance.superseded_by == head.id
    assert head.provenance is not None
    assert head.provenance.superseded_by is None


def test_a_caller_that_already_mints_distinct_ids_keeps_them() -> None:
    """``persona.autonomy`` puts the version in its own id; do not rewrite what is already safe."""
    backend = _IdKeyedBackend()
    store = _store(backend)
    logical = f"{_PERSONA}::{_KIND}::autonomy"

    store.write(_PERSONA, [_chunk("v1", chunk_id=f"{logical}::0001")], source=WriteSource.USER)
    store.write(
        _PERSONA,
        [_chunk("v2", chunk_id=f"{logical}::0002", logical_id=f"{logical}::0001")],
        source=WriteSource.USER,
    )

    ids = {c.id for c in backend.get_all(persona_id=_PERSONA, store_kind=_KIND)}
    assert ids == {f"{logical}::0001", f"{logical}::0002"}


# --- defect 2: recall must not lose current chunks to superseded ones ---------------------


def test_a_fact_edited_more_times_than_top_k_is_still_recalled() -> None:
    """Five versions, a window of three: every candidate in the window was dead."""
    backend = _IdKeyedBackend()
    store = _store(backend)
    first = make_chunk_id(_PERSONA, _KIND, 0)

    store.write(_PERSONA, [_chunk("v1", chunk_id=first)], source=WriteSource.USER)
    for n in range(2, 6):
        _edit(store, first, f"v{n}")

    got = store.query(_PERSONA, "what do I like", top_k=3)
    assert [c.text for c in got] == ["v5"], (
        f"the current version was lost; got={[c.text for c in got]}"
    )


def test_recall_still_honours_top_k() -> None:
    """Over-fetching to survive superseded rows must not widen what the caller asked for."""
    backend = _IdKeyedBackend()
    store = _store(backend)
    for i in range(10):
        store.write(
            _PERSONA,
            [_chunk(f"fact {i}", chunk_id=make_chunk_id(_PERSONA, _KIND, i))],
            source=WriteSource.USER,
        )

    assert len(store.query(_PERSONA, "anything", top_k=3)) == 3


def test_a_chain_does_not_crowd_out_other_facts() -> None:
    """The live shape: one much-edited chain (autonomy) beside ordinary facts."""
    backend = _IdKeyedBackend()
    store = _store(backend)
    edited = make_chunk_id(_PERSONA, _KIND, 0)

    store.write(_PERSONA, [_chunk("v1", chunk_id=edited)], source=WriteSource.USER)
    for n in range(2, 8):
        _edit(store, edited, f"v{n}")
    for i in range(1, 4):
        store.write(
            _PERSONA,
            [_chunk(f"fact {i}", chunk_id=make_chunk_id(_PERSONA, _KIND, i))],
            source=WriteSource.USER,
        )

    texts = [c.text for c in store.query(_PERSONA, "anything", top_k=3)]
    assert "fact 1" in texts, f"ordinary facts were crowded out by dead versions; got={texts}"

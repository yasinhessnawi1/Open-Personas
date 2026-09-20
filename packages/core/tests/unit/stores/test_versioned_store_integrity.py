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
        #: Every upsert call, in order, as the chunks it was handed (Spec K13, T2). The
        #: durability fix is about HOW MANY calls a versioned write makes and what is in
        #: them, so the fake records the calls rather than only their result.
        self.upsert_batches: list[list[PersonaChunk]] = []

    def _bucket(self, persona_id: str, store_kind: str) -> dict[str, PersonaChunk]:
        return self.rows.setdefault((persona_id, store_kind), {})

    def upsert(self, *, persona_id: str, store_kind: str, chunks: list[PersonaChunk]) -> None:
        self.upsert_batches.append(list(chunks))
        bucket = self._bucket(persona_id, store_kind)
        for chunk in chunks:
            bucket[chunk.id] = chunk

    def get_by_ids(self, *, persona_id: str, store_kind: str, ids: list[str]) -> list[PersonaChunk]:
        bucket = self._bucket(persona_id, store_kind)
        return [bucket[i] for i in ids if i in bucket]

    def relink(self, *, persona_id: str, store_kind: str, links: dict[str, str | None]) -> None:
        """Spec K13's repair primitive: move version pointers, touch nothing else.

        Faithful to both transports in the way that matters here: it rewrites
        ``superseded_by`` in place without re-reading text or re-embedding, so a test can
        tell a repair apart from a write.
        """
        bucket = self._bucket(persona_id, store_kind)
        for chunk_id, target in links.items():
            chunk = bucket.get(chunk_id)
            if chunk is None or chunk.provenance is None:
                continue  # unknown ids are skipped, like the real transports
            bucket[chunk_id] = chunk.model_copy(
                update={"provenance": chunk.provenance.model_copy(update={"superseded_by": target})}
            )

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

    def delete_persona(self, persona_id: str, store_kind: str) -> None:
        self.rows.pop((persona_id, store_kind), None)

    def delete_documents(self, *, persona_id: str, store_kind: str, ids: list[str]) -> None:
        """The real transports' delete: by PHYSICAL id, and unknown ids are a no-op.

        This fake used to carry a ``remove_documents(doc_ids=...)`` stub that did nothing and
        matched no method on the ``Backend`` protocol, which was harmless only for as long as
        nothing here deleted anything (Spec K13, T1 needed it).
        """
        bucket = self._bucket(persona_id, store_kind)
        for chunk_id in ids:
            bucket.pop(chunk_id, None)


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


# --- defect 3: the versioned write is ONE call, and one batch never carries an id twice ----
#
# Spec K13, T2. Two separate defects in the same four lines, found by the K13 phase-1 read and
# reproduced before either was fixed.
#
# **A versioned write was two unsynchronised upserts.** The supersede link went first, the new
# head second, with nothing around them. A process that died between them left the link
# pointing at a chunk that was never written: zero current heads, so an ordinary read could not
# find the node, and a chain the validator refuses, so history() and rollback() raised on that
# logical id forever. The crash itself is proven where it has to be, against the real
# transports with a real SIGKILL (``tests/integration/test_stores_crash_durability.py`` and its
# Postgres sibling in the api package). What is proven HERE is the shape that makes the crash
# impossible: one call.
#
# **One batch could carry the same id twice, and the unlinked copy won (R9-206).** When a
# single write() carries two updates to the same logical chain, the chunk that is version N+1
# for the first update is the prior head of the second, so it appears unlinked in ``prepared``
# and linked in ``supersede_updates``. Writing the links and then the heads overwrote the link
# with the unlinked copy: two current heads and a chain that raises on read, with no crash
# involved at all. The transports disagree about duplicates (Postgres silently keeps the last
# row of an executemany, Chroma raises DuplicateIDError and refuses the whole call), so the
# fix is to fold the duplicate away before either of them sees it, not to reorder the batch.


def _edit_batch(store: SelfFactsStore, chunk_id: str, texts: list[str]) -> None:
    """Several updates to ONE logical chain inside ONE write() call."""
    store.write(
        _PERSONA,
        [_chunk(text, chunk_id=chunk_id, logical_id=chunk_id) for text in texts],
        source=WriteSource.USER,
        reason="two corrections at once",
    )


def test_a_versioned_update_is_one_backend_call() -> None:
    """The kill window is closed by there being no instant between two writes to land on."""
    backend = _IdKeyedBackend()
    store = _store(backend)
    first = make_chunk_id(_PERSONA, _KIND, 0)

    store.write(_PERSONA, [_chunk("I like tea", chunk_id=first)], source=WriteSource.USER)
    backend.upsert_batches.clear()
    _edit(store, first, "I like coffee")

    assert len(backend.upsert_batches) == 1, (
        f"a versioned update made {len(backend.upsert_batches)} backend calls; a crash between "
        "them leaves the prior head pointing at a chunk that does not exist"
    )


def test_the_supersede_link_is_written_before_the_new_head() -> None:
    """Order inside the batch, kept for the partial unique index on heads (R9-205).

    With the index in place a batch that wrote the new head before the old one lost its NULL
    would trip it, one statement at a time. Nothing enforces that today, which is exactly why
    it needs a test now rather than a surprise later.
    """
    backend = _IdKeyedBackend()
    store = _store(backend)
    first = make_chunk_id(_PERSONA, _KIND, 0)

    store.write(_PERSONA, [_chunk("I like tea", chunk_id=first)], source=WriteSource.USER)
    backend.upsert_batches.clear()
    _edit(store, first, "I like coffee")

    batch = backend.upsert_batches[0]
    superseded = [i for i, c in enumerate(batch) if c.provenance and c.provenance.superseded_by]
    heads = [i for i, c in enumerate(batch) if c.provenance and not c.provenance.superseded_by]
    assert superseded, f"expected a supersede link in the batch, got {batch}"
    assert heads, f"expected a new head in the batch, got {batch}"
    assert max(superseded) < min(heads), (
        "the new head is written before the link that supersedes its predecessor; "
        "with R9-205's partial unique index in place that batch would be rejected"
    )


def test_two_updates_to_one_chain_in_one_batch_keep_the_chain_readable() -> None:
    """R9-206: the naive merge left two heads and a chain that raises, with no crash."""
    backend = _IdKeyedBackend()
    store = _store(backend)
    first = make_chunk_id(_PERSONA, _KIND, 0)

    store.write(_PERSONA, [_chunk("v1", chunk_id=first)], source=WriteSource.USER)
    _edit_batch(store, first, ["v2", "v3"])

    rows = backend.get_all(persona_id=_PERSONA, store_kind=_KIND)
    heads = [c for c in rows if c.provenance is None or c.provenance.superseded_by is None]
    assert len(heads) == 1, (
        f"expected one head after two updates in one batch, got {len(heads)}: "
        f"{[(c.id, c.provenance.superseded_by if c.provenance else None) for c in rows]}"
    )

    chain = store.history(_PERSONA, first)
    assert [c.provenance.version for c in chain if c.provenance] == [1, 2, 3]
    assert [c.text for c in chain] == ["v1", "v2", "v3"]


def test_the_transport_never_sees_the_same_id_twice_in_one_batch() -> None:
    """Why the fix is a dedupe and not a reordering.

    Chroma raises ``DuplicateIDError`` and refuses the whole call when one upsert carries an id
    twice; Postgres accepts it and silently keeps whichever row came last. So a batch that is
    merely ordered correctly is a write that works on one transport and throws on the other.
    """
    backend = _IdKeyedBackend()
    store = _store(backend)
    first = make_chunk_id(_PERSONA, _KIND, 0)

    store.write(_PERSONA, [_chunk("v1", chunk_id=first)], source=WriteSource.USER)
    _edit_batch(store, first, ["v2", "v3", "v4"])

    for batch in backend.upsert_batches:
        ids = [c.id for c in batch]
        assert len(ids) == len(set(ids)), (
            f"one upsert carried the same id twice: {ids}. Chroma refuses that call outright."
        )

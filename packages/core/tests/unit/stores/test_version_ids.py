"""A version's physical id is unique everywhere, not only inside its own chain.

Two defects with one cause: ids that were computed instead of minted.

**The store's own ids were not scoped to a persona.** ``memory_chunks.id`` is the primary key of
the whole table, and the store minted ``{logical_id}::vNNNN``. A logical id is only unique inside
one persona: ``autonomy`` and ``tool_consent::<tool>`` are the same string for everyone. So two
personas produced the same physical id, ``ON CONFLICT (id) DO UPDATE`` moved the first persona's
row to the second (``persona_id`` is in the update list), and the first persona's chain was left
pointing at a row it no longer owned. ``rollback`` did this every time; ``write`` did it when an
update reused an id.

**Two racing writers computed the same id.** Autonomy put ``len(chain) + 1`` in its chunk id, so
writers that read the same chain built the same id, and the second overwrote the first in place.
Nothing raised and the chain read as healthy, so the partial unique index on chain heads (R9-205)
could never have seen it: one row is one head. One update simply vanished, with an audit event
claiming it landed.

The fake here is keyed the way the table is, by ``id`` alone. The usual test fakes key by
``(persona_id, kind)``, which is exactly what hides the first defect.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from persona.audit import MemoryAuditLogger
from persona.autonomy import AUTONOMY_LOGICAL_ID, record_autonomy_update
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource
from persona.stores.core_memory import CoreMemoryStore
from persona.stores.self_facts import SelfFactsStore

_NOW = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)


class _TableKeyedBackend:
    """In-memory ``memory_chunks``: one row per id, across every persona and kind.

    ``upsert`` overwrites by id alone and takes the writer's ``persona_id`` with it, because
    ``PostgresBackend.upsert`` updates every column except ``id`` on conflict.

    ``before_next_upsert`` runs once, inside the next upsert, after that writer has already read
    its chain. That is the race, made deterministic: writer A has read, writer B runs to the
    end, then A lands on what it read.
    """

    def __init__(self) -> None:
        self.rows: dict[str, tuple[str, str, PersonaChunk]] = {}
        self.before_next_upsert: object = None

    def upsert(self, *, persona_id: str, store_kind: str, chunks: list[PersonaChunk]) -> None:
        hook, self.before_next_upsert = self.before_next_upsert, None
        if callable(hook):
            hook()
        for chunk in chunks:
            self.rows[chunk.id] = (persona_id, store_kind, chunk)

    def get_by_logical_ids(
        self, *, persona_id: str, store_kind: str, logical_ids: list[str]
    ) -> list[PersonaChunk]:
        return [
            chunk
            for owner, kind, chunk in self.rows.values()
            if owner == persona_id
            and kind == store_kind
            and chunk.provenance is not None
            and chunk.provenance.logical_id in logical_ids
        ]

    def texts_of(self, persona_id: str) -> set[str]:
        return {chunk.text for owner, _, chunk in self.rows.values() if owner == persona_id}


def _fact(chunk_id: str, text: str, logical_id: str) -> PersonaChunk:
    return PersonaChunk(
        id=chunk_id,
        text=text,
        metadata={"confidence": "0.9"},
        created_at=_NOW,
        provenance=ChunkProvenance(source=WriteSource.USER, logical_id=logical_id, written_at=_NOW),
    )


def _heads(backend: _TableKeyedBackend) -> list[PersonaChunk]:
    """Every row that claims to be current, oldest version first."""
    return sorted(
        (
            chunk
            for _, _, chunk in backend.rows.values()
            if chunk.provenance is not None and chunk.provenance.superseded_by is None
        ),
        key=lambda c: c.provenance.version if c.provenance else 0,
    )


def _store(backend: _TableKeyedBackend) -> SelfFactsStore:
    return SelfFactsStore(backend=backend, audit_logger=MemoryAuditLogger())  # type: ignore[arg-type]


def _two_versions(store: SelfFactsStore, persona_id: str, logical_id: str) -> None:
    store.write(
        persona_id,
        [_fact(f"{persona_id}::one", f"{persona_id} v1", logical_id)],
        source=WriteSource.USER,
    )
    store.write(
        persona_id,
        [_fact(f"{persona_id}::two", f"{persona_id} v2", logical_id)],
        source=WriteSource.USER,
    )


def test_rollback_on_a_shared_logical_id_stays_inside_its_own_persona() -> None:
    backend = _TableKeyedBackend()
    store = _store(backend)
    for persona_id in ("p1", "p2"):
        _two_versions(store, persona_id, "autonomy")
        store.rollback(persona_id, "autonomy", 1, source=WriteSource.USER)

    for persona_id in ("p1", "p2"):
        chain = store.history(persona_id, "autonomy")  # raised on p1 before the fix
        assert [c.text for c in chain] == [
            f"{persona_id} v1",
            f"{persona_id} v2",
            f"{persona_id} v1",
        ]
    assert len(backend.rows) == 6, "a persona's row was overwritten by another persona's rollback"


def test_a_write_that_reuses_an_id_stays_inside_its_own_persona() -> None:
    """The ``write`` half: the re-mint at the collision used the same unscoped shape."""
    backend = _TableKeyedBackend()
    store = _store(backend)
    for persona_id in ("p1", "p2"):
        reused = f"{persona_id}::fact"
        store.write(
            persona_id, [_fact(reused, f"{persona_id} v1", "shared")], source=WriteSource.USER
        )
        store.write(
            persona_id, [_fact(reused, f"{persona_id} v2", "shared")], source=WriteSource.USER
        )

    for persona_id in ("p1", "p2"):
        assert [c.text for c in store.history(persona_id, "shared")] == [
            f"{persona_id} v1",
            f"{persona_id} v2",
        ]


def test_minted_ids_name_their_persona_and_kind() -> None:
    """The same shape every other minted id has, so a row can be traced from its id."""
    backend = _TableKeyedBackend()
    store = _store(backend)
    _two_versions(store, "p1", "autonomy")
    store.rollback("p1", "autonomy", 1, source=WriteSource.USER)
    assert store.history("p1", "autonomy")[-1].id.startswith("p1::self_facts::")


def test_the_id_a_core_block_rollback_mints_names_the_core_kind() -> None:
    backend = _TableKeyedBackend()
    store = CoreMemoryStore(backend=backend, audit_logger=MemoryAuditLogger())  # type: ignore[arg-type]
    for chunk_id, text in (("c1", "block one"), ("c2", "block two")):
        store.write("p1", [_fact(chunk_id, text, "p1::core")], source=WriteSource.SYSTEM)
    store.rollback("p1", "p1::core", 1, source=WriteSource.SYSTEM)
    assert store.history("p1", "p1::core")[-1].id.startswith("p1::core_memory::")


def _autonomy(store: SelfFactsStore, level: str, *, now: datetime, session: str) -> None:
    record_autonomy_update(
        store,
        "p1",
        level,  # type: ignore[arg-type]
        now=now,
        written_by="frontier:test",
        reason=f"moved to {level}",
        session_id=session,
    )


def test_a_racing_autonomy_update_is_not_overwritten_in_place() -> None:
    """Both updates must survive as rows. Which one wins is the index's job (R9-205)."""
    backend = _TableKeyedBackend()
    store = _store(backend)
    # Seeded rather than written: the store stamps ``written_at`` from the wall clock, so a real
    # first write would put both racers inside the cooldown.
    earlier = _NOW - timedelta(days=2)
    seeded = PersonaChunk(
        id="p1::self_facts::seeded",
        text="Autonomy preference set to cautious.",
        metadata={"autonomy_level": "cautious", "confidence": "1.0"},
        created_at=earlier,
        provenance=ChunkProvenance(
            source=WriteSource.PERSONA_SELF, logical_id=AUTONOMY_LOGICAL_ID, written_at=earlier
        ),
    )
    backend.rows[seeded.id] = ("p1", "self_facts", seeded)

    backend.before_next_upsert = lambda: _autonomy(store, "decisive", now=_NOW, session="racer")
    _autonomy(store, "balanced", now=_NOW, session="s1")

    assert backend.texts_of("p1") >= {
        "Autonomy preference set to decisive.",
        "Autonomy preference set to balanced.",
    }, "the second writer overwrote the first in place, and nothing would ever have said so"
    heads = _heads(backend)
    assert [c.provenance.version for c in heads if c.provenance] == [2, 2]
    assert backend.rows[seeded.id][2].provenance.superseded_by in {c.id for c in heads}  # type: ignore[union-attr]


def test_two_racing_first_autonomy_updates_are_both_kept() -> None:
    """With no chain yet both writers computed ``::0001``, so the first write raced the same."""
    backend = _TableKeyedBackend()
    store = _store(backend)
    backend.before_next_upsert = lambda: _autonomy(store, "decisive", now=_NOW, session="racer")
    _autonomy(store, "balanced", now=_NOW, session="s1")

    assert len(backend.rows) == 2
    heads = _heads(backend)
    assert len(heads) == 2, "both are current, because neither writer saw the other"
    assert [c.provenance.version for c in heads if c.provenance] == [1, 1]
    assert {c.provenance.logical_id for c in heads if c.provenance} == {AUTONOMY_LOGICAL_ID}
    assert {c.text for c in heads} == {
        "Autonomy preference set to decisive.",
        "Autonomy preference set to balanced.",
    }


def test_two_racing_rollbacks_show_as_two_heads_on_a_transport_with_no_index() -> None:
    """The ids rollback mints used to collapse this silently, so one rollback just vanished.

    Pins the state this leaves where nothing enforces one head. It is visible now, not resolved:
    resolving it is the head index on Postgres, and on Chroma it stays a documented difference.
    """
    backend = _TableKeyedBackend()
    store = _store(backend)
    _two_versions(store, "p1", "L")

    backend.before_next_upsert = lambda: store.rollback(
        "p1", "L", to_version=2, source=WriteSource.USER
    )
    store.rollback("p1", "L", to_version=1, source=WriteSource.USER)

    heads = _heads(backend)
    assert [c.provenance.version for c in heads if c.provenance] == [3, 3]
    assert {c.text for c in heads} == {"p1 v1", "p1 v2"}

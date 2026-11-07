"""Two racing saves of one persona (the round 1 review of R9-205, finding M1).

``update_persona`` commits the YAML and then calls ``PersonaRegistry.load_persona``, which skips a
chunk whose head already matches and writes the rest under the chunk's stable id. That id is
already in the chain, so the store re-mints it. When the re-mint produced the same id for the same
save, two racing identical saves collapsed through ``ON CONFLICT (id)`` and nothing happened. When
it became a random uuid they turned into two rows at N+1, with no database constraint involved:
the chain then raised on every later read of it, so every PATCH of that persona returned 500
after its YAML had already committed, and ``persona repair`` refused it.

So the re-mint is deterministic on what is being saved. The same save makes the same id and
collapses. A different save makes a different id, which stays visible and reaches the head index.
The version is part of the id because content repeats: a fact edited from A to B and back to A
must not land on the row version 1 of A already holds.
"""

from __future__ import annotations

from pathlib import Path

from persona.audit import MemoryAuditLogger
from persona.registry import PersonaRegistry
from persona.schema.chunks import PersonaChunk, make_chunk_id, version_chunk_id
from persona.schema.persona import Persona
from persona.stores.episodic import EpisodicStore
from persona.stores.identity import IdentityStore
from persona.stores.self_facts import SelfFactsStore
from persona.stores.versioning import ChainDefect, diagnose_chain
from persona.stores.worldview import WorldviewStore

_PERSONA = "persona-under-test"
_YAML = (
    Path(__file__).parents[2] / "fixtures" / "personas" / "valid" / "03_legal_assistant_full.yaml"
)


class _TableKeyedBackend:
    """In-memory ``memory_chunks``: one row per id, last writer wins, like ``ON CONFLICT (id)``.

    ``before_next_upsert`` runs once, inside the next upsert of the given kind, after that writer
    has read its chain: writer A has read, writer B runs to the end, then A lands.
    """

    def __init__(self) -> None:
        self.rows: dict[str, tuple[str, str, PersonaChunk]] = {}
        self.before_next_upsert: object = None
        self.hook_kind = "self_facts"

    def _of(self, persona_id: str, store_kind: str) -> list[PersonaChunk]:
        return [
            chunk
            for owner, kind, chunk in self.rows.values()
            if owner == persona_id and kind == store_kind
        ]

    def upsert(self, *, persona_id: str, store_kind: str, chunks: list[PersonaChunk]) -> None:
        if store_kind == self.hook_kind:
            hook, self.before_next_upsert = self.before_next_upsert, None
            if callable(hook):
                hook()
        for chunk in chunks:
            self.rows[chunk.id] = (persona_id, store_kind, chunk)

    def get_all(self, *, persona_id: str, store_kind: str) -> list[PersonaChunk]:
        return self._of(persona_id, store_kind)

    def get_by_logical_ids(
        self, *, persona_id: str, store_kind: str, logical_ids: list[str]
    ) -> list[PersonaChunk]:
        return [
            chunk
            for chunk in self._of(persona_id, store_kind)
            if chunk.provenance is not None and chunk.provenance.logical_id in logical_ids
        ]

    def count(self, *, persona_id: str, store_kind: str, include_superseded: bool = False) -> int:
        _ = include_superseded
        return len(self._of(persona_id, store_kind))


def _registry(backend: _TableKeyedBackend) -> tuple[PersonaRegistry, SelfFactsStore]:
    audit = MemoryAuditLogger()
    stores = {
        "identity": IdentityStore(backend=backend, audit_logger=audit),  # type: ignore[arg-type]
        "self_facts": SelfFactsStore(backend=backend, audit_logger=audit),  # type: ignore[arg-type]
        "worldview": WorldviewStore(backend=backend, audit_logger=audit),  # type: ignore[arg-type]
        "episodic": EpisodicStore(backend=backend, audit_logger=audit),  # type: ignore[arg-type]
    }
    registry = PersonaRegistry(stores=stores, audit_logger=audit)  # type: ignore[arg-type]
    return registry, stores["self_facts"]  # type: ignore[return-value]


def _persona() -> Persona:
    return Persona.from_yaml(_YAML).model_copy(update={"persona_id": _PERSONA})


def _with_first_fact(persona: Persona, fact: str) -> Persona:
    edited = persona.self_facts[0].model_copy(update={"fact": fact})
    return persona.model_copy(update={"self_facts": [edited, *persona.self_facts[1:]]})


#: The first self fact's chain. The registry's chunk ids are stable per position.
_FIRST_FACT = make_chunk_id(_PERSONA, "self_facts", 0)


def _first_chain(store: SelfFactsStore) -> list[PersonaChunk]:
    return [
        c
        for c in store.get_all(_PERSONA, include_superseded=True)
        if c.provenance is not None and c.provenance.logical_id == _FIRST_FACT
    ]


def test_two_racing_identical_saves_collapse_into_one_version() -> None:
    backend = _TableKeyedBackend()
    registry, store = _registry(backend)
    base = _persona()
    registry.load_persona(base)

    edited = _with_first_fact(base, "An edited fact")
    backend.before_next_upsert = lambda: registry.load_persona(edited)
    registry.load_persona(edited)

    chain = _first_chain(store)
    assert diagnose_chain(chain).defect is ChainDefect.NONE, "the race left a broken chain"
    assert len(chain) == 2, "one save happened, however many callers asked for it"
    assert sum("An edited fact" in c.text for c in store.get_all(_PERSONA)) == 1


def test_the_persona_can_still_be_saved_after_the_race() -> None:
    """The failure the owner would see: every later PATCH of this persona returned 500."""
    backend = _TableKeyedBackend()
    registry, store = _registry(backend)
    base = _persona()
    registry.load_persona(base)
    edited = _with_first_fact(base, "An edited fact")
    backend.before_next_upsert = lambda: registry.load_persona(edited)
    registry.load_persona(edited)

    registry.load_persona(_with_first_fact(base, "A later edit"))  # raised before the fix

    assert any("A later edit" in c.text for c in store.get_all(_PERSONA))
    assert len(store.history(_PERSONA, _FIRST_FACT)) == 3


def test_two_racing_different_saves_stay_visible_as_two_rows() -> None:
    """Different content must never collapse: that would be a lost update nobody could see.

    On Postgres the second one hits the head index and retries. This fake has no index, which is
    the Chroma shape, so both rows are here and the chain says what happened.
    """
    backend = _TableKeyedBackend()
    registry, store = _registry(backend)
    base = _persona()
    registry.load_persona(base)

    first = _with_first_fact(base, "One save")
    second = _with_first_fact(base, "Another save")
    backend.before_next_upsert = lambda: registry.load_persona(second)
    registry.load_persona(first)

    assert {c.text for c in store.get_all(_PERSONA) if "save" in c.text.lower()} == {
        "self_fact: One save",
        "self_fact: Another save",
    }


def test_a_fact_edited_back_to_an_older_value_does_not_land_on_the_older_row() -> None:
    """A to B to A to B: the content repeats, so the version has to be part of the id."""
    backend = _TableKeyedBackend()
    registry, store = _registry(backend)
    base = _persona()
    registry.load_persona(base)
    for fact in ("Value A", "Value B", "Value A", "Value B"):
        registry.load_persona(_with_first_fact(base, fact))

    chain = sorted(_first_chain(store), key=lambda c: c.provenance.version if c.provenance else 0)
    assert diagnose_chain(chain).defect is ChainDefect.NONE
    assert len(chain) == 5, "an old version was overwritten in place"
    assert len({c.id for c in chain}) == 5


def test_the_id_is_scoped_to_persona_kind_chain_version_and_content() -> None:
    hash_a, hash_b = "a" * 64, "b" * 64
    base = version_chunk_id("p1", "self_facts", "L", 2, hash_a)
    assert base == version_chunk_id("p1", "self_facts", "L", 2, hash_a), "must be deterministic"
    assert base.startswith("p1::self_facts::")
    for other in (
        version_chunk_id("p2", "self_facts", "L", 2, hash_a),
        version_chunk_id("p1", "worldview", "L", 2, hash_a),
        version_chunk_id("p1", "self_facts", "M", 2, hash_a),
        version_chunk_id("p1", "self_facts", "L", 3, hash_a),
        version_chunk_id("p1", "self_facts", "L", 2, hash_b),
    ):
        assert other != base

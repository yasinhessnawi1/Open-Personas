"""Two writers on one chain, on real threads, against the real head index (R9-205).

The unit tests drive a deterministic interleaving over a model of the index. These prove the model
is honest. A barrier inside the embedder holds both writers at the exact point between reading the
chain and writing it, which is the window the race lives in, so both really do compute version
N+1 and both really do reach the database.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from persona.audit import MemoryAuditLogger
from persona.errors import ChainConflictError
from persona.registry import PersonaRegistry
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource, make_chunk_id
from persona.schema.persona import Persona
from persona.stores.core_memory import CoreMemoryStore
from persona.stores.episodic import EpisodicStore
from persona.stores.identity import IdentityStore
from persona.stores.postgres import CHAIN_HEAD_INDEX, PostgresBackend
from persona.stores.self_facts import SelfFactsStore
from persona.stores.versioning import ChainDefect, diagnose_chain
from persona.stores.worldview import WorldviewStore
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from persona.stores.base import TypedStore
    from sqlalchemy.engine import Engine
    from tests.conftest import HashEmbedder384

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
_PERSONA = "p1"
_LOGICAL = "chain"
_TIMEOUT_S = 30
_PERSONA_YAML = (
    Path(__file__).resolve().parents[3]
    / "core/tests/fixtures/personas/valid/03_legal_assistant_full.yaml"
)
_FIRST_FACT = make_chunk_id(_PERSONA, "self_facts", 0)


@pytest.fixture
def seeded(pg_engine: Engine) -> Engine:
    with pg_engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES ('u1', 'u1@example.com')"))
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES ('p1', 'u1', 'name: p1')")
        )
    return pg_engine


class _Gate:
    """Holds the first ``parties`` embedding calls at a barrier, then lets everything through."""

    def __init__(self, inner: HashEmbedder384, parties: int = 2) -> None:
        self.model_name = inner.model_name
        self.dimension = inner.dimension
        self._inner = inner
        self._barrier = threading.Barrier(parties, timeout=_TIMEOUT_S)
        self._armed = threading.Semaphore(parties)
        self.enabled = False

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        if self.enabled and self._armed.acquire(blocking=False):
            self._barrier.wait()
        return self._inner.encode(texts)


def _chunk(chunk_id: str, body: str) -> PersonaChunk:
    return PersonaChunk(
        id=chunk_id,
        text=body,
        created_at=_NOW,
        provenance=ChunkProvenance(source=WriteSource.USER, logical_id=_LOGICAL, written_at=_NOW),
    )


def _race(*writers: Callable[[], None]) -> list[BaseException]:
    errors: list[BaseException] = []

    def run(writer: Callable[[], None]) -> None:
        try:
            writer()
        except BaseException as exc:  # noqa: BLE001 - reported to the main thread
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(w,)) for w in writers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(_TIMEOUT_S)
    assert not any(t.is_alive() for t in threads), "a writer never finished"
    return errors


def _store(
    engine: Engine,
    gate: _Gate,
    store_class: type[TypedStore] = SelfFactsStore,
) -> tuple[TypedStore, MemoryAuditLogger]:
    audit = MemoryAuditLogger()
    backend = PostgresBackend(engine=engine, embedder=gate)  # type: ignore[arg-type]
    return store_class(backend=backend, audit_logger=audit), audit


def test_both_writers_land_as_two_versions(seeded: Engine, embedder: HashEmbedder384) -> None:
    gate = _Gate(embedder)
    store, audit = _store(seeded, gate)
    store.write(_PERSONA, [_chunk("base", "v1")], source=WriteSource.USER)
    gate.enabled = True

    errors = _race(
        lambda: store.write(_PERSONA, [_chunk("a", "from a")], source=WriteSource.USER),
        lambda: store.write(_PERSONA, [_chunk("b", "from b")], source=WriteSource.USER),
    )

    assert errors == []
    chain = store.history(_PERSONA, _LOGICAL)  # validates the chain
    assert [c.provenance.version for c in chain if c.provenance] == [1, 2, 3]
    assert {c.text for c in chain[1:]} == {"from a", "from b"}
    assert len(audit.events) == 3


def test_without_a_retry_the_loser_gets_the_domain_error(
    seeded: Engine, embedder: HashEmbedder384
) -> None:
    """The real driver's violation is recognised. A name mismatch would be an IntegrityError."""
    gate = _Gate(embedder)
    store, _ = _store(seeded, gate)
    store.write(_PERSONA, [_chunk("base", "v1")], source=WriteSource.USER)
    gate.enabled = True

    errors = _race(
        lambda: store.write(
            _PERSONA, [_chunk("a", "a")], source=WriteSource.USER, retry_on_conflict=False
        ),
        lambda: store.write(
            _PERSONA, [_chunk("b", "b")], source=WriteSource.USER, retry_on_conflict=False
        ),
    )

    assert [type(e) for e in errors] == [ChainConflictError]
    assert errors[0].context["constraint"] == CHAIN_HEAD_INDEX  # type: ignore[attr-defined]
    assert diagnose_chain(store.history(_PERSONA, _LOGICAL)).defect is ChainDefect.NONE


def test_two_refreshes_with_the_same_text_leave_one_version(
    seeded: Engine, embedder: HashEmbedder384
) -> None:
    gate = _Gate(embedder)
    store, audit = _store(seeded, gate, CoreMemoryStore)
    store.write(_PERSONA, [_chunk("c0", "block")], source=WriteSource.SYSTEM)
    gate.enabled = True

    errors = _race(
        lambda: store.write(_PERSONA, [_chunk("a", "same text")], source=WriteSource.SYSTEM),
        lambda: store.write(_PERSONA, [_chunk("b", "same text")], source=WriteSource.SYSTEM),
    )

    assert errors == []
    assert [c.text for c in store.history(_PERSONA, _LOGICAL)] == ["block", "same text"]
    assert len(audit.events) == 2, "one refresh landed, and the other changed nothing"


def test_a_first_write_race_is_caught_too(seeded: Engine, embedder: HashEmbedder384) -> None:
    gate = _Gate(embedder)
    store, _ = _store(seeded, gate)
    gate.enabled = True

    errors = _race(
        lambda: store.write(_PERSONA, [_chunk("a", "from a")], source=WriteSource.USER),
        lambda: store.write(_PERSONA, [_chunk("b", "from b")], source=WriteSource.USER),
    )

    assert errors == []
    assert [c.provenance.version for c in store.history(_PERSONA, _LOGICAL) if c.provenance] == [
        1,
        2,
    ]


def test_one_call_with_two_updates_to_one_chain_is_legal_under_the_index(
    seeded: Engine, embedder: HashEmbedder384
) -> None:
    """R9-206's batch: the row that is both a new version and a prior head."""
    gate = _Gate(embedder)
    store, _ = _store(seeded, gate)
    store.write(_PERSONA, [_chunk("base", "v1")], source=WriteSource.USER)

    store.write(_PERSONA, [_chunk("n1", "v2"), _chunk("n2", "v3")], source=WriteSource.USER)

    assert [c.text for c in store.history(_PERSONA, _LOGICAL)] == ["v1", "v2", "v3"]


def test_a_rollback_is_legal_under_the_index(seeded: Engine, embedder: HashEmbedder384) -> None:
    gate = _Gate(embedder)
    store, _ = _store(seeded, gate)
    store.write(_PERSONA, [_chunk("base", "v1")], source=WriteSource.USER)
    store.write(_PERSONA, [_chunk("next", "v2")], source=WriteSource.USER)

    store.rollback(_PERSONA, _LOGICAL, 1, source=WriteSource.USER)

    assert [c.text for c in store.history(_PERSONA, _LOGICAL)] == ["v1", "v2", "v1"]


def _versioned(chunk_id: str, version: int, superseded_by: str | None) -> PersonaChunk:
    return PersonaChunk(
        id=chunk_id,
        text=f"text of {chunk_id}",
        created_at=_NOW,
        provenance=ChunkProvenance(
            source=WriteSource.USER,
            logical_id=_LOGICAL,
            version=version,
            superseded_by=superseded_by,
            written_at=_NOW,
        ),
    )


def test_a_repair_that_moves_the_head_is_legal_under_the_index(
    seeded: Engine, embedder: HashEmbedder384
) -> None:
    """The head moves from v1 to v2. Clearing v2's link first would be two heads for a moment."""
    backend = PostgresBackend(engine=seeded, embedder=embedder)
    backend.upsert(
        persona_id=_PERSONA,
        store_kind="self_facts",
        chunks=[_versioned("v1", 1, None), _versioned("v2", 2, "never-written")],
    )
    store = SelfFactsStore(backend=backend, audit_logger=MemoryAuditLogger())

    assert store.repair(_PERSONA) == 1

    assert store.diagnose(_PERSONA).healthy
    assert [c.id for c in store.history(_PERSONA, _LOGICAL)] == ["v1", "v2"]


# --- two saves of one persona, the path ``update_persona`` takes (review finding M1) ----


def _registry(engine: Engine, gate: _Gate) -> tuple[PersonaRegistry, SelfFactsStore, Persona]:
    backend = PostgresBackend(engine=engine, embedder=gate)  # type: ignore[arg-type]
    audit = MemoryAuditLogger()
    self_facts = SelfFactsStore(backend=backend, audit_logger=audit)
    registry = PersonaRegistry(
        stores={
            "identity": IdentityStore(backend=backend, audit_logger=audit),
            "self_facts": self_facts,
            "worldview": WorldviewStore(backend=backend, audit_logger=audit),
            "episodic": EpisodicStore(backend=backend, audit_logger=audit),
        },
        audit_logger=audit,
    )
    persona = Persona.from_yaml(_PERSONA_YAML).model_copy(update={"persona_id": _PERSONA})
    registry.load_persona(persona)
    return registry, self_facts, persona


def _saved(persona: Persona, fact: str) -> Persona:
    edited = persona.self_facts[0].model_copy(update={"fact": fact})
    return persona.model_copy(update={"self_facts": [edited, *persona.self_facts[1:]]})


def test_two_identical_persona_saves_collapse_on_the_real_index(
    seeded: Engine, embedder: HashEmbedder384
) -> None:
    gate = _Gate(embedder)
    registry, store, base = _registry(seeded, gate)
    edited = _saved(base, "An edited fact")
    gate.enabled = True

    errors = _race(lambda: registry.load_persona(edited), lambda: registry.load_persona(edited))

    assert errors == []
    chain = store.history(_PERSONA, _FIRST_FACT)
    assert len(chain) == 2, "one save happened, however many callers asked for it"
    assert store.diagnose(_PERSONA).healthy


def test_two_different_persona_saves_both_land_and_the_persona_stays_saveable(
    seeded: Engine, embedder: HashEmbedder384
) -> None:
    gate = _Gate(embedder)
    registry, store, base = _registry(seeded, gate)
    gate.enabled = True

    errors = _race(
        lambda: registry.load_persona(_saved(base, "One save")),
        lambda: registry.load_persona(_saved(base, "Another save")),
    )

    assert errors == []
    texts = {c.text for c in store.history(_PERSONA, _FIRST_FACT)}
    assert {"self_fact: One save", "self_fact: Another save"} <= texts
    registry.load_persona(_saved(base, "A later save"))  # every PATCH used to 500 from here
    assert store.diagnose(_PERSONA).healthy

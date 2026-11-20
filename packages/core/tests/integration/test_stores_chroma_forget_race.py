"""A forget racing a write, on a real Chroma folder (R9-271).

Chroma has no transaction, so the append contract is kept by a lock: the check of what a write
supersedes and cites, and the one upsert after it, run under a lock that every delete on the same
folder and persona also takes. These tests drive real threads through a gate in the embedder,
which runs inside the upsert and so inside the lock.

The lock is held only for the check and the write: the new version is embedded before it is
taken, and a superseded version keeps the vector already stored for it (PR #39 review). So the
gate that holds a writer inside the lock sits where the rows are turned into Chroma metadata,
which only happens under it.

A forget that waits on the lock is shown to be waiting with a bounded wait that expects nothing to
happen. That can only fail in one direction: a slow machine can make the broken code look right,
never the correct code look broken.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona.audit import MemoryAuditLogger
from persona.errors import ChainConflictError, ForgottenEvidenceError
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource
from persona.stores import ChromaBackend, SelfFactsStore
from persona.stores import chroma as chroma_module

from tests._embedder import HashEmbedder

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)
_PERSONA = "p1"
_FACT = "fact-chain"
_GATE = "GATED"
_WAIT = 10.0  # a bound on every wait for something that must happen, so nothing hangs
_STILL_BLOCKED = 0.5  # how long a forget that must be waiting on the lock is watched


class _GatedEmbedder(HashEmbedder):
    """Records what it embeds. Once armed, stops on any text carrying the gate word."""

    def __init__(self) -> None:
        self.inside = threading.Event()
        self.release = threading.Event()
        self.embedded: list[str] = []
        self.armed = False

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        self.embedded.extend(texts)
        if self.armed and any(_GATE in t for t in texts):
            self.inside.set()
            assert self.release.wait(_WAIT), "the gate was never released"
        return super().encode(texts)


class _LockGate:
    """Stops a write of the gate word inside the folder lock, where its rows become metadata."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.inside = threading.Event()
        self.release = threading.Event()
        real = chroma_module._chunk_to_metadata

        def held(chunk: PersonaChunk) -> dict[str, str | float | int | bool]:
            if _GATE in chunk.text:
                self.inside.set()
                assert self.release.wait(_WAIT), "the gate was never released"
            return real(chunk)

        monkeypatch.setattr(chroma_module, "_chunk_to_metadata", held)


def _fact(chunk_id: str, text: str) -> PersonaChunk:
    return PersonaChunk(
        id=chunk_id,
        text=text,
        created_at=_NOW,
        provenance=ChunkProvenance(source=WriteSource.SYSTEM, logical_id=_FACT, written_at=_NOW),
    )


def _vectors(backend: ChromaBackend, kind: str = "self_facts") -> dict[str, list[float]]:
    raw = backend._collection(_PERSONA, kind).get(include=["embeddings"])  # noqa: SLF001
    return {
        str(i): [float(x) for x in e] for i, e in zip(raw["ids"], raw["embeddings"], strict=True)
    }


def _texts(backend: ChromaBackend, kind: str = "self_facts") -> list[str]:
    return [c.text for c in backend.get_all(persona_id=_PERSONA, store_kind=kind)]


def _thread(
    target: Callable[[], None],
) -> tuple[threading.Thread, threading.Event, list[BaseException]]:
    done = threading.Event()
    errors: list[BaseException] = []

    def run() -> None:
        try:
            target()
        except BaseException as exc:  # noqa: BLE001 - reported by the test
            errors.append(exc)
        finally:
            done.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, done, errors


@pytest.fixture
def embedder() -> _GatedEmbedder:
    return _GatedEmbedder()


@pytest.fixture
def backend(tmp_path: Path, embedder: _GatedEmbedder) -> ChromaBackend:
    return ChromaBackend(persist_path=tmp_path / "chroma", embedder=embedder)


def test_a_forget_between_the_read_and_the_write_does_not_bring_the_old_version_back(
    backend: ChromaBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SelfFactsStore(backend=backend, audit_logger=MemoryAuditLogger())
    store.write(_PERSONA, [_fact("f1", "the thing to forget")], force=True)
    read = backend.get_by_logical_ids
    armed = {"on": True}

    def read_then_forget(**kwargs: object) -> list[PersonaChunk]:
        chain = read(**kwargs)  # type: ignore[arg-type]
        if armed["on"]:
            armed["on"] = False
            store.remove_documents(_PERSONA, ["f1"])
        return chain

    monkeypatch.setattr(backend, "get_by_logical_ids", read_then_forget)

    store.write(_PERSONA, [_fact("f1", "a later, unrelated update")], force=True)

    assert _texts(backend) == ["a later, unrelated update"]


def _forget_waits_for_the_writer(
    writer_backend: ChromaBackend, forget_backend: ChromaBackend, gate: _LockGate
) -> None:
    writer = SelfFactsStore(backend=writer_backend, audit_logger=MemoryAuditLogger())
    forgetter = SelfFactsStore(backend=forget_backend, audit_logger=MemoryAuditLogger())
    writer.write(_PERSONA, [_fact("f1", "the thing to forget")], force=True)

    write, wrote, write_errors = _thread(
        lambda: writer.write(_PERSONA, [_fact("f1", f"{_GATE} update")], force=True)
    )
    assert gate.inside.wait(_WAIT), "the writer never reached its upsert"
    forget, forgot, forget_errors = _thread(lambda: forgetter.remove_documents(_PERSONA, ["f1"]))

    blocked = not forgot.wait(_STILL_BLOCKED)
    gate.release.set()
    assert wrote.wait(_WAIT)
    assert forgot.wait(_WAIT)
    write.join(_WAIT)
    forget.join(_WAIT)

    assert write_errors == []
    assert forget_errors == []
    assert blocked, "the forget ran while the writer was between its check and its upsert"
    assert "the thing to forget" not in _texts(writer_backend)


def test_a_forget_waits_for_a_write_that_has_checked_its_chain(
    backend: ChromaBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    _forget_waits_for_the_writer(backend, backend, _LockGate(monkeypatch))


def test_two_backends_on_one_folder_share_the_lock(
    tmp_path: Path,
    backend: ChromaBackend,
    embedder: _GatedEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The api and the connectors service each build their own backend on the same folder."""
    other = ChromaBackend(persist_path=tmp_path / "chroma", embedder=embedder)
    _forget_waits_for_the_writer(backend, other, _LockGate(monkeypatch))


def test_the_new_version_is_embedded_before_the_lock_is_taken(
    backend: ChromaBackend, embedder: _GatedEmbedder
) -> None:
    """A slow embedder must not hold off a forget of the same persona."""
    store = SelfFactsStore(backend=backend, audit_logger=MemoryAuditLogger())
    store.write(_PERSONA, [_fact("f1", "the thing to forget")], force=True)
    embedder.armed = True

    write, wrote, write_errors = _thread(
        lambda: store.write(_PERSONA, [_fact("f1", f"{_GATE} update")], force=True)
    )
    try:
        assert embedder.inside.wait(_WAIT), "the writer never reached its embedding"
        forget, forgot, forget_errors = _thread(lambda: store.remove_documents(_PERSONA, ["f1"]))
        finished_while_embedding = forgot.wait(_WAIT)
    finally:
        embedder.release.set()
    assert wrote.wait(_WAIT)
    write.join(_WAIT)
    forget.join(_WAIT)

    assert finished_while_embedding, "the forget waited for the writer's embedding"
    assert write_errors == []
    assert forget_errors == []
    assert _texts(backend) == [f"{_GATE} update"], "the write lands on a chain that starts again"


def test_a_superseded_version_is_not_embedded_again(
    backend: ChromaBackend, embedder: _GatedEmbedder
) -> None:
    store = SelfFactsStore(backend=backend, audit_logger=MemoryAuditLogger())
    store.write(_PERSONA, [_fact("f1", "first")], force=True)
    first = backend.get_all(persona_id=_PERSONA, store_kind="self_facts")
    vector_before = _vectors(backend)[first[0].id]

    store.write(_PERSONA, [_fact("f1", "second")], force=True)

    assert embedder.embedded.count("first") == 1, "the superseded version was embedded again"
    assert _vectors(backend)[first[0].id] == vector_before
    assert [c.text for c in store.history(_PERSONA, _FACT)] == ["first", "second"]


def test_an_append_citing_evidence_that_is_gone_lands_nothing(backend: ChromaBackend) -> None:
    backend.upsert(persona_id=_PERSONA, store_kind="episodic", chunks=[_fact("a", "evidence a")])
    with pytest.raises(ForgottenEvidenceError):
        backend.upsert(
            persona_id=_PERSONA,
            store_kind="core_memory",
            chunks=[_fact("block", "a summary of a and x")],
            requires=["a", "x"],
            requires_kinds=["episodic"],
        )
    assert _texts(backend, "core_memory") == []


def test_an_append_over_a_version_that_moved_lands_nothing(backend: ChromaBackend) -> None:
    store = SelfFactsStore(backend=backend, audit_logger=MemoryAuditLogger())
    store.write(_PERSONA, [_fact("f1", "first")], force=True)
    with pytest.raises(ChainConflictError):
        backend.upsert(
            persona_id=_PERSONA,
            store_kind="self_facts",
            chunks=[_fact("f2", "second")],
            supersedes={"f1": (2, "f2")},  # it is version 1
        )
    assert _texts(backend) == ["first"]

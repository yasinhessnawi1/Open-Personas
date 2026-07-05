"""Spec K8 T3 — reinforce_recalled wiring (K8-D-5): explicit, raw-only, fail-soft.

``retrieve_context`` stays pure; the turn paths call ``reinforce_recalled``
AFTER retrieval. Chat calls it synchronously (the loop, right after
``_retrieve``); voice calls it inside its retrieval worker thread, which
``asyncio.to_thread`` already runs off the event loop (the starvation rule
holds structurally — pinned in the voice package's assembler test).
"""

# ruff: noqa: ARG002 — protocol doubles ignore args by design.
from __future__ import annotations

from datetime import UTC, datetime

from persona.schema.chunks import PersonaChunk
from persona_runtime.prompt import RetrievedContext
from persona_runtime.retrieval import reinforce_recalled

_NOW = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)


def _chunk(cid: str, *, member_ids: tuple[str, ...] = ()) -> PersonaChunk:
    return PersonaChunk(id=cid, text=f"t {cid}", created_at=_NOW, member_ids=member_ids)


class _ReinforcingStore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    def reinforce(self, persona_id: str, chunk_ids: list[str]) -> None:
        self.calls.append((persona_id, chunk_ids))


class _PlainStore:
    """No reinforce method — the duck-typed no-op path (fail-soft)."""


class _ExplodingStore:
    def reinforce(self, persona_id: str, chunk_ids: list[str]) -> None:
        msg = "db down"
        raise RuntimeError(msg)


def _context(*episodic: PersonaChunk) -> RetrievedContext:
    return RetrievedContext(identity=[], self_facts=[], worldview=[], episodic=list(episodic))


def test_reinforces_raw_episodic_ids_in_one_call() -> None:
    store = _ReinforcingStore()
    ctx = _context(_chunk("a"), _chunk("b"))
    reinforce_recalled({"episodic": store}, "p1", ctx)  # type: ignore[arg-type]
    assert store.calls == [("p1", ["a", "b"])]


def test_gist_rows_are_excluded_members_reinforce_via_k9_not_here() -> None:
    store = _ReinforcingStore()
    ctx = _context(_chunk("raw"), _chunk("gist", member_ids=("raw", "other")))
    reinforce_recalled({"episodic": store}, "p1", ctx)  # type: ignore[arg-type]
    assert store.calls == [("p1", ["raw"])]


def test_empty_recall_is_a_noop() -> None:
    store = _ReinforcingStore()
    reinforce_recalled({"episodic": store}, "p1", _context())  # type: ignore[arg-type]
    assert store.calls == []


def test_store_without_the_command_is_a_noop_not_an_error() -> None:
    reinforce_recalled({"episodic": _PlainStore()}, "p1", _context(_chunk("a")))  # type: ignore[arg-type]


def test_reinforcement_failure_never_breaks_the_turn() -> None:
    reinforce_recalled({"episodic": _ExplodingStore()}, "p1", _context(_chunk("a")))  # type: ignore[arg-type]

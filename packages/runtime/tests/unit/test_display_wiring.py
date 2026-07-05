"""Spec K8 T4 — band-resolved display through the shared retrieval (K8-D-11).

The retrieval SET is unchanged (``episodic_recalled_ids`` records what was
FOUND); the displayed list swaps demoted hits for their gist rendering. The
hook is duck-typed + fail-soft like the reinforce hook: doubles without
``resolve_display`` (every pre-K8 test store) keep raw display; a resolution
crash falls back to raw, never a broken turn. Reinforcement targets the FOUND
ids — a demoted hit reinforces so important old memory re-promotes (K8-D-3/5).
"""

# ruff: noqa: ARG002 — protocol doubles ignore args by design.
from __future__ import annotations

from datetime import UTC, datetime

from persona.schema.chunks import PersonaChunk
from persona.stores.pyramid import OLDER_MEMORY_MARKER
from persona_runtime.retrieval import reinforce_recalled, retrieve_context

_NOW = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)


def _chunk(cid: str, *, band: int = 0, member_ids: tuple[str, ...] = ()) -> PersonaChunk:
    return PersonaChunk(
        id=cid, text=f"raw {cid}", created_at=_NOW, band=band, member_ids=member_ids
    )


class _EmptyStore:
    def get_all(self, persona_id: str, *, include_superseded: bool = False) -> list[PersonaChunk]:
        return []

    def query(self, persona_id: str, q: str, top_k: int) -> list[PersonaChunk]:
        return []

    def recent(self, persona_id: str, limit: int) -> list[PersonaChunk]:
        return []


class _ResolvingEpisodic(_EmptyStore):
    """Serves one demoted hit and resolves it to a gist rendering."""

    def __init__(self) -> None:
        self.demoted = _chunk("p1::episodic::old", band=1)
        # A fresh chunk (hash computed over the rendered text) — exactly what
        # the production resolve_display constructs; never a text-mutated copy.
        self.gist = PersonaChunk(
            id="p1::episodic_gist::g1",
            text=f"{OLDER_MEMORY_MARKER} the gist text",
            created_at=_NOW,
            band=1,
            member_ids=("p1::episodic::old",),
        )
        self.reinforced: list[list[str]] = []

    def query(self, persona_id: str, q: str, top_k: int) -> list[PersonaChunk]:
        return [self.demoted]

    def resolve_display(self, persona_id: str, chunks: list[PersonaChunk]) -> list[PersonaChunk]:
        return [self.gist if c.id == self.demoted.id else c for c in chunks]

    def reinforce(self, persona_id: str, chunk_ids: list[str]) -> None:
        self.reinforced.append(chunk_ids)


class _CrashingEpisodic(_ResolvingEpisodic):
    def resolve_display(self, persona_id: str, chunks: list[PersonaChunk]) -> list[PersonaChunk]:
        msg = "resolution exploded"
        raise RuntimeError(msg)


def _stores(episodic: object) -> dict[str, object]:
    return {
        "identity": _EmptyStore(),
        "self_facts": _EmptyStore(),
        "worldview": _EmptyStore(),
        "episodic": episodic,
    }


def test_demoted_hit_displays_as_gist_but_the_found_set_is_the_raw_id() -> None:
    store = _ResolvingEpisodic()
    ctx = retrieve_context(_stores(store), "p1", "anything")  # type: ignore[arg-type]
    assert [c.id for c in ctx.episodic] == [store.gist.id]  # displayed: the gist
    assert ctx.episodic[0].text.startswith(OLDER_MEMORY_MARKER)
    assert ctx.episodic_recalled_ids == (store.demoted.id,)  # found: the raw id


def test_reinforcement_targets_the_found_raw_ids_not_the_displayed_gist() -> None:
    store = _ResolvingEpisodic()
    ctx = retrieve_context(_stores(store), "p1", "anything")  # type: ignore[arg-type]
    reinforce_recalled(_stores(store), "p1", ctx)  # type: ignore[arg-type]
    # The demoted RAW chunk reinforces (re-promotion, K8-D-3); the gist doesn't.
    assert store.reinforced == [[store.demoted.id]]


def test_resolution_crash_falls_back_to_raw_display() -> None:
    store = _CrashingEpisodic()
    ctx = retrieve_context(_stores(store), "p1", "anything")  # type: ignore[arg-type]
    assert [c.id for c in ctx.episodic] == [store.demoted.id]  # raw, turn intact


def test_stores_without_the_hook_keep_the_historical_behaviour() -> None:
    class _Plain(_EmptyStore):
        def query(self, persona_id: str, q: str, top_k: int) -> list[PersonaChunk]:
            return [_chunk("p1::episodic::x")]

    ctx = retrieve_context(_stores(_Plain()), "p1", "anything")  # type: ignore[arg-type]
    assert [c.id for c in ctx.episodic] == ["p1::episodic::x"]
    assert ctx.episodic_recalled_ids == ("p1::episodic::x",)

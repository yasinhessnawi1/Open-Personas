"""Voice unified recall composition (Spec K9, T9) — no fork, off-loop-safe, gated.

Pins: ``build_voice_unified_recall`` reuses ``make_unified_recall`` + the shared adapters
(no voice fork), the K4 gate is IDENTICAL to chat's (the standing V13-D-5 identity), the
reranker is the fail-soft shell (stub ⇒ fused; a stall degrades to fused, never stalls the
spoken turn), and the whole thing is a plain sync callable safe to run inside the reply
producer's ``asyncio.to_thread`` (the reranker off the event loop).
"""

# ruff: noqa: ARG002 — the store fakes mirror the real Protocol signatures; args are unused by design.
from __future__ import annotations

from datetime import UTC, datetime

from persona.graph.models import ConceptNode, NodeKind, NodeProvenance
from persona.schema.chunks import PersonaChunk, WriteSource
from persona_runtime.unified_recall import UnifiedProjection
from persona_voice.model.graph import VoiceUnifiedComposition, build_voice_unified_recall

NOW = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)


class _FakeGraphStore:
    def flagged_nodes(self, owner_id: str) -> list[ConceptNode]:
        return []

    def node_ids_for_owner(self, owner_id: str) -> list[str]:
        return ["g1"]

    def search_dense(
        self, owner_id: str, query: str, top_k: int, **_kw: object
    ) -> list[ConceptNode]:
        return [
            ConceptNode(
                id="g1",
                node_kind=NodeKind.FACT,
                concept_name="address",
                content="5 Oak St",
                distance=0.4,
                provenance=(NodeProvenance(source=WriteSource.PERSONA_SELF, written_at=NOW),),
                created_at=NOW,
            )
        ]

    def search_fts(self, owner_id: str, query: str, top_k: int) -> list[ConceptNode]:
        return []

    def neighbors(self, owner_id: str, node_id: str, *, link_types: object, limit: int) -> list:  # type: ignore[type-arg]
        return []


class _FakePyramid:
    def query(self, persona_id: str, query: str, top_k: int) -> list[PersonaChunk]:
        return []

    def covering_gists(self, persona_id: str, ids: object) -> dict[str, PersonaChunk]:
        return {}

    def drill(self, persona_id: str, gist_id: str) -> list[PersonaChunk]:
        return []


class _FakeEpisodicStore:
    def __init__(self) -> None:
        self.pyramid = _FakePyramid()

    def query(self, persona_id: str, query: str, top_k: int) -> list[PersonaChunk]:
        return [PersonaChunk(id="e1", text="my address is 5 Oak", distance=0.02, created_at=NOW)]

    def resolve_display(self, persona_id: str, chunks: list[PersonaChunk]) -> list[PersonaChunk]:
        return chunks


def _build() -> VoiceUnifiedComposition:
    return build_voice_unified_recall(
        _FakeGraphStore(),  # type: ignore[arg-type]
        _FakeEpisodicStore(),  # type: ignore[arg-type]
        owner_id="owner-1",
        persona_id="p",
    )


def test_composition_returns_a_sync_retrieval_and_surfacing() -> None:
    comp = _build()
    assert callable(comp.retrieval)
    assert callable(comp.surfacing_guidance)


def test_retrieval_produces_a_projection_off_loop_safe() -> None:
    # A plain sync call (as it runs inside to_thread) — fused order under the stub reranker,
    # projecting episodic + graph into the seam. No await, no event loop touched.
    out = _build().retrieval("what is my address")
    assert isinstance(out, UnifiedProjection)
    assert not out.abstained
    assert [c.id for c in out.episodic] == ["e1"]
    assert any(i.concept_name == "address" for i in out.graph.items)


def test_a_failure_degrades_to_memoryless_never_stalls() -> None:
    class _BoomEpisodic(_FakeEpisodicStore):
        def query(self, persona_id: str, query: str, top_k: int) -> list[PersonaChunk]:
            raise RuntimeError("store down")

    comp = build_voice_unified_recall(
        _FakeGraphStore(),  # type: ignore[arg-type]
        _BoomEpisodic(),  # type: ignore[arg-type]
        owner_id="owner-1",
        persona_id="p",
    )
    out = comp.retrieval("anything")
    assert out.episodic == []  # memoryless, never turn-fatal
    assert out.graph.items == ()

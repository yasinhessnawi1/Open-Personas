"""The synthesis composition factory (Spec K2, T8c).

``build_synthesizer`` is the seam the worker root (T8d) calls to wire the extractor +
AMBIGUOUS-band judge (on the small/mid tier), entity resolution (K0's registry),
update resolution, and the graph store into a working ``Synthesizer``. This proves it
assembles + runs end-to-end with fakes (no model, no DB) — the wiring is correct.
"""

# ruff: noqa: ARG002 — fakes ignore some args by design.

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from persona.backends.types import ChatResponse, TokenUsage
from persona.extraction import InteractionKind
from persona.graph.models import CanonicalEntity, EntityAlias
from persona.graph.protocol import (
    KnowledgeCandidate,
    MergeAction,
    MergeOutcome,
    ResolutionDecision,
    ResolutionVerdict,
)
from persona_runtime.extraction import Synthesizer, build_synthesizer
from persona_runtime.extraction.windowing import build_window


class _Backend:
    """Returns one grounded candidate; the entity judge path is unused here."""

    @property
    def provider_name(self) -> str:
        return "anthropic"

    @property
    def model_name(self) -> str:
        return "claude-haiku-4-5-20251001"

    @property
    def supports_native_tools(self) -> bool:
        return False

    @property
    def supports_vision(self) -> bool:
        return False

    async def chat(self, messages: object, **kwargs: Any) -> ChatResponse:  # noqa: ANN401
        content = (
            '{"candidates": [{"concept_name": "vegetarian", "content": "is vegetarian",'
            ' "node_kind": "preference", "evidence_span": "I went vegetarian"}]}'
        )
        return ChatResponse(
            content=content,
            tool_calls=[],
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            model=self.model_name,
            provider=self.provider_name,
            latency_ms=0.0,
        )

    def chat_stream(self, *a: Any, **k: Any) -> Any:  # noqa: ANN401
        raise NotImplementedError


class _Registry:
    def resolve(self, owner_id: str, mention: str) -> ResolutionVerdict:
        return ResolutionVerdict(decision=ResolutionDecision.SEPARATE)

    def get_entity(self, owner_id: str, entity_id: str) -> CanonicalEntity | None:
        return None

    def create_entity(self, owner_id: str, *, canonical_name: str, **kw: Any) -> CanonicalEntity:  # noqa: ANN401
        return CanonicalEntity(id="e1", canonical_name=canonical_name, created_at=datetime.now(UTC))

    def add_alias(self, owner_id: str, entity_id: str, alias: EntityAlias) -> None: ...


class _Store:
    def __init__(self) -> None:
        self.merges: list[KnowledgeCandidate] = []

    def merge(self, owner_id: str, candidate: KnowledgeCandidate) -> MergeOutcome:
        self.merges.append(candidate)
        return MergeOutcome(action=MergeAction.CREATED, node_id="node-1")

    def search_dense(self, owner_id: str, query: str, top_k: int, **kw: Any) -> list[Any]:  # noqa: ANN401
        return []


def test_build_synthesizer_returns_a_synthesizer() -> None:
    s = build_synthesizer(graph_store=_Store(), registry=_Registry(), backend=_Backend())  # type: ignore[arg-type]
    assert isinstance(s, Synthesizer)


@pytest.mark.asyncio
async def test_composed_synthesizer_runs_end_to_end_with_fakes() -> None:
    store = _Store()
    synth = build_synthesizer(graph_store=store, registry=_Registry(), backend=_Backend())  # type: ignore[arg-type]
    window = build_window(
        messages=[("user", "I went vegetarian")],
        compacted_summary="",
        synthesised_up_to=0,
        interaction_kind=InteractionKind.CONVERSATION,
        interaction_id="c1",
        persona_id="p1",
    )
    assert window is not None
    outcomes = await synth.synthesise("u1", window.input)
    assert len(outcomes) == 1
    assert store.merges[0].content == "is vegetarian"


def test_build_synthesizer_carries_the_disclosure_recorder() -> None:
    """R9-253: the worker hands the recorder to the factory; the factory must not drop it."""
    recorder = object()
    s = build_synthesizer(
        graph_store=_Store(),  # type: ignore[arg-type]
        registry=_Registry(),  # type: ignore[arg-type]
        backend=_Backend(),  # type: ignore[arg-type]
        disclosures=recorder,  # type: ignore[arg-type]
    )
    assert s._disclosures is recorder  # noqa: SLF001

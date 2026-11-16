"""The direct-write tool ``record_user_fact`` (Spec K2, T7; D-K2-1).

A Spec-03 ``AsyncTool`` the persona invokes mid-conversation to record an explicit,
discrete, durable fact about the user. Fire-and-forget from the turn's perspective:
ONE fast inline ``GraphStore.merge`` (no LLM — the persona's in-context judgement IS
the extraction), so it never blocks/slows the reply (criterion 10). The fact is
grounded by construction (the persona asserts the user conveyed it → provenance
grounding). A write failure returns an error result, never crashes the turn.

These are mocked-store unit tests; the on-by-default api wiring + the cross-persona
retrieval E2E (criterion 1 end-to-end) land in T8/T9 (the GraphStore is composed
into the api there).
"""

# ruff: noqa: ARG002 — the GraphStore double ignores some args by design.

from __future__ import annotations

import pytest
from persona.graph.errors import NodeMergeError
from persona.graph.models import NodeKind
from persona.graph.protocol import KnowledgeCandidate, MergeAction, MergeOutcome
from persona.schema.chunks import WriteSource
from persona_runtime.extraction.direct_write import make_record_user_fact_tool


class _FakeGraphStore:
    """Records merge calls; optionally raises to exercise the failure path."""

    def __init__(self, *, raises: bool = False) -> None:
        self.merges: list[tuple[str, KnowledgeCandidate]] = []
        self._raises = raises

    def merge(self, owner_id: str, candidate: KnowledgeCandidate) -> MergeOutcome:
        self.merges.append((owner_id, candidate))
        if self._raises:
            raise NodeMergeError("merge failed", context={})
        return MergeOutcome(action=MergeAction.CREATED, node_id="u1::node::00000001")


def _tool(
    store: _FakeGraphStore, *, owner: str | None = "u1", persona_id: str | None = "assistant"
) -> object:
    return make_record_user_fact_tool(
        graph_store=store, owner_provider=lambda: owner, persona_id=persona_id
    )


@pytest.mark.asyncio
async def test_tool_metadata_and_guidance() -> None:
    t = _tool(_FakeGraphStore())
    assert t.name == "record_user_fact"  # type: ignore[attr-defined]
    desc = t.description.lower()  # type: ignore[attr-defined]
    # the silent / discrete-fact guidance (K2-R-3 behavioural surface)
    assert "do not narrate" in desc or "silently" in desc
    assert "means" in desc  # the self-harm means caution


@pytest.mark.asyncio
async def test_records_fact_through_a_single_inline_merge() -> None:
    store = _FakeGraphStore()
    res = await _tool(store).execute(fact="I'm allergic to shellfish")  # type: ignore[attr-defined]
    assert not res.is_error
    assert "00000001" in res.content
    assert len(store.merges) == 1  # criterion 10: exactly one fast inline merge
    owner, cand = store.merges[0]
    assert owner == "u1"
    assert cand.content == "I'm allergic to shellfish"
    assert cand.node_kind is NodeKind.FACT
    # grounded by construction + persona_self provenance (D-K2-1)
    assert cand.provenance.source is WriteSource.PERSONA_SELF
    assert cand.provenance.grounding == "I'm allergic to shellfish"
    assert cand.provenance.persona_id == "assistant"


@pytest.mark.asyncio
async def test_valid_category_tags_wellbeing() -> None:
    store = _FakeGraphStore()
    await _tool(store).execute(  # type: ignore[attr-defined]
        fact="I've been struggling with disordered eating", category="disordered_eating"
    )
    _, cand = store.merges[0]
    assert cand.wellbeing_category == "disordered_eating"


@pytest.mark.asyncio
async def test_invalid_category_is_ignored_and_the_write_still_succeeds() -> None:
    store = _FakeGraphStore()
    res = await _tool(store).execute(fact="a durable fact", category="made_up")  # type: ignore[attr-defined]
    assert not res.is_error
    _, cand = store.merges[0]
    assert cand.wellbeing_category is None


@pytest.mark.asyncio
async def test_empty_fact_is_rejected_without_writing() -> None:
    store = _FakeGraphStore()
    res = await _tool(store).execute(fact="   ")  # type: ignore[attr-defined]
    assert res.is_error
    assert store.merges == []


@pytest.mark.asyncio
async def test_no_owner_context_fails_closed_without_writing() -> None:
    store = _FakeGraphStore()
    res = await _tool(store, owner=None).execute(fact="something")  # type: ignore[attr-defined]
    assert res.is_error
    assert store.merges == []


@pytest.mark.asyncio
async def test_merge_failure_returns_an_error_result_not_a_raise() -> None:
    # Fire-and-forget safety: a write failure must not crash the live turn.
    store = _FakeGraphStore(raises=True)
    res = await _tool(store).execute(fact="a fact")  # type: ignore[attr-defined]
    assert res.is_error


@pytest.mark.asyncio
async def test_self_harm_means_are_rejected_before_the_merge() -> None:
    # The structural means backstop (D-K2-7): means specifics never reach the graph.
    store = _FakeGraphStore()
    res = await _tool(store).execute(  # type: ignore[attr-defined]
        fact="I keep thinking about taking all my pills at once", category="self_harm"
    )
    assert res.is_error
    assert store.merges == []  # rejected before the write


@pytest.mark.asyncio
async def test_means_free_self_harm_disclosure_is_still_recorded() -> None:
    # The care-relevant struggle (no means) IS kept — the backstop is not blanket.
    store = _FakeGraphStore()
    res = await _tool(store).execute(  # type: ignore[attr-defined]
        fact="the user disclosed self-harm urges recently", category="self_harm"
    )
    assert not res.is_error
    assert len(store.merges) == 1


@pytest.mark.asyncio
async def test_a_persona_that_no_longer_exists_records_nothing() -> None:
    """R9-284: a turn still running when its persona was erased must not grow the graph again.

    The graph is owner scoped, so nothing else refuses this write: the erasure would have
    taken the persona out, and the next fact would put it back in.
    """
    store = _FakeGraphStore()
    tool = make_record_user_fact_tool(
        graph_store=store,
        owner_provider=lambda: "u1",
        persona_id="assistant",
        persona_exists=lambda persona_id: persona_id != "assistant",
    )

    res = await tool.execute(fact="I'm allergic to shellfish")  # type: ignore[attr-defined]

    assert res.is_error
    assert store.merges == []


@pytest.mark.asyncio
async def test_a_persona_that_exists_still_records() -> None:
    store = _FakeGraphStore()
    tool = make_record_user_fact_tool(
        graph_store=store,
        owner_provider=lambda: "u1",
        persona_id="assistant",
        persona_exists=lambda _persona_id: True,
    )

    res = await tool.execute(fact="I'm allergic to shellfish")  # type: ignore[attr-defined]

    assert not res.is_error
    assert len(store.merges) == 1

"""V13-T2 — the adversarial gate proof (the spec's reason to exist).

Three guarantees, proven not asserted (the chat analog is
``runtime/tests/unit/test_graph_k4_seam.py``, lifted to the voice composition):

1. **Bare-wiring is impossible-green.** A voice composition MISSING the allowlist
   provider (bare) leaks a gate-eligible wellbeing node that the full
   ``build_voice_graph_retrieval`` subtracts — even when the store itself ignores
   the allowlist (a K1 regression). Shipping bare demonstrably FAILS the
   subtraction test the full composition passes.
2. **A misbehaving retrieval degrades to a clean memoryless turn.** A store that
   throws never breaks the call: ``take_graph_if_ready`` returns an empty bundle,
   the turn proceeds graph-off. (Graph is additive presence, never a failure path.)
3. **The recent-window ContextVar crosses ``asyncio.to_thread`` with retrieval
   LIVE.** K4 wired ``set_recent_window_from_messages`` on the producer; now that
   the query actually runs off-thread, the gate must still read the window
   in-thread — proven by a window-only lift surviving the thread hop.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime

import pytest
from persona.graph.config import GraphSettings
from persona.graph.models import ConceptNode, NodeKind, NodeProvenance
from persona.schema.chunks import WriteSource
from persona.schema.conversation import ConversationMessage
from persona.wellbeing_policy import WellbeingCategory
from persona_runtime.graph_selection import make_graph_retrieval
from persona_runtime.graph_voice import (
    VOICE_NODE_BUDGET,
    start_graph_retrieval,
    take_graph_if_ready,
    voice_graph_settings,
)
from persona_runtime.graph_window import set_recent_window_from_messages
from persona_runtime.prompt import GraphContext
from persona_runtime.wellbeing import surfacing_guidance
from persona_voice.model.graph import build_voice_graph_retrieval

_OWNER = "user-adv-1"
_CRISIS_TEXT = "a severe mental-health crisis with panic attacks"


def _node(node_id: str, content: str, *, category: str | None = None) -> ConceptNode:
    now = datetime.now(UTC)
    return ConceptNode(
        id=node_id,
        node_kind=NodeKind.CONCEPT,
        concept_name=node_id,
        content=content,
        wellbeing_category=category,
        distance=0.05,  # highly relevant — would inject if the gate allowed it
        provenance=(NodeProvenance(source=WriteSource.SYSTEM, persona_id="p1", written_at=now),),
        created_at=now,
    )


class _LeakyStore:
    """An ADVERSARY store: ``search_dense`` IGNORES the allowlist (a K1 regression).

    The full composition must still subtract the crisis node (its own allowlist
    plumbing catches the leak); a bare composition cannot.
    """

    def __init__(self) -> None:
        self._crisis = _node(
            "crisis", _CRISIS_TEXT, category=WellbeingCategory.MENTAL_HEALTH_CRISIS.value
        )
        self._hobby = _node("hobby", "enjoys hiking on weekends")
        self._nodes = [self._crisis, self._hobby]

    def search_dense(
        self,
        owner_id: str,  # noqa: ARG002 — GraphStore contract
        query: str,  # noqa: ARG002 — GraphStore contract
        top_k: int,
        *,
        allowlist: set[str] | None = None,  # noqa: ARG002 — DELIBERATELY ignored (adversary)
    ) -> list[ConceptNode]:
        return self._nodes[:top_k]

    def search_fts(
        self,
        owner_id: str,  # noqa: ARG002 — GraphStore contract
        query: str,  # noqa: ARG002 — GraphStore contract
        top_k: int,  # noqa: ARG002 — GraphStore contract
    ) -> list[ConceptNode]:
        return []

    def flagged_nodes(self, owner_id: str) -> list[ConceptNode]:  # noqa: ARG002 — contract
        return [self._crisis]

    def node_ids_for_owner(self, owner_id: str) -> list[str]:  # noqa: ARG002 — contract
        return [n.id for n in self._nodes]

    def neighbors(self, *args: object, **kwargs: object) -> list[object]:  # noqa: ARG002
        raise AssertionError("traversal must be OFF on the voice profile")


def _crisis_subtracted(retrieval: object) -> bool:
    """True iff the crisis node is absent on an unrelated, unopened turn."""
    graph = retrieval("help me plan a fun birthday party this weekend")  # type: ignore[operator]
    return _CRISIS_TEXT not in {item.content for item in graph.items}


def _bare_voice_retrieval(store: _LeakyStore) -> object:
    """The BARE variant: the voice profile + budget, but NO allowlist provider and
    NO recent-window — the gate element removed. Everything else identical."""
    from persona.graph.retrieval import HybridRetriever

    settings = voice_graph_settings(GraphSettings())
    return make_graph_retrieval(
        retriever=HybridRetriever(store=store, settings=settings),  # type: ignore[arg-type]
        owner_provider=lambda: _OWNER,
        settings=settings,
        max_items=VOICE_NODE_BUDGET,
        # allowlist_provider omitted, recent_window_provider defaulted — BARE.
    )


def test_full_composition_subtracts_even_when_the_store_leaks() -> None:
    # The full voice composition's allowlist plumbing catches a store that ignores
    # the allowlist — the crisis node never reaches the prompt.
    comp = build_voice_graph_retrieval(_LeakyStore(), owner_id=_OWNER)  # type: ignore[arg-type]
    assert _crisis_subtracted(comp.retrieval)


def test_bare_wiring_variant_fails_the_subtraction_impossible_green() -> None:
    # Impossible-green: the SAME adversarial subtraction the full composition PASSES
    # is FAILED by a bare variant — so shipping bare cannot pass this test.
    bare = _bare_voice_retrieval(_LeakyStore())
    assert not _crisis_subtracted(bare)  # bare LEAKS the gate-eligible node
    # Stated as the impossible-green contract: the assertion that guards the real
    # composition raises for the bare one.
    with pytest.raises(AssertionError):
        assert _crisis_subtracted(bare), "bare wiring must fail the gate"


class _ThrowingStore(_LeakyStore):
    """A store whose dense leg raises — the misbehaving-retrieval failure mode."""

    def search_dense(self, *args: object, **kwargs: object) -> list[ConceptNode]:  # noqa: ARG002
        raise RuntimeError("graph backend exploded mid-turn")


@pytest.mark.asyncio
async def test_throwing_retrieval_degrades_to_a_clean_memoryless_turn() -> None:
    # A retrieval that throws must never break the call: the off-loop task errors,
    # and take_graph_if_ready hands back an empty bundle (graph-off), no exception.
    comp = build_voice_graph_retrieval(_ThrowingStore(), owner_id=_OWNER)  # type: ignore[arg-type]
    task = start_graph_retrieval(comp.retrieval, "tell me about my week")
    with contextlib.suppress(Exception):
        await task  # force the task to completion (with its exception) deterministically
    graph = take_graph_if_ready(task)
    assert isinstance(graph, GraphContext)
    assert graph.items == ()  # clean memoryless turn — the call is unaffected


@pytest.mark.asyncio
async def test_recent_window_contextvar_crosses_to_thread_with_retrieval_live() -> None:
    # The live thread-boundary pin: the crisis node is UNRELATED to the query, so a
    # query-only gate would subtract it; it lifts ONLY via the recent-window
    # (set on this context before the off-thread query). If the ContextVar did not
    # cross asyncio.to_thread, the window would be empty in-thread and the node
    # would be subtracted — so its SURVIVAL proves the crossing.
    comp = build_voice_graph_retrieval(_LeakyStore(), owner_id=_OWNER)  # type: ignore[arg-type]
    set_recent_window_from_messages(
        [
            ConversationMessage(
                role="user",
                content="I've been having a severe mental-health crisis and panic attacks",
                created_at=datetime.now(UTC),
            )
        ]
    )
    task = start_graph_retrieval(comp.retrieval, "what's a good recipe for dinner tonight")
    graph = await task  # run to completion in the worker thread
    assert _CRISIS_TEXT in {item.content for item in graph.items}


def test_surfacing_guidance_identity_holds_under_adversary() -> None:
    # Even over the adversarial store, the surfacing provider is the shared K4
    # catalogue — the composition never swaps in a voice-local care text.
    comp = build_voice_graph_retrieval(_LeakyStore(), owner_id=_OWNER)  # type: ignore[arg-type]
    assert comp.surfacing_guidance is surfacing_guidance

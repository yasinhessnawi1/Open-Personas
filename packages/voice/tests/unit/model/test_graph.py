"""V13-T1 — the K4-gated voice graph composition (positive proof).

Proves ``build_voice_graph_retrieval`` composes the SAME gate chat runs — all four
gate elements present, mirrored from ``_build_graph_retrieval``, never a
voice-local variant:

1. **Allowlist subtraction** — a gate-eligible wellbeing node the caller has not
   opened is withheld (the composed ``make_allowlist_provider`` produces the
   positive allowlist, and the store is queried within it).
2. **Recent-window lift** — when the query opens the topic, the gate lifts (no
   subtraction) and the node surfaces.
3. **Surfacing** — the surfacing provider IS the shared K4 ``surfacing_guidance``
   (the same spoken-care catalogue chat uses).
4. **Recency** — a flagged node is banded (ACUTE here) before the gate decides.

Plus the voice adaptations (D-V13-1, non-gate): the fixed-caller owner scope and
the voice profile (traversal OFF, ``VOICE_NODE_BUDGET`` node cap).

The ADVERSARIAL guarantees (a bare-wiring variant must FAIL; a misbehaving
retriever cannot leak; the ContextVar crosses ``asyncio.to_thread`` live; the
real-Postgres subtract-then-lift) are V13-T2's — this task proves the composition
is wired correctly, on a fake store, at unit speed.
"""

from __future__ import annotations

from datetime import UTC, datetime

from persona.graph.models import ConceptNode, NodeKind, NodeProvenance
from persona.schema.chunks import WriteSource
from persona.wellbeing_policy import WellbeingCategory
from persona_runtime.wellbeing import surfacing_guidance as shared_surfacing_guidance
from persona_voice.model.graph import build_voice_graph_retrieval

_OWNER = "user-vm-1"


def _node(node_id: str, content: str, *, category: str | None = None) -> ConceptNode:
    """A recent (ACUTE) owner node; ``category`` tags it wellbeing-sensitive."""
    now = datetime.now(UTC)
    return ConceptNode(
        id=node_id,
        node_kind=NodeKind.CONCEPT,
        concept_name=node_id,
        content=content,
        wellbeing_category=category,
        distance=0.05,  # highly relevant — would inject if allowed
        provenance=(NodeProvenance(source=WriteSource.SYSTEM, persona_id="p1", written_at=now),),
        created_at=now,
    )


class _FakeGraphStore:
    """A minimal well-behaved GraphStore for the composition (traversal OFF).

    Records the owner ids it is queried with and the allowlists ``search_dense``
    receives, so the test can prove the fixed-owner scope + that the K4 allowlist
    was computed and passed. ``neighbors`` raises — the voice profile disables
    traversal, so it must never be called.
    """

    def __init__(self, nodes: list[ConceptNode], flagged: list[ConceptNode]) -> None:
        self._nodes = nodes
        self._flagged = flagged
        self.owners_seen: list[str] = []
        self.received_allowlists: list[set[str] | None] = []
        self.neighbors_calls = 0

    def search_dense(
        self,
        owner_id: str,
        query: str,  # noqa: ARG002 — GraphStore contract
        top_k: int,
        *,
        allowlist: set[str] | None = None,
    ) -> list[ConceptNode]:
        self.owners_seen.append(owner_id)
        self.received_allowlists.append(allowlist)
        pool = self._nodes if allowlist is None else [n for n in self._nodes if n.id in allowlist]
        return pool[:top_k]

    def search_fts(
        self,
        owner_id: str,
        query: str,  # noqa: ARG002 — GraphStore contract
        top_k: int,  # noqa: ARG002 — GraphStore contract
    ) -> list[ConceptNode]:
        self.owners_seen.append(owner_id)
        return []

    def flagged_nodes(self, owner_id: str) -> list[ConceptNode]:
        self.owners_seen.append(owner_id)
        return self._flagged

    def node_ids_for_owner(self, owner_id: str) -> list[str]:
        self.owners_seen.append(owner_id)
        return [n.id for n in self._nodes]

    def neighbors(self, *args: object, **kwargs: object) -> list[object]:  # noqa: ARG002 — must never be called
        self.neighbors_calls += 1
        raise AssertionError("traversal must be OFF on the voice profile")


def _crisis_and_hobby() -> _FakeGraphStore:
    crisis = _node(
        "crisis",
        "a severe mental-health crisis with panic attacks",
        category=WellbeingCategory.MENTAL_HEALTH_CRISIS.value,
    )
    hobby = _node("hobby", "enjoys hiking on weekends")
    return _FakeGraphStore(nodes=[crisis, hobby], flagged=[crisis])


def test_surfacing_guidance_is_the_shared_k4_provider() -> None:
    # Element 3, not-a-variant: the voice surfacing slot rides the SAME care-text
    # provider chat's composition passes — identity, not a re-implementation.
    comp = build_voice_graph_retrieval(_crisis_and_hobby(), owner_id=_OWNER)
    assert comp.surfacing_guidance is shared_surfacing_guidance


def test_retrieval_is_owner_scoped_to_the_fixed_caller() -> None:
    # The voice owner adaptation (D-V13-1): a fixed caller id, not chat's request
    # ContextVar. Every store read is confined to the one caller.
    store = _crisis_and_hobby()
    comp = build_voice_graph_retrieval(store, owner_id=_OWNER)
    comp.retrieval("tell me about my week")
    assert store.owners_seen  # the retrieval actually queried the store
    assert set(store.owners_seen) == {_OWNER}


def test_gate_eligible_unopened_node_is_subtracted() -> None:
    # Elements 1+4: an ACUTE gate-eligible node the caller has NOT opened is gated —
    # the composed allowlist provider yields owner_nodes − {crisis}, and the store is
    # queried within it, so the crisis node never reaches the prompt context.
    store = _crisis_and_hobby()
    comp = build_voice_graph_retrieval(store, owner_id=_OWNER)
    graph = comp.retrieval("help me plan a fun birthday party this weekend")
    contents = {item.content for item in graph.items}
    assert "a severe mental-health crisis with panic attacks" not in contents
    assert "enjoys hiking on weekends" in contents
    # The subtraction was computed by the K4 gate (a positive allowlist excluding
    # the crisis node was passed to the store) — proof the allowlist element is wired.
    passed = [a for a in store.received_allowlists if a is not None]
    assert passed
    assert all("crisis" not in a for a in passed)


def test_gate_lifts_when_the_caller_opens_the_topic() -> None:
    # Element 2: the caller opens the topic in the query → the gate lifts (no
    # subtraction, allowlist None) → the node surfaces. The recent-window lift path.
    store = _crisis_and_hobby()
    comp = build_voice_graph_retrieval(store, owner_id=_OWNER)
    graph = comp.retrieval("I've been having a severe mental-health crisis and panic attacks")
    contents = {item.content for item in graph.items}
    assert "a severe mental-health crisis with panic attacks" in contents
    assert None in store.received_allowlists  # nothing gated ⇒ no subtraction


def test_voice_profile_disables_traversal() -> None:
    # The voice profile (D-K3-6): traversal OFF — ``neighbors`` is never called, so
    # the tighter/cheaper slice holds (the fake raises if it is).
    store = _crisis_and_hobby()
    comp = build_voice_graph_retrieval(store, owner_id=_OWNER)
    comp.retrieval("I've been having a severe mental-health crisis and panic attacks")
    assert store.neighbors_calls == 0


def test_node_budget_caps_the_voice_slice() -> None:
    # The voice node budget: at most VOICE_NODE_BUDGET items reach the prompt.
    from persona_runtime.graph_voice import VOICE_NODE_BUDGET

    nodes = [_node(f"n{i}", f"fact number {i} about hiking and travel") for i in range(10)]
    store = _FakeGraphStore(nodes=nodes, flagged=[])
    comp = build_voice_graph_retrieval(store, owner_id=_OWNER)
    graph = comp.retrieval("tell me about hiking and travel")
    assert len(graph.items) <= VOICE_NODE_BUDGET

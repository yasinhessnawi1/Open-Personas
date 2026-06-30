"""T7 — the K4 seam (D-K3-X-k4-seam): subtraction-by-construction + the slot.

Two halves, proven not asserted:

- **Subtraction by construction (adversarial).** K4 subtracts wellbeing-sensitive
  nodes via the allowlist. K1 enforces it post-fusion (primary), and K3 re-drops
  outside-allowlist nodes as defense in depth (K3 is the last surface before the
  model). The adversarial proof feeds a *misbehaving* retriever that ignores the
  allowlist and returns a subtracted node anyway — and shows it STILL cannot
  reach the GraphContext or the rendered prompt.

- **The surfacing slot, reserved-never-built (A2-seam discipline).** The
  ``graph_surfacing_guidance`` slot exists and is consulted only for
  wellbeing-categorized nodes; K3 owns the slot, K4 owns the policy + care text.
  A categorized node's category is NEVER rendered raw to the model. K4's actual
  care logic is NOT built here — the stub fills nothing until K4 lands.
"""

from __future__ import annotations

from datetime import UTC, datetime

from persona.graph.config import GraphSettings
from persona.graph.fusion import HybridResult
from persona.graph.models import ConceptNode, NodeKind, NodeProvenance
from persona.schema.chunks import WriteSource
from persona.schema.persona import Persona, PersonaIdentity
from persona_runtime.graph_selection import make_graph_retrieval
from persona_runtime.prompt import GraphRecency, PromptBuilder, RetrievedContext

_NOW = datetime(2026, 6, 25, tzinfo=UTC)


def _node(node_id: str, content: str, *, wellbeing_category: str | None = None) -> ConceptNode:
    return ConceptNode(
        id=node_id,
        node_kind=NodeKind.CONCEPT,
        concept_name=node_id,
        content=content,
        wellbeing_category=wellbeing_category,
        distance=0.05,  # highly relevant — would inject if allowed
        provenance=(
            NodeProvenance(source=WriteSource.PERSONA_SELF, persona_id="kai", written_at=_NOW),
        ),
        created_at=_NOW,
    )


def _result(node: ConceptNode) -> HybridResult:
    return HybridResult(node=node, score=0.5, rank=1, dense_rank=1)


class _AllowlistHonouringRetriever:
    """A well-behaved retriever: filters to the allowlist (K1's contract)."""

    def __init__(self, nodes: list[ConceptNode]) -> None:
        self._nodes = nodes
        self.calls: list[set[str] | None] = []

    def retrieve(
        self,
        owner_id: str,  # noqa: ARG002 — contract
        query: str,  # noqa: ARG002 — contract
        *,
        allowlist: set[str] | None = None,
        top_k: int | None = None,  # noqa: ARG002 — contract
    ) -> list[HybridResult]:
        self.calls.append(allowlist)
        nodes = self._nodes if allowlist is None else [n for n in self._nodes if n.id in allowlist]
        return [_result(n) for n in nodes]


class _MisbehavingRetriever:
    """An ADVERSARY: ignores the allowlist and returns everything regardless."""

    def __init__(self, nodes: list[ConceptNode]) -> None:
        self._nodes = nodes

    def retrieve(
        self,
        owner_id: str,  # noqa: ARG002 — contract
        query: str,  # noqa: ARG002 — contract
        *,
        allowlist: set[str] | None = None,  # noqa: ARG002 — deliberately ignored
        top_k: int | None = None,  # noqa: ARG002 — contract
    ) -> list[HybridResult]:
        return [_result(n) for n in self._nodes]


def _settings() -> GraphSettings:
    return GraphSettings(inject_similarity_floor=0.66)


def _persona() -> Persona:
    return Persona(
        persona_id="astrid",
        identity=PersonaIdentity(name="Astrid", role="assistant", background="."),
        tools=[],
    )


class TestAllowlistThreading:
    def test_allowlist_passed_to_the_retriever(self) -> None:
        retriever = _AllowlistHonouringRetriever([_node("ok", "Likes tea.")])
        retrieve = make_graph_retrieval(
            retriever=retriever,
            owner_provider=lambda: "user-A",
            settings=_settings(),
            now=lambda: _NOW,
            allowlist_provider=lambda _ctx: {"ok"},
        )
        retrieve("q")
        assert retriever.calls == [{"ok"}]  # the K4-permitted set reached K1

    def test_no_provider_passes_none_whole_graph(self) -> None:
        retriever = _AllowlistHonouringRetriever([_node("ok", "Likes tea.")])
        retrieve = make_graph_retrieval(
            retriever=retriever,
            owner_provider=lambda: "user-A",
            settings=_settings(),
            now=lambda: _NOW,
        )
        retrieve("q")
        assert retriever.calls == [None]


class TestSubtractionByConstruction:
    def test_subtracted_node_excluded_by_a_well_behaved_retriever(self) -> None:
        nodes = [_node("safe", "Likes hiking."), _node("sensitive", "A wellbeing matter.")]
        retrieve = make_graph_retrieval(
            retriever=_AllowlistHonouringRetriever(nodes),
            owner_provider=lambda: "user-A",
            settings=_settings(),
            now=lambda: _NOW,
            allowlist_provider=lambda _ctx: {"safe"},  # K4 subtracts "sensitive"
        )
        ids = {item.content for item in retrieve("q").items}
        assert "Likes hiking." in ids
        assert "A wellbeing matter." not in ids

    def test_subtracted_node_unreachable_even_if_the_retriever_misbehaves(self) -> None:
        # The adversarial core: K1 regressed and leaks the subtracted node — K3's
        # last-surface defense-in-depth drop still removes it.
        nodes = [_node("safe", "Likes hiking."), _node("sensitive", "A wellbeing matter.")]
        retrieve = make_graph_retrieval(
            retriever=_MisbehavingRetriever(nodes),  # ignores the allowlist
            owner_provider=lambda: "user-A",
            settings=_settings(),
            now=lambda: _NOW,
            allowlist_provider=lambda _ctx: {"safe"},
        )
        contents = {item.content for item in retrieve("q").items}
        assert "A wellbeing matter." not in contents  # structurally dropped at K3
        assert "Likes hiking." in contents

    def test_subtracted_node_never_reaches_the_rendered_prompt(self) -> None:
        # End to end: the last surface before the model. Even an adversarial
        # retriever cannot put the subtracted node's content into the prompt.
        nodes = [_node("safe", "Likes hiking."), _node("sensitive", "A wellbeing matter.")]
        retrieve = make_graph_retrieval(
            retriever=_MisbehavingRetriever(nodes),
            owner_provider=lambda: "user-A",
            settings=_settings(),
            now=lambda: _NOW,
            allowlist_provider=lambda _ctx: {"safe"},
        )
        ctx = RetrievedContext(graph=retrieve("q"))
        msgs = PromptBuilder().build(
            _persona(), ctx, history=[], skill_index="", user_message="q", max_tokens=8000
        )
        system = msgs[0].content
        assert "A wellbeing matter." not in system
        assert "Likes hiking." in system

    def test_empty_allowlist_subtracts_everything(self) -> None:
        retrieve = make_graph_retrieval(
            retriever=_MisbehavingRetriever([_node("x", "Anything.")]),
            owner_provider=lambda: "user-A",
            settings=_settings(),
            now=lambda: _NOW,
            allowlist_provider=lambda _ctx: set(),  # K4 subtracts all
        )
        assert retrieve("q").items == ()


class TestSurfacingSlotReserved:
    def test_categorized_node_category_is_never_rendered_raw(self) -> None:
        # A surfaced (allowed) sensitive node uses its content, but the category
        # tag must never leak into the prompt as a raw label.
        retrieve = make_graph_retrieval(
            retriever=_AllowlistHonouringRetriever(
                [_node("n", "Has a sensitive circumstance.", wellbeing_category="eating_disorder")]
            ),
            owner_provider=lambda: "user-A",
            settings=_settings(),
            now=lambda: _NOW,
            allowlist_provider=lambda _ctx: {"n"},  # allowed (not subtracted)
        )
        ctx = RetrievedContext(graph=retrieve("q"))
        msgs = PromptBuilder().build(
            _persona(), ctx, history=[], skill_index="", user_message="q", max_tokens=8000
        )
        system = msgs[0].content
        assert "eating_disorder" not in system  # category never rendered raw
        assert "Has a sensitive circumstance." in system  # content surfaced

    def test_care_slot_consulted_only_for_categorized_nodes(self) -> None:
        seen: list[str] = []

        def care(category: str, recency: GraphRecency) -> str | None:  # noqa: ARG001 — recency unused here
            seen.append(category)
            return "be gentle here"

        retrieve = make_graph_retrieval(
            retriever=_AllowlistHonouringRetriever(
                [
                    _node("plain", "Likes tea."),
                    _node("flagged", "A matter.", wellbeing_category="crisis"),
                ]
            ),
            owner_provider=lambda: "user-A",
            settings=_settings(),
            now=lambda: _NOW,
        )
        ctx = RetrievedContext(graph=retrieve("q"))
        msgs = PromptBuilder().build(
            _persona(),
            ctx,
            history=[],
            skill_index="",
            user_message="q",
            max_tokens=8000,
            graph_surfacing_guidance=care,
        )
        system = msgs[0].content
        assert seen == ["crisis"]  # consulted ONLY for the categorized node
        assert "(be gentle here)" in system  # K4's care text rides the slot

    def test_slot_is_a_noop_stub_without_a_provider(self) -> None:
        # Reserved-never-built: no provider → no care text, no leak, content used.
        # The category value is a distinctive sentinel so the no-leak assertion tests
        # exactly that the K4 category label never reaches the prompt, immune to
        # legitimate prompt vocabulary (V11's character-lock wellbeing carve-out, for
        # one, properly speaks of "crisis"/"distress" — that is not a K4 tag leak).
        sentinel_category = "wb_cat_sentinel"
        retrieve = make_graph_retrieval(
            retriever=_AllowlistHonouringRetriever(
                [_node("n", "A matter.", wellbeing_category=sentinel_category)]
            ),
            owner_provider=lambda: "user-A",
            settings=_settings(),
            now=lambda: _NOW,
        )
        ctx = RetrievedContext(graph=retrieve("q"))
        msgs = PromptBuilder().build(
            _persona(), ctx, history=[], skill_index="", user_message="q", max_tokens=8000
        )
        system = msgs[0].content
        assert sentinel_category not in system
        assert "A matter." in system

"""R9-253: the wellbeing gate holds a disclosure after its tagged node is forgotten.

The gate used to derive "this interaction carried a disclosure" from the tagged nodes that
exist now. A user forgetting the tagged node therefore un-gated the untagged node synthesis
learned beside it (the B3 dinner-party shape), exactly when the user had signalled that the
topic is sensitive. The gate now also reads the durable disclosure fact, which no node or
conversation deletion clears. Unit level with fakes; the SQL of the fact store is proven on
Postgres in ``tests/integration/test_wellbeing_disclosure_store.py``.
"""

# ruff: noqa: ARG002, ANN401 (duck-typed store and reader fakes)
from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from persona.graph.models import ConceptNode, NodeKind, NodeProvenance
from persona.initiative import CitationKind, GroundingCitation
from persona.schema.chunks import WriteSource
from persona_api.events.candidate_producer import ApiEventWellbeingCheck
from persona_api.initiative.pipeline_wiring import ApiWellbeingSubjectCheck
from persona_api.initiative.readers import ApiScanGraphReader
from persona_api.initiative.wellbeing_gate import ConversationWellbeingGate

if TYPE_CHECKING:
    from collections.abc import Iterable

_NOW = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)


def _node(node_id: str, source: str, *, wellbeing: str | None = None) -> ConceptNode:
    return ConceptNode(
        id=node_id,
        node_kind=NodeKind.FACT,
        concept_name=node_id,
        content=f"content of {node_id}",
        wellbeing_category=wellbeing,
        provenance=(
            NodeProvenance(
                source=WriteSource.PERSONA_SELF,
                interaction_id=source,
                interaction_kind="conversation",
                written_at=_NOW,
            ),
        ),
        created_at=_NOW,
    )


_DISCLOSURE = _node("n-disclosure", "conv-d", wellbeing="disordered_eating")
_DINNER = _node("n-dinner", "conv-d")  # untagged, learned beside the disclosure
_TEACHER = _node("n-teacher", "conv-c")


class _Graph:
    """A graph the user can forget a node from."""

    def __init__(self, *nodes: ConceptNode) -> None:
        self.nodes = {n.id: n for n in nodes}

    def forget(self, node_id: str) -> None:
        del self.nodes[node_id]

    def flagged_nodes(self, owner_id: str) -> list[ConceptNode]:
        return [n for n in self.nodes.values() if n.wellbeing_category]

    def recent_nodes(self, owner_id: str, *, limit: int) -> list[ConceptNode]:
        return [n for n in self.nodes.values() if not n.wellbeing_category]

    def neighbors(self, owner_id: str, node_id: str, **_: Any) -> list[Any]:
        return []

    def get_node(self, owner_id: str, node_id: str) -> ConceptNode | None:
        return self.nodes.get(node_id)


class _Facts:
    """The disclosure facts: per owner, the interactions that ever carried a disclosure."""

    def __init__(self, **by_owner: set[str]) -> None:
        self._by_owner = by_owner
        self.asked: list[tuple[str, set[str]]] = []

    def disclosed(self, owner_id: str, refs: Iterable[str]) -> set[str]:
        wanted = set(refs)
        self.asked.append((owner_id, wanted))
        return self._by_owner.get(owner_id, set()) & wanted


def _forgotten() -> _Graph:
    graph = _Graph(_DISCLOSURE, _DINNER, _TEACHER)
    graph.forget("n-disclosure")
    return graph


def _fresh(monkeypatch: pytest.MonkeyPatch, gate: ConversationWellbeingGate) -> None:
    """Every conversation synthesised: the freshness half is the integration tests'."""
    monkeypatch.setattr(gate, "unsynthesised", lambda _owner, _ids: set())


def test_the_gate_still_holds_a_disclosure_whose_tagged_node_was_forgotten() -> None:
    gate = ConversationWellbeingGate(_forgotten(), disclosures=_Facts(u={"conv-d"}))  # type: ignore[arg-type]
    assert gate.disclosed("u", ["conv-d", "conv-c"]) == {"conv-d"}
    assert gate.gated("u", ["conv-d"], include_unsynthesised=False) == {"conv-d"}


def test_without_the_fact_the_forgotten_disclosure_is_lost() -> None:
    """The R9-253 defect itself, kept as the baseline the fact closes."""
    gate = ConversationWellbeingGate(_forgotten(), disclosures=_Facts())  # type: ignore[arg-type]
    assert gate.disclosed("u", ["conv-d"]) == set()


def test_the_fact_and_the_tagged_nodes_both_count() -> None:
    graph = _Graph(_DISCLOSURE, _TEACHER)
    gate = ConversationWellbeingGate(graph, disclosures=_Facts(u={"conv-c"}))  # type: ignore[arg-type]
    assert gate.disclosed("u", ["conv-d", "conv-c", "conv-x"]) == {"conv-d", "conv-c"}


def test_only_the_asked_refs_of_the_asking_owner_are_gated() -> None:
    facts = _Facts(u={"conv-d"}, someone_else={"conv-c"})
    gate = ConversationWellbeingGate(_Graph(), disclosures=facts)  # type: ignore[arg-type]
    assert gate.disclosed("u", ["conv-c", "conv-x"]) == set()
    assert facts.asked == [("u", {"conv-c", "conv-x"})]


def test_no_refs_asks_nothing() -> None:
    facts = _Facts(u={"conv-d"})
    gate = ConversationWellbeingGate(_Graph(), disclosures=facts)  # type: ignore[arg-type]
    assert gate.gated("u", [], include_unsynthesised=False) == set()
    assert facts.asked == []


def test_the_scan_pool_drops_the_sibling_of_a_forgotten_disclosure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = _forgotten()
    gate = ConversationWellbeingGate(graph, disclosures=_Facts(u={"conv-d"}))  # type: ignore[arg-type]
    _fresh(monkeypatch, gate)
    pool = ApiScanGraphReader(graph, gate).noticing_pool("u", limit=30)  # type: ignore[arg-type]
    assert [n.id for n in pool] == ["n-teacher"]


def test_the_subject_rule_gates_the_sibling_of_a_forgotten_disclosure() -> None:
    graph = _forgotten()
    check = ApiWellbeingSubjectCheck(graph, engine=None, disclosures=_Facts(u={"conv-d"}))  # type: ignore[arg-type]
    cited = (
        GroundingCitation(kind=CitationKind.NODE, ref="n-dinner"),
        GroundingCitation(kind=CitationKind.NODE, ref="n-teacher"),
        GroundingCitation(kind=CitationKind.CONVERSATION, ref="conv-d"),
    )
    assert check.tagged_refs("u", cited, include_unsynthesised=False) == {"n-dinner", "conv-d"}


def test_the_event_door_drops_a_grounding_from_a_forgotten_disclosure() -> None:
    door = ApiEventWellbeingCheck(_forgotten(), disclosures=_Facts(u={"conv-d"}))  # type: ignore[arg-type]
    assert door.is_gated_subject("u", "conversation", "conv-d") is True
    assert door.is_gated_subject("u", "conversation", "conv-c") is False


def test_a_fact_on_a_run_or_a_task_gates_that_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """Direct writes in a run or a task leg are keyed by its id (the kind-agnostic rule)."""
    check = ApiWellbeingSubjectCheck(
        _Graph(), engine=None, disclosures=_Facts(u={"run-7", "task-9"})
    )  # type: ignore[arg-type]
    origins = {"task-1": {"run-7"}, "task-9": set(), "task-2": {"run-8"}}
    monkeypatch.setattr(
        check._gate,  # noqa: SLF001
        "task_origins",
        lambda _owner, ids: {t: origins[t] for t in ids},
    )
    cited = tuple(
        GroundingCitation(kind=CitationKind.TASK, ref=t) for t in ("task-1", "task-9", "task-2")
    )
    assert check.tagged_refs("u", cited, include_unsynthesised=False) == {"task-1", "task-9"}


def test_an_engine_gives_the_gate_the_real_fact_store() -> None:
    from persona_api.initiative.disclosures import PgDisclosureStore  # noqa: PLC0415
    from sqlalchemy import create_engine  # noqa: PLC0415

    engine = create_engine("sqlite://")
    try:
        gate = ConversationWellbeingGate(_Graph(), engine)  # type: ignore[arg-type]
        assert isinstance(gate._disclosures, PgDisclosureStore)  # noqa: SLF001
        assert ConversationWellbeingGate(_Graph())._disclosures is None  # type: ignore[arg-type]  # noqa: SLF001
    finally:
        engine.dispose()

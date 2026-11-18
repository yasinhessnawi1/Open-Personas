"""D-ORCH-11-18-u: which initiative readers honour a release, and which stay strict.

A release lets the persona draw on what a conversation TAUGHT it: the untagged nodes learned
there, as pool nodes, lens targets and the judge's context notes. Everything else stays held:

- a tagged node, by its own tag, wherever it is read (K4's floor);
- the conversation as its own citation or grounding, whose raw transcript holds the
  disclosure (the subject rule and the A7 event door);
- every task that started there, since a task's goal text is never checked against tags
  (the scan's task history, the subject rule's task origins, the event door's task path).
"""

# ruff: noqa: ARG002, ANN401 (duck-typed store and reader fakes)
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from persona.graph.models import ConceptNode, LinkType, NodeKind, NodeProvenance, TypedLink
from persona.initiative import CitationKind, GroundingCitation
from persona.schema.chunks import WriteSource
from persona_api.events.candidate_producer import ApiEventWellbeingCheck
from persona_api.initiative import readers
from persona_api.initiative.disclosures import DisclosureState
from persona_api.initiative.pipeline_wiring import ApiWellbeingSubjectCheck
from persona_api.initiative.readers import ApiScanGraphReader, ApiScanTaskReader, judge_context
from persona_api.initiative.wellbeing_gate import ConversationWellbeingGate

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

_BEFORE = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)
_RELEASED_AT = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
_IN_FORCE = DisclosureState(disclosure_seq=1, released_seq=1, released_at=_RELEASED_AT)


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
                written_at=_BEFORE,
            ),
        ),
        created_at=_BEFORE,
    )


# conv-d carried the disclosure and is released; conv-h carried one and is still held.
_TAGGED = _node("n-tag", "conv-d", wellbeing="disordered_eating")
_DINNER = _node("n-dinner", "conv-d")
_HELD = _node("n-held", "conv-h")
_ANCHOR = _node("n-anchor", "conv-c")


class _Graph:
    def __init__(
        self,
        *nodes: ConceptNode,
        pool: tuple[str, ...] | None = None,
        links: dict[str, list[str]] | None = None,
    ) -> None:
        self.nodes = {n.id: n for n in nodes}
        self._pool = pool
        self._links = links or {}

    def flagged_nodes(self, owner_id: str) -> list[ConceptNode]:
        return [n for n in self.nodes.values() if n.wellbeing_category]

    def recent_nodes(self, owner_id: str, *, limit: int) -> list[ConceptNode]:
        """The store's SQL drops tagged nodes; the fake mirrors it."""
        ids = self._pool if self._pool is not None else tuple(self.nodes)
        return [self.nodes[i] for i in ids if not self.nodes[i].wellbeing_category]

    def neighbors(self, owner_id: str, node_id: str, **_: Any) -> list[Any]:
        return [
            (
                TypedLink(
                    id=f"e-{node_id}-{dst}",
                    src_node_id=node_id,
                    dst_node_id=dst,
                    link_type=LinkType.TEMPORAL,
                    created_at=_BEFORE,
                ),
                self.nodes[dst],
            )
            for dst in self._links.get(node_id, [])
        ]

    def get_node(self, owner_id: str, node_id: str) -> ConceptNode | None:
        return self.nodes.get(node_id)


class _Facts:
    def __init__(self) -> None:
        self._states = {
            "conv-d": _IN_FORCE,
            "conv-h": DisclosureState(disclosure_seq=1, released_seq=None, released_at=None),
        }

    def disclosed(self, owner_id: str, refs: Iterable[str]) -> set[str]:
        return set(self._states) & set(refs)

    def states(self, owner_id: str, refs: Iterable[str]) -> dict[str, DisclosureState]:
        return {r: s for r, s in self._states.items() if r in set(refs)}


_FACTS = _Facts()


def _synthesised(monkeypatch: pytest.MonkeyPatch, gate: ConversationWellbeingGate) -> None:
    monkeypatch.setattr(gate, "unsynthesised", lambda _owner, _ids: set())


def _gate(monkeypatch: pytest.MonkeyPatch, graph: _Graph) -> ConversationWellbeingGate:
    gate = ConversationWellbeingGate(graph, object(), disclosures=_FACTS, releases=_FACTS)  # type: ignore[arg-type]
    _synthesised(monkeypatch, gate)
    return gate


def test_the_pool_draws_on_what_a_released_conversation_taught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = _Graph(_TAGGED, _DINNER, _HELD, _ANCHOR)
    pool = ApiScanGraphReader(graph, _gate(monkeypatch, graph)).noticing_pool("u", limit=30)  # type: ignore[arg-type]
    assert sorted(n.id for n in pool) == ["n-anchor", "n-dinner"]


def test_a_lens_target_from_a_released_conversation_is_shown_and_a_tagged_one_never(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutant 6: the tagged target is dropped by its own tag, whatever the release says."""
    graph = _Graph(
        _TAGGED, _DINNER, _ANCHOR, pool=("n-anchor",), links={"n-anchor": ["n-dinner", "n-tag"]}
    )
    pool = ApiScanGraphReader(graph, _gate(monkeypatch, graph)).noticing_pool("u", limit=30)  # type: ignore[arg-type]
    assert [(n.id, [link.target_id for link in n.links]) for n in pool] == [
        ("n-anchor", ["n-dinner"])
    ]


def _check(monkeypatch: pytest.MonkeyPatch, graph: _Graph) -> ApiWellbeingSubjectCheck:
    check = ApiWellbeingSubjectCheck(
        graph,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        disclosures=_FACTS,
        releases=_FACTS,
    )
    gate = check._gate  # noqa: SLF001
    _synthesised(monkeypatch, gate)
    origins = {"task-from-d": {"conv-d"}, "task-other": {"conv-c"}}
    monkeypatch.setattr(gate, "task_origins", lambda _o, ids: {t: origins[t] for t in ids})
    return check


def test_the_subject_rule_lifts_a_node_learned_in_a_released_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    check = _check(monkeypatch, _Graph(_TAGGED, _DINNER, _HELD))
    cited = tuple(GroundingCitation(kind=CitationKind.NODE, ref=n) for n in ("n-dinner", "n-held"))
    assert check.tagged_refs("u", cited, include_unsynthesised=True) == {"n-held"}


def test_the_subject_rule_keeps_the_tagged_node_the_conversation_and_its_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutant 7: the conversation as its own citation feeds the judge its raw transcript."""
    check = _check(monkeypatch, _Graph(_TAGGED, _DINNER))
    cited = (
        GroundingCitation(kind=CitationKind.NODE, ref="n-tag"),
        GroundingCitation(kind=CitationKind.CONVERSATION, ref="conv-d"),
        GroundingCitation(kind=CitationKind.TASK, ref="task-from-d"),
        GroundingCitation(kind=CitationKind.TASK, ref="task-other"),
    )
    assert check.tagged_refs("u", cited, include_unsynthesised=True) == {
        "n-tag",
        "conv-d",
        "task-from-d",
    }


def test_the_event_door_keeps_a_released_conversation_and_its_tasks_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = _Graph(_TAGGED, _DINNER)
    door = ApiEventWellbeingCheck(graph, object(), disclosures=_FACTS)  # type: ignore[arg-type]
    gate = door._gate  # noqa: SLF001
    _synthesised(monkeypatch, gate)
    gate._releases = _FACTS  # noqa: SLF001 - a release reader that would lift it if asked
    monkeypatch.setattr(gate, "task_origins", lambda _o, ids: {t: {"conv-d"} for t in ids})
    assert door.is_gated_subject("u", "conversation", "conv-d") is True
    assert door.is_gated_subject("u", "task", "task-from-d") is True


@dataclass
class _Task:
    id: str
    conversation_id: str | None
    run_ids: list[str] = field(default_factory=list)
    contract: Any = field(default_factory=lambda: SimpleNamespace(goal="a goal"))
    state: Any = field(default_factory=lambda: SimpleNamespace(value="active"))


def test_the_scan_keeps_a_task_begun_in_a_released_conversation_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its goal text is never checked against tags, so a release does not lift it."""
    tasks = [_Task("task-from-d", "conv-d"), _Task("task-other", "conv-c")]

    class _Reader:
        def __init__(self, *_: Any) -> None: ...

        def list_active(self) -> list[_Task]:
            return tasks

        def list_recent_terminal(self, *, limit: int) -> list[_Task]:
            return []

        def get_latest_checkpoint(self, task_id: str) -> None:
            return None

    monkeypatch.setattr(readers, "APITaskStateReader", _Reader)
    graph = _Graph(_TAGGED)
    reader = ApiScanTaskReader(object(), object(), _gate(monkeypatch, graph))  # type: ignore[arg-type]
    assert [t.id for t in reader.task_history("u", limit=10)] == ["task-other"]


def test_the_judge_reads_context_notes_learned_in_a_released_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def note(content: str, source: str) -> Any:
        return SimpleNamespace(
            id=content,
            content=content,
            created_at=_BEFORE,
            provenance=[{"interaction_id": source}],
        )

    rows = [note("from released", "conv-d"), note("from held", "conv-h")]

    class _Conn:
        def execute(self, *_: Any) -> Any:
            return SimpleNamespace(all=lambda: rows)

    @contextmanager
    def connection(_engine: Any, _owner: str) -> Iterator[_Conn]:
        yield _Conn()

    monkeypatch.setattr(readers, "rls_connection", connection)
    gate = _gate(monkeypatch, _Graph(_TAGGED))
    notes = judge_context(object(), gate, "u", "n-anchor", limit=5)  # type: ignore[arg-type]
    assert [n.content for n in notes] == ["from released"]


def test_the_judge_reads_no_notes_from_a_released_conversation_synthesis_has_not_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR #44 delta review N1: the judge's context asks for freshness on the honoured path."""

    def note(content: str, source: str) -> Any:
        return SimpleNamespace(
            id=content, content=content, created_at=_BEFORE, provenance=[{"interaction_id": source}]
        )

    rows = [note("from released, unread", "conv-d"), note("from elsewhere", "conv-c")]

    class _Conn:
        def execute(self, *_: Any) -> Any:
            return SimpleNamespace(all=lambda: rows)

    @contextmanager
    def connection(_engine: Any, _owner: str) -> Iterator[_Conn]:
        yield _Conn()

    monkeypatch.setattr(readers, "rls_connection", connection)
    gate = _gate(monkeypatch, _Graph(_TAGGED))
    monkeypatch.setattr(gate, "unsynthesised", lambda _owner, ids: {"conv-d"} & set(ids))
    notes = judge_context(object(), gate, "u", "n-anchor", limit=5)  # type: ignore[arg-type]
    assert [n.content for n in notes] == ["from elsewhere"]


def test_the_judges_context_read_drops_tagged_notes_in_sql() -> None:
    """Mutant 6: the tagged filter is the query's, independent of any release."""
    sql = str(readers._CONTEXT_SQL)  # noqa: SLF001
    assert "g.wellbeing_category IS NULL" in sql


def _scan_candidate(*node_refs: str) -> Any:
    from persona.initiative import (  # noqa: PLC0415
        CandidateSource,
        InitiativeCandidate,
        InitiativeTrigger,
        PlannedStep,
        Urgency,
    )
    from persona.tools.categories import ActionCategory  # noqa: PLC0415

    return InitiativeCandidate(
        observation="The dinner is on Thursday.",
        citations=tuple(GroundingCitation(kind=CitationKind.NODE, ref=r) for r in node_refs),
        trigger=InitiativeTrigger.APPROACHING_COMMITMENT,
        why_now="The date entered the horizon.",
        plan=(PlannedStep(description="draft", categories=frozenset({ActionCategory.DRAFT})),),
        next_step="Draft a note.",
        value=0.9,
        acceptance=0.8,
        urgency=Urgency.BATCH,
        source=CandidateSource.SCAN,
        owner_id="u",
        persona_id="p1",
        prompt_version="a5-scan-v12",
        scanned_at=_BEFORE,
    )


@pytest.mark.parametrize(
    ("node", "unsynthesised", "expected"),
    [
        ("n-dinner", {"conv-d"}, "wait"),  # released, synthesis behind: WAIT, not expired
        ("n-dinner", set(), "clear"),  # released and synthesised: free to go
        ("n-held", set(), "disclosed"),  # never released: never goes out
    ],
)
def test_a_held_notice_from_a_released_conversation_waits_for_synthesis(
    monkeypatch: pytest.MonkeyPatch, node: str, unsynthesised: set[str], expected: str
) -> None:
    """PR #44 review LOW-3, through the pipeline's own held-notice rule."""
    from persona_runtime.initiative.pipeline import held_notice_subject  # noqa: PLC0415

    check = _check(monkeypatch, _Graph(_TAGGED, _DINNER, _HELD))
    monkeypatch.setattr(
        check._gate,  # noqa: SLF001
        "unsynthesised",
        lambda _owner, ids: unsynthesised & set(ids),
    )
    got = held_notice_subject(check, "u", _scan_candidate(node))
    assert got.value == expected


def test_the_pool_holds_a_released_conversation_until_synthesis_reads_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-ORCH-11-18-u (3): a release never goes out ahead of synthesis."""
    graph = _Graph(_TAGGED, _DINNER, _ANCHOR)
    gate = _gate(monkeypatch, graph)
    monkeypatch.setattr(gate, "unsynthesised", lambda _owner, ids: {"conv-d"} & set(ids))
    pool = ApiScanGraphReader(graph, gate).noticing_pool("u", limit=30)  # type: ignore[arg-type]
    assert [n.id for n in pool] == ["n-anchor"]

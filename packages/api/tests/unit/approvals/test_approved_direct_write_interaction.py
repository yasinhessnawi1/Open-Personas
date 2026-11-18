"""An approved ``record_user_fact`` replay names its task and records the fact (R9-253 review).

A category policy can gate the direct write into an approval. The approved write is then
replayed verbatim by the resolver, off the leg, where nothing was bound: it landed tagged,
with no interaction id and no disclosure fact, so its siblings were not gated. Every proposal
belongs to the leg of one task (``approval_proposals.task_id`` is required), so the replay is
bound to that task, the same interaction the leg itself was bound to.

Driven through the resolver's real execute path, the real ``ToolboxActionExecutor``, a real
``Toolbox`` and the real ``record_user_fact`` tool; the stores are stand-ins.
"""

# ruff: noqa: ARG002 - the doubles ignore some args by design.

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from persona.approvals import ActionProposal, ProposalStatus
from persona.graph.protocol import KnowledgeCandidate, MergeAction, MergeOutcome
from persona.tools import ActionCategory
from persona.tools.toolbox import Toolbox
from persona_api.approvals import ApprovalResolver
from persona_api.approvals.action_executor import ToolboxActionExecutor
from persona_runtime.extraction.direct_write import make_record_user_fact_tool
from persona_runtime.interaction_scope import current_interaction

_NOW = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)
_OWNER = "user_approver"
_PERSONA = "persona_approver"
_TASK = "task_gated"


class _Graph:
    def __init__(self) -> None:
        self.merged: list[KnowledgeCandidate] = []

    def merge(self, owner_id: str, candidate: KnowledgeCandidate) -> MergeOutcome:
        self.merged.append(candidate)
        return MergeOutcome(action=MergeAction.CREATED, node_id=f"{owner_id}::node::00000001")


class _Facts:
    def __init__(self) -> None:
        self.recorded: list[tuple[str, str, str]] = []

    def record(self, owner_id: str, persona_id: str, interaction_id: str) -> bool:
        self.recorded.append((owner_id, persona_id, interaction_id))
        return True


class _Approvals:
    def transition_proposal(self, owner_id: str, proposal_id: str, **_k: Any) -> object:  # noqa: ANN401
        return object()  # every CAS wins


def _resolver(graph: _Graph, facts: _Facts, monkeypatch: pytest.MonkeyPatch) -> ApprovalResolver:
    tool = make_record_user_fact_tool(
        graph_store=graph,  # type: ignore[arg-type]
        owner_provider=lambda: _OWNER,
        persona_id=_PERSONA,
        interaction_provider=current_interaction,
        disclosures=facts,
    )
    toolbox = Toolbox([tool], allow_list=["record_user_fact"])

    async def build() -> Toolbox:
        return toolbox

    resolver = ApprovalResolver(
        approvals=_Approvals(),  # type: ignore[arg-type]
        tasks=None,  # type: ignore[arg-type]
        checkpoints=None,  # type: ignore[arg-type]
        continuation=None,  # type: ignore[arg-type]
        interpreter=None,  # type: ignore[arg-type]
        executor=ToolboxActionExecutor(build),
        notifier=None,  # type: ignore[arg-type]
    )
    # The checkpoint and the resume are the resolver's own tests; here only the replay matters.
    monkeypatch.setattr(resolver, "_write_resolution_checkpoint", lambda *_a, **_k: None)
    monkeypatch.setattr(resolver, "_resume_or_hold", lambda *_a, **_k: (True, None))
    return resolver


def _proposal(arguments: dict[str, Any]) -> ActionProposal:
    return ActionProposal(
        proposal_id="prop-1",
        task_id=_TASK,
        owner_id=_OWNER,
        persona_id=_PERSONA,
        categories=frozenset({ActionCategory.EXTERNAL_MUTATE}),
        tool_name="record_user_fact",
        arguments=arguments,
        description="Remember something about you",
        status=ProposalStatus.PENDING,
        created_at=_NOW,
    )


@pytest.mark.asyncio
async def test_an_approved_disclosure_names_its_task_and_records_the_fact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph, facts = _Graph(), _Facts()
    resolver = _resolver(graph, facts, monkeypatch)
    arguments = {"fact": "I have been struggling with eating", "category": "disordered_eating"}

    outcome = await resolver._execute_and_resume(  # noqa: SLF001
        _OWNER, _proposal(arguments), arguments, "yes", _NOW, carries_edit=False
    )

    assert outcome.executed is True
    assert facts.recorded == [(_OWNER, _PERSONA, _TASK)]
    (written,) = graph.merged
    assert (written.provenance.interaction_id, written.provenance.interaction_kind) == (
        _TASK,
        "task",
    )
    assert current_interaction() is None, "the replay's binding ends with it"


@pytest.mark.asyncio
async def test_a_replay_that_fails_still_ends_its_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph, facts = _Graph(), _Facts()
    resolver = _resolver(graph, facts, monkeypatch)

    def broken(*_a: object, **_k: object) -> None:
        raise RuntimeError("checkpoint store down")

    monkeypatch.setattr(resolver, "_write_resolution_checkpoint", broken)
    arguments = {"fact": "a durable fact"}
    with pytest.raises(RuntimeError, match="checkpoint store down"):
        await resolver._execute_and_resume(  # noqa: SLF001
            _OWNER, _proposal(arguments), arguments, "yes", _NOW, carries_edit=False
        )
    assert graph.merged[0].provenance.interaction_id == _TASK
    assert current_interaction() is None

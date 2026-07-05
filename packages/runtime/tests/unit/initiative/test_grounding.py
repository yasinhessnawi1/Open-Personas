"""Unit tests — the mechanical grounding check (Spec A5, T4; A5-D-2, criterion 2).

Deterministic: a fake source + a stub judge backend. Pins the T4 bars —
resolution is pure-mechanical (merged/deleted/missing refs discard BEFORE the
judge runs), the judge decides only within the mechanically-admitted set (a
YES with a conjured quote is discarded by substring check), and every failure
mode is fail-soft (discard, never a raise, never a pass-through).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from persona.backends.types import ChatResponse, TokenUsage
from persona.initiative import (
    CandidateSource,
    CitationKind,
    GroundingCitation,
    InitiativeCandidate,
    InitiativeTrigger,
    PlannedStep,
    Urgency,
)
from persona.tools.categories import ActionCategory
from persona_runtime.initiative import (
    GroundingChecker,
    GroundingRejection,
)

_HEARING_CONTENT = "The user's custody hearing is on Friday 10 July."
_TASK_CONTENT = "Monitoring task: collected the required forms; hearing prep pending."


class _FakeSource:
    """A grounding source over an explicit dict; merged nodes read as None."""

    def __init__(self) -> None:
        self.nodes: dict[str, str | None] = {"node-hearing": _HEARING_CONTENT}
        self.conversations: dict[str, str] = {}
        self.tasks: dict[str, str] = {"task-prep": _TASK_CONTENT}
        self.raises = False

    def node_content(self, owner_id: str, node_id: str) -> str | None:  # noqa: ARG002
        if self.raises:
            msg = "source unavailable"
            raise RuntimeError(msg)
        return self.nodes.get(node_id)

    def conversation_content(self, owner_id: str, conversation_id: str) -> str | None:  # noqa: ARG002
        return self.conversations.get(conversation_id)

    def task_content(self, owner_id: str, task_id: str) -> str | None:  # noqa: ARG002
        return self.tasks.get(task_id)


class _StubJudge:
    """A judge backend returning canned content (or raising); records prompts."""

    def __init__(self, content: str = "", *, raises: bool = False) -> None:
        self._content = content
        self._raises = raises
        self.calls = 0
        self.last_user_prompt: str | None = None

    @property
    def provider_name(self) -> str:
        return "anthropic"

    @property
    def model_name(self) -> str:
        return "claude-haiku-4-5-20251001"

    async def chat(self, messages: Any, **kwargs: Any) -> ChatResponse:  # noqa: ANN401, ARG002
        self.calls += 1
        self.last_user_prompt = messages[-1].content
        if self._raises:
            msg = "provider down"
            raise RuntimeError(msg)
        return ChatResponse(
            content=self._content,
            model=self.model_name,
            provider=self.provider_name,
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            latency_ms=1.0,
        )


def _candidate(*citations: GroundingCitation) -> InitiativeCandidate:
    cites = citations or (GroundingCitation(kind=CitationKind.NODE, ref="node-hearing"),)
    return InitiativeCandidate(
        observation="The hearing is on Friday and prep is unfinished.",
        citations=tuple(cites),
        trigger=InitiativeTrigger.APPROACHING_COMMITMENT,
        why_now="The hearing entered the horizon.",
        plan=(
            PlannedStep(
                description="draft the prep checklist",
                categories=frozenset({ActionCategory.DRAFT}),
            ),
        ),
        next_step="Draft the checklist.",
        value=0.9,
        acceptance=0.7,
        urgency=Urgency.BATCH,
        source=CandidateSource.SCAN,
        owner_id="user-1",
        persona_id="persona-a",
        prompt_version="v1",
        scanned_at=datetime(2026, 7, 4, 6, 0, tzinfo=UTC),
    )


_YES = f'{{"entailed": true, "quote": "{_HEARING_CONTENT[11:40]}"}}'


class TestResolutionLayer:
    @pytest.mark.asyncio
    async def test_missing_node_discards_before_the_judge_runs(self) -> None:
        judge = _StubJudge(_YES)
        checker = GroundingChecker(source=_FakeSource(), backend=judge)
        verdict = await checker.check(
            _candidate(GroundingCitation(kind=CitationKind.NODE, ref="node-nonexistent"))
        )
        assert verdict.admitted is False
        assert verdict.rejection is GroundingRejection.UNRESOLVED_CITATION
        assert judge.calls == 0  # mechanics discard; the model is never consulted

    @pytest.mark.asyncio
    async def test_merged_node_reads_as_unresolvable(self) -> None:
        """K7 moved on mid-scan: a merged node's content read is None ⇒ discard."""
        source = _FakeSource()
        source.nodes["node-merged"] = None  # the store's merged/deleted signal
        judge = _StubJudge(_YES)
        checker = GroundingChecker(source=source, backend=judge)
        verdict = await checker.check(
            _candidate(GroundingCitation(kind=CitationKind.NODE, ref="node-merged"))
        )
        assert verdict.admitted is False
        assert verdict.rejection is GroundingRejection.UNRESOLVED_CITATION
        assert judge.calls == 0

    @pytest.mark.asyncio
    async def test_one_bad_citation_of_two_discards(self) -> None:
        judge = _StubJudge(_YES)
        checker = GroundingChecker(source=_FakeSource(), backend=judge)
        verdict = await checker.check(
            _candidate(
                GroundingCitation(kind=CitationKind.NODE, ref="node-hearing"),
                GroundingCitation(kind=CitationKind.CONVERSATION, ref="conv-unknown"),
            )
        )
        assert verdict.admitted is False
        assert verdict.rejection is GroundingRejection.UNRESOLVED_CITATION
        assert judge.calls == 0

    @pytest.mark.asyncio
    async def test_source_error_discards_fail_soft(self) -> None:
        source = _FakeSource()
        source.raises = True
        checker = GroundingChecker(source=source, backend=_StubJudge(_YES))
        verdict = await checker.check(_candidate())  # must not raise
        assert verdict.admitted is False
        assert verdict.rejection is GroundingRejection.UNRESOLVED_CITATION


class TestEntailmentLayer:
    @pytest.mark.asyncio
    async def test_entailed_with_verbatim_quote_admits(self) -> None:
        judge = _StubJudge(_YES)
        checker = GroundingChecker(source=_FakeSource(), backend=judge)
        verdict = await checker.check(_candidate())
        assert verdict.admitted is True
        assert verdict.supporting_quote is not None
        assert verdict.supporting_quote in _HEARING_CONTENT

    @pytest.mark.asyncio
    async def test_judge_sees_only_the_mechanically_admitted_set(self) -> None:
        """The K2 pattern: the deciding prompt contains the observation + the
        RESOLVED excerpts — the judge decides within what mechanics admitted."""
        judge = _StubJudge(_YES)
        checker = GroundingChecker(source=_FakeSource(), backend=judge)
        await checker.check(
            _candidate(
                GroundingCitation(kind=CitationKind.NODE, ref="node-hearing"),
                GroundingCitation(kind=CitationKind.TASK, ref="task-prep"),
            )
        )
        assert judge.last_user_prompt is not None
        assert _HEARING_CONTENT in judge.last_user_prompt
        assert _TASK_CONTENT in judge.last_user_prompt

    @pytest.mark.asyncio
    async def test_clear_no_discards(self) -> None:
        checker = GroundingChecker(source=_FakeSource(), backend=_StubJudge('{"entailed": false}'))
        verdict = await checker.check(_candidate())
        assert verdict.admitted is False
        assert verdict.rejection is GroundingRejection.NOT_ENTAILED

    @pytest.mark.asyncio
    async def test_yes_with_conjured_quote_discards(self) -> None:
        """The conjured-grounds trap: a YES whose quote is NOT verbatim in the
        admitted excerpts is discarded — the judge cannot conjure grounds."""
        conjured = '{"entailed": true, "quote": "the user asked to be reminded about this"}'
        checker = GroundingChecker(source=_FakeSource(), backend=_StubJudge(conjured))
        verdict = await checker.check(_candidate())
        assert verdict.admitted is False
        assert verdict.rejection is GroundingRejection.NOT_ENTAILED

    @pytest.mark.asyncio
    async def test_yes_with_empty_quote_discards(self) -> None:
        checker = GroundingChecker(
            source=_FakeSource(), backend=_StubJudge('{"entailed": true, "quote": ""}')
        )
        verdict = await checker.check(_candidate())
        assert verdict.admitted is False
        assert verdict.rejection is GroundingRejection.NOT_ENTAILED

    @pytest.mark.asyncio
    async def test_garbage_output_discards_as_judge_error(self) -> None:
        checker = GroundingChecker(
            source=_FakeSource(), backend=_StubJudge("I think this is probably fine!")
        )
        verdict = await checker.check(_candidate())
        assert verdict.admitted is False
        assert verdict.rejection is GroundingRejection.JUDGE_ERROR

    @pytest.mark.asyncio
    async def test_judge_exception_discards_fail_soft(self) -> None:
        checker = GroundingChecker(source=_FakeSource(), backend=_StubJudge(raises=True))
        verdict = await checker.check(_candidate())  # must not raise
        assert verdict.admitted is False
        assert verdict.rejection is GroundingRejection.JUDGE_ERROR

    @pytest.mark.asyncio
    async def test_non_dict_json_discards(self) -> None:
        checker = GroundingChecker(source=_FakeSource(), backend=_StubJudge('["yes"]'))
        verdict = await checker.check(_candidate())
        assert verdict.admitted is False
        assert verdict.rejection is GroundingRejection.JUDGE_ERROR

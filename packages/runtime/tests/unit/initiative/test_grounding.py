"""Unit tests — the mechanical grounding check (Spec A5, T4; A5-D-2, criterion 2).

Deterministic: a fake source + a stub judge backend. Pins the T4 bars —
resolution is pure-mechanical (merged/deleted/missing refs discard BEFORE the
judge runs), the judge decides only within the mechanically-admitted set (a
YES with a conjured quote is discarded by substring check), and every failure
mode is fail-soft (discard, never a raise, never a pass-through).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
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
    ENTAILMENT_PROMPT_VERSION,
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
        self.last_kwargs: dict[str, Any] = {}

    @property
    def provider_name(self) -> str:
        return "anthropic"

    @property
    def model_name(self) -> str:
        return "claude-haiku-4-5-20251001"

    async def chat(self, messages: Any, **kwargs: Any) -> ChatResponse:  # noqa: ANN401, ARG002
        self.calls += 1
        self.last_user_prompt = messages[-1].content
        self.last_kwargs = kwargs
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


class TestJudgeOutputCap:
    """R9-248: the judge's ``max_tokens`` is the setting, sized for reasoning models."""

    @pytest.mark.asyncio
    async def test_the_cap_defaults_to_the_setting_default(self) -> None:
        from persona.initiative import InitiativeSettings

        judge = _StubJudge('{"entailed": false}')
        await GroundingChecker(source=_FakeSource(), backend=judge).check(_candidate())
        assert judge.last_kwargs["max_tokens"] == InitiativeSettings().judge_max_output_tokens
        assert InitiativeSettings().judge_max_output_tokens == 1024

    @pytest.mark.asyncio
    async def test_the_cap_is_the_injected_value(self) -> None:
        judge = _StubJudge('{"entailed": false}')
        checker = GroundingChecker(source=_FakeSource(), backend=judge, max_output_tokens=2048)
        await checker.check(_candidate())
        assert judge.last_kwargs["max_tokens"] == 2048


class TestTheJudgeChecksWhatTheUserReads:
    """R9-249 re-review HIGH-B: since R9-256 the user reads why_now and next_step, not the
    observation, so those are judged too, in the same single call."""

    def _with(self, *, why_now: str, next_step: str = "draft the checklist for you") -> Any:  # noqa: ANN401
        return _candidate().model_copy(update={"why_now": why_now, "next_step": next_step})

    @pytest.mark.asyncio
    async def test_the_one_judge_call_carries_the_message_and_the_offer(self) -> None:
        judge = _StubJudge(_YES)
        checker = GroundingChecker(source=_FakeSource(), backend=judge)
        candidate = self._with(
            why_now="Your custody hearing is on Friday 10 July, if the prep is still open.",
            next_step="put together a prep checklist for you",
        )
        verdict = await checker.check(candidate)
        assert verdict.admitted is True  # a supported message passes
        assert judge.calls == 1
        prompt = judge.last_user_prompt or ""
        assert f"OBSERVATION (the grounds):\n{candidate.observation}" in prompt
        assert f"MESSAGE (what the user will read):\n{candidate.why_now}" in prompt
        assert f"OFFER (what the assistant offers to do):\n{candidate.next_step}" in prompt

    @pytest.mark.parametrize(
        "why_now",
        [
            "Your application is due Monday.",  # a weekday the notes never state
            "The hearing is on Friday 17 July.",  # a date the notes never state
            "Your hearing is at 14:00 on Friday.",  # a time the notes never state
            "The hearing moved to Thursday.",  # a changed fact
            "If the form is due in March, there is time.",  # a hedge cannot carry a date
            "Your hearing is on Friday 1 July.",  # "1" is not found inside "10"
            "Your hearing is on Mon 10 July.",  # an abbreviated weekday is checked too
        ],
    )
    @pytest.mark.asyncio
    async def test_an_invented_specific_in_the_message_is_refused_without_a_judge_call(
        self, why_now: str
    ) -> None:
        judge = _StubJudge(_YES)  # a judge that would say yes to anything
        checker = GroundingChecker(source=_FakeSource(), backend=judge)
        verdict = await checker.check(self._with(why_now=why_now))
        assert verdict.admitted is False
        assert verdict.rejection is GroundingRejection.UNSUPPORTED_SPECIFIC
        assert judge.calls == 0

    @pytest.mark.parametrize(
        "why_now",
        [
            "Your hearing is on Friday 10 July; if you haven't prepared yet, there is time.",
            "The hearing is on Fri 10 Jul, if the prep is still open.",  # abbreviated forms
            "If you haven't compared offers yet, the hearing is still some days away.",
        ],
    )
    @pytest.mark.asyncio
    async def test_a_supported_or_hedged_message_reaches_the_judge(self, why_now: str) -> None:
        judge = _StubJudge(_YES)
        checker = GroundingChecker(source=_FakeSource(), backend=judge)
        verdict = await checker.check(self._with(why_now=why_now))
        assert verdict.admitted is True
        assert judge.calls == 1

    @pytest.mark.asyncio
    async def test_the_judge_can_still_refuse_an_unsupported_message(self) -> None:
        """Layer 3 catches specifics; everything else is the judge's call, and a NO holds."""
        checker = GroundingChecker(source=_FakeSource(), backend=_StubJudge('{"entailed": false}'))
        verdict = await checker.check(self._with(why_now="Your landlord wants a reply."))
        assert verdict.admitted is False
        assert verdict.rejection is GroundingRejection.NOT_ENTAILED

    def test_the_judge_prompt_defines_the_hedge_and_the_offer_rules(self) -> None:
        from persona_runtime.initiative.grounding import _SYSTEM_PROMPT

        assert ENTAILMENT_PROMPT_VERSION == "a5-entailment-v3"
        flat = " ".join(_SYSTEM_PROMPT.split())
        assert 'introduced by "if", "unless" or "in case"' in flat
        assert '"if the form is due Friday" is a claim, not a hedge' in flat
        assert "The OFFER and the PLAN only propose actions. They assert no fact" in flat
        assert "Every factual claim in the MESSAGE is stated or directly supported" in flat


def _verdict(
    why_now: str,
    excerpt: str,
    *,
    next_step: str = "put it together for you to review",
    today: date = date(2026, 9, 26),
) -> tuple[bool, GroundingRejection | None, int]:
    """(admitted, rejection, judge calls) for one message over one excerpt, yes-judge."""
    source = _FakeSource()
    source.nodes["node-hearing"] = excerpt
    judge = _StubJudge(f'{{"entailed": true, "quote": "{excerpt[:12]}"}}')
    candidate = _candidate().model_copy(update={"why_now": why_now, "next_step": next_step})
    checker = GroundingChecker(source=source, backend=judge)
    verdict = asyncio.run(checker.check(candidate, today=today, timezone="Europe/Oslo"))
    return verdict.admitted, verdict.rejection, judge.calls


_ADMITTED = (True, None, 1)
_REFUSED = (False, GroundingRejection.UNSUPPORTED_SPECIFIC, 0)
_GLASS = "Quotes received: Hansen 8 400 kr, Oslo Glass 7 950 kr, valid until 1 October 2026."
_INSURANCE = "The car insurance with If renews automatically on 4 October 2026."
_HEARING = "The deposit hearing is on 2 October 2026 at 10am; the tribunal wants a reply."


class TestProductionShapedSpecifics:
    """Third review MEDIUM-1: honest production wording is never refused mechanically.
    Equality on the verdict: (admitted, rejection, judge calls)."""

    @pytest.mark.parametrize(
        ("why_now", "excerpt", "expected"),
        [
            # Norwegian thousands spacing, and the comma, point and plain forms of it.
            ("The Oslo Glass quote of 7,950 kr is open until 1 October.", _GLASS, _ADMITTED),
            ("Oslo Glass quoted 7.950 kr.", _GLASS, _ADMITTED),
            ("Oslo Glass quoted 7950 kr.", _GLASS, _ADMITTED),
            ("Hansen quoted 8 400 kr.", _GLASS, _ADMITTED),
            ("Hansen quoted 8,500 kr.", _GLASS, _REFUSED),  # a different amount
            # A weekday computed from a stated date (4 October 2026 is a Sunday).
            ("Your insurance renews on Sunday 4 October.", _INSURANCE, _ADMITTED),
            ("Your insurance renews on Monday 4 October.", _INSURANCE, _REFUSED),
            ("Your insurance renews on Sun 4 Oct.", _INSURANCE, _ADMITTED),
            # Times: 10:00, 10am and 10 am are one time.
            ("Your hearing is at 10:00 on Friday 2 October.", _HEARING, _ADMITTED),
            ("Your hearing is at 10 am on 2 October.", _HEARING, _ADMITTED),
            ("Your hearing is at 11:00 on 2 October.", _HEARING, _REFUSED),
            # A computed duration and ordinary words are the judge's, never refused here.
            ("Your hearing is in 6 days.", _HEARING, _ADMITTED),
            (
                "You sat on the reply; the sun was out; don't let it mar the week.",
                _HEARING,
                _ADMITTED,
            ),
            ("May I help with the reply?", _HEARING, _ADMITTED),
            ("The reply is due in May.", _HEARING, _REFUSED),  # "in May" is a month
            ("The reply is due May 5.", _HEARING, _REFUSED),
            # A weekday with no stated date at all is left to the judge.
            ("Reply by Monday.", "The tribunal wants a written reply soon.", _ADMITTED),
        ],
    )
    def test_the_verdict(
        self, why_now: str, excerpt: str, expected: tuple[bool, object, int]
    ) -> None:
        assert _verdict(why_now, excerpt) == expected

    def test_an_iso_or_norwegian_date_supports_its_day_month_and_weekday(self) -> None:
        iso = "The insurance renews on 2026-10-04."
        norwegian = "Forsikringen fornyes 4.10.2026."
        assert _verdict("It renews on Sunday 4 October.", iso) == _ADMITTED
        assert _verdict("It renews on Sunday 4 October.", norwegian) == _ADMITTED
        assert _verdict("It renews on 5 October.", iso) == _REFUSED

    def test_the_offer_numbers_are_checked_but_not_its_weekdays(self) -> None:
        """Third review LOW 3: "remind you on Wednesday" is a proposal, "at 9" a claim."""
        assert (
            _verdict("It renews on 4 October.", _INSURANCE, next_step="remind you on Wednesday")
            == _ADMITTED
        )
        assert (
            _verdict("It renews on 4 October.", _INSURANCE, next_step="remind you at 9") == _REFUSED
        )
        assert (
            _verdict("It renews on 4 October.", _INSURANCE, next_step="compare offers for 2026")
            == _ADMITTED
        )

    def test_the_judge_is_told_today_the_timezone_and_the_plan(self) -> None:
        """Third review LOW 2 and LOW 8: a relative claim is verified against TODAY."""
        judge = _StubJudge(_YES)
        checker = GroundingChecker(source=_FakeSource(), backend=judge)
        asyncio.run(checker.check(_candidate(), today=date(2026, 7, 6), timezone="Europe/Oslo"))
        prompt = judge.last_user_prompt or ""
        assert "TODAY (the user's local date, Europe/Oslo): Monday 6 July 2026" in prompt
        assert "PLAN (the steps the assistant would take):\n- draft the prep checklist" in prompt
        from persona_runtime.initiative.grounding import _SYSTEM_PROMPT

        flat = " ".join(_SYSTEM_PROMPT.split())
        assert "A RELATIVE time claim in the MESSAGE" in flat
        assert "If you cannot verify it from TODAY and the excerpts, rule 5 fails." in flat
        assert "The OFFER and the PLAN only propose actions." in flat

    def test_without_a_date_the_judge_is_told_the_scan_date_in_utc(self) -> None:
        judge = _StubJudge(_YES)
        checker = GroundingChecker(source=_FakeSource(), backend=judge)
        asyncio.run(checker.check(_candidate()))
        assert "TODAY (the user's local date, UTC): Saturday 4 July 2026" in (
            judge.last_user_prompt or ""
        )

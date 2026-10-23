"""Unit tests — the mechanical grounding check (Spec A5, T4; A5-D-2, criterion 2).

Deterministic: a fake source + a stub judge backend. Pins the T4 bars —
resolution is pure-mechanical (merged/deleted/missing refs discard BEFORE the
judge runs), the judge decides only within the mechanically-admitted set (a
YES with a conjured quote is discarded by substring check), and every failure
mode is fail-soft (discard, never a raise, never a pass-through).
"""

from __future__ import annotations

import asyncio
import json
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


#: The stub judge's identity as the chain reports it ("provider/model").
_HEAD = "anthropic/claude-haiku-4-5-20251001"


class _UnreadablePlan:
    """A plan wrapper whose plan read fails (a DB error) while its calls still work."""

    def __init__(self, inner: _StubJudge) -> None:
        self._inner = inner
        self.reads = 0

    def resolve_chain(self) -> _StubJudge:
        self.reads += 1
        msg = "could not read the subscription"
        raise ConnectionError(msg)

    async def chat(self, messages: Any, **kwargs: Any) -> ChatResponse:  # noqa: ANN401
        return await self._inner.chat(messages, **kwargs)


class _NamedJudge:
    """A judge with a chosen identity and mutable content; ``echo`` overrides the model
    its responses name (a faithful backend echoes its own)."""

    def __init__(self, content: str, provider: str, model: str, *, echo: str | None = None) -> None:
        self.content = content
        self.calls = 0
        self._provider, self._model, self._echo = provider, model, echo

    @property
    def provider_name(self) -> str:
        return self._provider

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def supports_native_tools(self) -> bool:
        return False

    @property
    def supports_vision(self) -> bool:
        return False

    async def chat(self, messages: Any, **kwargs: Any) -> ChatResponse:  # noqa: ANN401, ARG002
        self.calls += 1
        return ChatResponse(
            content=self.content,
            model=self._echo or self._model,
            provider=self._provider,
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            latency_ms=1.0,
        )


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
            "Your application is due Monday 13 July.",  # a date the notes never state
            "The hearing is on Friday 17 July.",  # a date the notes never state
            "Your hearing is at 14:00 on Friday 10 July.",  # a time the notes never state
            "The hearing moved to Thursday 9 July.",  # a changed fact
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
            "If you haven't compared offers yet, the hearing is worth preparing for.",
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
    judge = _StubJudge(f'{{"entailed": true, "quote": "{excerpt[:30]}"}}')
    candidate = _candidate().model_copy(update={"why_now": why_now, "next_step": next_step})
    checker = GroundingChecker(source=source, backend=judge)
    verdict = asyncio.run(checker.check(candidate, today=today, timezone="Europe/Oslo"))
    return verdict.admitted, verdict.rejection, judge.calls


_ADMITTED = (True, None, 1)
_REFUSED = (False, GroundingRejection.UNSUPPORTED_SPECIFIC, 0)
_RELATIVE = (False, GroundingRejection.RELATIVE_DATE, 0)
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
            ("Your hearing is in 6 days.", _HEARING, _RELATIVE),  # relative: refused (v6)
            (
                "You sat on the reply; the sun was out; don't let it mar the week.",
                _HEARING,
                _ADMITTED,
            ),
            ("May I help with the reply?", _HEARING, _ADMITTED),
            ("The reply is due in May.", _HEARING, _REFUSED),  # "in May" is a month
            ("The reply is due May 5.", _HEARING, _REFUSED),
            # A weekday with no stated date at all is left to the judge.
            ("Reply by Monday.", "The tribunal wants a written reply soon.", _RELATIVE),
        ],
    )
    def test_the_verdict(
        self, why_now: str, excerpt: str, expected: tuple[bool, object, int]
    ) -> None:
        assert _verdict(why_now, excerpt) == expected

    def test_an_iso_or_norwegian_date_supports_its_day_month_and_weekday(self) -> None:
        iso = "The insurance renews on 2026-10-04."
        norwegian = "Forsikringen fornyes 4.10.2026."
        slashed = "The insurance renews on 04/10/2026."
        assert _verdict("It renews on Sunday 4 October.", iso) == _ADMITTED
        assert _verdict("It renews on Sunday 4 October.", norwegian) == _ADMITTED
        assert _verdict("It renews on Sunday 4 October.", slashed) == _ADMITTED
        assert _verdict("It renews on 5 October.", iso) == _REFUSED
        assert _verdict("It renews on 5 October.", slashed) == _REFUSED
        assert _verdict("It renews on Monday 4 October.", slashed) == _REFUSED

    def test_a_slash_date_in_the_message_is_read_day_first(self) -> None:
        """Fifth review LOW-5: d/m/yyyy is a date (day first), for specifics and lapse."""
        from persona_runtime.initiative.specifics import earliest_upcoming_date

        assert _verdict("It renews on Sunday 4/10/2026.", _INSURANCE) == _ADMITTED
        assert _verdict("It renews on 5/10/2026.", _INSURANCE) == _REFUSED
        assert earliest_upcoming_date("due 4/10/2026", written_on=date(2026, 9, 26)) == date(
            2026, 10, 4
        )
        assert earliest_upcoming_date("due 31/2/2026", written_on=date(2026, 9, 26)) is None

    def test_the_offer_numbers_are_checked_but_not_its_weekdays(self) -> None:
        """Third review LOW 3: "remind you on Sunday 4 October" is a proposal; an offer's
        number is a claim; and since v6 an undated weekday is refused as relative."""
        why = "It renews on 4 October."
        assert _verdict(why, _INSURANCE, next_step="remind you on Sunday 4 October") == _ADMITTED
        assert _verdict(why, _INSURANCE, next_step="remind you at 9") == _REFUSED
        assert _verdict(why, _INSURANCE, next_step="compare offers for 2026") == _ADMITTED
        assert _verdict(why, _INSURANCE, next_step="remind you on Wednesday") == _RELATIVE

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


#: The A5-R-1 re-run's run-2 excerpts and judge quotes, exactly as recorded (2026-10-23).
_A2_TASK = (
    "Task goal: Watch the price of the Sony WH-1000XM6 headphones and tell me if it drops "
    "below 3,000 kr (status: completed)\n"
    "Checked Elkjop, Power and Komplett daily since Wednesday 16 September 2026.\n"
    "On Friday 25 September 2026 Power dropped the price to 2,790 kr, below the user's "
    "3,000 kr limit; the offer runs until Tuesday 29 September 2026."
)
_A2_NODE = (
    "The user wants noise-cancelling headphones before their trip to Lisbon on "
    "Thursday 8 October 2026."
)
_A2_QUOTE = "The offer runs until Tuesday 29 September 2026."
_A4_TASK = (
    "Task goal: Every morning, check Vy's traffic notices for the Bergen Line until my trip "
    "(status: active)\n"
    "Friday 25 September 2026: Vy announced the Bergen Line is closed between Voss and Myrdal "
    "from Monday 28 September 2026 to Thursday 1 October 2026 for track work; replacement buses "
    "run between Voss and Myrdal and add about two hours to the journey."
)
_A4_NODE = (
    "The user is taking the train from Oslo to Bergen on Tuesday 29 September 2026 for their "
    "cousin's wedding on Wednesday 30 September 2026."
)
_A4_QUOTE = (
    "Vy announced the Bergen Line is closed between Voss and Myrdal from Monday 28 September "
    "2026 to Thursday 1 October 2026 for track work; replacement buses run between Voss and "
    "Myrdal and add about two hours to the journey.\nThe user is taking the train from Oslo to "
    "Bergen on Tuesday 29 September 2026 for their cousin's wedding on Wednesday 30 September 2026."
)


class TestTheQuoteCheckNormalises:
    """The verbatim-quote check compares normalised text, and a quote spanning excerpts is
    accepted line by line (the re-run's A2 and A4 were refused only for this)."""

    def test_the_run_2_quotes_are_found(self) -> None:
        from persona_runtime.initiative.grounding import quote_in_excerpts

        assert quote_in_excerpts(_A2_QUOTE, [_A2_NODE, _A2_TASK]) is True  # case only
        assert quote_in_excerpts(_A4_QUOTE, [_A4_TASK, _A4_NODE]) is True  # two excerpts

    def test_spacing_and_quote_marks_are_normalised(self) -> None:
        from persona_runtime.initiative.grounding import quote_in_excerpts

        excerpt = "Quotes: Oslo Glass 7 950 kr, valid until 1 October; the user’s pick."
        assert quote_in_excerpts("Oslo Glass 7 950 kr, valid until 1 October", [excerpt])
        assert quote_in_excerpts("valid until 1 October; the user's pick", [excerpt])
        assert quote_in_excerpts("Oslo  Glass\n7 950 kr", [excerpt]) is False  # a short part

    def test_a_too_short_part_refuses_a_multi_line_quote(self) -> None:
        from persona_runtime.initiative.grounding import MIN_QUOTE_PART_CHARS, quote_in_excerpts

        long_part = _A4_NODE[:60]
        assert quote_in_excerpts(f"{long_part}\nVy announced", [_A4_TASK, _A4_NODE]) is False
        assert quote_in_excerpts(f"{long_part}\nVy announced the Bergen", [_A4_TASK, _A4_NODE])
        assert MIN_QUOTE_PART_CHARS == 20

    def test_a_short_quote_is_refused_on_one_line_too(self) -> None:
        from persona_runtime.initiative.grounding import quote_in_excerpts

        excerpts = [_A2_NODE, _A2_TASK]
        assert quote_in_excerpts("2026", excerpts) is False
        assert quote_in_excerpts("the offer runs until", excerpts) is True  # 20 characters
        assert quote_in_excerpts("the offer runs unti", excerpts) is False  # 19

    @pytest.mark.parametrize(
        "wrapped",
        [
            '"The offer runs until Tuesday 29 September 2026."',
            "'The offer runs until Tuesday 29 September 2026.'",
            "“The offer runs until Tuesday 29 September 2026.”",
            "‘The offer runs until Tuesday 29 September 2026.’",
            ' "The offer runs until Tuesday 29 September 2026." ',
        ],
    )
    def test_one_surrounding_pair_of_quotation_marks_is_set_aside(self, wrapped: str) -> None:
        from persona_runtime.initiative.grounding import quote_in_excerpts

        assert quote_in_excerpts(wrapped, [_A2_NODE, _A2_TASK]) is True

    def test_only_one_matching_pair_is_set_aside(self) -> None:
        from persona_runtime.initiative.grounding import quote_in_excerpts

        excerpts = [_A2_NODE, _A2_TASK]
        doubled = "\"'The offer runs until Tuesday 29 September 2026.'\""
        assert quote_in_excerpts(doubled, excerpts) is False
        unmatched = "\"The offer runs until Tuesday 29 September 2026.'"
        assert quote_in_excerpts(unmatched, excerpts) is False
        framed = "*The offer runs until Tuesday 29 September 2026.*"  # not quotation marks
        assert quote_in_excerpts(framed, excerpts) is False
        # A quote whose own text starts and ends with quotation marks still matches as is.
        said = "He said 'call me' and 'soon' to the agent today."
        assert quote_in_excerpts("'call me' and 'soon' to the agent", [said]) is True

    def test_a_conjured_or_altered_quote_is_still_refused(self) -> None:
        from persona_runtime.initiative.grounding import quote_in_excerpts

        excerpts = [_A2_NODE, _A2_TASK]
        assert quote_in_excerpts("The offer runs until Friday 2 October 2026.", excerpts) is False
        # Case-insensitive, but a different sentence: one changed word still refuses it.
        assert (
            quote_in_excerpts("THE OFFER ENDS UNTIL TUESDAY 29 SEPTEMBER 2026.", excerpts) is False
        )
        assert quote_in_excerpts("", excerpts) is False
        assert quote_in_excerpts("\n\n", excerpts) is False
        half_true = f"{_A4_NODE}\nThe hearing moved to Thursday 1 October 2026 at noon."
        assert quote_in_excerpts(half_true, [_A4_TASK, _A4_NODE]) is False

    @pytest.mark.asyncio
    async def test_the_judge_path_admits_the_run_2_quote(self) -> None:
        source = _FakeSource()
        source.nodes["node-hearing"] = _A4_NODE
        source.tasks["task-prep"] = _A4_TASK
        judge = _StubJudge(json.dumps({"entailed": True, "quote": _A4_QUOTE}))
        checker = GroundingChecker(source=source, backend=judge)
        verdict = await checker.check(
            _candidate(
                GroundingCitation(kind=CitationKind.TASK, ref="task-prep"),
                GroundingCitation(kind=CitationKind.NODE, ref="node-hearing"),
            ).model_copy(update={"why_now": "Your train to Bergen is affected by a closure."})
        )
        assert verdict.admitted is True
        assert verdict.supporting_quote == _A4_QUOTE


class TestTheFlushKeepsAnUnchangedVerdict:
    """The judge is re-asked only when what it read has changed (the re-run's flush flips)."""

    @pytest.mark.asyncio
    async def test_an_admission_carries_the_fingerprint_of_its_excerpts(self) -> None:
        from persona_runtime.initiative.grounding import excerpts_fingerprint

        judge = _StubJudge(_YES)
        verdict = await GroundingChecker(source=_FakeSource(), backend=judge).check(_candidate())
        assert verdict.admitted is True
        assert verdict.served_model == _HEAD
        assert verdict.fingerprint == excerpts_fingerprint([_HEARING_CONTENT])
        assert excerpts_fingerprint(["a", "b"]) != excerpts_fingerprint(["ab"])
        assert excerpts_fingerprint(["a", "b"]) != excerpts_fingerprint(["b", "a"])

    @pytest.mark.parametrize(
        ("content", "rejection"),
        [
            ('{"entailed": false}', GroundingRejection.NOT_ENTAILED),
            ("not json", GroundingRejection.JUDGE_ERROR),
            ('{"entailed": true, "quote": "a span found nowhere at all"}', None),
        ],
    )
    @pytest.mark.asyncio
    async def test_every_answered_verdict_names_the_model_that_served_it(
        self, content: str, rejection: GroundingRejection | None
    ) -> None:
        judge = _StubJudge(content)
        verdict = await GroundingChecker(source=_FakeSource(), backend=judge).check(_candidate())
        assert verdict.admitted is False
        assert verdict.rejection is (rejection or GroundingRejection.NOT_ENTAILED)
        assert verdict.served_model == _HEAD

    def test_the_fingerprint_covers_the_prompt_version(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from persona_runtime.initiative import grounding

        before = grounding.excerpts_fingerprint([_HEARING_CONTENT])
        monkeypatch.setattr(grounding, "ENTAILMENT_PROMPT_VERSION", "a5-entailment-v999")
        assert grounding.excerpts_fingerprint([_HEARING_CONTENT]) != before

    @pytest.mark.asyncio
    async def test_unchanged_grounds_keep_the_admission_without_a_model_call(self) -> None:
        from persona_runtime.initiative.grounding import excerpts_fingerprint

        judge = _StubJudge('{"entailed": false}')  # a judge that would now say no
        checker = GroundingChecker(source=_FakeSource(), backend=judge)
        verdict = await checker.check(
            _candidate(),
            admitted_fingerprint=excerpts_fingerprint([_HEARING_CONTENT]),
            admitted_served_model=_HEAD,
        )
        assert verdict.admitted is True
        assert verdict.served_model is None  # no model answered this time
        assert judge.calls == 0

    @pytest.mark.asyncio
    async def test_the_head_is_read_from_a_real_chain_s_attempt_ledger(self) -> None:
        """Fifth review LOW-1: a real MultiModelChatBackend. When the primary served, its
        YES is kept at the flush; when a fallback served, it is judged again."""
        from persona.backends.multi_model import MultiModelChatBackend
        from persona_runtime.initiative.grounding import excerpts_fingerprint

        fingerprint = excerpts_fingerprint([_HEARING_CONTENT])
        head = _NamedJudge(_YES, "openrouter", "openai/gpt-oss-120b")
        spare = _NamedJudge(_YES, "openrouter", "qwen/qwen3-30b-a3b")
        chain = MultiModelChatBackend([head, spare], max_retries_per_backend=0)
        checker = GroundingChecker(source=_FakeSource(), backend=chain)
        admitted = await checker.check(_candidate())
        assert admitted.served_model == "openrouter/openai/gpt-oss-120b"
        kept = await checker.check(
            _candidate(),
            admitted_fingerprint=fingerprint,
            admitted_served_model=admitted.served_model,
        )
        assert (kept.admitted, kept.kept, head.calls) == (True, True, 1)

        head.content = ""  # the primary now returns nothing: the chain falls back
        fallback = await checker.check(_candidate())
        assert fallback.admitted is True
        assert fallback.served_model == "openrouter/qwen/qwen3-30b-a3b"
        spare.content = '{"entailed": false}'
        rejudged = await checker.check(
            _candidate(),
            admitted_fingerprint=fingerprint,
            admitted_served_model=fallback.served_model,
        )
        assert (rejudged.admitted, rejudged.kept, spare.calls) == (False, False, 2)

    @pytest.mark.asyncio
    async def test_the_same_model_on_another_provider_is_not_the_head(self) -> None:
        from persona.backends.multi_model import MultiModelChatBackend

        head = _NamedJudge("", "openrouter", "openai/gpt-oss-120b")
        same_model = _NamedJudge(_YES, "groq", "openai/gpt-oss-120b")
        chain = MultiModelChatBackend([head, same_model], max_retries_per_backend=0)
        verdict = await GroundingChecker(source=_FakeSource(), backend=chain).check(_candidate())
        assert verdict.admitted is True
        assert verdict.served_model == "groq/openai/gpt-oss-120b"  # not the head's identity

    @pytest.mark.asyncio
    async def test_a_failing_plan_read_re_judges_instead_of_raising(self) -> None:
        """Sixth review LOW-4: the keep check cannot read the chain's head (a DB error in
        the plan read), so the judge is asked; nothing raises, nothing expires."""
        from persona_runtime.initiative.grounding import excerpts_fingerprint

        judge = _StubJudge(_YES)
        backend = _UnreadablePlan(judge)
        verdict = await GroundingChecker(source=_FakeSource(), backend=backend).check(  # type: ignore[arg-type]
            _candidate(),
            admitted_fingerprint=excerpts_fingerprint([_HEARING_CONTENT]),
            admitted_served_model=_HEAD,
        )
        assert (verdict.admitted, verdict.kept, judge.calls) == (True, False, 1)
        assert verdict.served_model is None  # unreadable, so never kept later either
        assert backend.reads == 2  # the keep check once, the served identity once

    @pytest.mark.asyncio
    async def test_an_echo_that_names_another_member_leaves_the_served_model_unclear(
        self,
    ) -> None:
        judge = _NamedJudge(_YES, "anthropic", "claude-haiku-4-5-20251001", echo="other-model")
        verdict = await GroundingChecker(source=_FakeSource(), backend=judge).check(_candidate())
        assert verdict.admitted is True
        assert verdict.served_model is None  # never kept at the flush

    @pytest.mark.parametrize("served", ["openrouter/qwen/qwen3-30b-a3b", None])
    @pytest.mark.asyncio
    async def test_a_fallback_or_unrecorded_model_s_yes_is_judged_again(
        self, served: str | None
    ) -> None:
        from persona_runtime.initiative.grounding import excerpts_fingerprint

        judge = _StubJudge('{"entailed": false}')
        verdict = await GroundingChecker(source=_FakeSource(), backend=judge).check(
            _candidate(),
            admitted_fingerprint=excerpts_fingerprint([_HEARING_CONTENT]),
            admitted_served_model=served,
        )
        assert verdict.rejection is GroundingRejection.NOT_ENTAILED
        assert judge.calls == 1

    @pytest.mark.parametrize(
        ("why_now", "next_step"),
        [
            ("Your hearing is on Friday 10 July.", "Draft the checklist."),
            ("Your hearing is on 10 July.", "Draft the checklist."),
            ("Your hearing is in July.", "Draft the checklist."),
            ("Your hearing is on Fri 10 Jul.", "Draft the checklist."),
            ("The hearing entered the horizon.", "Draft the checklist before Friday 10 July."),
            ("The hearing entered the horizon.", "Draft the checklist before the 10th of July."),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_message_that_names_a_time_is_judged_again_on_unchanged_grounds(
        self, why_now: str, next_step: str
    ) -> None:
        """Fourth review LOW 9: the fingerprint is unchanged and the model is the head,
        yet a dated message is re-read against the new TODAY."""
        from persona_runtime.initiative.grounding import excerpts_fingerprint

        judge = _StubJudge('{"entailed": false}')
        candidate = _candidate().model_copy(update={"why_now": why_now, "next_step": next_step})
        verdict = await GroundingChecker(source=_FakeSource(), backend=judge).check(
            candidate,
            admitted_fingerprint=excerpts_fingerprint([_HEARING_CONTENT]),
            admitted_served_model=_HEAD,
        )
        assert verdict.rejection is GroundingRejection.NOT_ENTAILED
        assert judge.calls == 1

    def test_the_time_words_are_any_date_weekday_or_month(self) -> None:
        from persona_runtime.initiative.specifics import time_words

        assert time_words("Your hearing is on Friday 10 July.") == ["Friday", "10 July", "July"]
        assert time_words("due 2026-10-04") == ["2026-10-04"]
        assert time_words("in Oct or by the 5th, Mon") == ["Mon", "Oct", "the 5th"]
        assert time_words("It renews tomorrow.") == ["tomorrow"]
        assert time_words("The hearing entered the horizon. May I help?") == []

    @pytest.mark.asyncio
    async def test_a_changed_excerpt_is_judged_again(self) -> None:
        from persona_runtime.initiative.grounding import excerpts_fingerprint

        source = _FakeSource()
        old = excerpts_fingerprint([_HEARING_CONTENT])
        source.nodes["node-hearing"] = "The user's custody hearing was cancelled."
        judge = _StubJudge('{"entailed": false}')
        verdict = await GroundingChecker(source=source, backend=judge).check(
            _candidate(), admitted_fingerprint=old, admitted_served_model=_HEAD
        )
        assert verdict.admitted is False
        assert verdict.rejection is GroundingRejection.NOT_ENTAILED
        assert judge.calls == 1

    @pytest.mark.asyncio
    async def test_the_mechanical_layers_still_run_on_unchanged_grounds(self) -> None:
        from persona_runtime.initiative.grounding import excerpts_fingerprint

        fingerprint = excerpts_fingerprint([_HEARING_CONTENT])
        judge = _StubJudge(_YES)
        checker = GroundingChecker(source=_FakeSource(), backend=judge)
        wrong = _candidate().model_copy(update={"why_now": "Your hearing is on 17 July."})
        refused = await checker.check(wrong, admitted_fingerprint=fingerprint)
        assert refused.rejection is GroundingRejection.UNSUPPORTED_SPECIFIC
        gone = _candidate(GroundingCitation(kind=CitationKind.NODE, ref="node-deleted"))
        unresolved = await checker.check(gone, admitted_fingerprint=fingerprint)
        assert unresolved.rejection is GroundingRejection.UNRESOLVED_CITATION
        assert judge.calls == 0


class TestDatesAreAbsolute:
    """v6: a held notice is read the next morning, so the user-facing texts state dates
    absolutely; a day named relative to the writing day is refused without a judge call."""

    @pytest.mark.parametrize(
        "why_now",
        [
            "Your flight leaves tomorrow.",
            "Your offer ends today.",
            "Check in tonight.",
            "The quote runs out this Thursday.",
            "Your appointment is next Tuesday.",
            "Your hearing is in 3 days.",
            "Your hearing is in two days.",
            "Your car is due on Monday.",
            # The fourth review's probes (MEDIUM-2):
            "The price dropped yesterday.",
            "Your insurance renews this week.",
            "Your insurance renews next week.",
            "Your insurance renews this weekend.",
            "Your insurance renews next month.",
            "Your insurance renews this year.",
            "The insurer wrote this morning.",
            "The insurer called this afternoon.",
            "Reply this evening.",
            "It renews at the end of the week.",
            "It renews by the end of the month.",
            "It renews in a week.",
            "It renews in 2 weeks.",
            "It renews in a month.",
            "It renews in a couple of days.",
            "It renews in a few days.",
            "It renews in a couple days.",
            "Your insurance renews on 4 October 2026, in a few days' time.",
            "Cancel the day before.",
            "It renews on Sun.",
            "It renews this coming Sunday.",
            "It renews this coming week.",
            "It renews on Sunday 4th.",
            "It renews on Sunday the 4th.",
            "It renews on the 4th.",
            "It renews every Sunday.",
            # The fifth review's countdown probes (MEDIUM-A):
            "Your renewal is 3 days left.",
            "There are a few days left before it renews.",
            "The renewal is less than a week away.",
            "The renewal is two days away.",
            "It renews within a week.",
            "It renews within 3 days.",
            "Cancel by the weekend.",
            "Cancel over the weekend.",
            "It renews soon.",
            "It renews shortly.",
            "The renewal is coming up shortly.",
            "It renews in under a week.",
            "Cancel in the next few days.",
            "It renewed overnight.",
            "A couple of weeks to go before it renews.",
            "Cancel at the weekend.",
            "Cancel in the next 3 days.",
            "A week remaining before it renews.",
            "Only 2 more days left before it renews.",
            # The sixth review (LOWs 2 and 3): still relative to now.
            "I can send it over shortly.",
            "The hearing is still some days away.",
            "We can ship it overnight to Bergen.",
            "It renews in a fortnight.",
            "A fortnight left before it renews.",
            "The renewal is only hours away.",
            "48 hours left before it renews.",
            "The renewal is days away.",
            "Weeks left before it renews.",
            "It renews within 48 hours.",
            "It renews in 48 hours.",
            "It renews within a fortnight.",
        ],
    )
    def test_a_relative_day_in_the_message_is_refused(self, why_now: str) -> None:
        assert _verdict(why_now, _INSURANCE) == _RELATIVE

    @pytest.mark.parametrize(
        ("text", "time_word"),
        [
            ("This year's renewal is on 4 October.", "This year"),
            ("Next month’s premium is set on 4 October.", "Next month"),
            ("It renews in a day or two, on 4 October.", "in a day"),
        ],
    )
    def test_a_possessive_period_or_a_day_or_two_is_not_refused_but_is_a_time_word(
        self, text: str, time_word: str
    ) -> None:
        """Fifth review LOW-2, both ways: admitted at submit, re-judged at the flush."""
        from persona_runtime.initiative.specifics import relative_day_words, time_words

        assert relative_day_words(text) == []
        assert time_word in time_words(text)
        assert _verdict(text, _INSURANCE) == _ADMITTED

    @pytest.mark.parametrize(
        ("text", "time_word"),
        [
            ("Cancel within 3 days of the renewal on 4 October.", "within 3 days"),
            ("Cancel within a day of the renewal on 4 October.", "within a day"),
            ("Cancel under 3 days of the renewal on 4 October.", "under 3 days"),
            ("The new cover starts soon after the renewal on 4 October.", "soon"),
            ("The new cover starts shortly after the renewal on 4 October.", "shortly"),
            ("Book overnight delivery before the renewal on 4 October.", "overnight"),
            ("Take the overnight train on 4 October.", "overnight"),
            ("Pack an overnight bag for 4 October.", "overnight"),
        ],
    )
    def test_an_anchored_span_soon_after_or_overnight_noun_is_not_refused(
        self, text: str, time_word: str
    ) -> None:
        """Sixth review LOW-2, both ways: admitted at submit, still a time word."""
        from persona_runtime.initiative.specifics import relative_day_words, time_words

        assert relative_day_words(text) == []
        assert time_word in time_words(text)
        assert _verdict(text, _INSURANCE) == _ADMITTED

    def test_the_exemptions_are_narrow(self) -> None:
        from persona_runtime.initiative.specifics import relative_day_words

        assert relative_day_words("It renews this year.") == ["this year"]
        assert relative_day_words("next week's bill") == ["next week"]  # weeks stay refused
        assert relative_day_words("in a day or so") == ["in a day"]
        assert relative_day_words("reply as soon as you can") == []
        assert relative_day_words("sooner or later") == []
        assert relative_day_words("within 3 days, of course") == ["within 3 days"]
        assert relative_day_words("soon, after all") == ["soon"]
        assert relative_day_words("overnight Delivery") == ["overnight"]  # lower-case noun only
        assert relative_day_words("overnight and early") == ["overnight"]

    def test_a_counted_duration_is_a_time_word_but_not_refused(self) -> None:
        """Fifth review MEDIUM-A: a countdown the refusal misses is still re-judged."""
        from persona_runtime.initiative.specifics import relative_day_words, time_words

        assert relative_day_words("The cover lasts 12 months.") == []
        assert time_words("The cover lasts 12 months.") == ["12 months"]
        assert time_words("a few days of rain") == ["a few days"]

    def test_a_relative_day_in_the_offer_is_refused(self) -> None:
        why = "It renews on Sunday 4 October."
        assert _verdict(why, _INSURANCE, next_step="remind you tomorrow") == _RELATIVE

    @pytest.mark.parametrize(
        "why_now",
        [
            "Your insurance renews on Sunday 4 October.",
            "Your insurance renews on Sunday, 4 October.",
            "Your insurance renews on Sun 4 Oct.",
            "Your insurance renews on Sun. 4 Oct.",
            "Your insurance renews on Sunday, October 4.",
            # The fourth review's LOW 6: a weekday beside its date, in either order.
            "Your insurance renews on 4 October (a Sunday).",
            "Your insurance renews on 4 October, a Sunday.",
            "Sunday's renewal on 4 October is automatic.",
            "Your insurance renews on Sunday the 4th of October.",
            "Your insurance renews on the 4th of October.",
        ],
    )
    def test_an_absolute_date_passes(self, why_now: str) -> None:
        assert _verdict(why_now, _INSURANCE) == _ADMITTED

    @pytest.mark.parametrize(
        "why_now",
        ["The Black Friday sale ends on 27 November.", "The shop is shut on Good Friday."],
    )
    def test_a_named_day_is_not_a_relative_weekday(self, why_now: str) -> None:
        sale = "Black Friday sale on 27 November 2026; the shop is shut on Good Friday."
        assert _verdict(why_now, sale) == _ADMITTED

    def test_the_relative_words_are_listed_once_each(self) -> None:
        from persona_runtime.initiative.specifics import relative_day_words

        assert relative_day_words("Do it this Friday or next Monday.") == [
            "this Friday",
            "next Monday",
        ]
        assert relative_day_words("On Friday 2 October, not Friday.") == ["Friday"]
        assert relative_day_words("Friday or 2 October.") == ["Friday"]  # two days
        assert relative_day_words("Friday the 2nd.") == ["Friday", "the 2nd"]
        assert relative_day_words("on the 2nd, not tomorrow") == ["the 2nd", "tomorrow"]
        assert relative_day_words("on the 2nd of the month") == ["the 2nd"]
        assert relative_day_words("I sat down on Wed 30 Sep; the sun was out.") == []
        assert relative_day_words("the 3rd time, on the 2nd floor") == []
        assert relative_day_words("Fri 2 Oct, FRI 9 Oct and FRI.") == ["FRI"]

    def test_the_lapse_date_is_the_earliest_date_on_or_after_the_writing_day(self) -> None:
        """Fourth review LOWs 3 and 4: the earliest stated date still ahead when written,
        from the message and the offer both."""
        from persona_runtime.initiative.specifics import earliest_upcoming_date

        sept_26 = date(2026, 9, 26)
        assert earliest_upcoming_date(
            "from 28 September to Thursday 1 October", written_on=sept_26
        ) == date(2026, 9, 28)
        # A date already past when written is history, not the notice's moment.
        assert earliest_upcoming_date(
            "Power dropped the price on 25 September; the offer runs until 29 September.",
            written_on=sept_26,
        ) == date(2026, 9, 29)
        assert earliest_upcoming_date("due 26 September", written_on=sept_26) == sept_26
        # The offer's dates count.
        assert earliest_upcoming_date(
            "Valid until 9 October.", "remind you on 5 October", written_on=sept_26
        ) == date(2026, 10, 5)
        assert earliest_upcoming_date(
            "The hearing is on Monday the 5th of October.", written_on=sept_26
        ) == date(2026, 10, 5)
        assert earliest_upcoming_date("nothing dated", written_on=sept_26) is None
        assert earliest_upcoming_date("on 2026-10-04", written_on=sept_26) == date(2026, 10, 4)

    def test_a_yearless_date_reads_back_at_most_150_days_else_forward(self) -> None:
        from persona_runtime.initiative.specifics import earliest_upcoming_date

        new_years_eve = date(2026, 12, 30)
        assert earliest_upcoming_date("due 2 January", written_on=new_years_eve) == date(2027, 1, 2)
        # Just past, across New Year: last year's, so not upcoming (not next December).
        jan_2 = date(2027, 1, 2)
        assert earliest_upcoming_date("paid on 30 December", written_on=jan_2) is None
        sept_26 = date(2026, 9, 26)
        assert earliest_upcoming_date("due 1 March", written_on=sept_26) == date(2027, 3, 1)
        assert earliest_upcoming_date("due 1 August", written_on=sept_26) is None  # 56 back
        assert earliest_upcoming_date("due 29 April", written_on=sept_26) is None  # 150 back
        assert earliest_upcoming_date("due 28 April", written_on=sept_26) == date(2027, 4, 28)
        # Fifth review LOW-3: 182 days back and 183 ahead reads forward, as this year's.
        new_year = date(2026, 1, 1)
        assert earliest_upcoming_date("Friday 3 July", written_on=new_year) == date(2026, 7, 3)
        assert earliest_upcoming_date("due 29 February", written_on=sept_26) is None

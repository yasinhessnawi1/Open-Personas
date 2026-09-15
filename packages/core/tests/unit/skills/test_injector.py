"""Tests for ``persona.skills.injector.SkillInjector`` (T06, D-04-7, D-04-8).

Three branches per spec §9 #3-#5:

- Content fits → verbatim pass-through.
- Content over budget + summariser → summariser output.
- Content over budget + no summariser → truncation with ``[truncated]`` marker.

Plus defensive fall-through: summariser returns over-budget output →
truncate the summary.

Also pins ``TOKEN_BUDGET == 2000`` as a regression guard.
"""

# ruff: noqa: ANN401, ARG001, ARG002

from __future__ import annotations

from pathlib import Path  # noqa: TC003 — used at runtime in fixtures

import pytest
from loguru import logger as _loguru_logger
from persona.schema.skills import SkillSpec
from persona.skills._tokens import count_tokens
from persona.skills.injector import MARKER, SkillInjector

from ._fakes import FakeSummariser, OverBudgetSummariser


def _spec(
    tmp_path: Path,
    name: str,
    content: str,
) -> SkillSpec:
    return SkillSpec(
        name=name,
        description="d",
        path=tmp_path,
        content=content,
        content_token_count=count_tokens(content),
    )


class TestTokenBudgetConstant:
    """Regression guard against silent constant drift (D-04-7).

    The budget is architecture §5.1.2 "non-negotiable" in v0.1. If this
    test ever fails, the change should be deliberate — the architecture
    document needs updating in the same commit.
    """

    def test_budget_is_exactly_2000(self) -> None:
        assert SkillInjector.TOKEN_BUDGET == 2000


class TestUnderBudgetVerbatim:
    @pytest.mark.asyncio
    async def test_short_content_passes_through(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path, "x", "Short body that fits trivially.")
        injector = SkillInjector()
        out = await injector.inject(spec)
        assert out == spec.content

    @pytest.mark.asyncio
    async def test_exactly_at_budget_passes_through(self, tmp_path: Path) -> None:
        # Build content that exactly hits the budget.
        sample = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. "
        text = ""
        while count_tokens(text) < SkillInjector.TOKEN_BUDGET:
            text += sample
        # Trim back to exactly budget.
        while count_tokens(text) > SkillInjector.TOKEN_BUDGET:
            text = text[:-1]
        spec = _spec(tmp_path, "x", text)
        assert spec.content_token_count <= SkillInjector.TOKEN_BUDGET

        injector = SkillInjector()
        out = await injector.inject(spec)
        assert out == spec.content

    @pytest.mark.asyncio
    async def test_summariser_not_called_when_under_budget(
        self,
        tmp_path: Path,
    ) -> None:
        spec = _spec(tmp_path, "x", "Short content.")
        summariser = FakeSummariser()
        injector = SkillInjector(summariser=summariser)
        await injector.inject(spec)
        assert summariser.calls == []


class TestOverBudgetWithSummariser:
    @pytest.mark.asyncio
    async def test_summariser_called_and_output_returned(
        self,
        tmp_path: Path,
    ) -> None:
        # Build something > budget.
        big = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 600
        spec = _spec(tmp_path, "x", big)
        assert spec.content_token_count > SkillInjector.TOKEN_BUDGET

        summariser = FakeSummariser(return_value="brief summary.")
        injector = SkillInjector(summariser=summariser)
        out = await injector.inject(spec)
        assert out == "brief summary."
        assert summariser.calls == [big]

    @pytest.mark.asyncio
    async def test_summariser_output_within_budget(self, tmp_path: Path) -> None:
        big = "x " * 5000  # well over 2000 tokens
        spec = _spec(tmp_path, "x", big)
        summariser = FakeSummariser(return_value="a short summary.")
        injector = SkillInjector(summariser=summariser)
        out = await injector.inject(spec)
        assert count_tokens(out) <= SkillInjector.TOKEN_BUDGET

    @pytest.mark.asyncio
    async def test_over_budget_summariser_falls_through_to_truncation(
        self,
        tmp_path: Path,
    ) -> None:
        # Summariser returns something STILL over budget — defensive
        # fall-through to truncation. Use plenty of headroom so the
        # truncated result must still contain the marker.
        big = "x " * 5000
        over_budget_summary = "y " * 5000  # ~5000 tokens
        spec = _spec(tmp_path, "x", big)
        summariser = OverBudgetSummariser(return_value=over_budget_summary)
        injector = SkillInjector(summariser=summariser)
        out = await injector.inject(spec)
        assert count_tokens(out) <= SkillInjector.TOKEN_BUDGET
        assert out.endswith(MARKER)


class TestOverBudgetWithoutSummariser:
    @pytest.mark.asyncio
    async def test_truncates_with_marker(self, tmp_path: Path) -> None:
        big = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 600
        spec = _spec(tmp_path, "x", big)
        injector = SkillInjector()  # no summariser
        out = await injector.inject(spec)
        assert out.endswith(MARKER)
        assert count_tokens(out) <= SkillInjector.TOKEN_BUDGET

    @pytest.mark.asyncio
    async def test_truncation_result_starts_with_original_prefix(
        self,
        tmp_path: Path,
    ) -> None:
        big = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 600
        spec = _spec(tmp_path, "x", big)
        injector = SkillInjector()
        out = await injector.inject(spec)
        # Strip the marker; remainder must be a prefix of the original
        # content.
        prefix = out[: -len(MARKER)]
        assert big.startswith(prefix)

    @pytest.mark.asyncio
    async def test_truncation_hits_budget_tightly(self, tmp_path: Path) -> None:
        # The bisection should produce output very close to the budget
        # (token count equal to budget, since the cl100k_base char/token
        # ratio is fine-grained enough that we don't lose much headroom).
        big = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 600
        spec = _spec(tmp_path, "x", big)
        injector = SkillInjector()
        out = await injector.inject(spec)
        out_tokens = count_tokens(out)
        # Within 5 tokens of budget — generous bound; the probe showed
        # exact hits in Phase 3 §5.
        assert SkillInjector.TOKEN_BUDGET - 5 <= out_tokens <= SkillInjector.TOKEN_BUDGET


class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_empty_content(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path, "x", "")
        injector = SkillInjector()
        out = await injector.inject(spec)
        assert out == ""

    @pytest.mark.asyncio
    async def test_very_long_content_terminates(self, tmp_path: Path) -> None:
        # Sanity: a 1 MB body still terminates in reasonable time and
        # produces a marker. Token-aware bisection is O(log N).
        big = "Lorem ipsum dolor sit amet. " * 40000  # ~1 MB
        spec = _spec(tmp_path, "x", big)
        injector = SkillInjector()
        out = await injector.inject(spec)
        assert out.endswith(MARKER)
        assert count_tokens(out) <= SkillInjector.TOKEN_BUDGET


class TestTruncateHelper:
    """Direct tests of the private ``_truncate`` helper to lock in
    edge-case behaviour the public API doesn't exercise via ``inject``."""

    def test_under_budget_returns_verbatim_no_marker(self) -> None:
        from persona.skills.injector import _truncate

        out = _truncate("short text.", budget=500, skill="probe", source="content")
        assert out == "short text."
        assert MARKER not in out

    def test_budget_smaller_than_marker_returns_marker_only(self) -> None:
        from persona.skills.injector import _truncate

        out = _truncate("any non-empty content " * 100, budget=2, skill="probe", source="content")
        # Marker is 5 tokens; budget < marker → just the marker.
        assert out == MARKER

    def test_budget_equals_marker_returns_marker_only(self) -> None:
        from persona.skills.injector import _MARKER_TOKENS, _truncate

        # Budget equals marker token count → target = 0 → return marker.
        out = _truncate(
            "plenty of content " * 100, budget=_MARKER_TOKENS, skill="probe", source="content"
        )
        assert out == MARKER

    def test_marker_is_exactly_five_tokens(self) -> None:
        # Pins the marker constant. If MARKER ever changes, this test
        # forces a deliberate update.
        from persona.skills.injector import _MARKER_TOKENS

        assert _MARKER_TOKENS == 5
        assert count_tokens(MARKER) == 5

    def test_deterministic(self) -> None:
        from persona.skills.injector import _truncate

        big = "Lorem ipsum dolor sit amet. " * 600
        out1 = _truncate(big, 500, skill="probe", source="content")
        out2 = _truncate(big, 500, skill="probe", source="content")
        assert out1 == out2


class TestStateless:
    @pytest.mark.asyncio
    async def test_two_skills_independent(self, tmp_path: Path) -> None:
        # The injector has no per-turn state; injecting one skill must
        # not affect the next. Spec §7.1's "only one per turn" rule is
        # enforced by the RUNTIME, not the injector.
        s1 = _spec(tmp_path, "a", "first body.")
        s2 = _spec(tmp_path, "b", "second body.")
        injector = SkillInjector()
        out1 = await injector.inject(s1)
        out2 = await injector.inject(s2)
        assert out1 == "first body."
        assert out2 == "second body."

    @pytest.mark.asyncio
    async def test_repeated_inject_same_skill_idempotent(
        self,
        tmp_path: Path,
    ) -> None:
        big = "Lorem ipsum dolor sit amet. " * 600
        spec = _spec(tmp_path, "x", big)
        injector = SkillInjector()
        out1 = await injector.inject(spec)
        out2 = await injector.inject(spec)
        assert out1 == out2


class TestTruncationIsAudible:
    """A skill cut to fit the budget must say so.

    Until 2026-09-16 truncation emitted nothing. The module's only warning guards the
    summariser overshoot, which cannot happen because no summariser is wired, so the branch
    that actually runs in production was silent. The built-in `web_research` skill has been
    losing 1,070 of its 3,069 tokens on every injection — its whole source-evaluation rubric
    — and an ingested skill measured 17,627 tokens against a 2,000 budget. Both were found by
    a person reading files, which is not a mechanism.
    """

    def test_a_truncated_skill_logs_its_name_and_what_was_lost(self) -> None:
        from persona.skills.injector import _truncate

        captured: list[str] = []
        sink = _loguru_logger.add(lambda m: captured.append(str(m)), level="WARNING")
        try:
            _truncate("plenty of content " * 500, 100, skill="web_research", source="content")
        finally:
            _loguru_logger.remove(sink)

        assert captured, "truncation must not be silent"
        said = " ".join(captured)
        assert "web_research" in said, "the log must name the skill, or it is not actionable"
        assert "truncated" in said.lower()

    def test_content_within_budget_logs_nothing(self) -> None:
        """The warning must mean something. A skill that fits is not an event."""
        from persona.skills.injector import _truncate

        captured: list[str] = []
        sink = _loguru_logger.add(lambda m: captured.append(str(m)), level="WARNING")
        try:
            _truncate("short.", 500, skill="tiny", source="content")
        finally:
            _loguru_logger.remove(sink)

        assert captured == []

"""Unit tests for the contract echo + adjust-by-reply (Spec A4, T3)."""

from __future__ import annotations

from persona.schedules import RecurrenceFreq, RecurrenceRule
from persona.tasks import UpdateGranularity, UpdatePreference
from persona.tools.categories import ActionCategory
from persona.tools.category_policy import CategoryDecision
from persona_runtime.task_origination import (
    ECHO_PROMPT,
    ECHO_PROMPT_VERSION,
    Clause,
    ContractDraft,
    GrantSpec,
    ParsedSchedule,
    amend_goal,
    amend_schedule,
    clear_grant,
    render_clause,
    render_echo,
    set_grant,
)


def _schedule(human: str = "every weekday at 07:00 your time") -> ParsedSchedule:
    return ParsedSchedule(
        recurrence=RecurrenceRule(freq=RecurrenceFreq.WEEKLY, byday=("MO", "TU", "WE", "TH", "FR")),
        timezone="Europe/Oslo",
        human_terms=human,
    )


def _draft_with_two_grants() -> ContractDraft:
    return ContractDraft(
        goal="track fares and book the trip",
        scope="under 2000kr",
        schedule=_schedule(),
        grants=(
            GrantSpec(
                category=ActionCategory.SPEND,
                cap_micros=15_000_000,
                human="I may book it if it's under 1500kr — a spend permission, 1500kr cap",
            ),
            GrantSpec(
                category=ActionCategory.EXTERNAL_MUTATE,
                human="I can hold a seat on the booking site",
            ),
        ),
        updates=UpdatePreference(granularity=UpdateGranularity.MILESTONES, channel="web"),
    )


def test_echo_includes_every_clause_and_each_grant_on_its_own_line() -> None:
    echo = render_echo(_draft_with_two_grants())
    assert "Goal: track fares and book the trip" in echo
    assert "Scope: under 2000kr" in echo
    assert "When: every weekday at 07:00 your time" in echo
    assert "Within bounds:" in echo
    # Each beyond-default grant is its own prominent line (criterion 4 — no surprise grants).
    assert "  - I may book it if it's under 1500kr — a spend permission, 1500kr cap" in echo
    assert "  - I can hold a seat on the booking site" in echo
    assert "Updates: at milestones (and when it's done) on web" in echo


def test_echo_with_no_grants_states_default_bounds() -> None:
    draft = ContractDraft(goal="just watch the portal", schedule=_schedule())
    echo = render_echo(draft)
    assert "Within bounds: nothing beyond your usual permissions." in echo


def test_quiet_updates_clause_is_explicit() -> None:
    draft = ContractDraft(
        goal="g",
        schedule=_schedule(),
        updates=UpdatePreference(granularity=UpdateGranularity.QUIET),
    )
    assert "Updates: quietly" in render_clause(draft, Clause.UPDATES)


def test_render_clause_recurring_shows_cadence_and_timezone_no_invite() -> None:
    # A recurring task renders the real cadence + the concrete timezone (transparent), and does NOT
    # invite recurrence (it already recurs).
    line = render_clause(ContractDraft(goal="g", schedule=_schedule()), Clause.SCHEDULE)
    assert "every weekday at 07:00 your time" in line
    assert "Europe/Oslo" in line  # the concrete zone, not just "your time"
    assert "recurring?" not in line  # no upgrade invite on an already-recurring task


def test_render_clause_one_time_states_runs_once_and_invites_recurrence() -> None:
    from datetime import UTC, datetime

    one_time = ParsedSchedule(
        one_time_at=datetime(2099, 1, 2, 8, 0, tzinfo=UTC),
        timezone="Europe/Oslo",
        human_terms="once, on Friday 02 January at 09:00 your time",
    )
    line = render_clause(ContractDraft(goal="g", schedule=one_time), Clause.SCHEDULE)
    assert "once," in line  # runs once — honest
    assert "Europe/Oslo" in line
    assert "Want it recurring?" in line  # the now-honest upgrade invite (schedule-attach works)


def test_render_clause_schedule_unset_states_one_off_and_promises_no_cadence() -> None:
    # No schedule is attached in the flow yet, so the task runs once. The schedule line must state
    # that plainly (the authoritative one-off framing) and must NOT invite a recurrence the backend
    # can't yet attach — so it neutralises even a judge goal that reads "brief you every morning".
    line = render_clause(ContractDraft(goal="brief me every morning"), Clause.SCHEDULE)
    assert line == "When: runs once — no recurring schedule"
    assert "recurring" in line  # the cadence is explicitly denied, not merely omitted


def test_amend_goal_returns_new_frozen_draft_unchanged_original() -> None:
    draft = ContractDraft(goal="old goal", schedule=_schedule())
    amended = amend_goal(draft, "new goal")
    assert amended.goal == "new goal"
    assert draft.goal == "old goal"  # original untouched (frozen)
    assert render_clause(amended, Clause.GOAL) == "Goal: new goal"


def test_amend_schedule_re_echoes_only_the_schedule_clause() -> None:
    draft = ContractDraft(goal="g", schedule=_schedule("every weekday at 07:00 your time"))
    amended = amend_schedule(draft, _schedule("every day at 08:00 your time"))
    # The re-echoed line carries the amended cadence + the concrete timezone (recurring, no invite).
    expected = "When: every day at 08:00 your time · Europe/Oslo"
    assert render_clause(amended, Clause.SCHEDULE) == expected


def test_set_grant_replaces_same_category() -> None:
    draft = _draft_with_two_grants()
    tighter = GrantSpec(
        category=ActionCategory.SPEND, cap_micros=2_000_000, human="up to 200kr only"
    )
    amended = set_grant(draft, tighter)
    spend_grants = [g for g in amended.grants if g.category is ActionCategory.SPEND]
    assert len(spend_grants) == 1
    assert spend_grants[0].cap_micros == 2_000_000


def test_clear_grant_removes_category() -> None:
    draft = _draft_with_two_grants()
    amended = clear_grant(draft, ActionCategory.EXTERNAL_MUTATE)
    assert all(g.category is not ActionCategory.EXTERNAL_MUTATE for g in amended.grants)
    assert any(g.category is ActionCategory.SPEND for g in amended.grants)


def test_default_grant_text_used_when_human_absent() -> None:
    draft = ContractDraft(
        goal="g",
        schedule=_schedule(),
        grants=(GrantSpec(category=ActionCategory.SPEND, cap_micros=5_000_000),),
    )
    assert "a spend permission, up to 500kr" in render_clause(draft, Clause.BOUNDS)


def test_echo_prompt_artifact_is_versioned_and_embeds_structure() -> None:
    assert ECHO_PROMPT_VERSION == "a4-echo-v1"
    assert "{structured_echo}" in ECHO_PROMPT
    # The artifact insists every permission line is preserved (no skim-past).
    assert "permission" in ECHO_PROMPT.lower()


def test_grant_decision_default_is_allow() -> None:
    grant = GrantSpec(category=ActionCategory.SPEND, cap_micros=1_000_000)
    assert grant.decision is CategoryDecision.ALLOW

"""Unit tests for the VOICE echo mode (Spec A9, T1; A9-D-2).

The VOICE mode renders the SAME clauses as the A4 chat echo — no grant dropped (the completeness
guarantee holds on voice) — phrased for the ear: sentence-shaped, no markdown/bullets, the schedule
``·`` spoken as a comma, granularity free of parentheticals, ending in one explicit confirm ask.
CHAT stays byte-identical (pinned here + by the existing A4 echo suite).
"""

from __future__ import annotations

import re

from persona.schedules import RecurrenceFreq, RecurrenceRule
from persona.tasks import UpdateGranularity, UpdatePreference
from persona.tools.categories import ActionCategory
from persona_runtime.task_origination import (
    ECHO_PROMPT_VOICE,
    ECHO_PROMPT_VOICE_VERSION,
    Clause,
    ContractDraft,
    EchoMode,
    GrantSpec,
    ParsedSchedule,
    render_clause,
    render_echo,
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
                human="I may book it if it's under 1500kr, a spend permission with a 1500kr cap",
            ),
            GrantSpec(
                category=ActionCategory.EXTERNAL_MUTATE,
                human="I can hold a seat on the booking site",
            ),
        ),
        updates=UpdatePreference(granularity=UpdateGranularity.MILESTONES, channel="web"),
    )


# --- the completeness guarantee holds on voice --------------------------------------------


def test_voice_echo_speaks_goal_schedule_tz_and_every_grant() -> None:
    echo = render_echo(_draft_with_two_grants(), EchoMode.VOICE)
    assert "track fares and book the trip" in echo
    assert "every weekday at 07:00 your time" in echo
    assert "Europe/Oslo" in echo  # the concrete zone is still named
    # BOTH grants are spoken (no grant dropped on voice — the completeness guarantee).
    assert "I may book it if it's under 1500kr, a spend permission with a 1500kr cap" in echo
    assert "I can hold a seat on the booking site" in echo


def test_voice_echo_ends_in_a_single_explicit_confirm_ask() -> None:
    echo = render_echo(_draft_with_two_grants(), EchoMode.VOICE)
    assert echo.rstrip().endswith("Say yes to confirm, or tell me what to change.")


# --- phrased for the ear: no markdown/bullets, no middot, no parentheticals ----------------


def test_voice_echo_has_no_markdown_bullets_or_middot() -> None:
    echo = render_echo(_draft_with_two_grants(), EchoMode.VOICE)
    assert "  - " not in echo  # no bulleted list (the V11 register forbids spoken lists)
    assert "\n" not in echo  # one flowing spoken block, not a multi-line structure
    assert "·" not in echo  # the tz separator is spoken as a comma, never the middot glyph


def test_voice_updates_clause_has_no_parenthetical() -> None:
    draft = ContractDraft(
        goal="g",
        schedule=_schedule(),
        updates=UpdatePreference(granularity=UpdateGranularity.MILESTONES),
    )
    line = render_clause(draft, Clause.UPDATES, EchoMode.VOICE)
    assert "(" not in line
    assert ")" not in line
    assert "at milestones, and when it's done" in line


def test_voice_quiet_updates_clause_has_no_em_dash() -> None:
    draft = ContractDraft(
        goal="g",
        schedule=_schedule(),
        updates=UpdatePreference(granularity=UpdateGranularity.QUIET),
    )
    line = render_clause(draft, Clause.UPDATES, EchoMode.VOICE)
    assert "—" not in line
    assert "–" not in line
    assert "quietly" in line


# --- schedule shapes on voice --------------------------------------------------------------


def test_voice_schedule_recurring_names_cadence_and_zone_with_comma() -> None:
    draft = ContractDraft(goal="g", schedule=_schedule())
    line = render_clause(draft, Clause.SCHEDULE, EchoMode.VOICE)
    assert line == "When: every weekday at 07:00 your time, Europe/Oslo."


def test_voice_schedule_one_time_states_runs_once_and_invites_recurrence() -> None:
    from datetime import UTC, datetime

    one_time = ParsedSchedule(
        one_time_at=datetime(2099, 1, 2, 8, 0, tzinfo=UTC),
        timezone="Europe/Oslo",
        human_terms="once, on Friday 02 January at 09:00 your time",
    )
    draft = ContractDraft(goal="g", schedule=one_time)
    line = render_clause(draft, Clause.SCHEDULE, EchoMode.VOICE)
    assert "runs once" in line
    assert "recurring" in line  # the honest upgrade invite
    assert "Europe/Oslo" in line


def test_voice_schedule_unset_states_one_off_denies_cadence() -> None:
    draft = ContractDraft(goal="brief me every morning")
    line = render_clause(draft, Clause.SCHEDULE, EchoMode.VOICE)
    assert line == "It runs once, with no recurring schedule."


# --- bounds on voice -----------------------------------------------------------------------


def test_voice_bounds_no_grants_is_a_short_sentence() -> None:
    draft = ContractDraft(goal="just watch the portal", schedule=_schedule())
    line = render_clause(draft, Clause.BOUNDS, EchoMode.VOICE)
    assert line == "This stays within your usual permissions."


def test_voice_bounds_folds_grants_into_one_clause_not_a_list() -> None:
    line = render_clause(_draft_with_two_grants(), Clause.BOUNDS, EchoMode.VOICE)
    assert line.startswith("I'll also be allowed to:")
    assert "  - " not in line
    # both grants present, joined
    assert "1500kr cap" in line
    assert "hold a seat" in line


def test_voice_bounds_uses_default_grant_text_when_human_absent() -> None:
    draft = ContractDraft(
        goal="g",
        schedule=_schedule(),
        grants=(GrantSpec(category=ActionCategory.SPEND, cap_micros=5_000_000),),
    )
    line = render_clause(draft, Clause.BOUNDS, EchoMode.VOICE)
    assert "a spend permission, up to 500kr" in line


# --- CHAT stays byte-identical -------------------------------------------------------------


def test_chat_mode_is_the_default_and_unchanged() -> None:
    draft = _draft_with_two_grants()
    # The default arg is CHAT, and passing CHAT explicitly is identical.
    assert render_echo(draft) == render_echo(draft, EchoMode.CHAT)
    assert render_clause(draft, Clause.GOAL) == render_clause(draft, Clause.GOAL, EchoMode.CHAT)


def test_chat_echo_snapshot_pinned() -> None:
    # A literal pin so any drift to the chat echo (A4's surface) is caught here too.
    draft = _draft_with_two_grants()
    expected = (
        "Goal: track fares and book the trip\n"
        "Scope: under 2000kr\n"
        "When: every weekday at 07:00 your time · Europe/Oslo\n"
        "Within bounds:\n"
        "  - I may book it if it's under 1500kr, a spend permission with a 1500kr cap\n"
        "  - I can hold a seat on the booking site\n"
        "Updates: at milestones (and when it's done) on web"
    )
    assert render_echo(draft, EchoMode.CHAT) == expected


# --- the voice prompt artifact -------------------------------------------------------------


def test_voice_prompt_artifact_is_versioned_and_embeds_structure() -> None:
    assert ECHO_PROMPT_VOICE_VERSION == "a9-echo-voice-v1"
    assert "{structured_echo}" in ECHO_PROMPT_VOICE
    assert "permission" in ECHO_PROMPT_VOICE.lower()  # completeness is insisted on for voice too
    assert "voice call" in ECHO_PROMPT_VOICE.lower()  # the register cue


def test_voice_echo_has_no_url_or_code_artifacts() -> None:
    # A spoken echo must never carry a URL or code/markdown token (the V12 leak-gate discipline).
    echo = render_echo(_draft_with_two_grants(), EchoMode.VOICE)
    assert not re.search(r"https?://|www\.|\{\{|\}\}|```|<[a-z/]", echo)

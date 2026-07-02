"""Unit tests for amendment materiality + clause-diff (Spec A4, T9; A4-D-4)."""

from __future__ import annotations

from persona.approvals.records import Materiality
from persona.schedules import RecurrenceFreq, RecurrenceRule
from persona.tasks import UpdateGranularity, UpdatePreference
from persona.tools.categories import ActionCategory
from persona_runtime.task_origination import (
    Clause,
    ContractDraft,
    GrantSpec,
    ParsedSchedule,
    changed_clauses,
    classify_amendment_materiality,
)


def _sched(
    byhour: tuple[int, ...], *, freq: RecurrenceFreq = RecurrenceFreq.DAILY
) -> ParsedSchedule:
    period = "day" if freq is RecurrenceFreq.DAILY else "week"
    return ParsedSchedule(
        recurrence=RecurrenceRule(freq=freq, byhour=byhour),
        timezone="Europe/Oslo",
        human_terms=f"every {period} at {byhour[0]:02d}:00",
    )


def _draft(**kwargs: object) -> ContractDraft:
    base: dict[str, object] = {"goal": "track the fares", "schedule": _sched((7,))}
    base.update(kwargs)
    return ContractDraft(**base)  # type: ignore[arg-type]


# --- clause diff -----------------------------------------------------------------------


def test_changed_clauses_detects_each_clause() -> None:
    before = _draft()
    assert changed_clauses(before, before.model_copy(update={"goal": "new"})) == (Clause.GOAL,)
    assert changed_clauses(before, before.model_copy(update={"scope": "x"})) == (Clause.SCOPE,)
    assert changed_clauses(before, before.model_copy(update={"schedule": _sched((8,))})) == (
        Clause.SCHEDULE,
    )


def test_no_change_is_empty() -> None:
    d = _draft()
    assert changed_clauses(d, d) == ()
    assert classify_amendment_materiality(d, d) is Materiality.IMMATERIAL


# --- tuning (immaterial): re-echo the clause, stay pending ------------------------------


def test_time_of_day_tweak_is_tuning() -> None:
    # "make it 8am" — same cadence, only the hour moves → tuning (A4-D-4 example).
    before = _draft(schedule=_sched((7,)))
    after = _draft(schedule=_sched((8,)))
    assert classify_amendment_materiality(before, after) is Materiality.IMMATERIAL


def test_scope_addition_is_tuning() -> None:
    before = _draft(scope="flights only")
    after = _draft(scope="flights and hotels")  # "also include hotels"
    assert classify_amendment_materiality(before, after) is Materiality.IMMATERIAL


def test_update_preference_tweak_is_tuning() -> None:
    before = _draft(updates=UpdatePreference(granularity=UpdateGranularity.MILESTONES))
    after = _draft(updates=UpdatePreference(granularity=UpdateGranularity.QUIET))
    assert classify_amendment_materiality(before, after) is Materiality.IMMATERIAL


# --- material: re-confirm the whole contract -------------------------------------------


def test_goal_change_is_material() -> None:
    before = _draft(goal="track the fares")
    after = _draft(goal="book the cheapest fare")
    assert classify_amendment_materiality(before, after) is Materiality.MATERIAL


def test_spend_cap_change_is_material() -> None:
    before = _draft(grants=(GrantSpec(category=ActionCategory.SPEND, cap_micros=10_000_000),))
    after = _draft(grants=(GrantSpec(category=ActionCategory.SPEND, cap_micros=20_000_000),))
    assert classify_amendment_materiality(before, after) is Materiality.MATERIAL


def test_schedule_cadence_rewrite_is_material() -> None:
    # daily → weekly is a rewrite, not a time tweak → material.
    before = _draft(schedule=_sched((7,), freq=RecurrenceFreq.DAILY))
    after = _draft(schedule=_sched((7,), freq=RecurrenceFreq.WEEKLY))
    assert classify_amendment_materiality(before, after) is Materiality.MATERIAL


def test_new_permission_is_material() -> None:
    before = _draft()
    after = _draft(grants=(GrantSpec(category=ActionCategory.EXTERNAL_MUTATE),))
    assert classify_amendment_materiality(before, after) is Materiality.MATERIAL


def test_material_dominates_a_mixed_change() -> None:
    # A change touching both a tuning clause (scope) and a material one (goal) is material.
    before = _draft(goal="g1", scope="s1")
    after = _draft(goal="g2", scope="s2")
    assert classify_amendment_materiality(before, after) is Materiality.MATERIAL

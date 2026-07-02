"""Adjust-by-reply on a pending contract draft (Spec A4, T9; A4-D-4).

A reply to a pending proposal that is not a clean confirmation is an **amendment**, not a
dead end: the persona applies it, re-echoes the changed clause, and stays in the proposal
state until a clean confirmation. A confirm-with-a-tweak ("yes but make it 8am") therefore
amends (8am) and re-echoes — it never silently loses the standing intent, and it never creates
on the tweak.

Whether an amendment is **material** (a bigger change the user should re-read whole) or
**tuning** (a small clause edit) is the shared A4-D-4/A3-D-3 line: this module reuses A3's
:func:`classify_modification` **verbatim**. The only A4-side part is the clause→payload-key
mapping (per A4-D-4) — it maps each changed contract clause onto a key whose class
``classify_modification`` already knows: goal / spend-amount / a new permission / a schedule
*cadence rewrite* land on non-phrasing keys (material), while a schedule *time-of-day* tweak, a
scope detail, or an update-preference tweak land on phrasing keys (tuning). The arbiter stays A3's.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from persona.approvals.records import Materiality, classify_modification
from persona.tools.categories import ActionCategory

from persona_runtime.task_origination.echo import Clause

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from persona_runtime.task_origination.draft import ContractDraft, ParsedSchedule

__all__ = [
    "AmendmentInterpreter",
    "changed_clauses",
    "classify_amendment_materiality",
]


@runtime_checkable
class AmendmentInterpreter(Protocol):
    """The model-backed step that turns a reply into an amended draft (or ``None``).

    Given the user's reply and the pending draft, return the **amended draft** when the reply
    adjusts the contract (a new time, a tightened cap, an added scope), or ``None`` when the
    reply is not an amendment (a question, a topic change). It never creates anything.
    """

    def interpret(self, reply: str, draft: ContractDraft) -> Awaitable[ContractDraft | None]:
        """Interpret ``reply`` as an amendment to ``draft``; return the amended draft or None."""
        ...


def changed_clauses(before: ContractDraft, after: ContractDraft) -> tuple[Clause, ...]:
    """The clauses that differ between two drafts (so only those are re-echoed)."""
    clauses: list[Clause] = []
    if before.goal != after.goal:
        clauses.append(Clause.GOAL)
    if before.scope != after.scope:
        clauses.append(Clause.SCOPE)
    if before.schedule != after.schedule:
        clauses.append(Clause.SCHEDULE)
    if before.grants != after.grants:
        clauses.append(Clause.BOUNDS)
    if before.updates != after.updates:
        clauses.append(Clause.UPDATES)
    return tuple(clauses)


def _spend_cap(draft: ContractDraft) -> int | None:
    """The draft's spend cap (the SPEND grant's cap), or ``None``."""
    return next(
        (g.cap_micros for g in draft.grants if g.category is ActionCategory.SPEND),
        None,
    )


def _permission_categories(draft: ContractDraft) -> frozenset[ActionCategory]:
    """The set of beyond-default permission categories the draft grants."""
    return frozenset(g.category for g in draft.grants)


def _is_time_only_schedule_change(
    before: ParsedSchedule | None, after: ParsedSchedule | None
) -> bool:
    """True iff the schedule change is only a time-of-day tweak (same cadence, same days)."""
    if before is None or after is None:
        return False
    rb, ra = before.recurrence, after.recurrence
    if rb is None or ra is None:
        return False  # a recurring↔one-time change is a rewrite, not a tweak
    return (
        rb.freq == ra.freq
        and rb.interval == ra.interval
        and rb.byday == ra.byday
        and rb.bymonthday == ra.bymonthday
        and (rb.byhour, rb.byminute) != (ra.byhour, ra.byminute)
    )


def _amendment_payloads(
    before: ContractDraft, after: ContractDraft
) -> tuple[dict[str, str], dict[str, str]]:
    """Map the changed clauses onto classify_modification keys (the A4-side mapping, A4-D-4).

    Material clauses land on non-phrasing keys (``goal``/``amount``/``permission``/
    ``schedule_cadence``); tuning clauses land on phrasing keys (``note``/``description``) so
    A3's arbiter classifies them immaterial.
    """
    before_map: dict[str, str] = {}
    after_map: dict[str, str] = {}

    def _set(key: str, b: str, a: str) -> None:
        before_map[key] = b
        after_map[key] = a

    if before.goal != after.goal:
        _set("goal", before.goal, after.goal)  # material (non-phrasing)
    if _spend_cap(before) != _spend_cap(after):
        _set("amount", str(_spend_cap(before)), str(_spend_cap(after)))  # material
    if _permission_categories(before) != _permission_categories(after):
        _set(  # a new/removed permission is material (non-phrasing key)
            "permission",
            ",".join(sorted(c.value for c in _permission_categories(before))),
            ",".join(sorted(c.value for c in _permission_categories(after))),
        )
    if before.schedule != after.schedule:
        if _is_time_only_schedule_change(before.schedule, after.schedule):
            _set("note", _schedule_terms(before), _schedule_terms(after))  # phrasing → tuning
        else:
            _set("schedule_cadence", _schedule_terms(before), _schedule_terms(after))  # material
    if before.scope != after.scope:
        _set("description", before.scope, after.scope)  # phrasing → tuning
    if before.updates != after.updates:
        _set("comment", _updates_terms(before), _updates_terms(after))  # phrasing → tuning
    return before_map, after_map


def _schedule_terms(draft: ContractDraft) -> str:
    return draft.schedule.human_terms if draft.schedule is not None else ""


def _updates_terms(draft: ContractDraft) -> str:
    return f"{draft.updates.granularity.value}:{draft.updates.channel or ''}"


def classify_amendment_materiality(before: ContractDraft, after: ContractDraft) -> Materiality:
    """Classify an amendment as material (re-confirm whole) or tuning (re-echo the clause).

    Reuses A3's :func:`classify_modification` verbatim over the A4 clause→key mapping (A4-D-4).
    No change → immaterial.
    """
    before_map, after_map = _amendment_payloads(before, after)
    if not before_map and not after_map:
        return Materiality.IMMATERIAL
    return classify_modification(before_map, after_map)

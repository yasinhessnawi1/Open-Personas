"""The contract echo + adjust-by-reply (Spec A4, T3; A4-D-1).

The echo is the one moment autonomy's terms are set, so it is *complete and grant-prominent
by construction*, not at the model's discretion: :func:`render_echo` deterministically lays
out what / when / within-what-bounds (every beyond-default grant its own line) / how-you'll-
hear, so no permission can be skimmed past (criterion 4 — no surprise grants). The persona
*voices* that structured echo through the versioned prompt artifact (:data:`ECHO_PROMPT`,
:data:`ECHO_PROMPT_VERSION` — Spec 10 discipline); the structure guarantees completeness,
the artifact guarantees voice.

Adjust-by-reply amends one clause and re-echoes only it (:func:`render_clause`): the flow
tolerates revision without re-interrogating the whole contract (the alternative teaches
people not to create tasks). Whether an amendment *re-confirms* (material) or *applies*
(tuning) is A4-D-4's shared line, decided in T9; this module is the mechanical apply +
re-render.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from persona.tasks import UpdateGranularity, UpdatePreference
from persona.tools.categories import ActionCategory

if TYPE_CHECKING:
    from persona_runtime.task_origination.draft import ContractDraft, GrantSpec, ParsedSchedule

__all__ = [
    "ECHO_PROMPT",
    "ECHO_PROMPT_VERSION",
    "Clause",
    "amend_goal",
    "amend_schedule",
    "amend_scope",
    "amend_updates",
    "clear_grant",
    "render_clause",
    "render_echo",
    "set_grant",
]

#: Version of the persona-voicing prompt artifact (Spec 10 discipline). Bump on any change
#: to ``ECHO_PROMPT`` so an echo's wording is traceable to the artifact that produced it.
ECHO_PROMPT_VERSION = "a4-echo-v1"

#: The versioned prompt artifact: instruct the persona to voice the structured echo compactly,
#: in its own voice, **without dropping or softening any line** — completeness is load-bearing.
ECHO_PROMPT = (
    "You are about to take on a standing task. Read the structured contract below back to "
    "the user in your own voice, in a few short lines — warm and plain, like a capable "
    "assistant playing back an instruction. Keep every line: the goal, the schedule, EACH "
    "permission line exactly as written (never merge, drop, or soften a permission), and how "
    "you'll keep them posted. End by asking them to confirm or adjust. Do not add new terms.\n\n"
    "{structured_echo}"
)

#: The schedule line when no cadence is attached at all — the authoritative one-off statement that
#: promises no recurrence (rare now that every confirmed task is schedule-backed, but kept honest).
_NO_SCHEDULE_WHEN = "runs once — no recurring schedule"

#: Offered on a ONE-TIME task — honest now that schedule-attach works: replying with a cadence makes
#: the task recurring (the amendment path re-parses it against the schedule's timezone).
_RECURRENCE_INVITE = "Want it recurring? Tell me a cadence like 'every weekday at 8am'."

_GRANULARITY_HUMAN: dict[UpdateGranularity, str] = {
    UpdateGranularity.EVERY_LEG: "after every step",
    UpdateGranularity.MILESTONES: "at milestones (and when it's done)",
    UpdateGranularity.COMPLETION_ONLY: "only when it's done",
    UpdateGranularity.QUIET: "quietly — I'll only reach out if I need you or something goes wrong",
}


class Clause(StrEnum):
    """The amendable clauses of a contract draft (adjust-by-reply addresses one at a time)."""

    GOAL = "goal"
    SCOPE = "scope"
    SCHEDULE = "schedule"
    BOUNDS = "bounds"
    UPDATES = "updates"


def render_echo(draft: ContractDraft) -> str:
    """Lay out the complete, grant-prominent structured echo (A4-D-1, criterion 4).

    Every clause present in the draft is rendered; every beyond-default grant gets its own
    line. This is the completeness guarantee the persona then voices (:data:`ECHO_PROMPT`).
    """
    lines = [render_clause(draft, Clause.GOAL)]
    if draft.scope:
        lines.append(render_clause(draft, Clause.SCOPE))
    lines.append(render_clause(draft, Clause.SCHEDULE))
    lines.append(render_clause(draft, Clause.BOUNDS))
    lines.append(render_clause(draft, Clause.UPDATES))
    return "\n".join(lines)


def render_clause(draft: ContractDraft, clause: Clause) -> str:
    """Render one clause line (used for the full echo and the adjust-by-reply re-echo)."""
    if clause is Clause.GOAL:
        return f"Goal: {draft.goal}"
    if clause is Clause.SCOPE:
        return f"Scope: {draft.scope}" if draft.scope else "Scope: (whatever it takes)"
    if clause is Clause.SCHEDULE:
        # The authoritative schedule line — it always states exactly what will happen, so even a
        # judge goal that reads "brief you every morning" cannot imply a cadence it won't keep:
        #   - recurring   → the real cadence + the concrete timezone (transparent, adjust-by-reply);
        #   - one-time    → runs once at that instant + an honest invite to make it recurring;
        #   - no schedule → the plain one-off statement (rare now that every task is scheduled).
        sched = draft.schedule
        if sched is None:
            return f"When: {_NO_SCHEDULE_WHEN}"
        when = f"{sched.human_terms} · {sched.timezone}"
        if sched.recurrence is None:  # a one-time task — offer the (now-honest) recurring upgrade
            return f"When: {when}. {_RECURRENCE_INVITE}"
        return f"When: {when}"
    if clause is Clause.BOUNDS:
        return _render_bounds(draft)
    if clause is Clause.UPDATES:
        return _render_updates(draft.updates)
    msg = f"unknown clause {clause!r}"  # pragma: no cover - exhaustive enum
    raise ValueError(msg)


def _render_bounds(draft: ContractDraft) -> str:
    """The bounds clause — each beyond-default grant on its own prominent line."""
    if not draft.grants:
        return "Within bounds: nothing beyond your usual permissions."
    grant_lines = "\n".join(
        f"  - {grant.human or _default_grant_text(grant)}" for grant in draft.grants
    )
    return f"Within bounds:\n{grant_lines}"


def _render_updates(updates: UpdatePreference) -> str:
    """The updates clause — granularity + channel."""
    granularity = _GRANULARITY_HUMAN[updates.granularity]
    where = f" on {updates.channel}" if updates.channel else ""
    return f"Updates: {granularity}{where}"


def _default_grant_text(grant: GrantSpec) -> str:
    """A safe fallback grant line when the draft did not supply persona-voiced text."""
    if grant.category is ActionCategory.SPEND and grant.cap_micros is not None:
        kr = grant.cap_micros / 10_000
        return f"a spend permission, up to {kr:g}kr"
    return f"a {grant.category.value} permission ({grant.decision.value})"


# --- adjust-by-reply: amend one clause, return a new frozen draft -----------------------


def amend_goal(draft: ContractDraft, goal: str) -> ContractDraft:
    """Return a copy of ``draft`` with a new goal."""
    return draft.model_copy(update={"goal": goal})


def amend_scope(draft: ContractDraft, scope: str) -> ContractDraft:
    """Return a copy of ``draft`` with a new scope."""
    return draft.model_copy(update={"scope": scope})


def amend_schedule(draft: ContractDraft, schedule: ParsedSchedule) -> ContractDraft:
    """Return a copy of ``draft`` with a new parsed schedule."""
    return draft.model_copy(update={"schedule": schedule})


def amend_updates(draft: ContractDraft, updates: UpdatePreference) -> ContractDraft:
    """Return a copy of ``draft`` with a new update preference."""
    return draft.model_copy(update={"updates": updates})


def set_grant(draft: ContractDraft, grant: GrantSpec) -> ContractDraft:
    """Add or replace (by category) one beyond-default grant; return a new draft."""
    others = tuple(g for g in draft.grants if g.category is not grant.category)
    return draft.model_copy(update={"grants": (*others, grant)})


def clear_grant(draft: ContractDraft, category: ActionCategory) -> ContractDraft:
    """Remove any grant for ``category``; return a new draft."""
    remaining = tuple(g for g in draft.grants if g.category is not category)
    return draft.model_copy(update={"grants": remaining})

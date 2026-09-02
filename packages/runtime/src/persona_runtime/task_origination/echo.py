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
    "ECHO_PROMPT_VOICE",
    "ECHO_PROMPT_VOICE_VERSION",
    "Clause",
    "EchoMode",
    "amend_goal",
    "amend_schedule",
    "amend_scope",
    "amend_updates",
    "clear_grant",
    "render_clause",
    "render_echo",
    "set_grant",
]


class EchoMode(StrEnum):
    """The channel the echo is rendered for (Spec A9, A9-D-2).

    One renderer, two modes — mirroring how the prompt system gained ``PromptMode`` (V11-D-1).
    ``CHAT`` is the A4 echo, **byte-identical** to before A9 (snapshot-pinned). ``VOICE`` renders
    the *same* clauses (no grant dropped — the completeness guarantee is unchanged) phrased for
    the ear: sentence-shaped, no markdown/bullets, grants folded into one spoken clause, the
    schedule's ``·`` separator spoken as a comma, ending in a single explicit confirm ask.
    """

    CHAT = "chat"
    VOICE = "voice"


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

#: Version of the VOICE persona-voicing artifact (Spec A9, A9-D-2). Bump on any change to
#: ``ECHO_PROMPT_VOICE`` so a spoken echo's wording is traceable to the artifact that produced it.
ECHO_PROMPT_VOICE_VERSION = "a9-echo-voice-v1"

#: The VOICE prompt artifact: voice the structured contract for the ear — short spoken sentences,
#: no lists, every permission still spoken (completeness is load-bearing on voice too). The confirm
#: ask is already in the structured echo; the persona keeps it. Same completeness contract as the
#: chat artifact, tuned to the voice register (V11): one idea per sentence, plain speech, no markup.
ECHO_PROMPT_VOICE = (
    "You are about to take on a standing task, and you are on a voice call. Say the contract "
    "below back to the user in your own voice, in a few short spoken sentences — warm and plain, "
    "like a capable assistant playing back an instruction out loud. Speak every part: the goal, "
    "the schedule, EACH permission exactly as meant (never merge, drop, or soften a permission), "
    "and how you'll keep them posted. Do not read it as a list and do not add new terms. Keep the "
    "closing question that asks them to confirm or change it.\n\n"
    "{structured_echo}"
)

#: The schedule line when no cadence is attached at all — the authoritative one-off statement that
#: promises no recurrence (rare now that every confirmed task is schedule-backed, but kept honest).
_NO_SCHEDULE_WHEN = "runs once, with no recurring schedule"

#: Offered on a ONE-TIME task — honest now that schedule-attach works: replying with a cadence makes
#: the task recurring (the amendment path re-parses it against the schedule's timezone).
_RECURRENCE_INVITE = "Want it recurring? Tell me a cadence like 'every weekday at 8am'."

_GRANULARITY_HUMAN: dict[UpdateGranularity, str] = {
    UpdateGranularity.EVERY_LEG: "after every step",
    UpdateGranularity.MILESTONES: "at milestones (and when it's done)",
    UpdateGranularity.COMPLETION_ONLY: "only when it's done",
    UpdateGranularity.QUIET: (
        "quietly, and I'll only reach out if I need you or something goes wrong"
    ),
}

#: The VOICE granularity phrasings (A9-D-2) — no parentheticals or em-dashes (the V11 register:
#: short clauses, no asides), so the spoken updates line reads as one clean sentence.
_GRANULARITY_HUMAN_VOICE: dict[UpdateGranularity, str] = {
    UpdateGranularity.EVERY_LEG: "after every step",
    UpdateGranularity.MILESTONES: "at milestones, and when it's done",
    UpdateGranularity.COMPLETION_ONLY: "only when it's done",
    UpdateGranularity.QUIET: (
        "quietly, and I'll only reach out if I need you or something goes wrong"
    ),
}

#: The spoken confirm ask that closes a VOICE echo (A9-D-2 — a single explicit ask). The render
#: layer bakes it into the structured echo so the persona always speaks it (the chat path relies on
#: ``ECHO_PROMPT`` to add the ask; on voice it is part of the structure, so it cannot be dropped).
_VOICE_CONFIRM_ASK = "Say yes to confirm, or tell me what to change."


class Clause(StrEnum):
    """The amendable clauses of a contract draft (adjust-by-reply addresses one at a time)."""

    GOAL = "goal"
    SCOPE = "scope"
    SCHEDULE = "schedule"
    TRIGGER = "trigger"
    BOUNDS = "bounds"
    UPDATES = "updates"


def render_echo(draft: ContractDraft, mode: EchoMode = EchoMode.CHAT) -> str:
    """Lay out the complete, grant-prominent structured echo (A4-D-1, criterion 4; A9-D-2).

    Every clause present in the draft is rendered; every beyond-default grant is spoken (its own
    line in ``CHAT``, folded into one clause in ``VOICE``). This is the completeness guarantee the
    persona then voices (:data:`ECHO_PROMPT` / :data:`ECHO_PROMPT_VOICE`). ``CHAT`` is unchanged
    from the A4 echo; ``VOICE`` renders the same clauses for the ear + the spoken confirm ask.
    """
    if mode is EchoMode.VOICE:
        return _render_echo_voice(draft)
    lines = [render_clause(draft, Clause.GOAL)]
    if draft.scope:
        lines.append(render_clause(draft, Clause.SCOPE))
    # The single "When:" line is time-driven XOR event-driven (A7-D-3): render the trigger clause
    # when the contract watches an event, else the schedule clause.
    when_clause = Clause.TRIGGER if draft.trigger is not None else Clause.SCHEDULE
    lines.append(render_clause(draft, when_clause))
    lines.append(render_clause(draft, Clause.BOUNDS))
    lines.append(render_clause(draft, Clause.UPDATES))
    return "\n".join(lines)


def _render_echo_voice(draft: ContractDraft) -> str:
    """The VOICE echo — the same clauses, sentence-shaped, ending in the confirm ask (A9-D-2).

    No markdown/bullets (a bulleted structure tempts the model to read a list — the V11 register
    forbids spoken lists); grants fold into one clause; the schedule ``·`` is spoken as a comma.
    Every grant still appears (the completeness guarantee holds on voice), and the closing ask is
    part of the structure so it is never dropped.
    """
    sentences = [render_clause(draft, Clause.GOAL, EchoMode.VOICE)]
    if draft.scope:
        sentences.append(render_clause(draft, Clause.SCOPE, EchoMode.VOICE))
    sentences.append(render_clause(draft, Clause.SCHEDULE, EchoMode.VOICE))
    sentences.append(render_clause(draft, Clause.BOUNDS, EchoMode.VOICE))
    sentences.append(render_clause(draft, Clause.UPDATES, EchoMode.VOICE))
    sentences.append(_VOICE_CONFIRM_ASK)
    return " ".join(s for s in sentences if s)


def render_clause(draft: ContractDraft, clause: Clause, mode: EchoMode = EchoMode.CHAT) -> str:
    """Render one clause (used for the full echo and the adjust-by-reply re-echo), per mode.

    ``CHAT`` is byte-identical to the A4 clause line; ``VOICE`` renders the same content
    sentence-shaped for the ear (A9-D-2). An amendment re-echoes one clause in the caller's mode.
    """
    if mode is EchoMode.VOICE:
        return _render_clause_voice(draft, clause)
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
        if sched.cadence_note:
            # The honest degradation rider (R4, BUG A): the asked cadence was
            # unrepresentable and the parse fell back — say so, never silently.
            return f"When: {when}. {sched.cadence_note}"
        if sched.recurrence is None:  # a one-time task — offer the (now-honest) recurring upgrade
            return f"When: {when}. {_RECURRENCE_INVITE}"
        return f"When: {when}"
    if clause is Clause.TRIGGER:
        # The event "When:" (A7): render the concrete filter the user named (not the mechanism) —
        # "whenever an email from landlord@… arrives" (A7-R-3, comprehension over cleverness).
        trig = draft.trigger
        if trig is None:  # pragma: no cover — render_echo only routes here when a trigger is set
            return f"When: {_NO_SCHEDULE_WHEN}"
        return f"When: whenever {trig.human_terms}"
    if clause is Clause.BOUNDS:
        return _render_bounds(draft)
    if clause is Clause.UPDATES:
        return _render_updates(draft.updates)
    msg = f"unknown clause {clause!r}"  # pragma: no cover - exhaustive enum
    raise ValueError(msg)


def _render_clause_voice(draft: ContractDraft, clause: Clause) -> str:
    """Render one clause for the ear (A9-D-2) — the same content, spoken-sentence-shaped.

    The schedule's ``·`` timezone separator is spoken as a comma; the updates granularity uses the
    parenthetical-free VOICE phrasings; grants fold into one clause (each still named).
    """
    if clause is Clause.GOAL:
        return f"Here's what I'll do: {draft.goal}."
    if clause is Clause.SCOPE:
        return f"Scope: {draft.scope}." if draft.scope else ""
    if clause is Clause.SCHEDULE:
        sched = draft.schedule
        if sched is None:
            return "It runs once, with no recurring schedule."
        when = f"{sched.human_terms}, {sched.timezone}"
        if sched.cadence_note:
            # The honest degradation rider (R4, BUG A) — spoken too, never silent.
            return f"When: {when}. {sched.cadence_note}"
        if sched.recurrence is None:  # one-time — the now-honest recurring upgrade, spoken plainly
            return f"When: {when}. It runs once. Tell me if you'd like it recurring."
        return f"When: {when}."
    if clause is Clause.BOUNDS:
        return _render_bounds_voice(draft)
    if clause is Clause.UPDATES:
        return _render_updates_voice(draft.updates)
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


def _render_bounds_voice(draft: ContractDraft) -> str:
    """The bounds clause for the ear (A9-D-2) — grants folded into one spoken clause.

    Each beyond-default grant is still named (the completeness guarantee — no grant dropped on
    voice); they are joined into one sentence rather than a bulleted block (a list is not spoken).
    """
    if not draft.grants:
        return "This stays within your usual permissions."
    spoken = "; ".join(grant.human or _default_grant_text(grant) for grant in draft.grants)
    return f"I'll also be allowed to: {spoken}."


def _render_updates(updates: UpdatePreference) -> str:
    """The updates clause — granularity + channel."""
    granularity = _GRANULARITY_HUMAN[updates.granularity]
    where = f" on {updates.channel}" if updates.channel else ""
    return f"Updates: {granularity}{where}"


def _render_updates_voice(updates: UpdatePreference) -> str:
    """The updates clause for the ear (A9-D-2) — parenthetical-free, sentence-shaped."""
    granularity = _GRANULARITY_HUMAN_VOICE[updates.granularity]
    where = f" on {updates.channel}" if updates.channel else ""
    return f"I'll keep you posted {granularity}{where}."


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

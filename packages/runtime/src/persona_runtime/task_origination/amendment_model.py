"""The model-backed amendment interpreter — adjust-by-reply's precision layer (Spec A4, T9).

A reply to a pending proposal that is not a clean confirmation may be an **amendment**: "make it
8am", "cap it at 500kr instead", "only on weekdays". This turns such a reply into an *amended*
:class:`ContractDraft` by asking the model which clauses changed and to what, then applying only
those clauses onto the existing draft (every unspecified clause is preserved — an amendment edits,
never rebuilds). A reply that is not an amendment (a question, a topic change, a plain refusal)
yields ``None`` and the loop falls through.

Safety: the model emits a **clause patch**, not a whole draft, so it cannot silently drop the
goal or a grant it forgot to restate. A schedule tweak is re-parsed through the same parse-honesty
boundary the flow uses (:func:`parse_recurrence` / :func:`parse_one_time`); an unrepresentable or
un-anchorable schedule tweak is skipped rather than coerced. The materiality of the resulting
change (re-confirm whole vs re-echo the clause) is A3's line, applied downstream (A4-D-4).
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from persona.logging import get_logger
from persona.schema.conversation import ConversationMessage
from persona.tasks import UpdateGranularity, UpdatePreference
from persona.tools.categories import ActionCategory

from persona_runtime.errors import ScheduleParseError
from persona_runtime.task_origination.draft import ContractDraft, GrantSpec
from persona_runtime.task_origination.echo import (
    amend_goal,
    amend_schedule,
    amend_scope,
    amend_updates,
    clear_grant,
    set_grant,
)
from persona_runtime.task_origination.schedule import parse_recurrence

if TYPE_CHECKING:
    from datetime import datetime

    from persona.backends.protocol import ChatBackend

__all__ = ["AMENDMENT_PROMPT_VERSION", "ModelAmendmentInterpreter"]

_logger = get_logger("runtime.task_origination")

AMENDMENT_PROMPT_VERSION = "a4-amendment-v1"

_SYSTEM_PROMPT = """\
The user is replying to a task proposal their assistant just described. Decide whether the reply \
ADJUSTS the proposal (a new time, a different spending cap, a tightened scope, a change to how \
they'll be updated) — versus not being an amendment at all (a question, a topic change, a plain \
yes/no with nothing to change).

Only report the clauses the user actually changed; leave everything else out (unstated clauses \
are kept as-is). Do NOT restate unchanged clauses.

Reply with ONLY a JSON object, no prose:
{"amends": true|false,
 "goal": "<new goal, if changed>",
 "scope": "<new scope, if changed>",
 "spend_cap_kr": <new spending cap in kroner, if changed>,
 "clear_spend": true,            // only if the user removed the spending permission
 "schedule_rrule": "FREQ=...;BYHOUR=..",  // an RFC-5545 RRULE, if the cadence/time changed
 "updates_granularity": "every_leg"|"milestones"|"completion_only"|"quiet",
 "updates_channel": "<channel key, if changed>"}
If the reply is not an amendment at all, reply with {"amends": false}.
"""

_MAX_TOKENS = 400
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
_MICROS_PER_KR = 10_000
_GRANULARITIES = {g.value for g in UpdateGranularity}


class ModelAmendmentInterpreter:
    """An :class:`AmendmentInterpreter` over a chat backend (mockable; clause-patch based)."""

    def __init__(self, *, backend: ChatBackend) -> None:
        """Inject the small/mid-tier chat backend (the precision layer)."""
        self._backend = backend

    async def interpret(self, reply: str, draft: ContractDraft) -> ContractDraft | None:
        """Interpret ``reply`` as an amendment to ``draft`` → the amended draft, or ``None``."""
        now = self._now()
        current = (
            f"goal: {draft.goal}\nscope: {draft.scope or '(none)'}\n"
            f"spend_cap_kr: {self._current_cap_kr(draft)}\n"
            f"updates: {draft.updates.granularity.value} / {draft.updates.channel or 'home'}"
        )
        user = (
            f"Current proposal:\n{current}\n\nUser reply:\n\"{reply}\"\n\n"
            "Reply with the JSON object."
        )
        messages = [
            ConversationMessage(role="system", content=_SYSTEM_PROMPT, created_at=now),
            ConversationMessage(role="user", content=user, created_at=now),
        ]
        response = await self._backend.chat(messages, temperature=0.0, max_tokens=_MAX_TOKENS)
        return self._apply(response.content, draft)

    @staticmethod
    def _now() -> datetime:
        from datetime import UTC, datetime

        return datetime.now(UTC)

    @staticmethod
    def _current_cap_kr(draft: ContractDraft) -> str:
        cap = next(
            (g.cap_micros for g in draft.grants if g.category is ActionCategory.SPEND), None
        )
        return "(none)" if cap is None else str(cap // _MICROS_PER_KR)

    def _apply(self, text: str, draft: ContractDraft) -> ContractDraft | None:
        """Apply the model's clause patch onto ``draft`` (present clauses only), or ``None``."""
        payload = self._load_json(text)
        if payload is None or payload.get("amends") is not True:
            return None
        amended = draft
        amended = self._apply_text_clauses(payload, amended)
        amended = self._apply_spend(payload, amended)
        amended = self._apply_schedule(payload, amended)
        amended = self._apply_updates(payload, amended)
        # Guard: if the patch claimed to amend but changed nothing representable, it is not an
        # actionable amendment — return None so the loop does not re-echo an identical draft.
        return amended if amended != draft else None

    @staticmethod
    def _apply_text_clauses(payload: dict[str, object], draft: ContractDraft) -> ContractDraft:
        goal = payload.get("goal")
        if isinstance(goal, str) and goal.strip():
            draft = amend_goal(draft, goal.strip())
        scope = payload.get("scope")
        if isinstance(scope, str) and scope.strip():
            draft = amend_scope(draft, scope.strip())
        return draft

    def _apply_spend(self, payload: dict[str, object], draft: ContractDraft) -> ContractDraft:
        if payload.get("clear_spend") is True:
            return clear_grant(draft, ActionCategory.SPEND)
        cap = payload.get("spend_cap_kr")
        if isinstance(cap, (int, float)) and not isinstance(cap, bool) and cap > 0:
            return set_grant(
                draft,
                GrantSpec(category=ActionCategory.SPEND, cap_micros=int(cap * _MICROS_PER_KR)),
            )
        return draft

    def _apply_schedule(self, payload: dict[str, object], draft: ContractDraft) -> ContractDraft:
        rrule = payload.get("schedule_rrule")
        if not isinstance(rrule, str) or not rrule.strip():
            return draft
        if draft.schedule is None:
            # No existing cadence frame (timezone) to anchor the tweak — decline rather than
            # invent a timezone. A first schedule is set earlier in the flow, not by amendment.
            _logger.info("schedule amendment without an existing cadence frame; skipping")
            return draft
        try:
            parsed = parse_recurrence(
                rrule.strip(), draft.schedule.timezone, phrase=rrule.strip()
            )
        except ScheduleParseError:
            _logger.info("amended schedule not representable; skipping the schedule clause")
            return draft
        return amend_schedule(draft, parsed)

    @staticmethod
    def _apply_updates(payload: dict[str, object], draft: ContractDraft) -> ContractDraft:
        gran_raw = payload.get("updates_granularity")
        chan_raw = payload.get("updates_channel")
        gran = draft.updates.granularity
        channel = draft.updates.channel
        changed = False
        if isinstance(gran_raw, str) and gran_raw.strip().lower() in _GRANULARITIES:
            gran = UpdateGranularity(gran_raw.strip().lower())
            changed = True
        if isinstance(chan_raw, str) and chan_raw.strip():
            channel = chan_raw.strip()
            changed = True
        if not changed:
            return draft
        return amend_updates(draft, UpdatePreference(granularity=gran, channel=channel))

    @staticmethod
    def _load_json(text: str) -> dict[str, object] | None:
        stripped = _FENCE_RE.sub("", text).strip()
        try:
            loaded = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            return None
        return loaded if isinstance(loaded, dict) else None

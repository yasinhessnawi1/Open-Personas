"""The model-backed reschedule interpreter — the precision layer (Spec A8, T6).

The cue net (:func:`detect_reschedule_cue`) admits a turn; this resolves it. Given the user's
message and the caller's active tasks, it decides WHICH task the user means (list-then-resolve over
the task goals) and the new cadence — an A1-shaped RRULE candidate, a one-time instant, or a
skip-next. Honest by construction: exactly one confident match → ``RESOLVED``; more than one
plausible match → ``AMBIGUOUS`` (the loop lists them and asks); no confident match → ``NOT_FOUND``.
A hallucinated / absent id is never applied. The new cadence is passed on as a *candidate* string;
the SAME parser as create validates it (parse-honesty stays in one place).
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from persona.logging import get_logger
from persona.schema.conversation import ConversationMessage

from persona_runtime.task_origination.reschedule import (
    RescheduleIntent,
    RescheduleResolution,
    RescheduleResolutionKind,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from persona.backends.protocol import ChatBackend
    from persona.tasks import TaskSummary

__all__ = ["RESCHEDULE_PROMPT_VERSION", "ModelRescheduleInterpreter"]

_logger = get_logger("runtime.task_origination")

RESCHEDULE_PROMPT_VERSION = "a8-reschedule-v1"

_SYSTEM_PROMPT = """\
The user is talking to their assistant, which runs one or more STANDING tasks on a schedule. \
Decide whether this message asks to RESCHEDULE one of those tasks — change when/how often it runs, \
or skip its next run — and if so, which task and the new cadence.

You are given the user's ACTIVE tasks, each with an id and a short goal. Match the user's words to \
a task's goal.

Rules:
- Only act when the user clearly wants to retime/rerule a running task ("move the morning brief to \
9", "make the fare check weekly", "skip tomorrow's briefing"). A new request, a question, or \
ordinary chat is NOT a reschedule — return {"match": "none"}.
- If SEVERAL listed tasks plausibly match and you cannot tell which, return {"match": "ambiguous", \
"candidate_ids": ["<id>", "<id>"]} — never guess one.
- On a single confident match, return {"match": "one", "task_id": "<listed id>", and ONE of:
  "recurrence_rrule": "<RFC-5545 RRULE, e.g. FREQ=WEEKLY;BYDAY=MO;BYHOUR=9;BYMINUTE=0>" for a \
recurring cadence, OR "one_time_at": "<ISO-8601 instant>" for a one-off, OR "skip_next": true to \
suppress just the next run}.
- The task_id / candidate_ids MUST be ids from the list. When unsure, return {"match": "none"}.

Reply with ONLY a JSON object, no prose.
"""

_MAX_TOKENS = 300
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
_NOT_FOUND = RescheduleResolution(kind=RescheduleResolutionKind.NOT_FOUND)


class ModelRescheduleInterpreter:
    """A :class:`RescheduleInterpreter` over a chat backend (mockable; conservative)."""

    def __init__(self, *, backend: ChatBackend) -> None:
        self._backend = backend

    async def interpret(
        self, message: str, active_tasks: Sequence[TaskSummary]
    ) -> RescheduleResolution:
        """Resolve ``message`` against ``active_tasks`` → a reschedule resolution.

        Returns ``NOT_FOUND`` with no model call when there are no active tasks, and on any
        unclear / unresolvable reply (the loop then declines rather than reschedule the wrong task).
        """
        if not active_tasks:
            return _NOT_FOUND
        by_id = {t.task_id: t for t in active_tasks}
        now = self._now()
        listing = "\n".join(f"- id={t.task_id}: {t.goal}" for t in active_tasks)
        user = f'Active tasks:\n{listing}\n\nUser message:\n"{message}"\n\nReply with the JSON.'
        messages = [
            ConversationMessage(role="system", content=_SYSTEM_PROMPT, created_at=now),
            ConversationMessage(role="user", content=user, created_at=now),
        ]
        response = await self._backend.chat(messages, temperature=0.0, max_tokens=_MAX_TOKENS)
        return self._parse(response.content, by_id)

    @staticmethod
    def _now() -> datetime:
        from datetime import UTC, datetime

        return datetime.now(UTC)

    def _parse(self, text: str, by_id: dict[str, TaskSummary]) -> RescheduleResolution:
        """Parse conservatively — any doubt resolves to NOT_FOUND; multi-match to AMBIGUOUS."""
        payload = self._load_json(text)
        if payload is None:
            return _NOT_FOUND
        match = str(payload.get("match", "")).strip().lower()

        if match == "ambiguous":
            raw_ids = payload.get("candidate_ids", [])
            ids = (
                [i for i in raw_ids if isinstance(i, str) and i in by_id]
                if isinstance(raw_ids, list)
                else []
            )
            if len(ids) >= 2:
                goals = tuple(by_id[i].goal for i in ids)
                return RescheduleResolution(
                    kind=RescheduleResolutionKind.AMBIGUOUS, candidate_goals=goals
                )
            return _NOT_FOUND  # "ambiguous" with < 2 resolvable ids is not actionable

        if match != "one":
            return _NOT_FOUND
        task_id = str(payload.get("task_id", "")).strip()
        if task_id not in by_id:
            _logger.info("reschedule verb without a resolvable task; declining")
            return _NOT_FOUND

        rrule = payload.get("recurrence_rrule")
        one_time = payload.get("one_time_at")
        skip = payload.get("skip_next") is True
        intent = self._build_intent(task_id, rrule, one_time, skip=skip)
        if intent is None:
            return _NOT_FOUND  # a target but no representable change → decline (the loop asks)
        return RescheduleResolution(kind=RescheduleResolutionKind.RESOLVED, intent=intent)

    @staticmethod
    def _build_intent(
        task_id: str, rrule: object, one_time: object, *, skip: bool
    ) -> RescheduleIntent | None:
        """Exactly one change kind (rrule XOR one-time XOR skip-next), else None (decline)."""
        from datetime import datetime

        if skip and not rrule and not one_time:
            return RescheduleIntent(task_id=task_id, skip_next=True)
        if isinstance(rrule, str) and rrule.strip() and not one_time and not skip:
            return RescheduleIntent(task_id=task_id, recurrence_rrule=rrule.strip())
        if isinstance(one_time, str) and one_time.strip() and not rrule and not skip:
            try:
                at = datetime.fromisoformat(one_time.strip().replace("Z", "+00:00"))
            except ValueError:
                return None
            return RescheduleIntent(task_id=task_id, one_time_at=at)
        return None

    @staticmethod
    def _load_json(text: str) -> dict[str, object] | None:
        stripped = _FENCE_RE.sub("", text).strip()
        try:
            loaded = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            return None
        return loaded if isinstance(loaded, dict) else None

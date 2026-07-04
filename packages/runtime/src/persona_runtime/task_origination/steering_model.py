"""The model-backed steering interpreter — the precision layer (Spec A4, T9b; composition-root).

The cue net (:func:`detect_steering_cue`) admits a turn; this resolves it. Given the user's
message and the caller's **active** tasks, it decides the steering verb (pause / resume / cancel)
and *which* concrete task the user meant, by matching the message against each task's goal
(list-then-resolve — the same active list the persona introspects with).

Conservative by construction (the no-wrong-task bar): it returns ``None`` — the loop then asks
rather than steering — whenever the message is not a steering request, the verb is unclear, or
the referenced task cannot be matched to exactly one active task. A misresolved ``task_id`` (one
not in the active set) is dropped, never applied. The interpreter never *mutates* anything; it
only resolves an intent the loop (and, for cancel, an explicit confirmation) acts on.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from persona.logging import get_logger
from persona.schema.conversation import ConversationMessage

from persona_runtime.task_origination.steering import SteeringIntent, SteeringVerb

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from persona.backends.protocol import ChatBackend
    from persona.tasks import TaskSummary

__all__ = ["STEERING_PROMPT_VERSION", "ModelSteeringInterpreter"]

_logger = get_logger("runtime.task_origination")

STEERING_PROMPT_VERSION = "a4-steering-v1"

_SYSTEM_PROMPT = """\
The user is talking to their assistant, which is running one or more STANDING tasks for them. \
Decide whether this message is a request to STEER one of those live tasks — pause it, resume a \
paused one, or cancel it — and if so, which task.

You are given the list of the user's ACTIVE tasks, each with an id and a short goal. Choose the \
verb and the single task id the user means by matching their words to a task's goal.

Rules:
- Only choose a verb when the user is clearly asking to control a running task ("pause the fare \
check", "cancel that", "start it again"). A new request, a question, or ordinary chat is NOT \
steering — return {"verb": null}.
- The task_id MUST be one of the ids in the list. If you cannot confidently match the message to \
exactly one listed task, return {"verb": null}.
- When unsure, return {"verb": null}. Never guess a task id.

Reply with ONLY a JSON object, no prose:
{"verb": "pause"|"resume"|"cancel"|null, "task_id": "<one of the listed ids, or omit>"}
"""

_MAX_TOKENS = 200
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
_VERBS = {v.value for v in SteeringVerb}


class ModelSteeringInterpreter:
    """A :class:`SteeringInterpreter` over a chat backend (mockable; conservative)."""

    def __init__(self, *, backend: ChatBackend) -> None:
        """Inject the small/mid-tier chat backend (the precision layer)."""
        self._backend = backend

    async def interpret(
        self, message: str, active_tasks: Sequence[TaskSummary]
    ) -> SteeringIntent | None:
        """Resolve ``message`` against ``active_tasks`` → a steering intent, or ``None``.

        Returns ``None`` (the loop asks, never steers the wrong task) with no model call when
        there are no active tasks, and on any unclear / unresolvable reply.
        """
        if not active_tasks:
            return None
        valid_ids = {t.task_id for t in active_tasks}
        now = self._now()
        listing = "\n".join(f"- id={t.task_id}: {t.goal}" for t in active_tasks)
        user = (
            f'Active tasks:\n{listing}\n\nUser message:\n"{message}"\n\nReply with the JSON object.'
        )
        messages = [
            ConversationMessage(role="system", content=_SYSTEM_PROMPT, created_at=now),
            ConversationMessage(role="user", content=user, created_at=now),
        ]
        response = await self._backend.chat(messages, temperature=0.0, max_tokens=_MAX_TOKENS)
        return self._parse(response.content, valid_ids)

    @staticmethod
    def _now() -> datetime:
        from datetime import UTC, datetime

        return datetime.now(UTC)

    def _parse(self, text: str, valid_ids: set[str]) -> SteeringIntent | None:
        """Parse conservatively — any doubt (bad verb, unknown/absent id) resolves to None."""
        payload = self._load_json(text)
        if payload is None:
            return None
        verb = str(payload.get("verb", "")).strip().lower()
        if verb not in _VERBS:
            return None  # null / unknown verb → not a steering request
        task_id = str(payload.get("task_id", "")).strip()
        if task_id not in valid_ids:
            # A hallucinated or omitted id must never steer a task — ask instead.
            _logger.info("steering verb without a resolvable task; declining")
            return None
        return SteeringIntent(verb=SteeringVerb(verb), task_id=task_id)

    @staticmethod
    def _load_json(text: str) -> dict[str, object] | None:
        stripped = _FENCE_RE.sub("", text).strip()
        try:
            loaded = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            return None
        return loaded if isinstance(loaded, dict) else None

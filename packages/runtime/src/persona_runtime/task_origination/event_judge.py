"""The model-backed event-trigger judge — step 2's precision layer for A7 (Spec A7, T8; A4-D-2).

The A4 :class:`~persona_runtime.task_origination.judge.ModelStandingIntentJudge` analogue, for the
event impulse. The event cue net (step 1) admits a turn; this judge decides whether the user is
asking the persona to watch for a platform event and, only when it is CLEAR, extracts the typed
filter. It is **conservative by construction** — the privacy-adjacent bar (criterion 3 of this
sub-task): a wrong watched-sender is a real miss, so the judge answers ``AMBIGUOUS`` (we ask) rather
than guess a sender/platform, and it refuses a filter with **no discriminating field** (which would
watch every inbound message — a firehose, never created silently).

v1 scope: the natural-language event condition is the inbound-message watch
(``connector.message_received`` + a :class:`~persona.events.MessageFilter`) — "when an email from X
arrives, summarise it". Lifecycle/link triggers are not natural chat phrasings and are out of the NL
recogniser (they are the A6/A8 surfaces' and door-a's business); the judge returns NOT_TRIGGER for
them. The judge never *creates* anything — it returns a verdict and, for a clear watch, a minimal
:class:`~persona_runtime.task_origination.draft.ContractDraft` carrying a
:class:`~persona.events.TriggerSpec` (schedule XOR trigger, A7-D-3). Creation still needs the echo +
the explicit confirmation (the existing A4 door).
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from persona.events import EventKind, MessageFilter, TriggerSpec
from persona.logging import get_logger
from persona.schema.conversation import ConversationMessage

from persona_runtime.task_origination.draft import ContractDraft
from persona_runtime.task_origination.event_recognizer import (
    EventTriggerJudgment,
    EventTriggerVerdict,
)

if TYPE_CHECKING:
    from datetime import datetime

    from persona.backends.protocol import ChatBackend

__all__ = ["EVENT_JUDGE_PROMPT_VERSION", "ModelEventTriggerIntentJudge"]

_logger = get_logger("runtime.task_origination.event")

EVENT_JUDGE_PROMPT_VERSION = "a7-event-judge-v1"

_SYSTEM_PROMPT = """\
You decide whether a user's message asks YOU to WATCH FOR AN INBOUND MESSAGE and then do \
something each time it arrives — a standing event-trigger ("when an email from my landlord \
arrives, summarise it"; "whenever I get a WhatsApp from the school, tell me"). This is different \
from a clock schedule ("every morning") — you handle ONLY the message-arrival kind.

Be conservative and literal. These are NOT event triggers:
- A one-off ("did an email from the bank arrive yet?" is a question to answer NOW).
- A clock routine ("every morning summarise my inbox" is time-driven, not event-driven).
- A lament or figure of speech ("I dread it whenever the landlord emails").

You MUST be able to name WHAT to watch for. NEVER invent or guess a sender, platform, or keyword \
the user did not state. If the user clearly wants a watch but you cannot pin the specific \
sender/platform/keyword, answer "ambiguous" — we will ask them. A watch with no specific filter \
would fire on EVERY message; never emit that.

When it is a clear message watch, extract:
- goal: what to do each time it arrives, in your words.
- platform: the channel if named ("email", "whatsapp", "sms", "telegram", "slack"), else omit.
- sender: the specific sender if named — a full email address (lowercased), a phone number, or a \
name/handle — else omit.
- keywords: specific words the message must contain, if the user named any, else omit.
- human_terms: a short phrase completing "whenever ___" for the user to confirm, e.g. \
"an email from landlord@example.com arrives" or "a WhatsApp from the school mentions 'pickup'".

Reply with ONLY a JSON object, no prose:
{"verdict": "trigger"|"not_trigger"|"ambiguous", "goal": "<what to do>", \
"platform": "<channel if named>", "sender": "<specific sender if named>", \
"keywords": ["<word>", ...], "human_terms": "<whenever ___ phrase>"}
For "not_trigger" or "ambiguous", reply with just {"verdict": "not_trigger"} or \
{"verdict": "ambiguous"}.
"""

_MAX_TOKENS = 400
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class ModelEventTriggerIntentJudge:
    """An :class:`EventTriggerIntentJudge` over a chat backend (mockable; conservative)."""

    def __init__(self, *, backend: ChatBackend) -> None:
        self._backend = backend

    async def judge(self, message: str, *, language: str) -> EventTriggerJudgment:
        """Judge ``message``; return a verdict (+ a trigger draft iff a clear message watch)."""
        now = self._now()
        user = f'User message (language "{language}"):\n"{message}"\n\nReply with the JSON object.'
        messages = [
            ConversationMessage(role="system", content=_SYSTEM_PROMPT, created_at=now),
            ConversationMessage(role="user", content=user, created_at=now),
        ]
        response = await self._backend.chat(messages, temperature=0.0, max_tokens=_MAX_TOKENS)
        return self._parse(response.content)

    @staticmethod
    def _now() -> datetime:
        from datetime import UTC, datetime

        return datetime.now(UTC)

    def _parse(self, text: str) -> EventTriggerJudgment:
        """Parse the model's JSON conservatively — any doubt resolves to AMBIGUOUS."""
        payload = self._load_json(text)
        if payload is None:
            return EventTriggerJudgment(verdict=EventTriggerVerdict.AMBIGUOUS)
        verdict = str(payload.get("verdict", "")).strip().lower()
        if verdict == EventTriggerVerdict.NOT_TRIGGER.value:
            return EventTriggerJudgment(verdict=EventTriggerVerdict.NOT_TRIGGER)
        if verdict != EventTriggerVerdict.TRIGGER.value:
            return EventTriggerJudgment(verdict=EventTriggerVerdict.AMBIGUOUS)
        return self._build_trigger(payload)

    def _build_trigger(self, payload: dict[str, object]) -> EventTriggerJudgment:
        """A ``trigger`` verdict → a validated draft, or AMBIGUOUS if the watch is not pinnable.

        The conservative floor (criterion 3): a goal, a human phrase, AND at least one
        discriminating filter field (sender / platform / keywords) are required. A trigger with none
        of those would match every inbound message — refuse it and ask, never guess.
        """
        goal = str(payload.get("goal", "")).strip()
        human_terms = str(payload.get("human_terms", "")).strip()
        platform = self._clean_str(payload.get("platform"))
        sender = self._clean_str(payload.get("sender"))
        keywords = self._clean_keywords(payload.get("keywords"))
        if not goal or not human_terms or not (sender or platform or keywords):
            # Clear intent but no pinnable subject — do not guess a watched sender; ask.
            _logger.info("event watch without a specific filter; degrading to ambiguous")
            return EventTriggerJudgment(verdict=EventTriggerVerdict.AMBIGUOUS)
        try:
            spec = TriggerSpec(
                event_kind=EventKind.CONNECTOR_MESSAGE_RECEIVED,
                filter=MessageFilter(
                    platform=platform,
                    sender=sender.lower() if sender and "@" in sender else sender,
                    keywords=keywords,
                ),
                human_terms=human_terms,
            )
        except (ValueError, TypeError):
            _logger.info("event trigger spec did not validate; degrading to ambiguous")
            return EventTriggerJudgment(verdict=EventTriggerVerdict.AMBIGUOUS)
        return EventTriggerJudgment(
            verdict=EventTriggerVerdict.TRIGGER,
            draft=ContractDraft(goal=goal, trigger=spec),
        )

    @staticmethod
    def _clean_str(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        cleaned = value.strip()
        return cleaned or None

    @staticmethod
    def _clean_keywords(value: object) -> tuple[str, ...]:
        if not isinstance(value, list):
            return ()
        return tuple(w.strip() for w in value if isinstance(w, str) and w.strip())

    @staticmethod
    def _load_json(text: str) -> dict[str, object] | None:
        stripped = _FENCE_RE.sub("", text).strip()
        try:
            loaded = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            return None
        return loaded if isinstance(loaded, dict) else None

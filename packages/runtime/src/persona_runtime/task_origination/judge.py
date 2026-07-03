"""The model-backed standing-intent judge — step 2's precision layer (Spec A4, T5; A4-D-2).

The cue net (step 1) admits a turn; this judge decides. It is **conservative by construction**
(the no-accidental-tasks bar, criterion 3): it answers ``STANDING`` only when the user is
clearly asking the persona to take on ongoing/recurring work, and answers ``AMBIGUOUS`` —
never guesses ``STANDING`` — when unsure, on a malformed reply, or when the goal is missing.
Laments ("I always forget to call my mum"), figures of speech ("monitor lizards are
fascinating"), and information questions that merely mention a routine ("remind me how tall
the Eiffel Tower is") are NOT standing tasks; the guidance is engineered to reject them.

The judge never *creates* anything — it returns a verdict and, for standing intent, a minimal
draft (goal/scope, a spend grant if one was offered, and the parsed cadence). The cadence is
extracted here (an RFC-5545 RRULE or a one-time instant the model emits) and validated through the
parse-honesty boundary; it is anchored in ``default_timezone`` (Spec A4 — the config default until
K6's per-user timezone lands). A STANDING task with no representable cadence falls back to a
**run-once-now** one-time schedule (Option B: every confirmed task is schedule-backed and actually
executes — never an inert row), which the echo frames honestly and the user can make recurring.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from persona.logging import get_logger
from persona.schema.conversation import ConversationMessage
from persona.tools.categories import ActionCategory

from persona_runtime.errors import ScheduleParseError
from persona_runtime.task_origination.draft import ContractDraft, GrantSpec, ParsedSchedule
from persona_runtime.task_origination.recognizer import StandingJudgment, StandingVerdict
from persona_runtime.task_origination.schedule import parse_one_time, parse_recurrence

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from persona.backends.protocol import ChatBackend

__all__ = ["JUDGE_PROMPT_VERSION", "ModelStandingIntentJudge"]

_logger = get_logger("runtime.task_origination")

JUDGE_PROMPT_VERSION = "a4-judge-v1"

_SYSTEM_PROMPT = """\
You decide whether a user's message is asking YOU to take on a STANDING task — ongoing or \
recurring work they want you to keep doing on their behalf ("every morning…", "keep an eye \
on…", "over the next week…") — versus NOW_WORK (a one-off request to do right now) versus \
neither.

Be conservative and literal about intent. The following are NOT standing tasks:
- A lament or self-description ("I always forget to call my mum", "I keep losing track of my \
spending"). The user is venting, not delegating.
- A figure of speech or a topic that merely contains a routine word ("monitor lizards are \
fascinating", "every cloud has a silver lining").
- An information question that mentions time or routine ("remind me how tall the Eiffel \
Tower is" is a question to answer NOW, not a standing reminder).

Only answer "standing" when the user is clearly asking you to do ongoing/recurring work for \
them. When you are unsure, answer "ambiguous" — we will ask the user. NEVER guess "standing".

When the task is standing, extract WHEN it should run. Prefer a recurring cadence as an RFC-5545 \
RRULE ("every weekday at 7am" -> "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=7;BYMINUTE=0"; "every \
morning at 8" -> "FREQ=DAILY;BYHOUR=8;BYMINUTE=0"). If instead it is a single future moment ("next \
Monday at 9"), give a one-time ISO-8601 local datetime. Times are in the user's local timezone. If \
no time is stated, omit both — do NOT invent a cadence.

Reply with ONLY a JSON object, no prose:
{"verdict": "standing"|"now_work"|"ambiguous", "goal": "<short goal in your words>", \
"scope": "<optional constraints>", "spend_cap_kr": <number if a spending limit was offered>, \
"spend_note": "<one short line restating the spend permission, if any>", \
"recurrence_rrule": "<an RFC-5545 RRULE if recurring, else omit>", \
"one_time_at": "<ISO-8601 local datetime if a single future moment, else omit>"}
For "now_work" or "ambiguous", reply with just {"verdict": "now_work"} or \
{"verdict": "ambiguous"}.
"""

_MAX_TOKENS = 400
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
_MICROS_PER_KR = 10_000


class ModelStandingIntentJudge:
    """A :class:`StandingIntentJudge` over a chat backend (mockable; conservative)."""

    def __init__(
        self,
        *,
        backend: ChatBackend,
        default_timezone: str = "UTC",
        timezone_provider: Callable[[], str] | None = None,
    ) -> None:
        """Inject the chat backend + the cadence timezone source.

        ``timezone_provider`` (Spec A8, A8-D-9 — the K6 seam realised) resolves the
        caller's per-user timezone at judge time (``users.timezone`` ?? the config
        default), so a drafted cadence is anchored in *the user's* zone. It is an
        api-side closure over the RLS ``current_user_id`` contextvar (the runtime
        stays DB-free). When ``None`` (the pre-A8 / off-request path), the judge
        falls back to ``default_timezone`` (the config default) — byte-identical to
        before, so existing tests and the community edition are unchanged.
        """
        self._backend = backend
        self._default_timezone = default_timezone
        self._timezone_provider = timezone_provider

    def _resolve_timezone(self) -> str:
        """The cadence timezone for this draft: the per-user provider, else the default.

        The provider is fail-soft (:func:`persona.timezone.resolve_timezone` already
        falls back to the config default on an unset/invalid zone); a provider that
        itself raises degrades to ``default_timezone`` so origination never breaks on
        a timezone lookup.
        """
        if self._timezone_provider is None:
            return self._default_timezone
        try:
            return self._timezone_provider()
        except Exception:  # noqa: BLE001 — a tz lookup must never break origination
            _logger.warning("timezone provider failed; using the config default")
            return self._default_timezone

    async def judge(self, message: str, *, language: str) -> StandingJudgment:
        """Judge ``message``; return a verdict (+ a draft iff clearly standing).

        Args:
            message: The user turn (already admitted by the cue net).
            language: The persona's default language (a hint for the drafted goal's voice).
        """
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

    def _parse(self, text: str) -> StandingJudgment:
        """Parse the model's JSON conservatively — any doubt resolves to AMBIGUOUS."""
        payload = self._load_json(text)
        if payload is None:
            return StandingJudgment(verdict=StandingVerdict.AMBIGUOUS)
        verdict = str(payload.get("verdict", "")).strip().lower()
        if verdict == StandingVerdict.NOW_WORK.value:
            return StandingJudgment(verdict=StandingVerdict.NOW_WORK)
        if verdict != StandingVerdict.STANDING.value:
            # "ambiguous", an unknown verdict, or a missing one — all conservative.
            return StandingJudgment(verdict=StandingVerdict.AMBIGUOUS)
        goal = str(payload.get("goal", "")).strip()
        if not goal:
            # STANDING without a goal is not actionable — do not guess; ask.
            _logger.info("standing verdict without a goal; degrading to ambiguous")
            return StandingJudgment(verdict=StandingVerdict.AMBIGUOUS)
        return StandingJudgment(
            verdict=StandingVerdict.STANDING,
            draft=ContractDraft(
                goal=goal,
                scope=str(payload.get("scope", "")).strip(),
                grants=self._spend_grant(payload),
                schedule=self._build_schedule(payload),
            ),
        )

    def _build_schedule(self, payload: dict[str, object]) -> ParsedSchedule:
        """Extract the cadence into a :class:`ParsedSchedule` (Option B — always schedule-backed).

        A recurring RRULE takes precedence; else a stated one-time instant; else a run-once-NOW
        fallback so a standing task with no representable cadence still executes (never inert) and
        the user can make it recurring by reply. Any parse failure degrades to the same safe
        run-once fallback — an unrepresentable cadence is declined, never coerced (parse-honesty).
        """
        tz = self._resolve_timezone()
        now = self._now()
        rrule = payload.get("recurrence_rrule")
        one_time = payload.get("one_time_at")
        try:
            if isinstance(rrule, str) and rrule.strip():
                return parse_recurrence(rrule.strip(), tz, phrase=rrule.strip())
            if isinstance(one_time, str) and one_time.strip():
                return parse_one_time(self._to_aware(one_time.strip(), tz), tz, phrase=one_time)
        except (ScheduleParseError, ValueError):
            _logger.info("cadence not representable; falling back to run-once")
        return parse_one_time(now, tz, phrase="run once")

    @staticmethod
    def _to_aware(iso: str, tz: str) -> datetime:
        """Parse an ISO datetime; localize a naive value to ``tz`` (the stated wall-clock)."""
        from datetime import datetime
        from zoneinfo import ZoneInfo

        parsed = datetime.fromisoformat(iso)
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=ZoneInfo(tz))

    @staticmethod
    def _load_json(text: str) -> dict[str, object] | None:
        stripped = _FENCE_RE.sub("", text).strip()
        try:
            loaded = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            return None
        return loaded if isinstance(loaded, dict) else None

    @staticmethod
    def _spend_grant(payload: dict[str, object]) -> tuple[GrantSpec, ...]:
        """Build a SPEND grant from an offered cap, or none."""
        cap = payload.get("spend_cap_kr")
        if not isinstance(cap, (int, float)) or isinstance(cap, bool) or cap <= 0:
            return ()
        note = str(payload.get("spend_note", "")).strip()
        return (
            GrantSpec(
                category=ActionCategory.SPEND,
                cap_micros=int(cap * _MICROS_PER_KR),
                human=note,
            ),
        )

"""The conversational initiative-verb family — ONE interpretation seam (Spec A5, T10).

Five verbs, one gate (the A4/A8 steering-seam pattern — a cheap high-recall cue
net admits, a conservative decision layer decides, the loop emits a data-only
``RunEvent`` the api service applies):

- **dial verbs** — "stop suggesting things" (off), "ask before doing things"
  (propose-only), "you can act on your own" (act-within-envelope). The dial
  moves by the user's hand only (A5-D-5); interpretation is conservative — an
  unclear message moves nothing.
- **confirm / decline** — the reply to a pending initiative proposal. The
  pending state is read from the LEDGER via an injected provider (the T9
  Option-C consolidation): reload-durable by construction — a proposal
  delivered in one request is confirmable in the next, which is exactly where
  the A4 metadata rail fails (state.md finding). Confirm uses A4's own
  clean-affirmative floor (:func:`is_affirmative_confirmation` — precision-
  biased, whole-message); decline uses the mirrored clean-negative net.

The gate NEVER acts on ambiguity: not-clean-yes and not-clean-no on a pending
proposal simply falls through to ordinary chat (the proposal stays pending —
asking again costs nothing; a wrong dial move or a wrong confirm costs trust).
"""

from __future__ import annotations

import asyncio
import re
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from persona.logging import get_logger

from persona_runtime.task_origination.confirm import is_affirmative_confirmation

if TYPE_CHECKING:
    from collections.abc import Awaitable

_logger = get_logger("runtime.initiative.verbs")

__all__ = [
    "InitiativeVerb",
    "InitiativeVerbInterpreter",
    "PendingProposalProvider",
    "detect_initiative_cue",
    "is_clean_decline",
    "is_clean_turn_off",
]


class InitiativeVerb(StrEnum):
    """The initiative verb the user expressed (the closed family)."""

    DIAL_OFF = "dial_off"
    DIAL_PROPOSE_ONLY = "dial_propose_only"
    DIAL_ACT = "dial_act"
    CONFIRM_PROPOSAL = "confirm_proposal"
    DECLINE_PROPOSAL = "decline_proposal"


# High-recall admission net (EN/NO/AR, the A4-R-4 discipline): cues only admit
# the turn to the model interpreter; they decide nothing.
_CUE_RE = re.compile(
    r"suggest|suggestion|proactive|initiative|check.?in|unprompted|"
    r"on your own|ask (?:me )?first|ask before|stop (?:doing|bringing)|"
    r"foreslå|forslag|på egen hånd|spør først|slutt å|"  # Norwegian
    r"اقتراح|اقتراحات|من تلقاء نفسك|اسألني أولا",  # Arabic
    re.IGNORECASE | re.UNICODE,
)

# The clean-negative mirror of A4's affirmative floor: the WHOLE reply is a
# decline (plus trailing punctuation) — "no but move it to 9" is NOT a clean
# decline (it carries an instruction; fall through to ordinary handling).
_DECLINE_RE = re.compile(
    r"^\s*(?:"
    r"no|nope|no\s+thanks|not\s+now|leave\s+it|don'?t|skip\s+it|"  # English
    r"nei|nei\s+takk|la\s+det\s+være|ikke\s+nå|dropp\s+det|"  # Norwegian
    r"لا|لا\s+شكرا|اتركه|ليس\s+الآن"  # Arabic
    r")\s*[!.…]*\s*$",
    re.IGNORECASE | re.UNICODE,
)


# An unambiguous turn-off, as the WHOLE message (plus "please" and closing punctuation):
# the dial moves to off without the model (review MEDIUM-2), so a model that answers
# nothing can never leave "stop suggesting things" undone. Precision-biased like the
# decline floor: "stop suggesting that restaurant" and "don't stop suggesting" go to the
# model, which may still read them; nothing here turns the dial ON.
_TURN_OFF_RE = re.compile(
    r"^\s*(?:please\s+)?(?:"
    r"stop\s+(?:suggesting|making\s+suggestions|bringing\s+(?:things|stuff)\s+up)"
    r"(?:\s+(?:things|stuff))?"
    r"|(?:stop|disable|turn\s+off)\s+(?:the\s+)?(?:suggestions|initiative)"
    r"|turn\s+(?:the\s+)?(?:suggestions|initiative)\s+off"
    r"|no\s+more\s+suggestions"
    r"|slutt\s+å\s+(?:foreslå|komme\s+med\s+forslag)(?:\s+ting)?"  # Norwegian
    r"|ingen\s+flere\s+forslag"
    r"|توقف\s+عن\s+الاقتراحات"  # Arabic
    r")(?:\s+please)?\s*[!.…]*\s*$",
    re.IGNORECASE | re.UNICODE,
)


def is_clean_turn_off(message: str) -> bool:
    """Whether ``message`` is, as a whole, an unambiguous "turn initiative off"."""
    return bool(_TURN_OFF_RE.match(message))


# A plain request FOR a suggestion (re-review MEDIUM-A): "what would you suggest for
# dinner?" talks to the persona, not about its initiative, and must not wait on the dial
# model. It is a cue only if it ALSO carries a dial word ("could you suggest less often?").
_ASKS_FOR_SUGGESTION_RE = re.compile(
    r"\b(?:what|which)\s+(?:would|do|could|can|should)\s+you\s+suggest"
    r"|\b(?:can|could|would|will)\s+you\s+suggest\b"
    r"|\b(?:any|some|got\s+any)\s+suggestions?\b"
    r"|\bdo\s+you\s+have\s+(?:any\s+)?suggestions?\b"
    r"|^\s*(?:please\s+)?suggest\s+(?:a|an|some|me|something|anything|one|two|three)\b",
    re.IGNORECASE,
)
#: A request that also says HOW OFTEN or WHEN to suggest is a dial command ("can you suggest
#: things only when I ask", "without me asking", "more often"): third review MEDIUM-1, recall
#: over precision.
_DIAL_WORD_RE = re.compile(
    r"\b(?:stop|stopp|turn\s+off|turn\s+on|disable|enable|no\s+more|fewer|less|more|often|only|"
    r"ask|asks|asked|asking|"
    r"don'?t|do\s+not|never|ask\s+(?:me\s+)?(?:first|before)|on\s+your\s+own|unprompted|"
    r"proactive|initiative|check.?in|bring(?:ing)?\s+(?:things|stuff)\s+up|slutt|ingen)\b",
    re.IGNORECASE | re.UNICODE,
)


def detect_initiative_cue(message: str) -> bool:
    """Whether the message plausibly talks ABOUT initiative/suggestions (admission only).

    A plain request FOR a suggestion is not (re-review MEDIUM-A), unless it also carries a
    dial word.
    """
    if not _CUE_RE.search(message):
        return False
    return not _ASKS_FOR_SUGGESTION_RE.search(message) or bool(_DIAL_WORD_RE.search(message))


def is_clean_decline(reply: str) -> bool:
    """Whether ``reply`` is a clean, whole-message decline (precision-biased)."""
    return bool(_DECLINE_RE.match(reply))


# Re-exported here so the gate has ONE import surface for both floors.
is_affirmative = is_affirmative_confirmation


@runtime_checkable
class PendingProposalProvider(Protocol):
    """The LEDGER read: the persona's latest confirmable proposal notice id.

    Api-implemented (a closure over the initiative ledger, RLS-scoped): the
    latest ``delivered``/``propose`` notice for this owner+persona that is not
    superseded and within the confirmable window. ``None`` ⇒ nothing pending —
    the confirm/decline floors are never consulted (a stray "yes" moves nothing).
    """

    def __call__(self, owner_id: str, persona_id: str) -> str | None:
        """The pending notice id, or ``None``."""
        ...


@runtime_checkable
class InitiativeVerbInterpreter(Protocol):
    """The conservative model-backed dial-verb decision (small tier).

    Returns a DIAL verb only when the user is clearly instructing the persona
    about its initiative posture; ``None`` on anything unclear (the loop falls
    through to ordinary chat — the dial never moves on a guess).
    """

    def interpret(self, message: str) -> Awaitable[InitiativeVerb | None]:
        """The dial verb, or ``None``."""
        ...


class ModelInitiativeVerbInterpreter:
    """The small-tier dial interpreter (the A4 judge pattern: JSON, conservative)."""

    _SYSTEM = """\
You decide whether the user is instructing their AI persona about its INITIATIVE posture —
whether and how it may bring things up or act unprompted. Three settings exist:
- "dial_off": stop suggesting/bringing things up entirely ("stop suggesting things").
- "dial_propose_only": it may suggest, but must ask before doing anything ("ask me first").
- "dial_act": it may act on safe things without asking ("you can act on your own").

Be conservative and literal. Talking ABOUT suggestions ("that was a good suggestion"),
asking a question, or anything ambiguous is NOT an instruction. Reply with ONLY a JSON
object: {"verb": "dial_off"|"dial_propose_only"|"dial_act"} or {"verb": null}.
"""

    def __init__(
        self,
        backend: object,
        *,
        max_output_tokens: int | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        """Inject the initiative tier's chat backend (the one deciding call).

        ``max_output_tokens`` is the call's cap; ``None`` takes the
        ``InitiativeSettings.verb_max_output_tokens`` default (a reasoning model reasons
        inside it). ``timeout_seconds`` bounds the wait (``verb_timeout_seconds``); past
        it the turn reads no verb.
        """
        from persona.initiative import InitiativeSettings

        fields = InitiativeSettings.model_fields
        self._backend = backend
        self._max_output_tokens = (
            max_output_tokens
            if max_output_tokens is not None
            else fields["verb_max_output_tokens"].default
        )
        self._timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else fields["verb_timeout_seconds"].default
        )

    async def interpret(self, message: str) -> InitiativeVerb | None:
        """The dial verb, or ``None``: an unambiguous turn-off needs no model; otherwise
        any doubt, malformed or empty output, or error is ``None``."""
        if is_clean_turn_off(message):
            return InitiativeVerb.DIAL_OFF
        import json
        from datetime import UTC, datetime

        from persona.schema.conversation import ConversationMessage

        now = datetime.now(UTC)
        try:
            response = await asyncio.wait_for(
                self._backend.chat(  # type: ignore[attr-defined]
                    [
                        ConversationMessage(role="system", content=self._SYSTEM, created_at=now),
                        ConversationMessage(
                            role="user", content=f'User message:\n"{message}"', created_at=now
                        ),
                    ],
                    temperature=0.0,
                    max_tokens=self._max_output_tokens,
                ),
                timeout=self._timeout_seconds,
            )
        except TimeoutError:
            _logger.warning(
                "dial-verb interpreter timed out after {s}s; no verb this turn",
                s=self._timeout_seconds,
            )
            return None
        except Exception:  # noqa: BLE001 - the dial never moves on an error
            return None
        try:
            payload = json.loads(str(response.content).strip().strip("`"))
            verb = payload.get("verb") if isinstance(payload, dict) else None
            if verb in (
                InitiativeVerb.DIAL_OFF.value,
                InitiativeVerb.DIAL_PROPOSE_ONLY.value,
                InitiativeVerb.DIAL_ACT.value,
            ):
                return InitiativeVerb(verb)
        except Exception:  # noqa: BLE001 — the dial never moves on an error
            return None
        return None


def resolve_verb_for_pending(
    user_message: str, pending_notice_id: str | None
) -> tuple[InitiativeVerb, str] | None:
    """The confirm/decline resolution over a LEDGER-pending proposal (pure).

    Only consulted when the ledger says a proposal is pending; a clean yes
    confirms it, a clean no declines it, anything else is ``None`` (fall
    through — the proposal stays pending).
    """
    if pending_notice_id is None:
        return None
    if is_affirmative(user_message):
        return (InitiativeVerb.CONFIRM_PROPOSAL, pending_notice_id)
    if is_clean_decline(user_message):
        return (InitiativeVerb.DECLINE_PROPOSAL, pending_notice_id)
    return None

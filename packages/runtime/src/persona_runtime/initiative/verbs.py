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

import re
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from persona_runtime.task_origination.confirm import is_affirmative_confirmation

if TYPE_CHECKING:
    from collections.abc import Awaitable

__all__ = [
    "InitiativeVerb",
    "InitiativeVerbInterpreter",
    "PendingProposalProvider",
    "detect_initiative_cue",
    "is_clean_decline",
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


def detect_initiative_cue(message: str) -> bool:
    """Whether the message plausibly talks ABOUT initiative/suggestions (admission only)."""
    return bool(_CUE_RE.search(message))


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

    def __init__(self, backend: object) -> None:
        """Inject the small-tier chat backend (the one cheap deciding call)."""
        self._backend = backend

    async def interpret(self, message: str) -> InitiativeVerb | None:
        """The dial verb, or ``None`` — any doubt, malformed output, or error is ``None``."""
        import json
        from datetime import UTC, datetime

        from persona.schema.conversation import ConversationMessage

        now = datetime.now(UTC)
        try:
            response = await self._backend.chat(  # type: ignore[attr-defined]
                [
                    ConversationMessage(role="system", content=self._SYSTEM, created_at=now),
                    ConversationMessage(
                        role="user", content=f'User message:\n"{message}"', created_at=now
                    ),
                ],
                temperature=0.0,
                max_tokens=60,
            )
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

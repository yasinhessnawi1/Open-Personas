"""Reply interpretation — the approve/deny/modify/clarify safety floor (A3-D-X-reply-parsing).

Misreading a reply is the unforgivable bug ("No, wait" parsed as approval on a ``spend`` once
empties the trust ledger). So the design separates a *fallible* interpreter from an
*infallible* floor:

- :class:`ReplyInterpreter` — the model-backed seam (the api wires a small/mid-tier backend,
  prompted for NO/EN). It produces a :class:`RawInterpretation` — an intent + a confidence —
  but it is **never trusted directly.**
- :func:`resolve_reply` — **the floor**, a pure deterministic function the model cannot talk
  past. It is the reliability bound: **approve only on a confident approve**; deny is honoured
  at any confidence (steering away is always safe); **clarify-once, then deny** (no zombie
  pending, no being-talked-into-approval); a modify routes through
  :func:`persona.approvals.classify_modification` so a material edit re-confirms rather than
  silently executing.
- :class:`LexiconReplyInterpreter` — a conservative deterministic NO/EN reference + fail-safe
  fallback. It decides **only bare unambiguous tokens** (``yes``/``ja`` → approve,
  ``no``/``nei`` → deny) and marks **every hedged form ambiguous** (``"no, wait"`` /
  ``"nei, vent"`` / ``"ja, men endre…"`` never become an approval). The api composes
  model-primary with this as the fallback (model down → clear cases still work, unclear ones
  clarify-then-deny — fail-safe).

Core, pure, model-free here; the model call + the api wiring are T7/T8.
"""

from __future__ import annotations

from collections.abc import Mapping  # noqa: TC003 — Pydantic needs runtime access
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from persona.approvals.records import DecisionType, Materiality, classify_modification

if TYPE_CHECKING:
    from persona.approvals.records import ActionProposal

__all__ = [
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "InterpretedIntent",
    "LexiconReplyInterpreter",
    "RawInterpretation",
    "ReplyInterpreter",
    "ResolvedReply",
    "is_decision_cue",
    "resolve_reply",
]

#: The confidence an approve / modify must clear for the floor to act on it (the api may
#: inject a tuned value). Below it, the floor clarifies-once-then-denies — never approves.
DEFAULT_CONFIDENCE_THRESHOLD = 0.75


class InterpretedIntent(StrEnum):
    """What an interpreter read in the reply (the *fallible* signal the floor bounds).

    ``ambiguous`` is the honest "couldn't tell" — empty, off-topic, hedged, or arguing
    replies land here, and the floor turns them into clarify-once-then-deny (never approve).
    """

    APPROVE = "approve"
    DENY = "deny"
    MODIFY = "modify"
    AMBIGUOUS = "ambiguous"


class RawInterpretation(BaseModel):
    """An interpreter's reading of a reply — intent + confidence (never acted on directly)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    intent: InterpretedIntent
    confidence: float = Field(ge=0.0, le=1.0)
    edited_arguments: Mapping[str, JsonValue] | None = None
    rationale: str = ""


class ResolvedReply(BaseModel):
    """The floor's verdict the orchestrator acts on (T8): the only path to an execution.

    ``outcome`` is the post-floor :class:`DecisionType`. For a ``modify`` it carries the
    :class:`Materiality` (material → re-confirm before replay; immaterial → replay the edit)
    and the ``edited_arguments``; for every other outcome those are ``None``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: DecisionType
    materiality: Materiality | None = None
    edited_arguments: Mapping[str, JsonValue] | None = None

    @model_validator(mode="after")
    def _modify_carries_materiality_and_edits(self) -> ResolvedReply:
        is_modify = self.outcome is DecisionType.MODIFY
        has_detail = self.materiality is not None and self.edited_arguments is not None
        if is_modify and not has_detail:
            msg = "a MODIFY outcome must carry materiality + edited_arguments"
            raise ValueError(msg)
        if not is_modify and (self.materiality is not None or self.edited_arguments is not None):
            msg = "materiality / edited_arguments are only valid on a MODIFY outcome"
            raise ValueError(msg)
        return self


def resolve_reply(
    raw: RawInterpretation,
    *,
    original_arguments: Mapping[str, JsonValue],
    clarifications_used: int,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> ResolvedReply:
    """The floor: turn a fallible interpretation into a safe verdict (A3-D-X-reply-parsing).

    The deterministic reliability bound — no interpreter output can manufacture an approval
    the user did not clearly give:

    - **deny** at any confidence → ``DENY`` (steering away is always safe to honour);
    - **approve** at/above the threshold → ``APPROVE``; below it → clarify-once-then-deny;
    - **modify** at/above the threshold *with* edits → ``MODIFY`` carrying the
      :func:`classify_modification` materiality (material re-confirms, immaterial executes);
      a low-confidence or edit-less modify → clarify-once-then-deny;
    - **ambiguous** (and every other uncertain case) → ``CLARIFY`` the first time, then
      ``DENY`` (no zombie pending; can't be talked into approval over turns).

    Args:
        raw: The interpreter's reading (never trusted directly).
        original_arguments: The proposal's recorded arguments (for materiality on a modify).
        clarifications_used: How many clarifications were already asked for this proposal
            (0 on the first reply). At ``>= 1`` an uncertain reply denies rather than re-asks.
        confidence_threshold: The bar an approve / modify must clear.

    Returns:
        The :class:`ResolvedReply` verdict.
    """
    if raw.intent is InterpretedIntent.DENY:
        return ResolvedReply(outcome=DecisionType.DENY)

    if raw.intent is InterpretedIntent.APPROVE and raw.confidence >= confidence_threshold:
        return ResolvedReply(outcome=DecisionType.APPROVE)

    if (
        raw.intent is InterpretedIntent.MODIFY
        and raw.confidence >= confidence_threshold
        and raw.edited_arguments is not None
    ):
        materiality = classify_modification(original_arguments, raw.edited_arguments)
        return ResolvedReply(
            outcome=DecisionType.MODIFY,
            materiality=materiality,
            edited_arguments=raw.edited_arguments,
        )

    # Everything else — ambiguous, a low-confidence approve/modify, a modify without edits:
    # clarify once, then deny. Never approve on uncertainty.
    if clarifications_used == 0:
        return ResolvedReply(outcome=DecisionType.CLARIFY)
    return ResolvedReply(outcome=DecisionType.DENY)


@runtime_checkable
class ReplyInterpreter(Protocol):
    """The model-backed interpretation seam (the api wires a small/mid-tier backend).

    Produces a :class:`RawInterpretation` from the user's reply + the proposal context. Its
    output is **always** passed through :func:`resolve_reply` — the floor — before any action.
    """

    async def interpret(self, reply: str, proposal: ActionProposal) -> RawInterpretation: ...


# --- the conservative deterministic NO/EN lexicon (reference + fail-safe fallback) ---------

_AFFIRM: frozenset[str] = frozenset(
    {
        # English
        "yes",
        "y",
        "yeah",
        "yep",
        "yup",
        "ok",
        "okay",
        "approve",
        "approved",
        "send",
        "go",
        "confirm",
        "confirmed",
        "sure",
        "do",
        # Norwegian
        "ja",
        "jepp",
        "greit",
        "godkjenn",
        "godkjent",
        "kjør",
    }
)
_DENY: frozenset[str] = frozenset(
    {
        # English
        "no",
        "n",
        "nope",
        "nah",
        "deny",
        "denied",
        "stop",
        "cancel",
        "dont",
        "don't",
        # Norwegian
        "nei",
        "niks",
        "avslå",
        "avslag",
        "avbryt",
        "stopp",
        "ikke",
    }
)
#: Hedge / reconsideration markers — their presence forces ambiguity even alongside an
#: affirm/deny token, so "no, wait" / "nei, vent" / "ja, men endre…" never decide.
_HEDGE: frozenset[str] = frozenset(
    {
        "but",
        "wait",
        "however",
        "change",
        "instead",
        "maybe",
        "unsure",
        "hmm",
        "actually",
        "men",
        "vent",
        "endre",
        "bytt",
        "heller",
        "kanskje",
        "usikker",
    }
)
#: Politeness / filler words ignored when checking whether a reply is *only* affirm/deny.
_FILLER: frozenset[str] = frozenset(
    {"please", "thanks", "thank", "you", "it", "that", "this", "takk", "vær", "så", "snill", "den"}
)
#: Explicit change/edit markers (NO/EN) — a *deliberate* modify signal on a pending approval,
#: distinct from the softer hedges (but/wait/maybe) so an off-topic aside is not mistaken for a
#: decision. Used only by :func:`is_decision_cue` (the A6 chat-twin gate), never by the floor.
_MODIFY_MARKERS: frozenset[str] = frozenset(
    {"change", "modify", "edit", "instead", "endre", "bytt", "rediger", "heller"}
)

_AFFIRM_CONFIDENCE = 0.95
_AMBIGUOUS_CONFIDENCE = 0.2


def _tokenise(reply: str) -> list[str]:
    """Lower-case word tokens, punctuation stripped (apostrophes kept for don't/don´t)."""
    cleaned = "".join(ch.lower() if (ch.isalnum() or ch in "' ") else " " for ch in reply)
    return cleaned.split()


def is_decision_cue(reply: str) -> bool:
    """True if a reply reads as a DECISION on a pending approval — affirm, deny, or modify.

    The A6 chat-twin's cue-gate: deterministic (no model), reusing the SAME NO/EN lexicon the floor
    uses, so twin-parity holds (the cue only supplies the "is this a reply to the approval?"
    determination the inbox gets structurally from a click; the resolver's floor then classifies).

    Only a decision-like message is routed to the resolver — anything else runs a normal chat turn
    and leaves the proposal PENDING (never auto-denied; the cue-gate's strictly-safer failure mode).
    A deliberate change/edit marker counts (the user is engaging the approval); the floor then
    governs (clarify / re-confirm — v1's lexicon can't extract NL edits, so a structured edit stays
    an inbox action).
    """
    content = [token for token in _tokenise(reply) if token not in _FILLER]
    return any(token in _AFFIRM or token in _DENY or token in _MODIFY_MARKERS for token in content)


class LexiconReplyInterpreter:
    """A conservative deterministic NO/EN interpreter — bare tokens only, else ambiguous.

    The safety contract: it decides ``approve``/``deny`` **only** for replies that reduce to
    bare affirmation/negation tokens (plus politeness fillers) with **no hedge marker**.
    Anything hedged, mixed, empty, or unrecognised is ``ambiguous`` — so a mis-parse can never
    upgrade to an approval (the floor then clarifies-once-then-denies). Implements the
    :class:`ReplyInterpreter` Protocol (``proposal`` is unused — bare-token decisions need no
    context); the api composes the model interpreter for the genuinely natural-language cases.
    """

    async def interpret(
        self,
        reply: str,
        proposal: ActionProposal | None = None,  # noqa: ARG002 — Protocol parity; lexicon is context-free
    ) -> RawInterpretation:
        tokens = _tokenise(reply)
        if any(token in _HEDGE for token in tokens):
            return RawInterpretation(
                intent=InterpretedIntent.AMBIGUOUS, confidence=_AMBIGUOUS_CONFIDENCE
            )
        content = [token for token in tokens if token not in _FILLER]
        has_affirm = any(token in _AFFIRM for token in content)
        has_deny = any(token in _DENY for token in content)
        if content and not has_deny and all(token in _AFFIRM for token in content):
            return RawInterpretation(
                intent=InterpretedIntent.APPROVE, confidence=_AFFIRM_CONFIDENCE
            )
        if content and not has_affirm and all(token in _DENY for token in content):
            return RawInterpretation(intent=InterpretedIntent.DENY, confidence=_AFFIRM_CONFIDENCE)
        return RawInterpretation(
            intent=InterpretedIntent.AMBIGUOUS, confidence=_AMBIGUOUS_CONFIDENCE
        )

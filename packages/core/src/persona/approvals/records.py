"""The approval record value types + the materiality classifier (Spec A3, T3).

The two durable artifacts of the approval flow, plus the A3-D-3 materiality line:

- :class:`ActionProposal` — **the safety artifact.** The *exact* action a gated leg wants to
  take: the tool name + the verbatim arguments (replayed exactly on approval, never re-derived
  by the model — A3-D-X-approved-execution) + a human-readable description (the email as it
  would send; the amount and payee) + the categories that triggered the gate. Durable, owner-
  scoped, one-pending-per-task (the api store enforces the queue, T6).
- :class:`ApprovalDecision` — the user's answer: ``approve`` / ``deny`` / ``modify`` /
  ``clarify`` (the LangChain HITL taxonomy), the **verbatim reply** and the **channel** it
  arrived on (the audit trail, criterion 9), and — for a ``modify`` — the edited arguments.
- :func:`classify_modification` — the A3-D-3 line: a change to **recipient / amount /
  commitment** is *material* and re-confirms before replay; a pure **phrasing** edit is
  *immaterial* and executes. It **defaults to material** for any change it does not recognise
  as phrasing (the safe direction — a wrongly-executed recipient change is the unrecoverable
  error; a needless re-confirm is mere friction).

Pure value types (frozen Pydantic, tz-aware UTC). The durable RLS store + the status CAS live
in persona-api (T6); the orchestrator (T8) drives proposal → C0 → reply → decision → replay.

This package imports **only** from :mod:`persona.tools` (the category taxonomy) — never the
reverse (the A3-D-X-import-boundary lock): tools stays unaware of approvals, and the leg-
ending :class:`persona.errors.GatedActionProposedError` lives in the central error module so
neither side closes a cycle.
"""

from __future__ import annotations

from collections.abc import Mapping  # noqa: TC003 — Pydantic needs runtime access
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, JsonValue, field_validator, model_validator

from persona.tools import ActionCategory  # noqa: TC001 — Pydantic needs runtime access

__all__ = [
    "ActionProposal",
    "ApprovalDecision",
    "DecisionType",
    "Materiality",
    "ProposalStatus",
    "classify_modification",
]


def _ensure_utc(value: datetime) -> datetime:
    """Reject naive datetimes; normalise tz-aware ones to UTC (house rule)."""
    if value.tzinfo is None:
        msg = "naive datetime not allowed; use datetime.now(UTC) or attach a tzinfo"
        raise ValueError(msg)
    return value.astimezone(UTC)


class ProposalStatus(StrEnum):
    """The proposal lifecycle; the api store transitions it under a CAS (A3-D-X-approved-execution).

    ``pending`` → awaiting the user. ``approved`` → decided yes **as proposed**, not yet
    executed. ``modified`` → decided yes **carrying the user's edits**, not yet executed: the
    same green light as ``approved`` (see :meth:`executable`), recorded separately so the
    durable record can tell "I approved what it asked" from "I approved my own version of it".
    A material edit still re-confirms first (it stays ``pending`` while the revised payload is
    put back to the user), so a proposal reaches ``modified`` only once the user has said yes
    to the edited action. ``denied`` → decided no. ``expired`` → auto-paused after the
    reminder grace. ``consumed`` → the decided payload has been executed exactly once (the
    at-most-once terminal, reached from either green light).
    """

    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    MODIFIED = "modified"
    EXPIRED = "expired"
    CONSUMED = "consumed"

    @classmethod
    def executable(cls) -> frozenset[ProposalStatus]:
        """The statuses that mean "the user said yes": the ONE admission predicate.

        Execution, and the consume CAS that follows it, admit exactly these. It is a single
        named door on purpose: ``modified`` was added to the enum long before anything wrote
        it, and the way that kind of member stays unreachable is a second site that checks
        ``status is APPROVED`` by hand and quietly excludes it.

        Returns:
            ``{approved, modified}``: decided yes as proposed, and decided yes with edits.
        """
        return frozenset({cls.APPROVED, cls.MODIFIED})


class DecisionType(StrEnum):
    """How the user answered a proposal (the LangChain Approve/Edit/Reject/Respond taxonomy)."""

    APPROVE = "approve"
    DENY = "deny"
    MODIFY = "modify"
    CLARIFY = "clarify"


class Materiality(StrEnum):
    """Whether a modification re-confirms (``material``) or executes directly (``immaterial``)."""

    MATERIAL = "material"
    IMMATERIAL = "immaterial"


class ActionProposal(BaseModel):
    """The exact gated action awaiting approval — the safety artifact (A3-D-X-approved-execution).

    Attributes:
        proposal_id: Durable id (the one-pending + replay key; the status CAS keys on it).
        task_id: The owning task (links to the A2 task that parks ``waiting(on_user)``).
        owner_id: The tenant the task runs as (the RLS scope + the C0 recipient).
        persona_id: The persona that wants to act (the persona-voiced C0 request is name-tagged).
        categories: The tool's resolved action categories that triggered the gate.
        tool_name: The tool the leg called.
        arguments: The **exact** recorded arguments — replayed verbatim on approval, never
            re-derived by the model.
        description: A precise, human-readable rendering for the user (the email as it would
            send; the amount and payee).
        status: The proposal lifecycle state (defaults to ``pending``).
        created_at: When the proposal was recorded (tz-aware UTC; drives expiry, A3-D-2).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    proposal_id: str
    task_id: str
    owner_id: str
    persona_id: str
    categories: frozenset[ActionCategory]
    tool_name: str
    arguments: Mapping[str, JsonValue] = {}
    description: str
    status: ProposalStatus = ProposalStatus.PENDING
    created_at: datetime

    @field_validator("created_at", mode="after")
    @classmethod
    def _created_at_tz_aware(cls, value: datetime) -> datetime:
        return _ensure_utc(value)


class ApprovalDecision(BaseModel):
    """The user's recorded answer to a proposal (criterion 9 audit: verbatim reply + channel).

    Attributes:
        decision_id: Durable id.
        proposal_id: The proposal this answers.
        type: The decision kind (approve / deny / modify / clarify).
        verbatim_reply: The user's exact words — recorded verbatim (never paraphrased).
        channel: The channel the reply arrived on (the audit trail + cadence reasoning).
        edited_arguments: The edited payload for a ``modify`` (``None`` for every other type).
        decided_at: When the decision was recorded (tz-aware UTC).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision_id: str
    proposal_id: str
    type: DecisionType
    verbatim_reply: str
    channel: str
    edited_arguments: Mapping[str, JsonValue] | None = None
    decided_at: datetime

    @field_validator("decided_at", mode="after")
    @classmethod
    def _decided_at_tz_aware(cls, value: datetime) -> datetime:
        return _ensure_utc(value)

    @model_validator(mode="after")
    def _edits_iff_modify(self) -> ApprovalDecision:
        is_modify = self.type is DecisionType.MODIFY
        if is_modify and self.edited_arguments is None:
            msg = "a MODIFY decision must carry edited_arguments"
            raise ValueError(msg)
        if not is_modify and self.edited_arguments is not None:
            msg = "edited_arguments is only valid on a MODIFY decision"
            raise ValueError(msg)
        return self


#: Argument keys whose change is *phrasing* — the only edits that execute without re-confirm
#: (A3-D-3). Matched case-insensitively on the exact key. Anything not here is treated as
#: material (the safe default): recipient/amount/commitment-bearing fields, and unknown fields.
_PHRASING_KEYS: frozenset[str] = frozenset(
    {
        "body",
        "text",
        "message",
        "content",
        "note",
        "comment",
        "subject",
        "title",
        "caption",
        "description",
    }
)


def _is_phrasing_key(key: str) -> bool:
    return key.lower() in _PHRASING_KEYS


def classify_modification(
    original: Mapping[str, JsonValue], edited: Mapping[str, JsonValue]
) -> Materiality:
    """Classify a modification as material (re-confirm) or immaterial (execute) — A3-D-3.

    A change is **immaterial** only if *every* changed key (added, removed, or value-changed)
    is a known phrasing key. Any other change — a recipient, an amount, a commitment, or any
    key not recognised as phrasing — is **material**, defaulting to re-confirm (the safe
    direction). No change at all is immaterial (a bare approve in edit's clothing).

    Args:
        original: The proposal's recorded arguments.
        edited: The user's edited arguments.

    Returns:
        :attr:`Materiality.MATERIAL` if any non-phrasing key changed, else
        :attr:`Materiality.IMMATERIAL`.
    """
    changed = {
        key for key in original.keys() | edited.keys() if original.get(key) != edited.get(key)
    }
    if all(_is_phrasing_key(key) for key in changed):
        return Materiality.IMMATERIAL
    return Materiality.MATERIAL

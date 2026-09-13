"""Entity ↔ row serialization for the A3 approval artifacts (Spec A3, T6).

The single place that maps the frozen ``persona.approvals`` value types to/from their durable
rows (``approval_proposals`` / ``approval_decisions``). Kept apart from the store so the
column set lives in one spot (mirrors ``persona_api.tasks.serde``).

The proposal's ``arguments`` round-trip **value-exact** through ``arguments_json`` (JSONB) — it
is the replay payload executed verbatim on approval, never re-derived. ``categories`` persist
as a sorted JSON array of the category strings; ``ApprovalDecision`` carries no ``owner_id``
(the RLS scope is supplied to the store + pinned to the proposal by the composite FK), so
:func:`decision_values` takes it as a separate argument, like ``checkpoint_values``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from persona.approvals import (
    ActionProposal,
    ApprovalDecision,
    DecisionType,
    ProposalStatus,
)
from persona.tools import ActionCategory

if TYPE_CHECKING:
    from sqlalchemy import RowMapping

__all__ = [
    "decision_values",
    "proposal_values",
    "row_to_decision",
    "row_to_proposal",
]


def _utc(value: datetime) -> datetime:
    """Re-attach UTC to a stored instant that came back naive (community SQLite)."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def proposal_values(proposal: ActionProposal) -> dict[str, Any]:
    """The ``approval_proposals`` column map for an INSERT (``updated_at`` server-defaulted)."""
    return {
        "id": proposal.proposal_id,
        "owner_id": proposal.owner_id,
        "task_id": proposal.task_id,
        "persona_id": proposal.persona_id,
        "tool_name": proposal.tool_name,
        "arguments_json": dict(proposal.arguments),
        "categories_json": sorted(c.value for c in proposal.categories),
        "description": proposal.description,
        "status": proposal.status.value,
        "created_at": proposal.created_at,
    }


def row_to_proposal(row: RowMapping) -> ActionProposal:
    """Build an :class:`ActionProposal` from an ``approval_proposals`` row (args value-exact)."""
    return ActionProposal(
        proposal_id=row["id"],
        owner_id=row["owner_id"],
        task_id=row["task_id"],
        persona_id=row["persona_id"],
        tool_name=row["tool_name"],
        arguments=row["arguments_json"],
        categories=frozenset(ActionCategory(c) for c in row["categories_json"]),
        description=row["description"],
        status=ProposalStatus(row["status"]),
        # Storage is UTC; the community SQLite engine hands instants back naive (the task
        # serde has the same guard). Postgres values are already aware and pass through.
        created_at=_utc(row["created_at"]),
    )


def decision_values(decision: ApprovalDecision, owner_id: str) -> dict[str, Any]:
    """The ``approval_decisions`` column map for an INSERT (``owner_id`` is the RLS scope)."""
    return {
        "id": decision.decision_id,
        "owner_id": owner_id,
        "proposal_id": decision.proposal_id,
        "type": decision.type.value,
        "verbatim_reply": decision.verbatim_reply,
        "channel": decision.channel,
        "edited_arguments_json": (
            dict(decision.edited_arguments) if decision.edited_arguments is not None else None
        ),
        "decided_at": decision.decided_at,
    }


def row_to_decision(row: RowMapping) -> ApprovalDecision:
    """Rebuild an :class:`ApprovalDecision` from an ``approval_decisions`` row."""
    edited = row["edited_arguments_json"]
    return ApprovalDecision(
        decision_id=row["id"],
        proposal_id=row["proposal_id"],
        type=DecisionType(row["type"]),
        verbatim_reply=row["verbatim_reply"],
        channel=row["channel"],
        edited_arguments=edited if edited is not None else None,
        decided_at=row["decided_at"],
    )

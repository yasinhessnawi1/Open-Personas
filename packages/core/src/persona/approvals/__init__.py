"""``persona.approvals`` — the A3 approval-flow value types (core, pure).

The permission *taxonomy* + per-task *policy matrix* live in :mod:`persona.tools`
(``ActionCategory`` / ``CategoryPolicy``); this package holds the approval-*flow* artifacts:
the :class:`ActionProposal` (the exact gated action), the :class:`ApprovalDecision` (the
user's answer), and the :func:`classify_modification` materiality line (A3-D-3).

**Import boundary (A3-D-X-import-boundary):** ``persona.approvals`` imports from
``persona.tools``, never the reverse. The leg-ending exception
:class:`persona.errors.GatedActionProposedError` lives in the central error module, so the
T7 ``PolicyGatedToolbox`` (here, above tools) can raise it and the runtime leg executor can
catch it without a tools ↔ approvals cycle.
"""

from __future__ import annotations

from persona.approvals.gating import (
    GateContext,
    PolicyGatedToolbox,
    ProposalRecorder,
)
from persona.approvals.interpret import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    InterpretedIntent,
    LexiconReplyInterpreter,
    RawInterpretation,
    ReplyInterpreter,
    ResolvedReply,
    is_decision_cue,
    resolve_reply,
)
from persona.approvals.records import (
    ActionProposal,
    ApprovalDecision,
    DecisionType,
    Materiality,
    ProposalStatus,
    classify_modification,
)

__all__ = [
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "ActionProposal",
    "ApprovalDecision",
    "DecisionType",
    "GateContext",
    "InterpretedIntent",
    "LexiconReplyInterpreter",
    "Materiality",
    "PolicyGatedToolbox",
    "ProposalRecorder",
    "ProposalStatus",
    "RawInterpretation",
    "ReplyInterpreter",
    "ResolvedReply",
    "classify_modification",
    "is_decision_cue",
    "resolve_reply",
]

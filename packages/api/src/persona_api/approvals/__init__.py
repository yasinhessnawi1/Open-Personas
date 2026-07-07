"""persona_api.approvals — the durable side of the A3 approval spine.

The RLS-scoped, audited :class:`ApprovalStore` over the ``approval_proposals`` +
``approval_decisions`` tables: the at-most-once status CAS, the one-pending-aware create, and
the verbatim decision audit trail. The pure value types (``ActionProposal`` /
``ApprovalDecision`` / the reply floor) live in ``persona.approvals`` (core).
"""

from __future__ import annotations

from persona_api.approvals.budget import (
    PLATFORM_DEFAULT_BUDGET_MICROS,
    BudgetEnforcer,
    BudgetState,
    parse_extension_micros,
)
from persona_api.approvals.cadence import (
    DEFAULT_DAILY_CAP,
    CadenceDecision,
    CadenceGate,
    DigestSink,
    MessagePriority,
    bypasses_cap,
)
from persona_api.approvals.failure import (
    FailureAccount,
    FailureKind,
    account_for_budget_pause,
    account_for_cancel_failure,
    account_for_expired_approval,
    account_for_origination_failure,
    account_for_stuck,
    all_failure_kinds_have_a_builder,
)
from persona_api.approvals.kill_switch import (
    GLOBAL_PAUSE_KEY,
    AutonomyPauseCheck,
    KillSwitchCommand,
    KillSwitchStore,
    never_paused,
    parse_kill_switch,
)
from persona_api.approvals.metric import GateFatigueMetric
from persona_api.approvals.resolver import (
    ActionExecutor,
    ApprovalNotifier,
    ApprovalResolver,
    InboxDecision,
    ResolutionOutcome,
)
from persona_api.approvals.store import ApprovalStore
from persona_api.approvals.sweep import (
    APPROVAL_SWEEP_LOCK_KEY,
    EXPIRE_AFTER_DEFAULT,
    REMIND_AFTER_DEFAULT,
    ApprovalSweeper,
    ApprovalSweepRunner,
    ApprovalSweepVoicer,
    SweepResult,
)

__all__ = [
    "APPROVAL_SWEEP_LOCK_KEY",
    "DEFAULT_DAILY_CAP",
    "EXPIRE_AFTER_DEFAULT",
    "GLOBAL_PAUSE_KEY",
    "PLATFORM_DEFAULT_BUDGET_MICROS",
    "REMIND_AFTER_DEFAULT",
    "ActionExecutor",
    "ApprovalNotifier",
    "ApprovalResolver",
    "ApprovalStore",
    "ApprovalSweepRunner",
    "ApprovalSweepVoicer",
    "ApprovalSweeper",
    "AutonomyPauseCheck",
    "BudgetEnforcer",
    "BudgetState",
    "CadenceDecision",
    "CadenceGate",
    "DigestSink",
    "FailureAccount",
    "FailureKind",
    "GateFatigueMetric",
    "InboxDecision",
    "KillSwitchCommand",
    "KillSwitchStore",
    "MessagePriority",
    "ResolutionOutcome",
    "SweepResult",
    "account_for_budget_pause",
    "account_for_expired_approval",
    "account_for_cancel_failure",
    "account_for_origination_failure",
    "account_for_stuck",
    "all_failure_kinds_have_a_builder",
    "bypasses_cap",
    "never_paused",
    "parse_extension_micros",
    "parse_kill_switch",
]

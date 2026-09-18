"""persona.tasks — the autonomous task model (Spec A2).

The durable **task** entity above runs, the per-leg **checkpoint** that carries between
legs, the state machine, context reconstruction, and leg-boxing — all pure, ``mypy
--strict``, no DB/IO/clock (the durable RLS store + leg executor compose these in
persona-api / persona-runtime). T1 lands the checkpoint (the architectural lock, D-A2-1).
"""

from __future__ import annotations

from persona.tasks.acceptance import (
    CriterionClaim,
    LegEvidence,
    RejectedClaim,
    settle_criteria,
)
from persona.tasks.artifacts import (
    MAX_ARTIFACT_POINTERS,
    WORKSPACE_POINTER_KIND,
    merge_artifact_pointers,
    pointers_from_artifacts,
)
from persona.tasks.boxing import (
    DEFAULT_LEG_MAX_STEPS,
    DEFAULT_LEG_WALL_CLOCK_SECONDS,
    LegBox,
    LegBoxLimit,
)
from persona.tasks.checkpoint import (
    CHECKPOINT_SCHEMA_VERSION,
    DEFAULT_CHECKPOINT_TOKEN_BUDGET,
    ArtifactPointer,
    Decision,
    TaskCheckpoint,
    checkpoint_token_count,
    enforce_checkpoint_budget,
)
from persona.tasks.contract import (
    AcceptanceCriterion,
    AcceptanceStatus,
    Contract,
    ContractBounds,
    Deliverable,
    DeliverableFormat,
    UpdateGranularity,
    UpdatePreference,
    bound_reached,
)
from persona.tasks.entity import TASK_SCHEMA_VERSION, Task
from persona.tasks.ledger import (
    MICROS_PER_CENT,
    MICROS_PER_DOLLAR,
    CostLedger,
    SpendKind,
    format_micros,
    micros_from_cents,
    micros_from_dollars,
)
from persona.tasks.leg_spend import (
    SUBSUMED_EXTERNAL_CALL_CENTS,
    LegSpendReporter,
    bind_leg_spend_reporter,
    report_leg_spend,
    reset_leg_spend_reporter,
)
from persona.tasks.reader import (
    IntrospectionStatus,
    TaskStateReader,
    TaskStateView,
    TaskSummary,
    project_task_state,
    summarise_task,
)
from persona.tasks.reconstruction import (
    METHOD_BLOCK,
    RecentLegSummary,
    ReconstructionBlock,
    ReconstructionStage,
    reconstruct_context,
)
from persona.tasks.reports import (
    TRANSIENT_RETRY_AFTER,
    CancellationSummary,
    CompletionReport,
    StuckReport,
    build_cancellation_summary,
    build_completion_report,
    build_stuck_report,
    classify_retryable,
)
from persona.tasks.state import (
    TERMINAL_STATES,
    TaskKind,
    TaskState,
    WaitKind,
    can_transition,
    is_terminal,
    validate_transition,
)
from persona.tasks.trigger import (
    AutoRetry,
    EventFire,
    EventTrigger,
    ResumeTrigger,
    Revived,
    ScheduledFire,
    TaskResumer,
    UserDispatch,
    UserReply,
    wait_kind_for,
)

__all__ = [
    "CHECKPOINT_SCHEMA_VERSION",
    "MAX_ARTIFACT_POINTERS",
    "WORKSPACE_POINTER_KIND",
    "merge_artifact_pointers",
    "pointers_from_artifacts",
    "DEFAULT_CHECKPOINT_TOKEN_BUDGET",
    "DEFAULT_LEG_MAX_STEPS",
    "DEFAULT_LEG_WALL_CLOCK_SECONDS",
    "TASK_SCHEMA_VERSION",
    "TERMINAL_STATES",
    "AcceptanceCriterion",
    "AcceptanceStatus",
    "CriterionClaim",
    "LegEvidence",
    "RejectedClaim",
    "settle_criteria",
    "ArtifactPointer",
    "CancellationSummary",
    "CompletionReport",
    "Contract",
    "ContractBounds",
    "Deliverable",
    "DeliverableFormat",
    "MICROS_PER_CENT",
    "MICROS_PER_DOLLAR",
    "CostLedger",
    "Decision",
    "AutoRetry",
    "EventFire",
    "EventTrigger",
    "IntrospectionStatus",
    "StuckReport",
    "TRANSIENT_RETRY_AFTER",
    "LegBox",
    "LegBoxLimit",
    "RecentLegSummary",
    "ReconstructionBlock",
    "METHOD_BLOCK",
    "ReconstructionStage",
    "ResumeTrigger",
    "Revived",
    "ScheduledFire",
    "SpendKind",
    "format_micros",
    "micros_from_cents",
    "micros_from_dollars",
    "SUBSUMED_EXTERNAL_CALL_CENTS",
    "LegSpendReporter",
    "bind_leg_spend_reporter",
    "report_leg_spend",
    "reset_leg_spend_reporter",
    "Task",
    "TaskCheckpoint",
    "TaskKind",
    "TaskResumer",
    "TaskState",
    "TaskStateReader",
    "TaskStateView",
    "TaskSummary",
    "UpdateGranularity",
    "UpdatePreference",
    "UserDispatch",
    "UserReply",
    "WaitKind",
    "project_task_state",
    "summarise_task",
    "bound_reached",
    "build_cancellation_summary",
    "build_completion_report",
    "build_stuck_report",
    "classify_retryable",
    "can_transition",
    "checkpoint_token_count",
    "enforce_checkpoint_budget",
    "is_terminal",
    "reconstruct_context",
    "validate_transition",
    "wait_kind_for",
]

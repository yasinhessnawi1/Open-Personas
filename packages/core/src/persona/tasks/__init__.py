"""persona.tasks — the autonomous task model (Spec A2).

The durable **task** entity above runs, the per-leg **checkpoint** that carries between
legs, the state machine, context reconstruction, and leg-boxing — all pure, ``mypy
--strict``, no DB/IO/clock (the durable RLS store + leg executor compose these in
persona-api / persona-runtime). T1 lands the checkpoint (the architectural lock, D-A2-1).
"""

from __future__ import annotations

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
    UpdateGranularity,
    UpdatePreference,
)
from persona.tasks.entity import TASK_SCHEMA_VERSION, Task
from persona.tasks.ledger import CostLedger, SpendKind
from persona.tasks.reader import (
    IntrospectionStatus,
    TaskStateReader,
    TaskStateView,
    TaskSummary,
    project_task_state,
    summarise_task,
)
from persona.tasks.reconstruction import (
    RecentLegSummary,
    ReconstructionBlock,
    ReconstructionStage,
    reconstruct_context,
)
from persona.tasks.reports import (
    CancellationSummary,
    CompletionReport,
    StuckReport,
    build_cancellation_summary,
    build_completion_report,
    build_stuck_report,
)
from persona.tasks.state import (
    TERMINAL_STATES,
    TaskState,
    WaitKind,
    can_transition,
    is_terminal,
    validate_transition,
)
from persona.tasks.trigger import (
    EventFire,
    EventTrigger,
    ResumeTrigger,
    ScheduledFire,
    TaskResumer,
    UserReply,
    wait_kind_for,
)

__all__ = [
    "CHECKPOINT_SCHEMA_VERSION",
    "DEFAULT_CHECKPOINT_TOKEN_BUDGET",
    "DEFAULT_LEG_MAX_STEPS",
    "DEFAULT_LEG_WALL_CLOCK_SECONDS",
    "TASK_SCHEMA_VERSION",
    "TERMINAL_STATES",
    "AcceptanceCriterion",
    "AcceptanceStatus",
    "ArtifactPointer",
    "CancellationSummary",
    "CompletionReport",
    "Contract",
    "ContractBounds",
    "CostLedger",
    "Decision",
    "EventFire",
    "EventTrigger",
    "IntrospectionStatus",
    "StuckReport",
    "LegBox",
    "LegBoxLimit",
    "RecentLegSummary",
    "ReconstructionBlock",
    "ReconstructionStage",
    "ResumeTrigger",
    "ScheduledFire",
    "SpendKind",
    "Task",
    "TaskCheckpoint",
    "TaskResumer",
    "TaskState",
    "TaskStateReader",
    "TaskStateView",
    "TaskSummary",
    "UpdateGranularity",
    "UpdatePreference",
    "UserReply",
    "WaitKind",
    "project_task_state",
    "summarise_task",
    "build_cancellation_summary",
    "build_completion_report",
    "build_stuck_report",
    "can_transition",
    "checkpoint_token_count",
    "enforce_checkpoint_budget",
    "is_terminal",
    "reconstruct_context",
    "validate_transition",
    "wait_kind_for",
]

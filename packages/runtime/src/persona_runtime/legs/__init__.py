"""persona_runtime.legs — the leg executor (Spec A2, T6).

One leg = one bounded execution of the unmodified Spec-06 ``AgenticLoop``, book-ended by
context reconstruction and a checkpoint write. The executor composes the loop (criterion
12), enforces the box (wall-clock trip at a step boundary), and writes the checkpoint
through the :class:`CheckpointSink` port (the api's ``CheckpointStore.append`` — A2-R-4).
"""

from __future__ import annotations

from persona_runtime.legs.acceptance import (
    ACCEPTANCE_PROMPT_VERSION,
    DEFAULT_ASSESS_TIMEOUT_S,
    AcceptanceAssessor,
    evidence_from_run,
)
from persona_runtime.legs.distiller import CompactingCheckpointWriter
from persona_runtime.legs.executor import (
    AgenticRunner,
    BasicCheckpointWriter,
    CheckpointSink,
    CheckpointWriter,
    LegDisposition,
    LegExecutor,
    LegOutcome,
)
from persona_runtime.legs.memory import (
    MILESTONE_TEXT_CAP,
    MilestoneRecorder,
    TaskEpisodicSink,
    TaskMilestone,
    milestone_for,
    render_milestone_summary,
)

__all__ = [
    "ACCEPTANCE_PROMPT_VERSION",
    "DEFAULT_ASSESS_TIMEOUT_S",
    "MILESTONE_TEXT_CAP",
    "AcceptanceAssessor",
    "AgenticRunner",
    "BasicCheckpointWriter",
    "CheckpointSink",
    "CompactingCheckpointWriter",
    "CheckpointWriter",
    "LegDisposition",
    "LegExecutor",
    "LegOutcome",
    "MilestoneRecorder",
    "TaskEpisodicSink",
    "TaskMilestone",
    "evidence_from_run",
    "milestone_for",
    "render_milestone_summary",
]

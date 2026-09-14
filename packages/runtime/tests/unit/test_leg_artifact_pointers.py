"""A file one leg writes reaches the next leg's reconstruction (R9-162).

``artifact_pointers`` was a closed loop: the type, the checkpoint field, the ARTIFACTS block
in the reconstruction, the completion report and the task workspace all existed, and every
write site copied the prior (always empty) tuple forward. ``ToolResult.artifacts`` (Spec 28)
already carried the durable byte-outputs a tool persisted; nothing joined the two.

These tests drive the real chain: a run whose tool result carries a persisted artifact →
the writer's checkpoint → the NEXT leg's rendered context.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from persona.schema.tools import PersistedArtifact, ToolCall, ToolResult
from persona.tasks import (
    MAX_ARTIFACT_POINTERS,
    ArtifactPointer,
    Contract,
    ScheduledFire,
    Task,
    TaskCheckpoint,
    reconstruct_context,
)
from persona_runtime.agentic.run import Run, RunStatus
from persona_runtime.agentic.step import Step, StepType
from persona_runtime.legs import BasicCheckpointWriter, CompactingCheckpointWriter
from persona_runtime.legs.ledger import artifacts_from_run

_NOW = datetime(2026, 6, 25, 9, 0, tzinfo=UTC)
_TRIGGER = ScheduledFire(schedule_id="s1", fire_time=_NOW)


def _task() -> Task:
    return Task(
        id="t1",
        owner_id="u",
        persona_id="p",
        contract=Contract(goal="write the weekly rent report"),
        created_at=_NOW,
        updated_at=_NOW,
    )


def _artifact(path: str, *, mime: str = "text/markdown") -> PersistedArtifact:
    return PersistedArtifact(workspace_path=path, mime_type=mime, size_bytes=128)


def _run_writing(*paths: str, is_error: bool = False, output: str = "wrote it") -> Run:
    """A run whose ``file_write`` step persisted each path into the workspace."""
    return Run(
        persona_id="p",
        task="x",
        status=RunStatus.COMPLETED,
        steps=[
            Step(
                type=StepType.TOOL_CALL,
                tool_calls=[ToolCall(name="file_write", args={"path": p}) for p in paths],
                results=[
                    ToolResult(
                        tool_name="file_write",
                        content=f"wrote {p}",
                        is_error=is_error,
                        artifacts=(_artifact(p),),
                    )
                    for p in paths
                ],
            )
        ],
        output=output,
        started_at=_NOW,
        finished_at=_NOW,
    )


def test_artifacts_from_run_reads_the_typed_channel() -> None:
    pointers = artifacts_from_run(_run_writing("reports/week-24.md", "reports/prices.csv"))
    assert pointers == (
        ArtifactPointer(kind="workspace", ref="reports/week-24.md"),
        ArtifactPointer(kind="workspace", ref="reports/prices.csv"),
    )


def test_a_failed_tool_contributes_no_pointer() -> None:
    """A half-written file is worse than no pointer: the next leg would treat it as done."""
    assert artifacts_from_run(_run_writing("reports/week-24.md", is_error=True)) == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", [CompactingCheckpointWriter(), BasicCheckpointWriter()])
async def test_the_file_a_leg_writes_reaches_the_next_legs_reconstruction(
    writer: CompactingCheckpointWriter | BasicCheckpointWriter,
) -> None:
    """The end-to-end claim, on both live writers: produce → checkpoint → recite."""
    checkpoint = await writer.write(
        task=_task(),
        prior=None,
        run=_run_writing("reports/week-24.md"),
        leg_id="t1:leg:0",
        seq=0,
        now=_NOW,
    )
    assert checkpoint.artifact_pointers == (
        ArtifactPointer(kind="workspace", ref="reports/week-24.md"),
    )
    rendered = "\n\n".join(
        block.content
        for block in reconstruct_context(
            contract=_task().contract, trigger=_TRIGGER, checkpoint=checkpoint
        )
    )
    assert "ARTIFACTS:" in rendered
    assert "- workspace: reports/week-24.md" in rendered


@pytest.mark.asyncio
async def test_pointers_accumulate_across_legs_and_a_rewrite_is_one_entry() -> None:
    writer = CompactingCheckpointWriter()
    prior: TaskCheckpoint | None = None
    for leg, paths in enumerate(
        [("reports/week-24.md",), ("reports/prices.csv",), ("reports/week-24.md",)]
    ):
        prior = await writer.write(
            task=_task(),
            prior=prior,
            run=_run_writing(*paths),
            leg_id=f"t1:leg:{leg}",
            seq=leg,
            now=_NOW,
        )
    assert prior is not None
    # Three legs, two distinct files; the re-written one keeps its original position.
    assert [p.ref for p in prior.artifact_pointers] == [
        "reports/week-24.md",
        "reports/prices.csv",
    ]


@pytest.mark.asyncio
async def test_the_pointer_list_is_bounded() -> None:
    """The pointers sit outside the token budget, so the merge is what bounds them."""
    writer = CompactingCheckpointWriter()
    prior = TaskCheckpoint(
        task_id="t1",
        leg_id="t1:leg:0",
        checkpoint_seq=0,
        artifact_pointers=tuple(
            ArtifactPointer(kind="workspace", ref=f"out/{i:04d}.md")
            for i in range(MAX_ARTIFACT_POINTERS)
        ),
        updated_at=_NOW,
    )
    written = await writer.write(
        task=_task(),
        prior=prior,
        run=_run_writing("out/newest.md"),
        leg_id="t1:leg:1",
        seq=1,
        now=_NOW,
    )
    assert len(written.artifact_pointers) == MAX_ARTIFACT_POINTERS
    assert written.artifact_pointers[-1].ref == "out/newest.md"  # the newest survives
    assert "out/0000.md" not in [p.ref for p in written.artifact_pointers]  # the oldest fell off

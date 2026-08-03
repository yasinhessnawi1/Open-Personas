"""The production checkpoint distiller stays under budget (Spec A2, T12; D-A2-1).

The reason the live path must NOT use ``BasicCheckpointWriter``: it appends every leg's
output and overflows. The ``CompactingCheckpointWriter`` reflect-and-compacts so a many-leg
task never trips ``CheckpointTooLargeError``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from persona.tasks import (
    Contract,
    Task,
    TaskCheckpoint,
    checkpoint_token_count,
    enforce_checkpoint_budget,
)
from persona_runtime.agentic.run import Run, RunStatus
from persona_runtime.legs import CompactingCheckpointWriter

_NOW = datetime(2026, 6, 25, 9, 0, tzinfo=UTC)


def _task() -> Task:
    return Task(
        id="t1",
        owner_id="u",
        persona_id="p",
        contract=Contract(goal="g"),
        created_at=_NOW,
        updated_at=_NOW,
    )


def _run(output: str) -> Run:
    return Run(
        persona_id="p",
        task="x",
        status=RunStatus.COMPLETED,
        steps=[],
        output=output,
        started_at=_NOW,
        finished_at=_NOW,
    )


def test_distiller_stays_under_budget_over_many_legs() -> None:
    writer = CompactingCheckpointWriter(token_budget=200)
    prior: TaskCheckpoint | None = None
    for i in range(30):
        cp = writer.write(
            task=_task(),
            prior=prior,
            run=_run(f"leg {i} established a fairly wordy conclusion about the work so far " * 3),
            leg_id=f"t1:leg:{i}",
            seq=i,
            now=_NOW,
        )
        # Never overflows the store's hard gate — the whole point.
        enforce_checkpoint_budget(cp, token_budget=200)
        prior = cp
    assert checkpoint_token_count(prior) <= 200  # type: ignore[arg-type]
    # The oldest findings were compacted into a marker, not lost (restorable via run records).
    assert any("earlier findings compacted" in c for c in prior.progress_conclusions)  # type: ignore[union-attr]
    assert prior.event_log_cursor is not None  # type: ignore[union-attr]


def test_basic_writer_would_overflow_proving_the_distiller_is_needed() -> None:
    # Contrast: the stand-in keeps every output, so a long task trips the budget. This is why
    # the live path MUST wire the distiller (the T12 no-unwired-seam audit).
    from persona.errors import CheckpointTooLargeError
    from persona_runtime.legs import BasicCheckpointWriter

    writer = BasicCheckpointWriter()
    prior: TaskCheckpoint | None = None
    overflowed = False
    for i in range(40):
        prior = writer.write(
            task=_task(),
            prior=prior,
            run=_run("a wordy conclusion " * 30),
            leg_id=f"t1:leg:{i}",
            seq=i,
            now=_NOW,
        )
        try:
            enforce_checkpoint_budget(prior, token_budget=200)
        except CheckpointTooLargeError:
            overflowed = True
            break
    assert overflowed, "BasicCheckpointWriter should overflow — that's why the distiller exists"


def test_next_step_never_echoes_the_leg_output() -> None:
    """THE loop regression (R9-103): an answer must never become an instruction.

    ``next_step`` is defined as "the single concrete action this leg's successor
    runs first", and ``reconstruction`` recites it verbatim as ``NEXT STEP: …``.
    Assigning ``run.output`` handed the successor a finished ANSWER as its
    INSTRUCTION, so it re-derived the same answer and wrote it back -- a closed
    loop the task could never escape. Production: a recurring task emitted
    byte-identical output every hour and its progress log filled with copies of
    one paragraph, at full price per fire.
    """
    answer = "Here is a curated list of notable GitHub repositories related to AI personas."
    cp = CompactingCheckpointWriter(token_budget=2000).write(
        task=_task(),
        prior=None,
        run=_run(answer),
        leg_id="t1:leg:0",
        seq=0,
        now=_NOW,
    )
    assert cp.next_step != answer, "the leg's answer must not become the successor's instruction"
    assert not cp.next_step, (
        "a DETERMINISTIC writer cannot generate a next action, so it must emit empty -- "
        "reconstruction then omits the NEXT STEP block and the successor replans from "
        "the contract, instead of being told to repeat itself"
    )
    # The finding itself is still preserved; only the misuse as an instruction is gone.
    assert answer in cp.progress_conclusions


def test_a_bad_next_step_cannot_propagate_forward() -> None:
    """The ``or prior.next_step`` fallback let one bad value persist indefinitely.

    Once an answer landed in ``next_step`` it was carried into every later
    checkpoint, so the loop survived even legs that produced no output.
    """
    poisoned = TaskCheckpoint(
        task_id="t1",
        leg_id="t1:leg:0",
        checkpoint_seq=0,
        progress_conclusions=(),
        next_step="Here is a curated list of notable GitHub repositories.",
        updated_at=_NOW,
    )
    cp = CompactingCheckpointWriter(token_budget=2000).write(
        task=_task(),
        prior=poisoned,
        run=_run(""),  # this leg produced nothing to append
        leg_id="t1:leg:1",
        seq=1,
        now=_NOW,
    )
    assert not cp.next_step, "a poisoned next_step must not be carried forward"

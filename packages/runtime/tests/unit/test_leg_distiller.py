"""The production checkpoint distiller stays under budget (Spec A2, T12; D-A2-1).

The reason the live path must NOT use ``BasicCheckpointWriter``: it appends every leg's
output and overflows. The ``CompactingCheckpointWriter`` reflect-and-compacts so a many-leg
task never trips ``CheckpointTooLargeError``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from persona.schema.tools import ToolCall, ToolResult
from persona.tasks import (
    Contract,
    ScheduledFire,
    Task,
    TaskCheckpoint,
    checkpoint_token_count,
    enforce_checkpoint_budget,
    reconstruct_context,
)
from persona_runtime.agentic.run import Run, RunStatus
from persona_runtime.agentic.step import Step, StepType
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


@pytest.mark.asyncio
async def test_distiller_stays_under_budget_over_many_legs() -> None:
    writer = CompactingCheckpointWriter(token_budget=200)
    prior: TaskCheckpoint | None = None
    for i in range(30):
        cp = await writer.write(
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


@pytest.mark.asyncio
async def test_basic_writer_would_overflow_proving_the_distiller_is_needed() -> None:
    # Contrast: the stand-in keeps every output, so a long task trips the budget. This is why
    # the live path MUST wire the distiller (the T12 no-unwired-seam audit).
    from persona.errors import CheckpointTooLargeError
    from persona_runtime.legs import BasicCheckpointWriter

    writer = BasicCheckpointWriter()
    prior: TaskCheckpoint | None = None
    overflowed = False
    for i in range(40):
        prior = await writer.write(
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


@pytest.mark.asyncio
async def test_next_step_never_echoes_the_leg_output() -> None:
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
    cp = await CompactingCheckpointWriter(token_budget=2000).write(
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


@pytest.mark.asyncio
async def test_a_bad_next_step_cannot_propagate_forward() -> None:
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
    cp = await CompactingCheckpointWriter(token_budget=2000).write(
        task=_task(),
        prior=poisoned,
        run=_run(""),  # this leg produced nothing to append
        leg_id="t1:leg:1",
        seq=1,
        now=_NOW,
    )
    assert not cp.next_step, "a poisoned next_step must not be carried forward"


# --- the query / source ledgers (Spec W1, T11; D-W1-16) ---------------------


def _search_step(query: str, urls: list[str], *, failed: bool = False) -> Step:
    """One step that searched and got results, in the shape the loop records."""
    call = ToolCall(name="web_search", args={"query": query}, call_id="c1")
    result = ToolResult(
        tool_name="web_search",
        call_id="c1",
        content="three results" if not failed else "upstream 503",
        is_error=failed,
        data={"results": [{"title": "t", "url": u, "snippet": "s"} for u in urls]},
    )
    return Step(type=StepType.TOOL_CALL, tool_calls=[call], results=[result])


def _run_with(steps: list[Step], output: str = "a conclusion") -> Run:
    return Run(
        persona_id="p",
        task="x",
        status=RunStatus.COMPLETED,
        steps=steps,
        output=output,
        started_at=_NOW,
        finished_at=_NOW,
    )


@pytest.mark.asyncio
async def test_leg_two_knows_what_leg_one_searched() -> None:
    """The point of the whole ledger: a two-leg task through the REAL writer, and the
    successor's context names the queries and the sources its predecessor already used."""
    writer = CompactingCheckpointWriter()
    first = await writer.write(
        task=_task(),
        prior=None,
        run=_run_with([_search_step("husleieloven deposit interest", ["https://lovdata.no/a"])]),
        leg_id="t1:leg:0",
        seq=0,
        now=_NOW,
    )
    second = await writer.write(
        task=_task(),
        prior=first,
        run=_run_with([_search_step("husleietvistutvalget statistics", ["https://htu.no/b"])]),
        leg_id="t1:leg:1",
        seq=1,
        now=_NOW,
    )

    assert second.queries_run == (
        "husleieloven deposit interest",
        "husleietvistutvalget statistics",
    )
    assert second.sources_seen == ("https://lovdata.no/a", "https://htu.no/b")

    # And the successor READS them: the reconstruction the third leg runs on names both.
    blocks = reconstruct_context(
        contract=_task().contract,
        trigger=ScheduledFire(schedule_id="s", fire_time=_NOW),
        checkpoint=second,
    )
    rendered = "\n".join(b.content for b in blocks)
    assert "husleieloven deposit interest" in rendered
    assert "https://lovdata.no/a" in rendered
    assert "QUERIES ALREADY RUN:" in rendered
    assert "SOURCES ALREADY SEEN:" in rendered


@pytest.mark.asyncio
async def test_the_same_query_on_two_legs_occupies_one_line() -> None:
    writer = CompactingCheckpointWriter()
    first = await writer.write(
        task=_task(),
        prior=None,
        run=_run_with([_search_step("deposit rules", ["https://a.no/x"])]),
        leg_id="l0",
        seq=0,
        now=_NOW,
    )
    second = await writer.write(
        task=_task(),
        prior=first,
        run=_run_with([_search_step("deposit rules", ["https://a.no/x"])]),
        leg_id="l1",
        seq=1,
        now=_NOW,
    )

    assert second.queries_run == ("deposit rules",)
    assert second.sources_seen == ("https://a.no/x",)


@pytest.mark.asyncio
async def test_a_search_that_failed_is_not_recorded_as_asked() -> None:
    """A rate limit is not an answer. Recording it would let one bad minute cost the task
    that source for the rest of its life, since later legs read the ledger as "done"."""
    writer = CompactingCheckpointWriter()
    checkpoint = await writer.write(
        task=_task(),
        prior=None,
        run=_run_with([_search_step("deposit rules", [], failed=True)]),
        leg_id="l0",
        seq=0,
        now=_NOW,
    )

    assert checkpoint.queries_run == ()
    assert checkpoint.sources_seen == ()


@pytest.mark.asyncio
async def test_the_ledgers_stay_inside_the_budget_over_many_legs() -> None:
    """D-W1-16: they joined the accumulating core, so they are inside its gate. The oldest
    fold into a count marker rather than being cut in half."""
    writer = CompactingCheckpointWriter(token_budget=200)
    prior: TaskCheckpoint | None = None
    for i in range(40):
        prior = await writer.write(
            task=_task(),
            prior=prior,
            run=_run_with(
                [
                    _search_step(
                        f"a reasonably wordy query number {i}", [f"https://site{i}.no/page"]
                    )
                ],
                output=f"leg {i} concluded something",
            ),
            leg_id=f"l{i}",
            seq=i,
            now=_NOW,
        )
        enforce_checkpoint_budget(prior, token_budget=200)

    assert prior is not None
    assert prior.queries_run[0].startswith("[")  # the oldest folded into a marker
    assert "folded" in prior.queries_run[0]
    assert prior.queries_run[-1] == "a reasonably wordy query number 39"  # recency survives
    assert prior.sources_seen[-1] == "https://site39.no/page"
    # Nothing is half an entry: every kept line is a whole query or a whole marker.
    assert all(line == line.strip() for line in prior.queries_run)


@pytest.mark.asyncio
async def test_a_task_that_searches_nothing_carries_empty_ledgers() -> None:
    writer = CompactingCheckpointWriter()
    checkpoint = await writer.write(
        task=_task(), prior=None, run=_run("just thinking"), leg_id="l0", seq=0, now=_NOW
    )

    assert checkpoint.queries_run == ()
    assert checkpoint.sources_seen == ()


@pytest.mark.asyncio
async def test_one_leg_that_asks_the_same_thing_twice_records_it_once() -> None:
    """The dedupe is per ledger, not only across legs: a run that searched the same phrase
    on two steps asked the world one question, and the next leg should read one line."""
    writer = CompactingCheckpointWriter()
    checkpoint = await writer.write(
        task=_task(),
        prior=None,
        run=_run_with(
            [
                _search_step("deposit rules", ["https://a.no/x"]),
                _search_step("deposit rules", ["https://a.no/x", "https://b.no/y"]),
            ]
        ),
        leg_id="l0",
        seq=0,
        now=_NOW,
    )

    assert checkpoint.queries_run == ("deposit rules",)
    assert checkpoint.sources_seen == ("https://a.no/x", "https://b.no/y")

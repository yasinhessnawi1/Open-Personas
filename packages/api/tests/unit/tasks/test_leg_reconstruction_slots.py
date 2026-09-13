"""A leg reads its own history and its own memory (Spec W1, T12).

The reconstruction has carried a RECENT_LEGS slot and a RETRIEVAL slot since A2, and the
handler filled neither (research V-3). So a recurring task woke every morning knowing only
its last checkpoint, searched the web for things its own persona had known for weeks, and
the "live retrieval brings today's knowledge" half of D-A2-3 was a comment.

These drive the REAL :class:`TaskLegHandler` and read the reconstruction the runner is
actually handed, because that string IS the leg's whole world: what is not in it did not
reach the model, whatever the wiring looks like from the outside.

What is pinned:

- both blocks appear, with the prior legs' own words and the persona's own memory in them;
- the newest checkpoint is NOT repeated as a summary (it is the CHECKPOINT block);
- the window is bounded by its configured size and ordered oldest first;
- retrieval is queried with the CONTRACT GOAL, not the reconstruction text;
- a slow recall never delays a leg: past the deadline it runs memoryless, and a recall that
  raises does not fail the leg either. Memory improves a leg; it is never a precondition.
"""

# ruff: noqa: ARG002 (Protocol-conformance stubs keep the full production signatures)
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona.backends.types import ChatResponse, TokenUsage
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource
from persona.tasks import Contract, ScheduledFire, SpendKind, Task, TaskCheckpoint
from persona_api.db.community import (
    create_community_schema,
    ensure_owner,
    make_community_engine,
)
from persona_api.db.models import personas as personas_t
from persona_api.services.llm_usage_collector import UsageCollectingBackend
from persona_api.tasks import TaskLegHandler, TaskLegPayload, TaskStore
from persona_api.tasks.handler import MAX_RECENT_LEG_SUMMARIES
from persona_api.tasks.leg_retrieval import RECALL_SNIPPET_CAP, LegRetrieval
from persona_runtime.agentic.run import Run, RunStatus
from persona_runtime.legs import CompactingCheckpointWriter
from persona_runtime.unified_recall import UnifiedProjection
from sqlalchemy import insert

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator, Mapping
    from pathlib import Path

    from persona.tasks import LegBox
    from persona_runtime.agentic.events import RunEvent
    from persona_runtime.agentic.run import CancelToken, StepUsage
    from sqlalchemy import Engine

_NOW = datetime(2026, 9, 12, 9, 0, tzinfo=UTC)
_OWNER = "user_a"
_PERSONA = "persona_a"
_TASK = "t1"
_GOAL = "track Norwegian rental deposit rules"
_TRIGGER = ScheduledFire(schedule_id="sched-1", fire_time=_NOW)


class _Runner:
    """Keeps the reconstruction it was handed: that string is the leg's whole world."""

    def __init__(self) -> None:
        self.tasks_seen: list[str] = []

    async def run(
        self,
        task: str,
        *,
        on_event: Callable[[RunEvent], Awaitable[None]],
        cancel_token: CancelToken,
        on_step_usage: Callable[[StepUsage], Awaitable[None]] | None = None,
    ) -> Run:
        self.tasks_seen.append(task)
        return Run(
            persona_id=_PERSONA,
            task=task,
            status=RunStatus.COMPLETED,
            steps=[],
            output="nothing new today",
            started_at=_NOW,
            finished_at=_NOW,
        )


class _RunnerBuilder:
    def __init__(self, runner: _Runner) -> None:
        self._runner = runner

    def build(self, task_id: str, persona_id: str, box: LegBox, *, task: object = None) -> _Runner:
        return self._runner


class _Checkpoints:
    """A checkpoint store stand-in holding a task's history, newest first on read."""

    def __init__(self, history: list[TaskCheckpoint] | None = None) -> None:
        self.history = history or []
        self.list_recent_calls: list[int] = []

    def get_latest(self, owner_id: str, task_id: str) -> TaskCheckpoint | None:
        return max(self.history, key=lambda c: c.checkpoint_seq, default=None)

    def list_recent(self, owner_id: str, task_id: str, *, limit: int) -> list[TaskCheckpoint]:
        self.list_recent_calls.append(limit)
        newest_first = sorted(self.history, key=lambda c: c.checkpoint_seq, reverse=True)
        return newest_first[:limit]

    def append(
        self,
        task: Task,
        checkpoint: TaskCheckpoint,
        *,
        spend: Mapping[SpendKind, int] | None = None,
        now: datetime,
    ) -> Task:
        # Records only: the CAS and the head advance are the store's business (and are
        # pinned where that is the subject). These tests are about what the leg READ.
        self.history.append(checkpoint)
        return task


class _IgnoresTheLimit(_Checkpoints):
    """A checkpoint store that returns everything it has, whatever ``limit`` said.

    ``CheckpointStore`` is a Protocol, and the handler's own slice is the only thing that
    bounds the window if an implementation reads the limit loosely (a cache, a different
    backend, a future store). A mutation removing that slice survived every other test,
    because the production store's SQL ``LIMIT`` was quietly doing the work.
    """

    def list_recent(self, owner_id: str, task_id: str, *, limit: int) -> list[TaskCheckpoint]:
        self.list_recent_calls.append(limit)
        return sorted(self.history, key=lambda c: c.checkpoint_seq, reverse=True)


class _RaisingCheckpoints(_Checkpoints):
    def list_recent(self, owner_id: str, task_id: str, *, limit: int) -> list[TaskCheckpoint]:
        msg = "the store is having a bad day"
        raise RuntimeError(msg)


class _Context:
    def __init__(self, owner_id: str = _OWNER) -> None:
        self._owner_id = owner_id
        self.metered: list[dict[str, str]] = []

    @property
    def owner_id(self) -> str:
        return self._owner_id

    @property
    def job_id(self) -> str:
        return "job-1"

    def meter(self, *, amount_micros: int, kind: str, detail: object = None) -> None:
        if isinstance(detail, dict):
            self.metered.append(dict(detail))


def _checkpoint(seq: int, conclusion: str, *, blocked_on: str | None = None) -> TaskCheckpoint:
    return TaskCheckpoint(
        task_id=_TASK,
        leg_id=f"leg-{seq}",
        checkpoint_seq=seq,
        progress_conclusions=("an older finding", conclusion),
        blocked_on=blocked_on,
        updated_at=_NOW,
    )


def _chunk(text: str) -> PersonaChunk:
    return PersonaChunk(
        id=f"{_PERSONA}::episodic::0001",
        text=text,
        created_at=_NOW,
        provenance=ChunkProvenance(
            source=WriteSource.SYSTEM,
            logical_id="l1",
            version=1,
            written_at=_NOW,
            written_by="test",
        ),
    )


def _recall_returning(*texts: str) -> tuple[LegRetrieval, list[str]]:
    """A retrieval whose recall answers with ``texts`` and records what it was asked."""
    asked: list[str] = []

    def recall(query: str) -> UnifiedProjection:
        asked.append(query)
        return UnifiedProjection(episodic=[_chunk(t) for t in texts])

    return LegRetrieval(recall_for=lambda _persona: recall), asked


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = make_community_engine(tmp_path / "legs.db")
    create_community_schema(eng)
    ensure_owner(eng, owner_id=_OWNER, email="a@example.com")
    with eng.begin() as conn:
        conn.execute(insert(personas_t).values(id=_PERSONA, owner_id=_OWNER, yaml="name: x"))
    yield eng
    eng.dispose()


@pytest.fixture
def tasks(engine: Engine) -> TaskStore:
    store = TaskStore(engine)
    store.create(
        Task(
            id=_TASK,
            owner_id=_OWNER,
            persona_id=_PERSONA,
            contract=Contract(goal=_GOAL),
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    store.start(_OWNER, _TASK, now=_NOW)
    return store


def _handler(
    tasks: TaskStore,
    runner: _Runner,
    checkpoints: _Checkpoints,
    *,
    retrieval: LegRetrieval | None = None,
    window: int = 3,
) -> TaskLegHandler:
    return TaskLegHandler(
        task_store=tasks,
        checkpoint_store=checkpoints,  # type: ignore[arg-type]  # duck-typed CheckpointSink
        runner_builder=_RunnerBuilder(runner),
        recent_leg_summaries=window,
        retrieval=retrieval,
    )


async def _run_leg(handler: TaskLegHandler, *, predecessor_seq: int | None = 2) -> _Context:
    context = _Context()
    await handler.handle(
        TaskLegPayload(task_id=_TASK, predecessor_seq=predecessor_seq, trigger=_TRIGGER),
        context,  # type: ignore[arg-type]  # duck-typed JobContext
    )
    return context


@pytest.mark.asyncio
async def test_the_leg_reads_the_recent_window_and_its_own_memory(tasks: TaskStore) -> None:
    runner = _Runner()
    checkpoints = _Checkpoints(
        [
            _checkpoint(0, "leg zero found A"),
            _checkpoint(1, "leg one found B"),
            _checkpoint(2, "leg two found C"),
        ]
    )
    retrieval, asked = _recall_returning("the user rents in Bergen")

    await _run_leg(_handler(tasks, runner, checkpoints, retrieval=retrieval))

    reconstruction = runner.tasks_seen[0]
    assert "leg zero found A" in reconstruction  # the window carries prior legs' own words
    assert "leg one found B" in reconstruction
    assert "the user rents in Bergen" in reconstruction  # and the persona's own memory
    assert asked == [_GOAL]  # queried with the goal, not the reconstruction text


@pytest.mark.asyncio
async def test_the_newest_checkpoint_is_not_repeated_as_a_summary(tasks: TaskStore) -> None:
    """It is the CHECKPOINT block, rendered in full. Summarising it too would spend the
    window on something the leg is already reading."""
    runner = _Runner()
    checkpoints = _Checkpoints(
        [_checkpoint(1, "leg one found B"), _checkpoint(2, "leg two found C")]
    )

    await _run_leg(_handler(tasks, runner, checkpoints))

    reconstruction = runner.tasks_seen[0]
    assert reconstruction.count("leg two found C") == 1  # once, in the checkpoint block
    assert "leg-2" not in reconstruction  # the window names the legs it summarises
    assert "leg-1" in reconstruction


@pytest.mark.asyncio
async def test_the_window_is_bounded_and_reads_oldest_first(tasks: TaskStore) -> None:
    runner = _Runner()
    history = [_checkpoint(seq, f"leg {seq} found something") for seq in range(6)]
    checkpoints = _Checkpoints(history)

    await _run_leg(_handler(tasks, runner, checkpoints, window=2), predecessor_seq=5)

    reconstruction = runner.tasks_seen[0]
    assert "leg-3" in reconstruction  # the two legs before the newest
    assert "leg-4" in reconstruction
    assert "leg-0" not in reconstruction  # and nothing older
    assert reconstruction.index("leg-3") < reconstruction.index("leg-4")  # forward, like the work


@pytest.mark.asyncio
async def test_a_blocked_leg_says_so_in_the_window(tasks: TaskStore) -> None:
    """The outcome is read off the checkpoint, never invented: a leg that recorded what it
    was waiting on is summarised as blocked, and one that did not is not."""
    runner = _Runner()
    checkpoints = _Checkpoints(
        [
            _checkpoint(0, "leg zero found A", blocked_on="waiting for the tenancy board"),
            _checkpoint(1, "leg one found B"),
        ]
    )

    await _run_leg(_handler(tasks, runner, checkpoints), predecessor_seq=1)

    reconstruction = runner.tasks_seen[0]
    assert "blocked: waiting for the tenancy board" in reconstruction


@pytest.mark.asyncio
async def test_a_slow_recall_does_not_delay_the_leg(tasks: TaskStore) -> None:
    """The V13 posture. Past the deadline the leg runs memoryless, and it still runs."""
    runner = _Runner()

    def slow(_query: str) -> UnifiedProjection:
        import time

        time.sleep(0.3)
        return UnifiedProjection(episodic=[_chunk("too late to matter")])

    retrieval = LegRetrieval(recall_for=lambda _p: slow, timeout_s=0.05)

    await _run_leg(_handler(tasks, runner, _Checkpoints(), retrieval=retrieval))

    assert runner.tasks_seen, "the leg must run even when its memory does not arrive"
    assert "too late to matter" not in runner.tasks_seen[0]


@pytest.mark.asyncio
async def test_a_recall_that_raises_does_not_fail_the_leg(tasks: TaskStore) -> None:
    runner = _Runner()

    def broken(_query: str) -> UnifiedProjection:
        msg = "the recall path is down"
        raise RuntimeError(msg)

    retrieval = LegRetrieval(recall_for=lambda _p: broken)

    await _run_leg(_handler(tasks, runner, _Checkpoints(), retrieval=retrieval))

    assert runner.tasks_seen


@pytest.mark.asyncio
async def test_an_abstaining_gate_is_respected(tasks: TaskStore) -> None:
    """K9-D-9: the gate decided this query has no business reading that memory. A leg is
    not a reason to overrule it."""
    runner = _Runner()

    def abstains(_query: str) -> UnifiedProjection:
        return UnifiedProjection(episodic=[_chunk("private thing")], abstained=True)

    retrieval = LegRetrieval(recall_for=lambda _p: abstains)

    await _run_leg(_handler(tasks, runner, _Checkpoints(), retrieval=retrieval))

    assert "private thing" not in runner.tasks_seen[0]


@pytest.mark.asyncio
async def test_a_failing_checkpoint_store_costs_the_window_not_the_leg(tasks: TaskStore) -> None:
    runner = _Runner()

    await _run_leg(_handler(tasks, runner, _RaisingCheckpoints()))

    assert runner.tasks_seen


@pytest.mark.asyncio
async def test_without_wiring_a_leg_is_byte_identical_to_before(tasks: TaskStore) -> None:
    """No retrieval and a zero window is the pre-W1 behaviour exactly: the two blocks are
    absent, and nothing about the rest of the reconstruction moves."""
    runner = _Runner()
    checkpoints = _Checkpoints([_checkpoint(0, "leg zero found A")])

    await _run_leg(_handler(tasks, runner, checkpoints, window=0), predecessor_seq=0)

    reconstruction = runner.tasks_seen[0]
    assert "leg-0" not in reconstruction
    assert checkpoints.list_recent_calls == []  # not even asked for
    assert _GOAL in reconstruction  # the contract still leads


@pytest.mark.asyncio
async def test_the_window_asks_for_one_more_than_it_keeps(tasks: TaskStore) -> None:
    """Because the newest is dropped: asking for exactly N would return N-1 usable legs."""
    runner = _Runner()
    checkpoints = _Checkpoints([_checkpoint(0, "a"), _checkpoint(1, "b")])

    await _run_leg(_handler(tasks, runner, checkpoints, window=3), predecessor_seq=1)

    assert checkpoints.list_recent_calls == [4]


def test_the_retrieval_seam_is_off_the_event_loop() -> None:
    """A synchronous recall doing real work (embeddings, a cross-encoder) must not block the
    worker's loop while a leg waits for it."""
    running: list[bool] = []

    def recall(_query: str) -> UnifiedProjection:
        running.append(_loop_is_running())
        return UnifiedProjection()

    async def drive() -> tuple[str, ...]:
        return await LegRetrieval(recall_for=lambda _p: recall).snippets(_PERSONA, _GOAL)

    asyncio.run(drive())
    assert running == [False]  # it ran in a worker thread, not on the loop


def _loop_is_running() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


@pytest.mark.asyncio
async def test_a_window_wider_than_the_maximum_is_clamped(tasks: TaskStore) -> None:
    """``MAX_RECENT_LEG_SUMMARIES`` is the documented ceiling. The window sits between the
    contract and the live retrieval, so a long tail of old summaries pushes both away from
    where a model attends; a misconfigured 9 must not be able to do that."""
    runner = _Runner()
    history = [_checkpoint(seq, f"leg {seq} found something") for seq in range(9)]
    checkpoints = _Checkpoints(history)

    await _run_leg(_handler(tasks, runner, checkpoints, window=9), predecessor_seq=8)

    reconstruction = runner.tasks_seen[0]
    carried = [seq for seq in range(9) if f"leg-{seq}" in reconstruction]
    assert len(carried) == MAX_RECENT_LEG_SUMMARIES
    assert checkpoints.list_recent_calls == [MAX_RECENT_LEG_SUMMARIES + 1]


@pytest.mark.asyncio
async def test_the_snippet_cap_bounds_what_a_leg_carries() -> None:
    """``RECALL_SNIPPET_CAP``: the retrieval block is context a leg reads before it works,
    not the work itself. Past a handful it crowds out the contract it sits under."""
    many = [f"remembered thing {n}" for n in range(12)]

    def recall(_query: str) -> UnifiedProjection:
        return UnifiedProjection(episodic=[_chunk(t) for t in many])

    snippets = await LegRetrieval(recall_for=lambda _p: recall).snippets(_PERSONA, _GOAL)

    assert len(snippets) == RECALL_SNIPPET_CAP
    assert snippets[0] == "remembered thing 0"  # and it keeps the front, not a random slice


@pytest.mark.asyncio
async def test_the_handler_bounds_the_window_itself(tasks: TaskStore) -> None:
    """Not only the store's LIMIT: the handler slices what it was given, so a store that
    answers loosely cannot push six legs of history between the contract and the work."""
    runner = _Runner()
    history = [_checkpoint(seq, f"leg {seq} found something") for seq in range(8)]
    checkpoints = _IgnoresTheLimit(history)

    await _run_leg(_handler(tasks, runner, checkpoints, window=2), predecessor_seq=7)

    reconstruction = runner.tasks_seen[0]
    carried = [seq for seq in range(8) if f"leg-{seq}" in reconstruction]
    assert carried == [5, 6]  # the two before the newest, and nothing older


@pytest.mark.asyncio
async def test_the_leg_records_its_own_shape_beside_its_spend(tasks: TaskStore) -> None:
    """Spec W1 (T14), research V-4: the profile rides the audit detail the handler already
    writes, so the close-out can ask whether ten steps and 180 seconds are the right bounds
    from what legs actually do. No bound moves in W1; this is the measurement."""
    runner = _Runner()

    context = await _run_leg(_handler(tasks, runner, _Checkpoints()))

    assert context.metered, "a leg that ran must meter"
    detail = context.metered[0]
    assert detail["surface"] == "task_leg"  # the pre-existing fields are untouched
    assert detail["task_id"] == _TASK
    for field in (
        "steps",
        "tool_calls",
        "tool_calls_per_step",
        "distinct_queries",
        "repeats_refused",
        "tokens_total",
        "tokens_per_step_max",
        "wall_clock_ms",
        "box_limit",
        "reads_served_from_ledger",
    ):
        assert field in detail, field
    assert all(isinstance(value, str) for value in detail.values())


# --- who pays for the distillation, through the real handler (D-W1-44) ------


class _Credits:
    """A credits policy that records what the leg was actually charged."""

    def __init__(self) -> None:
        self.captures: list[dict[str, object]] = []

    def require_credits(self, **_kwargs: object) -> None:
        return

    def capture_up_to_idempotent(self, **kwargs: object) -> None:
        self.captures.append(dict(kwargs))


class _TokenSpendingBackend:
    """A backend whose one answer reports the usage a distillation would have spent."""

    def __init__(self, prompt_tokens: int, completion_tokens: int) -> None:
        self._prompt = prompt_tokens
        self._completion = completion_tokens

    @property
    def provider_name(self) -> str:
        return "nvidia"

    @property
    def model_name(self) -> str:
        return "nvidia/nemotron-3-super-120b-a12b"

    @property
    def supports_native_tools(self) -> bool:
        return False

    @property
    def supports_vision(self) -> bool:
        return False

    async def chat(self, messages: object, **_kwargs: object) -> ChatResponse:  # noqa: ARG002
        return ChatResponse(
            content="{}",
            model=self.model_name,
            provider=self.provider_name,
            usage=TokenUsage(
                prompt_tokens=self._prompt,
                completion_tokens=self._completion,
                total_tokens=self._prompt + self._completion,
            ),
            latency_ms=1.0,
        )


class _DistillingWriter:
    """A writer that spends tokens the way the metered distiller does.

    The real distiller's backend is wrapped in a ``UsageCollectingBackend``, which records
    into whatever ``collect_llm_usage`` sink is active. That wrapper is M3's, and is proven
    where it lives; what is unproven, and what this stands in for, is whether the HANDLER
    has a sink open around the write at all.
    """

    def __init__(self, *, prompt_tokens: int, completion_tokens: int) -> None:
        self._prompt = prompt_tokens
        self._completion = completion_tokens

    async def write(
        self, *, task: Task, prior: object, run: object, leg_id: str, seq: int, now: datetime
    ) -> TaskCheckpoint:  # noqa: ARG002
        # Through the REAL wrapper the worker root wires, so what is exercised here is the
        # production recording path and not a hand-written call into the sink.
        backend = UsageCollectingBackend(_TokenSpendingBackend(self._prompt, self._completion))
        await backend.chat([])
        return TaskCheckpoint(
            task_id=task.id,
            leg_id=leg_id,
            checkpoint_seq=seq,
            progress_conclusions=("something was concluded",),
            updated_at=now,
        )


async def _charge_for(
    tasks: TaskStore, engine: Engine, writer: object, task_id: str
) -> list[dict[str, object]]:
    """Run one leg of its OWN task with billing wired, and return what the owner was charged.

    Its own task because a completed leg leaves the task terminal, and a second leg on a
    terminal task is skipped before it can bill: that skip is correct, and it would quietly
    turn this comparison into "nothing versus nothing".
    """
    tasks.create(
        Task(
            id=task_id,
            owner_id=_OWNER,
            persona_id=_PERSONA,
            contract=Contract(goal=_GOAL),
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    tasks.start(_OWNER, task_id, now=_NOW)
    wallet = _Credits()
    handler = TaskLegHandler(
        task_store=tasks,
        checkpoint_store=_Checkpoints(),  # type: ignore[arg-type]
        runner_builder=_RunnerBuilder(_Runner()),
        writer=writer,  # type: ignore[arg-type]
        credits_policy=wallet,  # type: ignore[arg-type]
        rls_engine=engine,
    )
    await handler.handle(
        TaskLegPayload(task_id=task_id, predecessor_seq=None, trigger=_TRIGGER),
        _Context(),  # type: ignore[arg-type]
    )
    return wallet.captures


@pytest.mark.asyncio
async def test_the_distillation_reaches_the_legs_charge(tasks: TaskStore, engine: Engine) -> None:
    """D-W1-44 through the real handler.

    The accumulator being able to take the totals is not the claim. The claim is that the
    handler opens a sink around the write and hands them over, which a mutation removing
    that one line would otherwise pass straight through.

    These legs run a scripted runner that meters nothing, so the distillation is the ONLY
    thing either of them spent. That makes the comparison stark: with it, the leg has a real
    cost to bill; without it, there is nothing to bill and the handler correctly bills
    nothing at all.
    """
    spent = await _charge_for(
        tasks, engine, _DistillingWriter(prompt_tokens=1800, completion_tokens=320), "t-spent"
    )
    silent = await _charge_for(
        tasks, engine, _DistillingWriter(prompt_tokens=0, completion_tokens=0), "t-silent"
    )

    assert len(spent) == 1, "the distillation's real cost has to reach the leg's charge"
    assert float(spent[0]["cost_cents"]) > 0.0  # type: ignore[arg-type]
    assert spent[0]["cost_basis"] == "estimate_static"  # priced, not waved through
    assert silent == [], "a leg that metered nothing bills nothing"


@pytest.mark.asyncio
async def test_the_deterministic_writer_leaves_the_charge_where_it_was(
    tasks: TaskStore, engine: Engine
) -> None:
    """The contrast that matters most, because it is what runs when the flag is off.

    The REAL deterministic writer makes no model call, so nothing reaches the sink, nothing
    folds, and the leg bills exactly what it billed before D-W1-44 existed. A distillation
    charge that appeared here would mean the handler was inventing cost rather than
    recording it.
    """
    charges = await _charge_for(tasks, engine, CompactingCheckpointWriter(), "t-deterministic")

    assert charges == []


@pytest.mark.asyncio
async def test_the_distillation_is_one_charge_not_a_second_row(
    tasks: TaskStore, engine: Engine
) -> None:
    """It rides the leg's own billing key, so a re-delivery still collapses to one deduct."""
    wallet = _Credits()
    handler = TaskLegHandler(
        task_store=tasks,
        checkpoint_store=_Checkpoints(),  # type: ignore[arg-type]
        runner_builder=_RunnerBuilder(_Runner()),
        writer=_DistillingWriter(prompt_tokens=1800, completion_tokens=320),  # type: ignore[arg-type]
        credits_policy=wallet,  # type: ignore[arg-type]
        rls_engine=engine,
    )

    await handler.handle(
        TaskLegPayload(task_id=_TASK, predecessor_seq=None, trigger=_TRIGGER),
        _Context(),  # type: ignore[arg-type]
    )

    assert len(wallet.captures) == 1  # one deduct, not a second row for the distillation
    assert wallet.captures[0]["billing_key"] == f"{_TASK}:leg:0"  # the leg's own key

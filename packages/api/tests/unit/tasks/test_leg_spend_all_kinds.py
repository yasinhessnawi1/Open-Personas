"""The leg ledger meters every spend kind, not only the model (part1 F4 tail).

``SpendKind`` has three members. Until now the leg handler's meter returned MODEL alone
and said why: a sandbox execution charged the owner from inside the tool and told the
enclosing leg nothing, and connector / MCP infra is subsumed by the leg's floor. So the
per-task budget cap, and since R9-176 the per-leg spend bound, saw model spend only. A
task that ran the sandbox hard could pass far beyond the cap the user set, invisibly.

What these drive, through the REAL chain (the real hosted ``code_execution`` tool over a
fake substrate, dispatched from inside a real leg, reporting through the ambient door the
handler binds):

1. a leg whose sandbox tool runs records SANDBOX micros equal to what the tool billed;
2. the per-leg spend probe sees that spend, so a small budget trips BUDGET on sandbox
   spend alone;
3. an interactive run (no leg bound) records nothing anywhere;
4. the owner is billed for the sandbox exactly once, by the tool, and the leg's own
   owner deduct prices the model cost only, so nothing is charged twice;
5. an external (MCP) call inside a leg is written under EXTERNAL with the ruled value.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from persona.billing import credits_charged
from persona.sandbox.result import ExecutionResult, NetworkPolicy, ResourceLimits, SandboxFile
from persona.tasks import (
    SUBSUMED_EXTERNAL_CALL_CENTS,
    Contract,
    ContractBounds,
    LegBox,
    LegBoxLimit,
    ScheduledFire,
    SpendKind,
    Task,
    TaskCheckpoint,
    micros_from_cents,
)
from persona.tools.mcp.adapter import MCPToolAdapter
from persona_api.db.community import create_community_schema, ensure_owner, make_community_engine
from persona_api.db.models import personas as personas_t
from persona_api.sandbox import (
    SandboxRequestContext,
    make_pool_code_execution_tool,
    reset_sandbox_request_context,
    set_sandbox_request_context,
)
from persona_api.sandbox.pool import SandboxPool
from persona_api.tasks import TaskLegPayload, TaskStore
from persona_api.tasks.handler import TaskLegHandler, _LegCost, _run_metered_leg
from persona_runtime.agentic.events import RunEvent
from persona_runtime.agentic.run import CancelToken, Run, RunStatus, StepUsage
from persona_runtime.agentic.step import Step, StepType
from persona_runtime.legs import BasicCheckpointWriter, LegExecutor, LegOutcome
from sqlalchemy import insert

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping
    from pathlib import Path

    from persona.tools.protocol import AsyncTool

_NOW = datetime(2026, 9, 18, 9, 0, tzinfo=UTC)
_TRIGGER = ScheduledFire(schedule_id="sched-1", fire_time=_NOW)

#: The flat per-execution charge the hosted sandbox tool bills (D-12-3), in credits,
#: which M3 accounts at one credit = one US cent. One cent is 100 ledger micros.
_EXEC_CREDITS = 1
_EXEC_MICROS = micros_from_cents(_EXEC_CREDITS)

#: One priced model step, so the tests can tell model money from sandbox money.
_STEP_COST_USD = 0.02
_STEP_CENTS = 2.0
_STEP_MICROS = 200


class _FakeSandbox:
    """The smallest substrate that says every execution succeeded."""

    def __init__(self) -> None:
        self.executions = 0

    async def execute(
        self,
        code: str,  # noqa: ARG002 (part of the substrate contract)
        *,
        language: str = "python",  # noqa: ARG002
        session_id: str | None = None,  # noqa: ARG002
        timeout_s: float = 30.0,  # noqa: ARG002
        limits: ResourceLimits | None = None,  # noqa: ARG002
        network: NetworkPolicy | None = None,  # noqa: ARG002
        input_files: list[SandboxFile] | None = None,  # noqa: ARG002
    ) -> ExecutionResult:
        self.executions += 1
        return ExecutionResult(
            stdout="ok\n", stderr="", exit_status=0, outcome="ok", duration_ms=1.0
        )

    async def create_session(self, session_id: str, *, limits: object, network: object) -> None:  # noqa: ARG002
        return None

    async def destroy_session(self, session_id: str) -> None:  # noqa: ARG002
        return None

    async def aclose(self) -> None:
        return None


@pytest_asyncio.fixture
async def sandbox_tool() -> AsyncIterator[tuple[AsyncTool, MagicMock, _FakeSandbox]]:
    """The REAL hosted ``code_execution`` tool, the way the runtime factory builds it,
    over a fake substrate and a recording credits policy (the billing seam)."""
    fake = _FakeSandbox()
    pool = SandboxPool(sandbox=fake, max_per_user=2, idle_timeout_s=60.0, reap_interval_s=60.0)
    policy = MagicMock()
    policy.deduct.return_value = 99
    tool = make_pool_code_execution_tool(
        pool=pool,
        rls_engine=object(),  # type: ignore[arg-type] (the policy is a recorder)
        credits_policy=policy,
        credit_cost=_EXEC_CREDITS,
    )
    try:
        yield tool, policy, fake
    finally:
        await pool.aclose()


def _mcp_adapter() -> MCPToolAdapter:
    call_tool = AsyncMock(
        return_value=SimpleNamespace(
            content=[SimpleNamespace(text="found", type="text")],
            isError=False,
            structuredContent=None,
        )
    )
    return MCPToolAdapter(
        server_name="crm",
        session=SimpleNamespace(call_tool=call_tool),  # type: ignore[arg-type]
        tool_def=SimpleNamespace(name="lookup", description="", inputSchema={}),  # type: ignore[arg-type]
    )


async def _dispatch_sandbox(tool: AsyncTool) -> None:
    """Dispatch the sandbox tool the way the interactive worker does: with the request
    context bound, so the tool acquires its pool session and bills the owner."""
    token = set_sandbox_request_context(
        SandboxRequestContext(owner_id="user_a", conversation_id="conv_1")
    )
    try:
        result = await tool.execute(code="print('ok')")
    finally:
        reset_sandbox_request_context(token)
    assert result.is_error is False


class _ToolRunningRunner:
    """A leg that, at each step, dispatches its tools and then reports one priced model
    call. The order is what a real run does: tools run inside the step, the usage lands
    at its end, and the box is consulted at the NEXT ``thinking`` event."""

    def __init__(
        self,
        *,
        steps: int,
        sandbox: AsyncTool | None = None,
        execs_per_step: int = 1,
        mcp: MCPToolAdapter | None = None,
        model_cost_usd: float | None = _STEP_COST_USD,
    ) -> None:
        self._steps = steps
        self._sandbox = sandbox
        self._execs_per_step = execs_per_step
        self._mcp = mcp
        self._model_cost_usd = model_cost_usd
        self.steps_run = 0

    async def run(
        self,
        task: str,
        *,
        on_event,  # noqa: ANN001 (part of the runner contract)
        cancel_token: CancelToken,
        on_step_usage=None,  # noqa: ANN001 (part of the runner contract)
    ) -> Run:
        for index in range(self._steps):
            if cancel_token.is_cancelled:
                break
            await on_event(RunEvent.thinking(index))
            self.steps_run += 1
            if self._sandbox is not None:
                for _ in range(self._execs_per_step):
                    await _dispatch_sandbox(self._sandbox)
            if self._mcp is not None:
                await self._mcp.execute(q="acme")
            if on_step_usage is not None and self._model_cost_usd is not None:
                await on_step_usage(
                    StepUsage(
                        step=index,
                        provider="openrouter",
                        model="z-ai/glm-4.6",
                        prompt_tokens=4_000,
                        completion_tokens=500,
                        cost_usd=self._model_cost_usd,
                    )
                )
        return Run(
            persona_id="persona_a",
            task=task,
            status=RunStatus.CANCELLED if cancel_token.is_cancelled else RunStatus.COMPLETED,
            steps=[
                Step(type=StepType.REASONING, content=f"step {i}", tokens=100)
                for i in range(self.steps_run)
            ],
            output="",
            started_at=_NOW,
            finished_at=_NOW,
        )


class _RecordingSink:
    """Accrues the spend into the task exactly as ``CheckpointStore.append`` does."""

    def __init__(self) -> None:
        self.spend: dict[SpendKind, int] = {}

    def append(self, task: Task, checkpoint: TaskCheckpoint, *, spend, now: datetime) -> Task:  # noqa: ANN001
        self.spend = dict(spend)
        advanced = task.advance_checkpoint(checkpoint.checkpoint_seq, now=now)
        for kind, micros in spend.items():
            advanced = advanced.record_spend(kind, micros, now=now)
        return advanced


class _SpyCost(_LegCost):
    """The real accumulator, remembering what came through the reporter door."""

    def __init__(self) -> None:
        super().__init__(None, _NO_USAGE)
        self.reports: list[tuple[SpendKind, float]] = []

    def report(self, kind: SpendKind, cost_cents: float) -> None:
        self.reports.append((kind, cost_cents))
        super().report(kind, cost_cents)


class _NoUsage:
    """A distillation sink that saw no model call."""

    def totals(self) -> SimpleNamespace:
        return SimpleNamespace(
            prompt_tokens=0, completion_tokens=0, provider=None, model=None, cost_usd=None
        )


_NO_USAGE = _NoUsage()


def _task(cap_micros: int | None = None) -> Task:
    return Task(
        id="t1",
        owner_id="user_a",
        persona_id="persona_a",
        contract=Contract(
            goal="reconcile the ledger", bounds=ContractBounds(total_budget_micros=cap_micros)
        ),
        state="active",
        created_at=_NOW,
        updated_at=_NOW,
    )


async def _run_leg(
    runner: _ToolRunningRunner, *, budget_micros: int | None = None
) -> tuple[LegOutcome, _RecordingSink, _SpyCost]:
    """Drive a real leg the way the handler does: the accumulator is the meter, the
    probe and the reporter, and the reporter is bound around the run."""
    sink = _RecordingSink()
    cost = _SpyCost()
    executor = LegExecutor(
        runner=runner, writer=BasicCheckpointWriter(), sink=sink, meter=cost.ledger_spend
    )
    outcome = await _run_metered_leg(
        cost,
        executor,
        task=_task(),
        trigger=_TRIGGER,
        box=LegBox(max_steps=50, wall_clock_seconds=3_600.0, budget_micros=budget_micros),
        now=_NOW,
    )
    return outcome, sink, cost


@pytest.mark.asyncio
async def test_a_leg_that_runs_the_sandbox_records_what_the_tool_billed(
    sandbox_tool: tuple[AsyncTool, MagicMock, _FakeSandbox],
) -> None:
    """The load-bearing one. Three executions, one credit each: the tool billed three
    credits and the ledger's SANDBOX column reads three cents in micros, next to the
    model money in its own column."""
    tool, policy, fake = sandbox_tool
    runner = _ToolRunningRunner(steps=3, sandbox=tool)

    outcome, sink, _ = await _run_leg(runner)

    assert fake.executions == 3
    billed = sum(call.kwargs["amount"] for call in policy.deduct.call_args_list)
    assert billed == 3 * _EXEC_CREDITS
    assert sink.spend[SpendKind.SANDBOX] == micros_from_cents(billed) == 3 * _EXEC_MICROS
    assert sink.spend[SpendKind.MODEL] == 3 * _STEP_MICROS
    assert outcome.task.ledger.sandbox_micros == 3 * _EXEC_MICROS
    assert outcome.task.ledger.model_micros == 3 * _STEP_MICROS


@pytest.mark.asyncio
async def test_the_per_leg_spend_bound_trips_on_sandbox_spend_alone(
    sandbox_tool: tuple[AsyncTool, MagicMock, _FakeSandbox],
) -> None:
    """No model money at all: a leg that only runs the sandbox, two executions a step,
    against a bound of 250 micros. After the first step the probe reads 200 (inside);
    after the second it reads 400 (not), so the leg stops there and names BUDGET. Before
    this the probe read zero forever on such a leg."""
    tool, _, _ = sandbox_tool
    runner = _ToolRunningRunner(steps=6, sandbox=tool, execs_per_step=2, model_cost_usd=None)

    outcome, sink, _ = await _run_leg(runner, budget_micros=250)

    assert outcome.box_limit == LegBoxLimit.BUDGET
    assert runner.steps_run < 6, "the leg must be cut short, not run to completion"
    assert sink.spend[SpendKind.MODEL] == 0
    assert sink.spend[SpendKind.SANDBOX] == runner.steps_run * 2 * _EXEC_MICROS


@pytest.mark.asyncio
async def test_an_interactive_run_records_nothing_and_bills_as_before(
    sandbox_tool: tuple[AsyncTool, MagicMock, _FakeSandbox],
) -> None:
    """Outside a leg there is no ledger to keep. The tool still bills the owner once per
    execution, exactly as it did, and no accumulator anywhere hears about it."""
    tool, policy, _ = sandbox_tool
    bystander = _SpyCost()

    await _dispatch_sandbox(tool)

    assert policy.deduct.call_count == 1
    assert bystander.reports == []
    assert bystander.ledger_spend(MagicMock())[SpendKind.SANDBOX] == 0


@pytest.mark.asyncio
async def test_the_owner_pays_for_the_sandbox_once_and_the_leg_deduct_prices_the_model_only(
    sandbox_tool: tuple[AsyncTool, MagicMock, _FakeSandbox],
) -> None:
    """No double charge. The tool billed the owner through the seam once per execution;
    the leg's own owner deduct, driven through the real ``_bill_leg``, carries the model
    cost and nothing else. The ledger is an accounting of a charge that already happened,
    not a second one."""
    tool, policy, _ = sandbox_tool
    runner = _ToolRunningRunner(steps=2, sandbox=tool)
    _, sink, cost = await _run_leg(runner)
    assert policy.deduct.call_count == 2  # the tool, once per execution
    assert sink.spend[SpendKind.SANDBOX] == 2 * _EXEC_MICROS

    leg_policy = MagicMock()
    handler = TaskLegHandler(
        task_store=object(),  # type: ignore[arg-type]
        checkpoint_store=object(),  # type: ignore[arg-type]
        runner_builder=object(),  # type: ignore[arg-type]
        credits_policy=leg_policy,
        rls_engine=object(),  # type: ignore[arg-type]
    )
    await handler._bill_leg("user_a", "t1", 0, cost)  # noqa: SLF001

    leg_policy.capture_up_to_idempotent.assert_called_once()
    charged = leg_policy.capture_up_to_idempotent.call_args.kwargs
    assert charged["cost_cents"] == pytest.approx(2 * _STEP_CENTS)
    assert charged["amount"] == credits_charged(
        provider_cents=2 * _STEP_CENTS, infra_flat_cents=0.0, markup=1.0, floor=1
    )
    assert policy.deduct.call_count == 2, "the leg deduct must not touch the sandbox seam"


@pytest.mark.asyncio
async def test_an_external_call_inside_a_leg_is_written_at_the_ruled_value() -> None:
    """The EXTERNAL column is written by a real writer. The value is zero because M3 (T7)
    subsumes a call made inside a billed op into that op's floor; the point is that the
    column now carries the ruled figure from the dispatch point rather than a default
    nobody wrote."""
    runner = _ToolRunningRunner(steps=2, mcp=_mcp_adapter())

    outcome, sink, cost = await _run_leg(runner)

    assert cost.reports == [(SpendKind.EXTERNAL, SUBSUMED_EXTERNAL_CALL_CENTS)] * 2
    assert SpendKind.EXTERNAL in sink.spend
    assert sink.spend[SpendKind.EXTERNAL] == micros_from_cents(2 * SUBSUMED_EXTERNAL_CALL_CENTS)
    assert outcome.task.ledger.external_micros == 0


@pytest.mark.asyncio
async def test_the_binding_does_not_outlive_the_leg(
    sandbox_tool: tuple[AsyncTool, MagicMock, _FakeSandbox],
) -> None:
    """After the leg, a dispatch on the same context belongs to nobody's ledger."""
    tool, _, _ = sandbox_tool
    _, _, cost = await _run_leg(_ToolRunningRunner(steps=1, sandbox=tool))
    before = list(cost.reports)

    await _dispatch_sandbox(tool)

    assert cost.reports == before


# --- the whole production path: the handler binds the door, A0 sees each kind ---------


class _CheckpointsRecordingSpend:
    """A checkpoint store stand-in that keeps what the leg asked it to accrue."""

    def __init__(self) -> None:
        self.spend: dict[SpendKind, int] = {}

    def get_latest(self, owner_id: str, task_id: str) -> TaskCheckpoint | None:  # noqa: ARG002
        return None

    def list_recent(self, owner_id: str, task_id: str, *, limit: int) -> list[TaskCheckpoint]:  # noqa: ARG002
        return []

    def append(
        self,
        task: Task,
        checkpoint: TaskCheckpoint,  # noqa: ARG002
        *,
        spend: Mapping[SpendKind, int] | None = None,
        now: datetime,  # noqa: ARG002
    ) -> Task:
        self.spend = dict(spend or {})
        return task


class _Builder:
    def __init__(self, runner: _ToolRunningRunner) -> None:
        self._runner = runner

    def build(
        self,
        task_id: str,  # noqa: ARG002 (the builder port's full signature)
        persona_id: str,  # noqa: ARG002
        box: LegBox,  # noqa: ARG002
        *,
        task: object = None,  # noqa: ARG002
    ) -> _ToolRunningRunner:
        return self._runner


class _Context:
    """The A0 job context, remembering every meter event as ``(kind, micros)``."""

    def __init__(self) -> None:
        self.metered: list[tuple[str, int]] = []

    @property
    def owner_id(self) -> str:
        return "user_a"

    @property
    def job_id(self) -> str:
        return "job-1"

    def meter(self, *, amount_micros: int, kind: str, detail: object = None) -> None:  # noqa: ARG002
        self.metered.append((kind, amount_micros))


@pytest.fixture
def task_store(tmp_path: Path) -> TaskStore:
    engine = make_community_engine(tmp_path / "legs.db")
    create_community_schema(engine)
    ensure_owner(engine, owner_id="user_a", email="a@example.com")
    with engine.begin() as conn:
        conn.execute(insert(personas_t).values(id="persona_a", owner_id="user_a", yaml="name: x"))
    store = TaskStore(engine)
    # Created in its defined state, then started, the way the product does it.
    store.create(
        Task(
            id="t1",
            owner_id="user_a",
            persona_id="persona_a",
            contract=Contract(goal="reconcile the ledger"),
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    store.start("user_a", "t1", now=_NOW)
    return store


@pytest.mark.asyncio
async def test_through_the_real_handler_the_sandbox_reaches_the_ledger_and_a0(
    task_store: TaskStore, sandbox_tool: tuple[AsyncTool, MagicMock, _FakeSandbox]
) -> None:
    """The production call site. ``TaskLegHandler.handle`` binds the reporter around the
    leg, the tool it never met reports through it, the checkpoint append is asked to
    accrue the SANDBOX kind, and A0's forensics record that kind under its own name
    rather than folding it into the model's."""
    tool, policy, _ = sandbox_tool
    checkpoints = _CheckpointsRecordingSpend()
    runner = _ToolRunningRunner(steps=2, sandbox=tool, execs_per_step=2)
    handler = TaskLegHandler(
        task_store=task_store,
        checkpoint_store=checkpoints,  # type: ignore[arg-type] (duck-typed store)
        runner_builder=_Builder(runner),
        writer=BasicCheckpointWriter(),
    )
    context = _Context()

    await handler.handle(
        TaskLegPayload(task_id="t1", predecessor_seq=None, trigger=_TRIGGER),
        context,  # type: ignore[arg-type] (duck-typed JobContext)
    )

    assert policy.deduct.call_count == 4, "the tool billed once per execution, as before"
    assert checkpoints.spend[SpendKind.SANDBOX] == 4 * _EXEC_MICROS
    assert checkpoints.spend[SpendKind.MODEL] == 2 * _STEP_MICROS
    assert ("sandbox", 4 * _EXEC_MICROS) in context.metered
    assert ("model", 2 * _STEP_MICROS) in context.metered
    assert not any(kind == "external" for kind, _ in context.metered), "nothing external ran"

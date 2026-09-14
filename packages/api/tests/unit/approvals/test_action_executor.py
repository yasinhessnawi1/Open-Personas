"""The ungated action executor: verbatim replay + the never-crash guardrail (Spec A6, T-seam).

Drives :class:`ToolboxActionExecutor` against a REAL :class:`Toolbox` (the real allow-list +
dispatch + tool.execute path) with a probe tool as the observable target — so the load-bearing
property is proven on real machinery, not a fake:

- **Verbatim replay** — the EXACT recorded ``(tool_name, arguments)`` reaches the tool unchanged
  (the model never re-derives; the approval is the authorization).
- **The guardrail** — a build failure, a dispatch exception, and a tool-level error each return an
  ``"Execution failed: …"`` summary; the executor NEVER raises (an approval reply must not 500).
- **The produced files travel** (R9-162) — a tool that persisted bytes surfaces them on
  :attr:`persona.schema.tools.ToolResult.artifacts`, and the executor carries them out so the
  resolution checkpoint can record the file rather than only a sentence about it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from persona.schema.tools import ToolResult
from persona.tools.protocol import tool
from persona.tools.toolbox import Toolbox
from persona_api.approvals.action_executor import ToolboxActionExecutor

if TYPE_CHECKING:
    from persona.tools.protocol import AsyncTool

pytestmark = pytest.mark.asyncio

_ARGS = {"to": "bob@example.com", "subject": "the appeal", "amount": 1500}


def _probe_tool(sink: list[dict]) -> AsyncTool:
    @tool(name="send_email", description="probe: record the exact args it was dispatched with")
    async def _send(to: str, subject: str, amount: int) -> ToolResult:
        sink.append({"to": to, "subject": subject, "amount": amount})
        return ToolResult(tool_name="send_email", content=f"sent to {to}")

    return _send


def _executor_over(*tools: AsyncTool, allow: list[str]) -> ToolboxActionExecutor:
    toolbox = Toolbox(list(tools), allow_list=allow)

    async def _build() -> Toolbox:
        return toolbox

    return ToolboxActionExecutor(_build)


async def test_executor_replays_the_exact_recorded_call_verbatim() -> None:
    sink: list[dict] = []
    executor = _executor_over(_probe_tool(sink), allow=["send_email"])

    result = await executor.execute("send_email", _ARGS)

    assert sink == [_ARGS]  # the EXACT args reached the tool — nothing re-derived
    assert result.summary == "sent to bob@example.com"
    assert result.artifacts == ()  # a text-only tool produces no files


async def test_executor_build_failure_returns_failed_summary_never_raises() -> None:
    async def _bad_build() -> Toolbox:
        msg = "toolbox build blew up"
        raise RuntimeError(msg)

    executor = ToolboxActionExecutor(_bad_build)
    result = await executor.execute("send_email", _ARGS)  # must not raise
    assert result.summary.startswith("Execution failed:")
    assert "toolbox build blew up" in result.summary
    assert result.artifacts == ()


async def test_executor_dispatch_exception_returns_failed_summary_never_raises() -> None:
    @tool(name="boom", description="always raises")
    async def _boom() -> ToolResult:
        msg = "kaboom"
        raise RuntimeError(msg)

    executor = _executor_over(_boom, allow=["boom"])
    result = await executor.execute("boom", {})  # dispatch raises → caught, no propagation
    assert result.summary.startswith("Execution failed:")


async def test_executor_tool_error_result_is_a_failed_summary() -> None:
    @tool(name="soft_fail", description="returns an error result")
    async def _soft() -> ToolResult:
        return ToolResult(tool_name="soft_fail", content="quota exceeded", is_error=True)

    executor = _executor_over(_soft, allow=["soft_fail"])
    result = await executor.execute("soft_fail", {})
    assert result.summary == "Execution failed: quota exceeded"


async def test_a_tool_that_persisted_a_file_carries_its_artifacts_out() -> None:
    """R9-162: the Spec-28 channel reaches the resolver, so the pointer list learns the file."""
    from persona.schema.tools import PersistedArtifact

    produced = PersistedArtifact(
        workspace_path="uploads/appeal.pdf", mime_type="application/pdf", size_bytes=2048
    )

    @tool(name="make_pdf", description="writes a file into the workspace")
    async def _make() -> ToolResult:
        return ToolResult(
            tool_name="make_pdf", content="wrote uploads/appeal.pdf", artifacts=(produced,)
        )

    executor = _executor_over(_make, allow=["make_pdf"])
    result = await executor.execute("make_pdf", {})
    assert result.artifacts == (produced,)


async def test_a_failed_tool_carries_no_artifacts() -> None:
    """A half-written file must not read to the next leg as the work having landed."""
    from persona.schema.tools import PersistedArtifact

    @tool(name="half_write", description="errors after writing part of a file")
    async def _half() -> ToolResult:
        return ToolResult(
            tool_name="half_write",
            content="disk full",
            is_error=True,
            artifacts=(
                PersistedArtifact(
                    workspace_path="uploads/partial.pdf",
                    mime_type="application/pdf",
                    size_bytes=1,
                ),
            ),
        )

    executor = _executor_over(_half, allow=["half_write"])
    result = await executor.execute("half_write", {})
    assert result.summary.startswith("Execution failed:")
    assert result.artifacts == ()

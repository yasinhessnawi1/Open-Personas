"""The ambient leg spend reporter, and the MCP adapter writing through it.

A leg's ledger has three columns. Until now only ``model`` was ever written: the sandbox
tool billed the owner and told the leg nothing, and the MCP adapter had nowhere to report
at all. What these pin is the door itself (bound → delivered, unbound → dropped, reset →
restored) and the one core writer behind it: an MCP call inside a leg records the EXTERNAL
kind with the value M3 rules for it, and a call that never reached the server records
nothing.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from persona.tasks import (
    SUBSUMED_EXTERNAL_CALL_CENTS,
    LegSpendReporter,
    SpendKind,
    bind_leg_spend_reporter,
    report_leg_spend,
    reset_leg_spend_reporter,
)
from persona.tools.mcp.adapter import MCPToolAdapter


class _Recorder:
    """A reporter that remembers every report, in order."""

    def __init__(self) -> None:
        self.reports: list[tuple[SpendKind, float]] = []

    def report(self, kind: SpendKind, cost_cents: float) -> None:
        self.reports.append((kind, cost_cents))


def _adapter(*, call_tool: AsyncMock) -> MCPToolAdapter:
    return MCPToolAdapter(
        server_name="crm",
        session=SimpleNamespace(call_tool=call_tool),  # type: ignore[arg-type]
        tool_def=SimpleNamespace(
            name="lookup", description="Look a record up.", inputSchema={"type": "object"}
        ),  # type: ignore[arg-type]
    )


def _mcp_result(text: str = "ok") -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(text=text, type="text")], isError=False, structuredContent=None
    )


def test_a_recorder_satisfies_the_reporter_protocol() -> None:
    assert isinstance(_Recorder(), LegSpendReporter)


def test_outside_a_leg_a_report_is_dropped_and_says_so() -> None:
    """Interactive chat, a run, the CLI: nothing is bound, so nothing is accounted."""
    assert report_leg_spend(SpendKind.SANDBOX, 1.0) is False


def test_inside_a_leg_a_report_reaches_the_bound_reporter() -> None:
    recorder = _Recorder()
    token = bind_leg_spend_reporter(recorder)
    try:
        assert report_leg_spend(SpendKind.SANDBOX, 1.0) is True
        assert report_leg_spend(SpendKind.EXTERNAL, 0.0) is True
    finally:
        reset_leg_spend_reporter(token)

    assert recorder.reports == [(SpendKind.SANDBOX, 1.0), (SpendKind.EXTERNAL, 0.0)]


def test_reset_restores_the_prior_binding() -> None:
    """A leg's binding must not outlive the leg: the next dispatch on this context is
    someone else's, and their spend must not land on a finished task."""
    recorder = _Recorder()
    token = bind_leg_spend_reporter(recorder)
    reset_leg_spend_reporter(token)

    assert report_leg_spend(SpendKind.SANDBOX, 1.0) is False
    assert recorder.reports == []


@pytest.mark.asyncio
async def test_an_mcp_call_inside_a_leg_records_external_at_the_ruled_value() -> None:
    """The writer runs. The value is zero because M3 (T7) subsumes a call made inside a
    billed op into that op's floor, and recording the ruled zero from a real writer is
    what turns "never written" into "written with the ruled value"."""
    recorder = _Recorder()
    adapter = _adapter(call_tool=AsyncMock(return_value=_mcp_result()))

    token = bind_leg_spend_reporter(recorder)
    try:
        result = await adapter.execute(q="acme")
    finally:
        reset_leg_spend_reporter(token)

    assert result.is_error is False
    assert recorder.reports == [(SpendKind.EXTERNAL, SUBSUMED_EXTERNAL_CALL_CENTS)]


@pytest.mark.asyncio
async def test_an_mcp_call_that_never_reached_the_server_records_nothing() -> None:
    """A transport failure is a graceful error result, not a call that cost anything."""
    recorder = _Recorder()
    adapter = _adapter(call_tool=AsyncMock(side_effect=ConnectionError("gone")))

    token = bind_leg_spend_reporter(recorder)
    try:
        result = await adapter.execute(q="acme")
    finally:
        reset_leg_spend_reporter(token)

    assert result.is_error is True
    assert recorder.reports == []


@pytest.mark.asyncio
async def test_an_mcp_call_outside_a_leg_is_unchanged() -> None:
    """No leg listening: the adapter dispatches exactly as before and drops the report."""
    adapter = _adapter(call_tool=AsyncMock(return_value=_mcp_result("found")))

    result = await adapter.execute(q="acme")

    assert result.is_error is False
    assert result.content == "found"

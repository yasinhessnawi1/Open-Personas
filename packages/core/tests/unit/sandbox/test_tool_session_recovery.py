"""Unit tests for the D-25-4 session auto-recovery wrapper (Spec 25 T09).

Acceptance criterion 3: when a stateful sandbox session has vanished
underneath the tool (substrate reaped it; pod restart; idle-timeout race),
``make_code_execution_tool`` recreates the session ONCE and retries the
execution EXACTLY ONCE. A second ``no_session`` (or any other failure) must
NOT trigger a third attempt — masking a persistent session failure with
extended retries hides real issues (kickoff "don't auto-recover more than
once" discipline). The per-turn telemetry flag
``sandbox_session_recreated`` lands in the :class:`ToolResult` metadata so the
runtime turn loop can mirror it into the additive
``TurnLog.sandbox_session_recreated`` field (wired in Cluster B/D).

These tests use a purpose-built sequencing fake (the shared
:class:`tests._sandbox_fakes.FakeSandbox` returns the same result/side-effect
on every call and so cannot express "fail once, then succeed").
"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003 — runtime use in copy_produced_file_to

import pytest
from persona.sandbox import (
    CodeSandboxError,
    ExecutionResult,
    NetworkPolicy,
    ResourceLimits,
    SandboxFile,
    SandboxUnavailableError,
    make_code_execution_tool,
)


def _no_session_error(session_id: str) -> CodeSandboxError:
    """Mirror the exact error local_docker.py:1066 raises on a vanished session."""
    return CodeSandboxError(
        f"session {session_id!r} does not exist; call create_session() first",
        context={"reason": "no_session", "session_id": session_id},
    )


class SequencingSandbox:
    """Protocol-conforming fake whose :meth:`execute` walks a scripted list.

    Each entry in ``execute_script`` is either a :class:`BaseException` to
    raise or an :class:`ExecutionResult` to return, consumed in order. Tracks
    ``execute_calls`` and ``create_session_calls`` so tests can assert the
    recovery sequence (execute → create_session → execute) precisely.
    """

    def __init__(self, execute_script: list[BaseException | ExecutionResult]) -> None:
        self._script = list(execute_script)
        self._idx = 0
        self.execute_calls: list[dict[str, object]] = []
        self.create_session_calls: list[dict[str, object]] = []

    async def execute(
        self,
        code: str,
        *,
        language: str = "python",  # noqa: ARG002 — Protocol contract; fake ignores
        session_id: str | None = None,
        timeout_s: float = 30.0,
        limits: ResourceLimits | None = None,
        network: NetworkPolicy | None = None,
        input_files: list[SandboxFile] | None = None,
    ) -> ExecutionResult:
        self.execute_calls.append(
            {
                "code": code,
                "session_id": session_id,
                "timeout_s": timeout_s,
                "limits": limits,
                "network": network,
                "input_files": input_files,
            }
        )
        if self._idx >= len(self._script):  # pragma: no cover — guards over-dispatch
            raise AssertionError("execute called more times than the script provides")
        step = self._script[self._idx]
        self._idx += 1
        if isinstance(step, BaseException):
            raise step
        return step

    async def create_session(
        self,
        session_id: str,
        *,
        limits: ResourceLimits,
        network: NetworkPolicy,
    ) -> None:
        self.create_session_calls.append(
            {"session_id": session_id, "limits": limits, "network": network}
        )

    async def destroy_session(self, session_id: str) -> None:  # noqa: ARG002 — Protocol  # pragma: no cover
        return None

    async def aclose(self) -> None:  # pragma: no cover
        return None

    async def copy_produced_file_to(
        self,
        session_id: str,  # noqa: ARG002 — Protocol contract
        ref: str,  # noqa: ARG002 — Protocol contract
        target_path: Path,  # noqa: ARG002 — Protocol contract
        *,
        root: Path | None = None,  # noqa: ARG002 - Protocol contract
    ) -> None:  # pragma: no cover
        return None

    async def read_produced_file_bytes(
        self,
        session_id: str,  # noqa: ARG002 — Protocol contract
        ref: str,  # noqa: ARG002 — Protocol contract
    ) -> bytes:  # pragma: no cover
        return b""


_OK = ExecutionResult(stdout="ok\n", stderr="", exit_status=0, outcome="ok", duration_ms=2.0)


class TestSessionAutoRecovery:
    @pytest.mark.asyncio
    async def test_no_session_triggers_recreate_then_retry_success(self) -> None:
        """D-25-4: first execute raises ``no_session`` → create_session called
        once → execute retried once → success surfaces as a normal result."""
        sandbox = SequencingSandbox([_no_session_error("u:c"), _OK])
        tool = make_code_execution_tool(
            sandbox,
            session_id_provider=lambda: "u:c",
        )
        result = await tool.execute(code="print('ok')")

        assert result.is_error is False
        assert "ok\n" in result.content
        # Recreated exactly once, retried exactly once (2 execute calls total).
        assert len(sandbox.create_session_calls) == 1
        assert sandbox.create_session_calls[0]["session_id"] == "u:c"
        assert len(sandbox.execute_calls) == 2

    @pytest.mark.asyncio
    async def test_recovery_passes_factory_bound_limits_and_network(self) -> None:
        """The recreate uses the SAME factory-bound limits + network policy as
        the dispatch — never anything the model supplied (D-12-4)."""
        policy = NetworkPolicy(enabled=True, allowed_hosts=("example.com",))
        limits = ResourceLimits(memory_mb=128)
        sandbox = SequencingSandbox([_no_session_error("u:c"), _OK])
        tool = make_code_execution_tool(
            sandbox,
            network_policy=policy,
            resource_limits=limits,
            session_id_provider=lambda: "u:c",
        )
        await tool.execute(code="pass")

        assert sandbox.create_session_calls[0]["limits"] is limits
        assert sandbox.create_session_calls[0]["network"] is policy

    @pytest.mark.asyncio
    async def test_telemetry_flag_set_on_successful_recovery(self) -> None:
        """D-25-4 telemetry: ``sandbox_session_recreated`` is set in the
        ToolResult metadata when a recovery happened this turn."""
        sandbox = SequencingSandbox([_no_session_error("u:c"), _OK])
        tool = make_code_execution_tool(sandbox, session_id_provider=lambda: "u:c")
        result = await tool.execute(code="pass")
        assert result.metadata["sandbox_session_recreated"] == "True"

    @pytest.mark.asyncio
    async def test_telemetry_flag_false_when_no_recovery_needed(self) -> None:
        """The flag is present and ``False`` on a normal, first-try success —
        so the runtime turn loop reads a stable key every turn."""
        sandbox = SequencingSandbox([_OK])
        tool = make_code_execution_tool(sandbox, session_id_provider=lambda: "u:c")
        result = await tool.execute(code="pass")
        assert result.metadata["sandbox_session_recreated"] == "False"
        assert len(sandbox.create_session_calls) == 0
        assert len(sandbox.execute_calls) == 1

    @pytest.mark.asyncio
    async def test_second_no_session_does_not_trigger_third_attempt(self) -> None:
        """D-25-4: ONLY ONE recovery attempt. A retry that ALSO raises
        ``no_session`` must NOT recreate again — it falls through to the
        structured fail-loud error path. Asserts: 1 create_session, 2 executes
        (NOT a third), and a structured error result carrying the flag."""
        sandbox = SequencingSandbox([_no_session_error("u:c"), _no_session_error("u:c")])
        tool = make_code_execution_tool(sandbox, session_id_provider=lambda: "u:c")
        result = await tool.execute(code="pass")

        assert result.is_error is True
        assert "CodeSandboxError" in result.content
        assert len(sandbox.create_session_calls) == 1  # recreated once only
        assert len(sandbox.execute_calls) == 2  # NO third execute
        # Recovery WAS attempted this turn even though it ultimately failed.
        assert result.metadata["sandbox_session_recreated"] == "True"
        assert result.data is not None
        assert result.data["error_type"] == "CodeSandboxError"

    @pytest.mark.asyncio
    async def test_create_session_failure_falls_through_to_error(self) -> None:
        """If ``create_session`` itself fails, there is no second execute — the
        failure surfaces as a structured error (operator-actionable), and the
        recovery is NOT retried."""

        class _FailingRecreateSandbox(SequencingSandbox):
            async def create_session(
                self,
                session_id: str,
                *,
                limits: ResourceLimits,
                network: NetworkPolicy,
            ) -> None:
                self.create_session_calls.append(
                    {"session_id": session_id, "limits": limits, "network": network}
                )
                raise SandboxUnavailableError(
                    "substrate unreachable while recreating session",
                    context={"session_id": session_id},
                )

        sandbox = _FailingRecreateSandbox([_no_session_error("u:c"), _OK])
        tool = make_code_execution_tool(sandbox, session_id_provider=lambda: "u:c")
        result = await tool.execute(code="pass")

        assert result.is_error is True
        assert "SandboxUnavailableError" in result.content
        assert len(sandbox.create_session_calls) == 1
        # Only the first execute ran; the retry never happened because the
        # recreate raised first.
        assert len(sandbox.execute_calls) == 1
        assert result.metadata["sandbox_session_recreated"] == "False"

    @pytest.mark.asyncio
    async def test_no_session_on_oneshot_does_not_recreate(self) -> None:
        """``session_id is None`` (stateless one-shot) cannot have a session to
        recreate — a ``no_session`` error (pathological for a one-shot) falls
        straight through to the structured-error path without a recreate."""
        sandbox = SequencingSandbox([_no_session_error("<none>")])
        tool = make_code_execution_tool(sandbox)  # default provider → None
        result = await tool.execute(code="pass")

        assert result.is_error is True
        assert len(sandbox.create_session_calls) == 0
        assert len(sandbox.execute_calls) == 1
        assert result.metadata["sandbox_session_recreated"] == "False"

    @pytest.mark.asyncio
    async def test_non_no_session_error_does_not_recreate(self) -> None:
        """A SandboxError whose ``reason`` is NOT ``no_session`` is a real
        failure — it must NOT trigger the recovery path (don't mask real
        failures). Falls straight through to the structured error."""
        sandbox = SequencingSandbox(
            [
                SandboxUnavailableError(
                    "Docker daemon unreachable",
                    context={"reason": "daemon_not_running"},
                )
            ]
        )
        tool = make_code_execution_tool(sandbox, session_id_provider=lambda: "u:c")
        result = await tool.execute(code="pass")

        assert result.is_error is True
        assert "SandboxUnavailableError" in result.content
        assert len(sandbox.create_session_calls) == 0
        assert len(sandbox.execute_calls) == 1
        assert result.metadata["sandbox_session_recreated"] == "False"

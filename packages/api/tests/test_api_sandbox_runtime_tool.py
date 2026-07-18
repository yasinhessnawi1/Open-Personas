"""Unit tests for ``make_pool_code_execution_tool`` + ``make_code_execution_tool`` hooks (T10).

Covers:

- The new ``pre_execute_hook`` (T03 widening): awaited before substrate execute;
  ``SandboxError`` raised by the hook flows through the existing catch-and-convert
  path → structured ``ToolResult(is_error=True)``.
- The new ``on_execute_success`` hook: fires on outcome=="ok"; NOT fired on
  outcome="error" / "timeout" / "oom" / "killed" (mirrors D-08-6 "failed turn
  deducts nothing", applied per-execution per D-12-3).
- The api-side wrapper ``make_pool_code_execution_tool`` reads the contextvar
  for ``(owner_id, conversation_id)``; without context, no pool acquire happens
  and no credits deduct (CLI / one-shot path).
- Credits hook composition: ``credits_service.deduct`` is called with
  ``user_id=owner_id, amount=1, reason="sandbox:infra_flat"`` (Spec M3 T7 — the
  standalone infra-flat surface: zero provider cost, ``cost_cents=0.0``,
  ``cost_basis="infra_flat"``) after a successful execute. Hook failure is logged
  but doesn't break the tool result.
"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003 — runtime use in copy_produced_file_to tests
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from persona.sandbox.errors import (
    ProducedFileSizeError,
    SandboxQuotaExceededError,
    SandboxUnavailableError,
)
from persona.sandbox.result import ExecutionResult, NetworkPolicy, ResourceLimits, SandboxFile
from persona.sandbox.tool import make_code_execution_tool
from persona_api.sandbox import (
    SandboxRequestContext,
    make_pool_code_execution_tool,
    reset_sandbox_request_context,
    set_sandbox_request_context,
)
from persona_api.sandbox.pool import SandboxPool

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


# ----------------------------------------------------- shared substrate fake


class _FakeSandbox:
    """Minimal CodeSandbox satisfying the Protocol."""

    def __init__(
        self,
        *,
        outcome: str = "ok",
        execute_raises: BaseException | None = None,
        produced_files: tuple[SandboxFile, ...] = (),
        copy_raises: BaseException | None = None,
    ) -> None:
        self.outcome = outcome
        self.execute_raises = execute_raises
        self.produced_files = produced_files
        self.copy_raises = copy_raises
        self.execute_calls: list[dict[str, object]] = []
        self.created: list[str] = []
        self.destroyed: list[str] = []
        self.aclose_calls: int = 0
        # D-12-X-read-produced-file Protocol contract additions:
        self.copy_calls: list[tuple[str, str, Path]] = []
        self.read_calls: list[tuple[str, str]] = []

    async def execute(
        self,
        code: str,
        *,
        language: str = "python",  # noqa: ARG002 — Protocol contract
        session_id: str | None = None,
        timeout_s: float = 30.0,  # noqa: ARG002 — Protocol contract
        limits: ResourceLimits | None = None,  # noqa: ARG002 — Protocol contract
        network: NetworkPolicy | None = None,  # noqa: ARG002 — Protocol contract
        input_files: list[SandboxFile] | None = None,  # noqa: ARG002 — Protocol contract
    ) -> ExecutionResult:
        self.execute_calls.append(
            {"code": code, "session_id": session_id, "input_files": input_files or []}
        )
        if self.execute_raises is not None:
            raise self.execute_raises
        return ExecutionResult(
            stdout="hello\n",
            stderr="",
            exit_status=0,
            outcome=self.outcome,  # type: ignore[arg-type]
            duration_ms=12.3,
            produced_files=self.produced_files,
        )

    async def create_session(
        self,
        session_id: str,
        *,
        limits: ResourceLimits,  # noqa: ARG002 — Protocol contract
        network: NetworkPolicy,  # noqa: ARG002 — Protocol contract
    ) -> None:
        self.created.append(session_id)

    async def destroy_session(self, session_id: str) -> None:
        self.destroyed.append(session_id)

    async def aclose(self) -> None:
        self.aclose_calls += 1

    async def copy_produced_file_to(self, session_id: str, ref: str, target_path: Path) -> None:
        self.copy_calls.append((session_id, ref, target_path))
        if self.copy_raises is not None:
            raise self.copy_raises

    async def read_produced_file_bytes(self, session_id: str, ref: str) -> bytes:
        self.read_calls.append((session_id, ref))
        return b"fake-bytes"


@pytest_asyncio.fixture
async def pool_with_fake() -> AsyncIterator[tuple[SandboxPool, _FakeSandbox]]:
    """A pool wrapping the fake substrate; aclose on test exit."""
    fake = _FakeSandbox()
    pool = SandboxPool(sandbox=fake, max_per_user=2, idle_timeout_s=60.0, reap_interval_s=60.0)
    try:
        yield pool, fake
    finally:
        await pool.aclose()


# ============================================================ make_code_execution_tool hooks


@pytest.mark.asyncio
async def test_pre_execute_hook_awaited_before_substrate_execute() -> None:
    """The hook fires BEFORE substrate.execute (load-bearing for pool acquire)."""
    fake = _FakeSandbox()
    call_order: list[str] = []

    async def _hook() -> None:
        call_order.append("hook")

    # Wrap fake.execute to record ordering relative to hook.
    original = fake.execute

    async def _tracked_execute(*a: object, **k: object) -> ExecutionResult:
        call_order.append("execute")
        return await original(*a, **k)  # type: ignore[arg-type]

    fake.execute = _tracked_execute  # type: ignore[method-assign]

    tool = make_code_execution_tool(fake, pre_execute_hook=_hook)
    result = await tool.execute(code="print('hi')")
    assert not result.is_error
    assert call_order == ["hook", "execute"]


@pytest.mark.asyncio
async def test_pre_execute_hook_raising_sandbox_error_becomes_tool_error() -> None:
    """Pool quota / unavailability from the hook surfaces as a structured failure."""
    fake = _FakeSandbox()

    async def _hook_raises_quota() -> None:
        raise SandboxQuotaExceededError(
            "user already at cap",
            context={"user_id": "alice", "current_count": "2", "cap": "2"},
        )

    tool = make_code_execution_tool(fake, pre_execute_hook=_hook_raises_quota)
    result = await tool.execute(code="print('hi')")
    assert result.is_error
    assert "SandboxQuotaExceededError" in result.content
    # Substrate was NOT called — quota check happens before execute.
    assert fake.execute_calls == []


@pytest.mark.asyncio
async def test_on_execute_success_called_only_on_outcome_ok() -> None:
    """Hook fires on outcome=='ok'; not on error/timeout/oom/killed (D-12-3 mirror of D-08-6)."""
    hook_calls: list[ExecutionResult] = []

    async def _hook(r: ExecutionResult) -> None:
        hook_calls.append(r)

    # outcome=ok → hook fires
    fake_ok = _FakeSandbox(outcome="ok")
    tool_ok = make_code_execution_tool(fake_ok, on_execute_success=_hook)
    await tool_ok.execute(code="print('ok')")
    assert len(hook_calls) == 1

    # outcome=error → hook does NOT fire
    hook_calls.clear()
    fake_err = _FakeSandbox(outcome="error")
    tool_err = make_code_execution_tool(fake_err, on_execute_success=_hook)
    await tool_err.execute(code="raise RuntimeError('boom')")
    assert hook_calls == []


@pytest.mark.asyncio
async def test_on_execute_success_failure_logged_but_does_not_break_tool_result() -> None:
    """Credits-write outage must not corrupt the structured tool result."""
    fake = _FakeSandbox(outcome="ok")

    async def _hook_raises() -> None:
        raise RuntimeError("credits db unreachable")

    async def _hook(_r: ExecutionResult) -> None:
        await _hook_raises()

    tool = make_code_execution_tool(fake, on_execute_success=_hook)
    result = await tool.execute(code="print('ok')")
    # Tool result is still success — model sees the successful execution.
    assert not result.is_error
    assert "hello" in result.content


# =================================================== make_pool_code_execution_tool (api wrapper)


@pytest.mark.asyncio
async def test_pool_tool_without_request_context_skips_pool_acquire_and_credits(
    pool_with_fake: tuple[SandboxPool, _FakeSandbox],
) -> None:
    """CLI / one-shot path: no contextvar → no pool acquire, no credits."""
    pool, fake = pool_with_fake
    # Stub the engine — we won't actually touch the DB because no context is bound.
    mock_policy = MagicMock()
    tool = make_pool_code_execution_tool(pool=pool, rls_engine=object(), credits_policy=mock_policy)  # type: ignore[arg-type]
    # No context set → contextvar returns None → no acquire, no deduct.
    result = await tool.execute(code="print('hi')")
    assert not result.is_error
    # Pool didn't acquire (no session created in the fake substrate via pool).
    assert pool._user_counts == {}  # noqa: SLF001 — verify internal invariant
    # Credits never deducted.
    mock_policy.deduct.assert_not_called()


@pytest.mark.asyncio
async def test_pool_tool_with_context_acquires_pool_and_deducts_credits(
    pool_with_fake: tuple[SandboxPool, _FakeSandbox],
) -> None:
    """End-to-end: context bound → pool acquire fires + credits deducted on ok."""
    pool, fake = pool_with_fake
    fake_engine = object()  # never touched because the credits policy is mocked
    mock_policy = MagicMock()
    mock_policy.deduct.return_value = 99  # arbitrary post-deduction balance
    token = set_sandbox_request_context(
        SandboxRequestContext(owner_id="alice", conversation_id="c1")
    )
    try:
        tool = make_pool_code_execution_tool(
            pool=pool,
            rls_engine=fake_engine,  # type: ignore[arg-type]
            credits_policy=mock_policy,
        )
        result = await tool.execute(code="print('hi')")
        assert not result.is_error
        # Pool acquired: session_id "alice:c1" in pool.
        assert "alice:c1" in pool._sessions  # noqa: SLF001 — verify integration
        # Substrate execute called with the right session_id.
        assert fake.execute_calls == [
            {"code": "print('hi')", "session_id": "alice:c1", "input_files": []}
        ]
        # Credits deducted exactly once with the M3 infra-flat shape (Spec M3, T7):
        # sandbox is a standalone infra-flat surface — ~1-credit charge, zero provider
        # cost, basis + reason reclassified into the M3 schema.
        mock_policy.deduct.assert_called_once()
        kwargs = mock_policy.deduct.call_args.kwargs
        assert kwargs["user_id"] == "alice"
        assert kwargs["amount"] == 1
        assert kwargs["reason"] == "sandbox:infra_flat"
        assert kwargs["cost_cents"] == 0.0
        assert kwargs["cost_basis"] == "infra_flat"
    finally:
        reset_sandbox_request_context(token)


@pytest.mark.asyncio
async def test_pool_tool_does_not_deduct_credits_on_substrate_error(
    pool_with_fake: tuple[SandboxPool, _FakeSandbox],
) -> None:
    """Failed execute → outcome!=ok → credits NOT deducted (D-08-6 mirror)."""
    pool, fake = pool_with_fake
    fake.outcome = "error"
    token = set_sandbox_request_context(
        SandboxRequestContext(owner_id="alice", conversation_id="c1")
    )
    mock_policy = MagicMock()
    try:
        tool = make_pool_code_execution_tool(
            pool=pool,
            rls_engine=object(),  # type: ignore[arg-type]
            credits_policy=mock_policy,
        )
        result = await tool.execute(code="boom")
        assert result.is_error
        mock_policy.deduct.assert_not_called()
    finally:
        reset_sandbox_request_context(token)


@pytest.mark.asyncio
async def test_pool_tool_quota_rejection_surfaces_as_tool_error(
    pool_with_fake: tuple[SandboxPool, _FakeSandbox],
) -> None:
    """When pool.acquire raises SandboxQuotaExceededError, tool returns is_error=True."""
    pool, fake = pool_with_fake
    # Fill the cap for alice: 2 sessions.
    h1_token = set_sandbox_request_context(
        SandboxRequestContext(owner_id="alice", conversation_id="c1")
    )
    try:
        await pool.acquire(user_id="alice", conversation_id="c1")
        await pool.acquire(user_id="alice", conversation_id="c2")
    finally:
        reset_sandbox_request_context(h1_token)

    # Now bind a context for a NEW conversation that should trip the cap.
    token = set_sandbox_request_context(
        SandboxRequestContext(owner_id="alice", conversation_id="c3")
    )
    mock_policy = MagicMock()
    try:
        tool = make_pool_code_execution_tool(
            pool=pool,
            rls_engine=object(),  # type: ignore[arg-type]
            credits_policy=mock_policy,
        )
        result = await tool.execute(code="print('hi')")
        assert result.is_error
        assert "SandboxQuotaExceededError" in result.content
        # Substrate never called for the rejected acquire.
        assert all(c["session_id"] != "alice:c3" for c in fake.execute_calls)
        # No credits charged for a quota-rejected attempt.
        mock_policy.deduct.assert_not_called()
    finally:
        reset_sandbox_request_context(token)


@pytest.mark.asyncio
async def test_pool_tool_substrate_unavailable_surfaces_as_tool_error(
    pool_with_fake: tuple[SandboxPool, _FakeSandbox],
) -> None:
    """SandboxUnavailableError from execute path → structured tool error, no credits."""
    pool, fake = pool_with_fake
    fake.execute_raises = SandboxUnavailableError(
        "E2B down", context={"reason": "e2b_create_failed"}
    )
    token = set_sandbox_request_context(
        SandboxRequestContext(owner_id="alice", conversation_id="c1")
    )
    mock_policy = MagicMock()
    try:
        tool = make_pool_code_execution_tool(
            pool=pool,
            rls_engine=object(),  # type: ignore[arg-type]
            credits_policy=mock_policy,
        )
        result = await tool.execute(code="print('hi')")
        assert result.is_error
        assert "SandboxUnavailableError" in result.content
        mock_policy.deduct.assert_not_called()
    finally:
        reset_sandbox_request_context(token)


# =========================================================== contextvar invariants


def test_sandbox_request_context_session_id_shape() -> None:
    """Spec 12 kickoff trip-up #6: session_id is `{owner_id}:{conversation_id}`."""
    ctx = SandboxRequestContext(owner_id="user-42", conversation_id="conv-7")
    assert ctx.session_id == "user-42:conv-7"


def test_sandbox_request_context_is_frozen() -> None:
    """SandboxRequestContext is frozen — mutation is a programming error."""
    ctx = SandboxRequestContext(owner_id="alice", conversation_id="c1")
    with pytest.raises(Exception):  # noqa: B017, PT011 — dataclasses.FrozenInstanceError
        ctx.owner_id = "mallory"  # type: ignore[misc]


def test_contextvar_set_get_reset_round_trip() -> None:
    """Token-based set/reset must restore the prior (None) context."""
    from persona_api.sandbox import get_sandbox_request_context

    assert get_sandbox_request_context() is None
    token = set_sandbox_request_context(
        SandboxRequestContext(owner_id="alice", conversation_id="c1")
    )
    assert get_sandbox_request_context() is not None
    reset_sandbox_request_context(token)
    assert get_sandbox_request_context() is None


# ====================== T04c — Spec 17 D-12-X-read-produced-file + D-17-X-bytes-persistence


class TestProducedFilePersistence:
    """Inner-factory ``produced_file_persister`` wiring (D-12-X-read-produced-file).

    Verifies that ``make_code_execution_tool`` invokes the persister for each
    entry in ``ExecutionResult.produced_files`` after a successful dispatch —
    the bytes-persistence layer the V4/V5 verifications didn't trace.
    """

    @pytest.mark.asyncio
    async def test_persister_called_for_each_produced_file(self) -> None:
        produced = (
            SandboxFile(path="charts/sales.png", size_bytes=12_345, media_type="image/png"),
            SandboxFile(path="uploads/export.csv", size_bytes=4_096, media_type="text/csv"),
        )
        fake = _FakeSandbox(produced_files=produced)
        calls: list[tuple[str, str]] = []

        async def _persister(session_id: str, ref: str) -> None:
            calls.append((session_id, ref))

        tool = make_code_execution_tool(
            fake,
            session_id_provider=lambda: "user-1:conv-9",
            produced_file_persister=_persister,
        )
        result = await tool.execute(code="plt.savefig('charts/sales.png')")
        assert not result.is_error
        # Both produced files persisted via the injected persister, in order.
        assert calls == [
            ("user-1:conv-9", "charts/sales.png"),
            ("user-1:conv-9", "uploads/export.csv"),
        ]

    @pytest.mark.asyncio
    async def test_persister_not_called_when_no_session_id(self) -> None:
        """Stateless one-shot dispatches (no session) don't persist.

        The persister contract is session-keyed (``copy_produced_file_to`` reads
        from the session's host_out); without a session there's no source dir.
        """
        produced = (SandboxFile(path="charts/x.png", size_bytes=1, media_type="image/png"),)
        fake = _FakeSandbox(produced_files=produced)
        calls: list[tuple[str, str]] = []

        async def _persister(session_id: str, ref: str) -> None:
            calls.append((session_id, ref))

        tool = make_code_execution_tool(
            fake,
            session_id_provider=lambda: None,  # stateless
            produced_file_persister=_persister,
        )
        result = await tool.execute(code="print('hi')")
        assert not result.is_error
        assert calls == []

    @pytest.mark.asyncio
    async def test_size_cap_surfaces_as_structured_tool_error(self) -> None:
        """ProducedFileSizeError flows through the existing SandboxError catch path.

        D-12-X-read-produced-file contract: the model sees a structured error
        (is_error=True + the cap info in data["context"]) so it can produce
        a smaller file. Never a crashed stream.
        """
        produced = (
            SandboxFile(path="charts/huge.png", size_bytes=200_000_000, media_type="image/png"),
        )
        fake = _FakeSandbox(produced_files=produced)

        async def _persister_raises(session_id: str, ref: str) -> None:
            raise ProducedFileSizeError(
                f"produced file {ref!r} exceeds cap",
                context={
                    "ref": ref,
                    "size_bytes": "200000000",
                    "cap_bytes": "104857600",
                    "session_id": session_id,
                },
            )

        tool = make_code_execution_tool(
            fake,
            session_id_provider=lambda: "user-1:conv-9",
            produced_file_persister=_persister_raises,
        )
        result = await tool.execute(code="plt.savefig('charts/huge.png')")
        assert result.is_error is True
        assert result.data is not None
        assert result.data["error_type"] == "ProducedFileSizeError"
        ctx = result.data["context"]
        assert isinstance(ctx, dict)
        assert ctx.get("ref") == "charts/huge.png"
        assert ctx.get("cap_bytes") == "104857600"


class TestWorkspaceRootPersisterWiring:
    """Outer-wrapper ``workspace_root`` plumbing (D-17-X-bytes-persistence).

    Verifies ``make_pool_code_execution_tool`` builds the right persister
    closure: bytes land at ``workspace_root/owner_id/persona_id/<ref>``.
    """

    @pytest.mark.asyncio
    async def test_workspace_root_threads_to_persist_target(
        self,
        pool_with_fake: tuple[SandboxPool, _FakeSandbox],
        tmp_path: Path,
    ) -> None:
        pool, fake = pool_with_fake
        fake.produced_files = (
            SandboxFile(path="charts/sales.png", size_bytes=10, media_type="image/png"),
        )
        rls_engine = object()
        workspace_root = tmp_path / "workspaces"
        tool = make_pool_code_execution_tool(
            pool=pool,
            rls_engine=rls_engine,  # type: ignore[arg-type]
            persona_id="persona-A",
            workspace_root=workspace_root,
        )
        token = set_sandbox_request_context(
            SandboxRequestContext(owner_id="alice", conversation_id="c-7")
        )
        try:
            result = await tool.execute(code="plt.savefig('charts/sales.png')")
        finally:
            reset_sandbox_request_context(token)
        assert not result.is_error
        # Persister called with the per-tenant workspace target.
        assert len(fake.copy_calls) == 1
        session_id, ref, target = fake.copy_calls[0]
        assert session_id == "alice:c-7"
        assert ref == "charts/sales.png"
        assert target == workspace_root / "alice" / "persona-A" / "charts/sales.png"

    @pytest.mark.asyncio
    async def test_no_persist_when_workspace_root_absent(
        self,
        pool_with_fake: tuple[SandboxPool, _FakeSandbox],
    ) -> None:
        """CLI / test path: workspace_root=None ⇒ no persist (back-compat)."""
        pool, fake = pool_with_fake
        fake.produced_files = (
            SandboxFile(path="charts/x.png", size_bytes=10, media_type="image/png"),
        )
        rls_engine = object()
        tool = make_pool_code_execution_tool(
            pool=pool,
            rls_engine=rls_engine,  # type: ignore[arg-type]
            persona_id="persona-A",
            workspace_root=None,
        )
        token = set_sandbox_request_context(
            SandboxRequestContext(owner_id="alice", conversation_id="c-7")
        )
        try:
            result = await tool.execute(code="plt.savefig('charts/x.png')")
        finally:
            reset_sandbox_request_context(token)
        assert not result.is_error
        assert fake.copy_calls == []

    @pytest.mark.asyncio
    async def test_produced_file_sidecar_scopes_to_conversation(
        self,
        pool_with_fake: tuple[SandboxPool, _FakeSandbox],
        tmp_path: Path,
    ) -> None:
        """The F5 sidecar carries THIS conversation's id + the ref basename.

        Regression: a ``None`` conversation_id is filtered out of the
        conversation-scoped artifact walk, so the produced file persists on
        disk but never appears in the Files viewer. The ref basename gives the
        viewer a human name instead of falling back to a hash.
        """
        from persona_api.services.artifact_metadata import read_artifact_sidecar

        pool, fake = pool_with_fake
        fake.produced_files = (
            SandboxFile(
                path="marketing_strategy.pdf", size_bytes=30030, media_type="application/pdf"
            ),
        )
        rls_engine = object()
        workspace_root = tmp_path / "workspaces"
        # write_artifact_sidecar does not autocreate parents — the real persister
        # creates them on copy; the fake copy is a no-op, so make uploads/ here.
        (workspace_root / "alice" / "persona-A" / "uploads").mkdir(parents=True)
        tool = make_pool_code_execution_tool(
            pool=pool,
            rls_engine=rls_engine,  # type: ignore[arg-type]
            persona_id="persona-A",
            workspace_root=workspace_root,
        )
        token = set_sandbox_request_context(
            SandboxRequestContext(owner_id="alice", conversation_id="conv-42")
        )
        try:
            result = await tool.execute(code="pdf.savefig(fig)")
        finally:
            reset_sandbox_request_context(token)
        assert not result.is_error
        target = workspace_root / "alice" / "persona-A" / "uploads" / "marketing_strategy.pdf"
        meta = read_artifact_sidecar(target)
        assert meta is not None
        assert meta.source == "generated"
        assert meta.conversation_id == "conv-42"
        assert meta.original_name == "marketing_strategy.pdf"


class TestCrossTurnIntermediateStaging:
    """D-17-X-bytes-persistence inverse flow: ``intermediate/*`` staged before dispatch."""

    @pytest.mark.asyncio
    async def test_intermediate_files_staged_into_input_files(
        self,
        pool_with_fake: tuple[SandboxPool, _FakeSandbox],
        tmp_path: Path,
    ) -> None:
        pool, fake = pool_with_fake
        # Pre-populate <workspace>/alice/persona-A/intermediate/df.parquet so the
        # augmented input-files provider stages it on dispatch.
        workspace_root = tmp_path / "workspaces"
        persona_workspace = workspace_root / "alice" / "persona-A"
        intermediate = persona_workspace / "intermediate"
        intermediate.mkdir(parents=True)
        (intermediate / "df.parquet").write_bytes(b"parquet-bytes-here")

        rls_engine = object()
        tool = make_pool_code_execution_tool(
            pool=pool,
            rls_engine=rls_engine,  # type: ignore[arg-type]
            persona_id="persona-A",
            workspace_root=workspace_root,
        )
        token = set_sandbox_request_context(
            SandboxRequestContext(owner_id="alice", conversation_id="c-7")
        )
        try:
            result = await tool.execute(code="df = pd.read_parquet('intermediate/df.parquet')")
        finally:
            reset_sandbox_request_context(token)
        assert not result.is_error
        # Exactly one execute happened; its input_files carries the staged parquet.
        assert len(fake.execute_calls) == 1
        staged = fake.execute_calls[0]["input_files"]
        assert isinstance(staged, list)
        assert len(staged) == 1
        sf = staged[0]
        assert isinstance(sf, SandboxFile)
        assert sf.path == "intermediate/df.parquet"
        assert sf.content_bytes == b"parquet-bytes-here"

    @pytest.mark.asyncio
    async def test_no_staging_when_intermediate_absent(
        self,
        pool_with_fake: tuple[SandboxPool, _FakeSandbox],
        tmp_path: Path,
    ) -> None:
        """First-turn dispatches with no prior intermediate ⇒ empty staged list."""
        pool, fake = pool_with_fake
        workspace_root = tmp_path / "workspaces"
        # Don't create intermediate/.
        rls_engine = object()
        tool = make_pool_code_execution_tool(
            pool=pool,
            rls_engine=rls_engine,  # type: ignore[arg-type]
            persona_id="persona-A",
            workspace_root=workspace_root,
        )
        token = set_sandbox_request_context(
            SandboxRequestContext(owner_id="alice", conversation_id="c-7")
        )
        try:
            result = await tool.execute(code="print('first turn')")
        finally:
            reset_sandbox_request_context(token)
        assert not result.is_error
        assert fake.execute_calls[0]["input_files"] == []


# =================================== T02c — D-F4-X-bare-ref-resolution policy =====


class TestPersisterPolicyBareRefResolution:
    """Spec F4 T02c — three-branch persister target policy.

    Pre-T02c, the persister at ``runtime_tool.py:231`` wrote every produced
    file to ``persona_workspace / ref`` verbatim. Combined with the
    slash-aware resolver at ``image_service.fetch:300`` (which prepends
    ``uploads/`` on slash-less refs), this meant **Spec 16 docs
    persisted at workspace root but the GET endpoint looked under
    ``uploads/``** → every document download 404'd.

    Post-T02c, ``_persist_produced_file`` applies a three-branch policy:

      * ``charts/<id>.png`` (Spec 17 matplotlib) — stays at workspace root
        (load-bearing for F4's inline-vs-download discriminator per
        D-17-X-inline-hint-shape).
      * ``intermediate/<name>.parquet`` (Spec 17 cross-turn cache) — stays
        at workspace root (consumed by ``_augmented_input_files_provider``
        on the next-turn dispatch).
      * everything else — routes into ``uploads/<filename>.<ext>`` so the
        slash-aware resolver's slash-less branch lands on the right path.

    Each branch is a regression assertion (A + B preserve existing
    behaviour) OR the primary fix assertion (C). All three must hold for
    the catch from Phase 3 R-F4-1 to be considered closed.

    10th entry in the additive-precedent chain (D-01-12 → ... → this).
    """

    @pytest.mark.asyncio
    async def test_branch_a_charts_stays_at_workspace_root(
        self,
        pool_with_fake: tuple[SandboxPool, _FakeSandbox],
        tmp_path: Path,
    ) -> None:
        """Branch A regression: ``charts/<id>.png`` keeps the ``charts/``
        prefix on disk.

        Moving charts into ``uploads/`` would break the F4 dispatcher's
        ``path.startsWith("charts/")`` → InlineChart routing, breaking
        D-17-X-inline-hint-shape and the visual taxonomy.
        """
        pool, fake = pool_with_fake
        fake.produced_files = (
            SandboxFile(path="charts/q1.png", size_bytes=10, media_type="image/png"),
        )
        rls_engine = object()
        workspace_root = tmp_path / "workspaces"
        tool = make_pool_code_execution_tool(
            pool=pool,
            rls_engine=rls_engine,  # type: ignore[arg-type]
            persona_id="persona-A",
            workspace_root=workspace_root,
        )
        token = set_sandbox_request_context(
            SandboxRequestContext(owner_id="alice", conversation_id="c-7")
        )
        try:
            result = await tool.execute(code="plt.savefig('charts/q1.png')")
        finally:
            reset_sandbox_request_context(token)
        assert not result.is_error
        assert len(fake.copy_calls) == 1
        _session_id, ref, target = fake.copy_calls[0]
        assert ref == "charts/q1.png"
        # Charts land verbatim at <workspace>/<owner>/<persona>/charts/<id>.png.
        assert target == workspace_root / "alice" / "persona-A" / "charts" / "q1.png"

    @pytest.mark.asyncio
    async def test_branch_b_intermediate_stays_at_workspace_root(
        self,
        pool_with_fake: tuple[SandboxPool, _FakeSandbox],
        tmp_path: Path,
    ) -> None:
        """Branch B regression: ``intermediate/<name>.parquet`` keeps the
        ``intermediate/`` prefix on disk.

        Consumed by ``_augmented_input_files_provider`` on the next-turn
        dispatch (the SKILL.md parquet re-load pattern). Moving under
        ``uploads/`` would break the next-turn re-load discipline.
        """
        pool, fake = pool_with_fake
        fake.produced_files = (
            SandboxFile(
                path="intermediate/df_q1.parquet",
                size_bytes=20,
                media_type="application/octet-stream",
            ),
        )
        rls_engine = object()
        workspace_root = tmp_path / "workspaces"
        tool = make_pool_code_execution_tool(
            pool=pool,
            rls_engine=rls_engine,  # type: ignore[arg-type]
            persona_id="persona-A",
            workspace_root=workspace_root,
        )
        token = set_sandbox_request_context(
            SandboxRequestContext(owner_id="alice", conversation_id="c-7")
        )
        try:
            result = await tool.execute(code="df.to_parquet('intermediate/df_q1.parquet')")
        finally:
            reset_sandbox_request_context(token)
        assert not result.is_error
        assert len(fake.copy_calls) == 1
        _session_id, ref, target = fake.copy_calls[0]
        assert ref == "intermediate/df_q1.parquet"
        # Intermediate lands verbatim at <workspace>/<owner>/<persona>/intermediate/.
        assert target == workspace_root / "alice" / "persona-A" / "intermediate" / "df_q1.parquet"

    @pytest.mark.parametrize(
        "filename",
        [
            "report.docx",
            "presentation.pptx",
            "budget.xlsx",
            "quarterly-summary.pdf",
            "raw-output.txt",
            "data.json",
        ],
    )
    @pytest.mark.asyncio
    async def test_branch_c_bare_filename_routes_to_uploads(
        self,
        pool_with_fake: tuple[SandboxPool, _FakeSandbox],
        tmp_path: Path,
        filename: str,
    ) -> None:
        """Branch C — **THE PRIMARY assertion** that Spec 16 download 404 is fixed.

        Pre-T02c: bare ``<filename>.<ext>`` persisted at workspace root;
        ``image_service.fetch:300``'s slash-less branch prepended
        ``uploads/`` → 404.

        Post-T02c: bare files persist into ``uploads/<filename>.<ext>``;
        the resolver lands on the right path; GET serves correctly.

        Parametrised across the four Spec 16 doc formats + two general
        bare cases — the policy is extension-agnostic.
        """
        pool, fake = pool_with_fake
        fake.produced_files = (
            SandboxFile(
                path=filename,
                size_bytes=10,
                media_type="application/octet-stream",
            ),
        )
        rls_engine = object()
        workspace_root = tmp_path / "workspaces"
        tool = make_pool_code_execution_tool(
            pool=pool,
            rls_engine=rls_engine,  # type: ignore[arg-type]
            persona_id="persona-A",
            workspace_root=workspace_root,
        )
        token = set_sandbox_request_context(
            SandboxRequestContext(owner_id="alice", conversation_id="c-7")
        )
        try:
            result = await tool.execute(code=f"doc.save('{filename}')")
        finally:
            reset_sandbox_request_context(token)
        assert not result.is_error
        assert len(fake.copy_calls) == 1
        _session_id, ref, target = fake.copy_calls[0]
        assert ref == filename
        # Bare ref routes to uploads/<filename> so the slash-aware resolver
        # at image_service.fetch:300 lands on the right path.
        assert target == workspace_root / "alice" / "persona-A" / "uploads" / filename

    @pytest.mark.asyncio
    async def test_charts_prefix_is_exact_match_not_substring(
        self,
        pool_with_fake: tuple[SandboxPool, _FakeSandbox],
        tmp_path: Path,
    ) -> None:
        """Defensive: ``charts_archive/<id>.png`` does NOT match the
        ``charts/`` rule.

        The prefix check uses the trailing slash so a non-Spec-17 file
        under an unrelated subdirectory isn't accidentally pinned at
        workspace root.
        """
        pool, fake = pool_with_fake
        fake.produced_files = (
            SandboxFile(path="charts_archive/old.png", size_bytes=10, media_type="image/png"),
        )
        rls_engine = object()
        workspace_root = tmp_path / "workspaces"
        tool = make_pool_code_execution_tool(
            pool=pool,
            rls_engine=rls_engine,  # type: ignore[arg-type]
            persona_id="persona-A",
            workspace_root=workspace_root,
        )
        token = set_sandbox_request_context(
            SandboxRequestContext(owner_id="alice", conversation_id="c-7")
        )
        try:
            result = await tool.execute(code="open('charts_archive/old.png','w')")
        finally:
            reset_sandbox_request_context(token)
        assert not result.is_error
        _session_id, ref, target = fake.copy_calls[0]
        assert ref == "charts_archive/old.png"
        # Routes to uploads/charts_archive/old.png — NOT to /charts_archive/old.png.
        assert (
            target
            == workspace_root / "alice" / "persona-A" / "uploads" / "charts_archive" / "old.png"
        )


# =================================================== image-workspace cascade (Part 4)


@pytest.mark.asyncio
async def test_uploaded_image_is_staged_into_sandbox_input_files(
    pool_with_fake: tuple[SandboxPool, _FakeSandbox],
) -> None:
    """A turn's uploaded image, staged on the loop's deferred_input_files, reaches
    the substrate as an ``input_files`` entry so ``code_execution`` can read it.

    End-to-end faked-sandbox proof of the image-workspace cascade Part 4: the
    loop's ``_stage_images_as_input_files`` output (the production staging shape)
    is drained by the runtime tool's provider and handed to ``execute()``.
    """
    from persona_runtime.images import TurnImage
    from persona_runtime.loop import _stage_images_as_input_files

    pool, fake = pool_with_fake
    # The loop stages the uploaded image exactly as it does in turn().
    holder: list[SandboxFile] = list(
        _stage_images_as_input_files(
            [
                TurnImage(
                    workspace_path="uploads/cat.png",
                    media_type="image/png",
                    content_bytes=b"\x89PNG\r\n\x1a\nFAKE",
                )
            ]
        )
    )

    def _drain_and_clear() -> list[SandboxFile]:
        snapshot = list(holder)
        holder.clear()
        return snapshot

    token = set_sandbox_request_context(
        SandboxRequestContext(owner_id="alice", conversation_id="c1")
    )
    mock_policy = MagicMock()
    try:
        tool = make_pool_code_execution_tool(
            pool=pool,
            rls_engine=object(),  # type: ignore[arg-type]
            credits_policy=mock_policy,
            deferred_input_files_provider=_drain_and_clear,
        )
        result = await tool.execute(code="import os; print(os.listdir('in'))")
        assert not result.is_error
    finally:
        reset_sandbox_request_context(token)

    assert len(fake.execute_calls) == 1
    staged = fake.execute_calls[0]["input_files"]
    assert isinstance(staged, list)
    paths = [f.path for f in staged]
    assert "uploads/cat.png" in paths
    img = next(f for f in staged if f.path == "uploads/cat.png")
    assert img.content_bytes == b"\x89PNG\r\n\x1a\nFAKE"
    assert img.media_type == "image/png"


# =============================================== document-workspace cascade (sandbox)


@pytest.mark.asyncio
async def test_uploaded_document_is_staged_into_sandbox_input_files(
    pool_with_fake: tuple[SandboxPool, _FakeSandbox],
) -> None:
    """A turn's uploaded document, staged on the loop's deferred_input_files,
    reaches the substrate as an ``input_files`` entry so a sandbox
    ``file_read`` / ``code_execution`` listing finds THAT file (not a stale,
    unrelated file from another context).

    End-to-end faked-sandbox proof of the document-workspace cascade: a
    ``SandboxFile`` document (the production staging shape, built by
    ``chat_service._resolve_turn_documents``) is drained by the runtime tool's
    provider and handed to ``execute()`` under the ``in/`` mount.
    """
    pool, fake = pool_with_fake
    body = b"# Polly's Layout Test\n\nThe ACTUAL uploaded document.\n"
    holder: list[SandboxFile] = [
        SandboxFile(
            path="uploads/pollys-layout-test.md",
            content_bytes=body,
            size_bytes=len(body),
            media_type="text/markdown",
        )
    ]

    def _drain_and_clear() -> list[SandboxFile]:
        snapshot = list(holder)
        holder.clear()
        return snapshot

    token = set_sandbox_request_context(
        SandboxRequestContext(owner_id="alice", conversation_id="c1")
    )
    mock_policy = MagicMock()
    try:
        tool = make_pool_code_execution_tool(
            pool=pool,
            rls_engine=object(),  # type: ignore[arg-type]
            credits_policy=mock_policy,
            deferred_input_files_provider=_drain_and_clear,
        )
        result = await tool.execute(code="print(open('in/uploads/pollys-layout-test.md').read())")
        assert not result.is_error
    finally:
        reset_sandbox_request_context(token)

    assert len(fake.execute_calls) == 1
    staged = fake.execute_calls[0]["input_files"]
    assert isinstance(staged, list)
    paths = [f.path for f in staged]
    assert "uploads/pollys-layout-test.md" in paths
    doc = next(f for f in staged if f.path == "uploads/pollys-layout-test.md")
    assert doc.content_bytes == body
    assert doc.media_type == "text/markdown"

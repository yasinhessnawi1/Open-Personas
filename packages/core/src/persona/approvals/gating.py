"""The policy-gated toolbox — the A3 enforcement boundary (Spec A3, T7; A3-D-X-gate-mechanism).

:class:`PolicyGatedToolbox` is the **only** place the per-task category policy meets a live tool
call. It decorates the Spec-03 :class:`~persona.tools.Toolbox` (subclass, so it satisfies the
agentic loop's ``toolbox: Toolbox`` type with **no ``loop.py`` edit**, D-A2-X-no-loop-mod) and
intercepts ``dispatch``:

- **allow** → delegate to the inner ``Toolbox.dispatch`` unchanged (chat-time behaviour);
- **deny** (unattended-denied) → return a **recoverable** ``ToolResult(is_error=True)`` — the
  model sees "not available unattended," adapts, and the leg **continues** (chat-allowed,
  unattended-denied; never a leg-end);
- **gate** → record the exact :class:`~persona.approvals.ActionProposal` **durably first**
  (via the injected :class:`ProposalRecorder` — the api ``ApprovalStore``), **then** raise
  :class:`~persona.errors.GatedActionProposedError`. The proposal is persisted before the
  raise, so the ``waiting(on_user)`` the executor produces always has something to resume
  against.

The exception propagates through the **unmodified** loop (whose ``_dispatch`` catches only
``ToolNotAllowedError`` / ``ToolExecutionError``) to the A2 ``LegExecutor``, which maps it to a
``WAITING_APPROVAL`` disposition. The error carries ``activity_status="awaiting_approval"`` so
P2's outer ``ObservedToolbox`` (if wrapped around this one) can emit an awaiting-approval
activity end before re-raising (the merge-back composition: P2 outermost, A3 inner).

Injected only at the **leg** runner (the api composition root) — the chat path keeps the bare
``Toolbox``, so there is no chat-permission regression (criterion 11).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from persona.approvals.records import ActionProposal
from persona.errors import GatedActionProposedError
from persona.logging import get_logger
from persona.schema.tools import ToolResult
from persona.tools import CategoryDecision, Toolbox, resolve_action_categories

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from persona.schema.tools import ToolCall
    from persona.tools import CategoryPolicy
    from persona.tools.protocol import AsyncTool

__all__ = ["GateContext", "PolicyGatedToolbox", "ProposalRecorder"]

_log = get_logger("approvals.gating")

_MAX_VALUE_CHARS = 80


@runtime_checkable
class ProposalRecorder(Protocol):
    """The durable proposal-recording seam (the api ``ApprovalStore.create_proposal`` satisfies it).

    Idempotent against the one-pending-per-task index: a re-delivered gated leg's create
    returns the task's existing pending proposal rather than raising (T6).
    """

    def create_proposal(self, proposal: ActionProposal) -> ActionProposal: ...


class GateContext(BaseModel):
    """The leg-scoped identity the gate stamps onto a recorded proposal."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    owner_id: str
    task_id: str
    persona_id: str


def _short(value: object) -> str:
    """A compact, readable rendering of one argument value for the proposal description."""
    text = str(value)
    return text if len(text) <= _MAX_VALUE_CHARS else f"{text[:_MAX_VALUE_CHARS]}…"


def _render_description(tool_name: str, args: dict[str, object]) -> str:
    """A precise, factual rendering of the exact action (the audit/UX artifact, A3 §3).

    Factual, not persona-voiced — the C0 message (T8) wraps this in the persona's voice. The
    exact payload is preserved verbatim in ``ActionProposal.arguments``; this is the human view.
    """
    if not args:
        return f"Run `{tool_name}`"
    parts = ", ".join(f"{key}={_short(val)}" for key, val in args.items())
    return f"Run `{tool_name}` with {parts}"


class PolicyGatedToolbox(Toolbox):
    """A :class:`Toolbox` that enforces the per-task category policy on every dispatch (T7).

    Args:
        tools: The registered tools (as for :class:`Toolbox`).
        allow_list: The persona's chat allow-list (as for :class:`Toolbox`).
        policy: The task's :class:`~persona.tools.CategoryPolicy` (from the contract).
        recorder: The durable proposal sink (the api ``ApprovalStore``).
        context: The leg-scoped owner/task/persona identity stamped onto a proposal.
        network_enabled: Whether the persona's sandbox has network egress (escalates
            ``code_execution`` to ``external_mutate`` — the back-door close, A3-D-1).
        clock: Injected now-source (core stays clock-free); defaults to ``datetime.now(UTC)``.
    """

    def __init__(
        self,
        tools: Iterable[AsyncTool],
        *,
        allow_list: list[str] | None = None,
        policy: CategoryPolicy,
        recorder: ProposalRecorder,
        context: GateContext,
        network_enabled: bool = False,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(tools, allow_list=allow_list)
        self._policy = policy
        self._recorder = recorder
        self._ctx = context
        self._network_enabled = network_enabled
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)

    async def dispatch(self, tool_call: ToolCall) -> ToolResult:
        """Evaluate the policy, then allow / deny / gate (A3-D-X-gate-mechanism)."""
        categories = resolve_action_categories(
            tool_call.name, network_enabled=self._network_enabled
        )
        decision = self._policy.decide_tool(categories)

        if decision is CategoryDecision.ALLOW:
            return await super().dispatch(tool_call)

        if decision is CategoryDecision.DENY:
            # Recoverable — the model adapts within the leg; the leg does NOT end.
            _log.info("tool denied unattended", tool=tool_call.name, task_id=self._ctx.task_id)
            return ToolResult(
                tool_name=tool_call.name,
                call_id=tool_call.call_id,
                is_error=True,
                content=(
                    f"Tool '{tool_call.name}' is not available unattended (denied by the task's "
                    "permission policy). Adapt your approach or report that you cannot proceed."
                ),
            )

        # GATE — record the exact proposal durably, THEN raise (order matters: the raise must
        # never beat the persist, or the resulting waiting(on_user) has nothing to resume).
        proposal = ActionProposal(
            proposal_id=f"prop_{uuid.uuid4().hex}",
            owner_id=self._ctx.owner_id,
            task_id=self._ctx.task_id,
            persona_id=self._ctx.persona_id,
            categories=categories,
            tool_name=tool_call.name,
            arguments=dict(tool_call.args),
            description=_render_description(tool_call.name, dict(tool_call.args)),
            created_at=self._clock(),
        )
        persisted = self._recorder.create_proposal(proposal)
        _log.info(
            "gated action proposed",
            tool=tool_call.name,
            task_id=self._ctx.task_id,
            proposal_id=persisted.proposal_id,
        )
        raise GatedActionProposedError(
            "gated action awaiting approval",
            context={
                "proposal_id": persisted.proposal_id,
                "tool": tool_call.name,
                "task_id": self._ctx.task_id,
                # R9-163: the same sentence the inbox shows, so the park can say WHAT it is
                # waiting for and not merely that it waits.
                "description": persisted.description,
            },
        )

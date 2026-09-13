"""The leg gate: a task's category policy meets the toolbox it runs on (Spec W1, T2; D-W1-1).

Spec A3 built :class:`~persona.approvals.gating.PolicyGatedToolbox` and the whole approval
flow behind it (the executor's ``WAITING_APPROVAL`` disposition, the continuation's park, the
inbox, the resolver), and documented it as "injected only at the leg runner". The injection
never happened: no production module constructed the gated toolbox, so every leg ran on the
bare one and a contract's category policy was never enforced (found by W1 Phase 4, T2).

This module is that injection, kept tiny and pure: a frozen value holding what the gate
needs (the task's policy, the durable proposal recorder, the leg-scoped identity, and the
sandbox-egress flag), plus one method that renders it as the
:class:`~persona.tools.ToolboxFactory` the runtime factory substitutes at construction. The
runner builder creates one per leg from the task; nothing here touches a store.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from persona.approvals.gating import GateContext, PolicyGatedToolbox

if TYPE_CHECKING:
    from collections.abc import Iterable

    from persona.approvals.gating import ProposalRecorder
    from persona.tasks import Task
    from persona.tools import CategoryPolicy, Toolbox, ToolboxFactory
    from persona.tools.protocol import AsyncTool

__all__ = ["LegGate"]


@dataclass(frozen=True)
class LegGate:
    """What one leg's policy gate needs, and the toolbox factory that applies it.

    Attributes:
        policy: The task contract's category policy (frozen on the task, D-A2-1).
        recorder: The durable proposal sink (the api ``ApprovalStore``).
        context: The owner / task / persona identity stamped onto a recorded proposal.
        network_enabled: Whether the sandbox has network egress (escalates
            ``code_execution`` to ``external_mutate``, A3-D-1). ``False`` until a
            deployment exposes the flag; the conservative reading.
    """

    policy: CategoryPolicy
    recorder: ProposalRecorder
    context: GateContext
    network_enabled: bool = False

    @classmethod
    def for_task(
        cls, task: Task, *, recorder: ProposalRecorder, network_enabled: bool = False
    ) -> LegGate:
        """The gate for ``task``: its contract policy, scoped to its owner and persona."""
        return cls(
            policy=task.contract.category_policy,
            recorder=recorder,
            context=GateContext(
                owner_id=task.owner_id, task_id=task.id, persona_id=task.persona_id
            ),
            network_enabled=network_enabled,
        )

    def toolbox_factory(self) -> ToolboxFactory:
        """The factory the runtime factory calls in place of the bare ``Toolbox`` class."""

        def _make(tools: Iterable[AsyncTool], *, allow_list: list[str] | None = None) -> Toolbox:
            return PolicyGatedToolbox(
                tools,
                allow_list=allow_list,
                policy=self.policy,
                recorder=self.recorder,
                context=self.context,
                network_enabled=self.network_enabled,
            )

        return _make

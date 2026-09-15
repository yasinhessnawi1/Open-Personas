"""The notify-on-park voice asks only about proposals that are still pending (part1 F6).

The sweep recorded ``ApprovalResolver.announce`` as built, tested and never called. That was
half right, and the missing half is the interesting one: the proactive "may I do X?" voice WAS
wired, through a near-copy of ``announce`` living in ``worker_root`` that had dropped its
``PENDING`` guard. So the guarded implementation had no callers, the calling implementation had
no guard, and a re-delivered leg job could ask the user about a proposal they had already
answered.

Both now go through one function. These pin the guard, because the guard is the behaviour that
production was actually missing.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from persona.approvals.records import ActionProposal, ProposalStatus
from persona.tools.category_policy import ActionCategory
from persona_api.approvals import announce_parked_proposal

pytestmark = pytest.mark.asyncio

_NOW = datetime(2026, 9, 15, 9, 0, tzinfo=UTC)


def _proposal(status: ProposalStatus) -> ActionProposal:
    return ActionProposal(
        proposal_id="p1",
        task_id="t1",
        owner_id="user_a",
        persona_id="persona_a",
        categories=frozenset({ActionCategory.EXTERNAL_MUTATE}),
        tool_name="send_email",
        arguments={"to": "bob@example.com"},
        description="Send the appeal to bob@example.com",
        status=status,
        created_at=_NOW,
    )


class _Store:
    def __init__(self, proposal: ActionProposal) -> None:
        self._proposal = proposal

    def get_proposal(self, owner_id: str, proposal_id: str) -> ActionProposal:  # noqa: ARG002
        return self._proposal


class _Notifier:
    def __init__(self) -> None:
        self.asked: list[str] = []

    async def ask(self, proposal: ActionProposal) -> None:
        self.asked.append(proposal.proposal_id)

    async def reconfirm(self, proposal: ActionProposal) -> None: ...
    async def clarify(self, proposal: ActionProposal) -> None: ...
    async def remind(self, proposal: ActionProposal) -> None: ...
    async def expired(self, proposal: ActionProposal) -> None: ...


async def test_a_pending_proposal_is_announced() -> None:
    notifier = _Notifier()
    await announce_parked_proposal(
        _Store(_proposal(ProposalStatus.PENDING)),  # type: ignore[arg-type]
        notifier,
        owner_id="user_a",
        proposal_id="p1",
    )
    assert notifier.asked == ["p1"]


@pytest.mark.parametrize(
    "status",
    [s for s in ProposalStatus if s is not ProposalStatus.PENDING],
)
async def test_an_already_resolved_proposal_is_never_announced(status: ProposalStatus) -> None:
    """The guard. Asking "may I do X?" about something already answered is worse than silence:
    it reads as the persona having ignored the reply."""
    notifier = _Notifier()
    await announce_parked_proposal(
        _Store(_proposal(status)),  # type: ignore[arg-type]
        notifier,
        owner_id="user_a",
        proposal_id="p1",
    )
    assert notifier.asked == [], f"a {status.value} proposal must not be announced"

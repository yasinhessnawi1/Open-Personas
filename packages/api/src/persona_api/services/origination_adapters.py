"""The real store / C0 plugs for the A4 origination seam (Spec A4, composition-root wiring).

:class:`OriginationService` (T6) and :class:`TaskSteeringService` (T9b) depend on narrow
Protocols so their invariants are fake-tested; this module supplies the **production** plugs that
back those Protocols with the real owner-scoped stores and the real C0 delivery composition:

- :class:`TaskCreatorAdapter` / :class:`ScheduleCreatorAdapter` — wrap the owner-scoped
  ``TaskStore`` / ``ScheduleStore`` with the idempotent ``create_if_absent`` + ``get_optional``
  the service needs. Idempotency is belt-and-braces: the service probes ``get_optional`` first,
  and ``create_if_absent`` additionally swallows a unique-violation, so a racing replay converges
  on exactly one row (A4-D-X).
- :class:`OriginatorFailureNotifier` — the un-suppressible failure channel: it originates the
  :class:`FailureAccount` as a persona-voiced C0 message on the confirm turn's conversation via the
  real :class:`Originator` (recorder + :class:`DeliveryRouter`), so a confirmed-but-failed contract
  is **durably visible** in the conversation regardless of any digest/cadence setting.

``render_failure_account`` is the shared account→text rendering (headline + honest cause + the
concrete next options), reused by the cancel-failure path.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import yaml as _yaml
from persona.audit import AuditLogger, JSONLAuditLogger
from persona.errors import ScheduleNotFoundError, TaskNotFoundError
from persona.logging import get_logger
from persona.originator import Originator
from persona.schema.origination import PersonaIdentityTag
from persona.stores.episodic import EpisodicStore
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from persona_api.db.models import personas as personas_t
from persona_api.services.delivery_router import DeliveryRouter
from persona_api.services.origination import OriginationRecorder
from persona_api.services.web_deliverer import WebAppDeliverer

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from persona.approvals import ActionProposal
    from persona.schedules import Schedule
    from persona.schema.origination import OriginatedMessage
    from persona.stores.backend import Backend
    from persona.tasks import Task
    from sqlalchemy import Engine

    from persona_api.approvals.cadence import MessagePriority
    from persona_api.approvals.failure import FailureAccount
    from persona_api.config import Edition
    from persona_api.schedules.store import ScheduleStore
    from persona_api.services.web_deliverer import LiveSessionRegistry, LiveSessionSink
    from persona_api.tasks.store import TaskStore

__all__ = [
    "OriginatorApprovalNotifier",
    "OriginatorFailureNotifier",
    "OriginatorUpdateSender",
    "ScheduleCreatorAdapter",
    "TaskCreatorAdapter",
    "make_persona_tag_resolver",
    "render_approval_message",
    "render_failure_account",
    "resolve_persona_tag",
]

_logger = get_logger("api.origination_adapters")


async def _originate_on_conversation(
    *,
    engine: Engine,
    episodic: EpisodicStore,
    edition: Edition,
    persona: PersonaIdentityTag,
    owner_id: str,
    content: str,
    conversation_id: str,
    now: datetime,
    channel: str | None = None,
    sessions: LiveSessionRegistry | None = None,
) -> None:
    """Originate one persona message on ``conversation_id`` via the real C0 composition.

    The shared plumbing behind the A4 failure account + digest update: an :class:`Originator`
    over the RLS-scoped :class:`OriginationRecorder` (durable conversation + episodic write) and a
    :class:`DeliveryRouter` (best-effort live delivery; ``channel`` feeds ``resolve_channel``,
    falling back to the web home when that channel has no deliverer here).

    ``sessions`` is the live-session registry the ``WebAppDeliverer`` consults (Spec A11): with
    the real channel-backed registry an OPEN tab receives the message live (``message.delivered``);
    ``None`` keeps the pre-A11 ``_NoLiveSessions`` behaviour (persist-only → present-on-next-open).
    The recorder persists the message BEFORE ``deliver`` runs, so the live event is always emitted
    AFTER the durable commit (the client's refetch finds the row — A11 post-commit rule).
    """
    web = WebAppDeliverer(rls_engine=engine, sessions=sessions or _NoLiveSessions())
    router = DeliveryRouter(
        deliverers={"web": web}, rls_engine=engine, resolve_channel=lambda _m: channel
    )
    recorder = OriginationRecorder(rls_engine=engine, episodic_store=episodic, edition=edition)
    originator = Originator(recorder=recorder, deliverer=router)
    await originator.originate(
        persona=persona,
        owner_user_id=owner_id,
        content=content,
        created_at=now,
        conversation_id=conversation_id or None,
    )


def resolve_persona_tag(engine: Engine, persona_id: str) -> PersonaIdentityTag | None:
    """Build a :class:`PersonaIdentityTag` from the persona row (RLS-scoped read).

    The name-tag the user sees on an originated update/failure. ``None`` when the persona row is
    absent (a deleted persona → nothing to voice). Mirrors the C0 within-runtime tag lookup.
    """
    with engine.begin() as conn:
        row = (
            conn.execute(
                select(personas_t.c.yaml, personas_t.c.avatar_url).where(
                    personas_t.c.id == persona_id
                )
            )
            .mappings()
            .first()
        )
    if row is None:
        return None
    name = persona_id
    try:
        raw = _yaml.safe_load(row["yaml"]) or {}
        identity = raw.get("identity") or {}
        name = identity.get("name") or persona_id
    except Exception:  # noqa: BLE001 — a malformed YAML must not break origination
        name = persona_id
    return PersonaIdentityTag(
        persona_id=persona_id, display_name=name, visual_ref=row["avatar_url"]
    )


def make_persona_tag_resolver(
    engine: Engine,
) -> Callable[[str], PersonaIdentityTag | None]:
    """A persona-id → tag resolver closed over ``engine`` (for the steering-service DI)."""
    return lambda persona_id: resolve_persona_tag(engine, persona_id)


class TaskCreatorAdapter:
    """Back the service's ``TaskCreator`` Protocol with the owner-scoped ``TaskStore``."""

    def __init__(self, tasks: TaskStore) -> None:
        self._tasks = tasks

    def get_optional(self, owner_id: str, task_id: str) -> Task | None:
        """The task, or ``None`` on a miss (the idempotency probe; RLS-scoped)."""
        try:
            return self._tasks.get(owner_id, task_id)
        except TaskNotFoundError:
            return None

    def create_if_absent(self, task: Task) -> None:
        """Persist ``task``; swallow a unique-violation so a racing replay is a no-op."""
        try:
            self._tasks.create(task)
        except IntegrityError:
            _logger.info(
                "task create raced to a duplicate; idempotent no-op task_id={tid}", tid=task.id
            )


class ScheduleCreatorAdapter:
    """Back the service's ``ScheduleCreator`` Protocol with the owner-scoped ``ScheduleStore``."""

    def __init__(self, schedules: ScheduleStore) -> None:
        self._schedules = schedules

    def create_if_absent(self, schedule: Schedule, *, now: datetime) -> None:
        """Persist ``schedule``; swallow a unique-violation (idempotent under replay)."""
        try:
            self._schedules.create(schedule, now=now)
        except IntegrityError:
            _logger.info(
                "schedule create raced to a duplicate; idempotent no-op schedule_id={sid}",
                sid=schedule.id,
            )

    def delete(self, owner_id: str, schedule_id: str) -> None:
        """Remove a schedule (the compensating action); a missing one is already gone."""
        with contextlib.suppress(ScheduleNotFoundError):
            self._schedules.delete(owner_id, schedule_id)


class _NoLiveSessions:
    """A live-session registry with no open sessions — delivery is durable-persist only.

    Outside a run there is no open SSE stream bound to the conversation, so live delivery yields
    ``pending`` (D-C0-4, not a drop) while the recorder's conversation write is the durable,
    un-suppressible visibility the failure account relies on.
    """

    def lookup(self, message: OriginatedMessage) -> LiveSessionSink | None:  # noqa: ARG002
        return None


def render_failure_account(account: FailureAccount) -> str:
    """Render a :class:`FailureAccount` as a persona-voiced line — cause + concrete options."""
    options = "; ".join(account.options)
    return f"{account.headline} {account.cause}. You can: {options}."


class OriginatorFailureNotifier:
    """Deliver an A4 :class:`FailureAccount` as an originated C0 message (durable + best-effort).

    Mirrors the C0 composition (:class:`Originator` over :class:`OriginationRecorder` +
    :class:`DeliveryRouter`), so the failure account is **persisted** to the confirm turn's
    conversation (the un-suppressible floor) and delivered inline if a session happens to be open.
    Owner-scoped by the recorder's ownership guard + RLS.
    """

    def __init__(
        self,
        *,
        rls_engine: Engine,
        memory_backend: Backend,
        edition: Edition,
        audit_root: Path,
        audit_logger: AuditLogger | None = None,
        sessions: LiveSessionRegistry | None = None,
    ) -> None:
        self._engine = rls_engine
        self._edition = edition
        self._sessions = sessions
        # R5-D-2: backend-selected audit when supplied (worker parity);
        # audit_root stays the byte-unchanged JSONL fallback.
        self._episodic = EpisodicStore(
            backend=memory_backend, audit_logger=audit_logger or JSONLAuditLogger(audit_root)
        )

    async def notify(
        self,
        account: FailureAccount,
        *,
        persona: PersonaIdentityTag,
        owner_id: str,
        conversation_id: str,
    ) -> None:
        """Originate the failure account on ``conversation_id`` (persisted; owner-scoped)."""
        await _originate_on_conversation(
            engine=self._engine,
            episodic=self._episodic,
            edition=self._edition,
            persona=persona,
            owner_id=owner_id,
            content=render_failure_account(account),
            conversation_id=conversation_id,
            now=datetime.now(UTC),
            sessions=self._sessions,
        )


def render_approval_message(kind: str, proposal: ActionProposal) -> str:
    """Render an approval-loop C0 line deterministically (no model) from the proposal (A6 T-seam).

    ``kind`` ∈ {ask, reconfirm, clarify, remind, expired}. Honest + glanceable: it names what the
    persona wants to do (``proposal.description``, verbatim) — never a paraphrase of the action.
    """
    d = proposal.description
    desc = (d[0].lower() + d[1:]) if d else f"run {proposal.tool_name}"
    lines = {
        "ask": f"I'd like to {desc}. Reply to approve, deny, or tell me what to change.",
        "reconfirm": f"Updated — I'd now {desc}. Approve the change, or deny.",
        "clarify": f"To be sure: should I go ahead and {desc}? Please reply yes or no.",
        "remind": f"Still waiting on you: I'd like to {desc}.",
        "expired": f"The request to {desc} expired, so I did not do it.",
    }
    return lines.get(kind, lines["ask"])


class OriginatorApprovalNotifier:
    """The C0 plug for the A3 approval loop (Spec A6, T-seam) — persona-voiced ask/reconfirm/
    clarify/remind/expired on the task's conversation, via the real :class:`Originator`.

    Satisfies the resolver's ``ApprovalNotifier`` Protocol (each method takes only the proposal),
    so it resolves the persona tag (from ``proposal.persona_id``) and the conversation
    (``Task.conversation_id`` for ``proposal.task_id``) itself. Deterministic render (no model) —
    the same honest-template approach as :class:`OriginatorFailureNotifier`. A deleted persona
    (no tag) is a graceful no-op (nothing to voice); a task with no conversation starts a fresh one
    (the :class:`Originator` handles ``conversation_id=None``).
    """

    def __init__(
        self,
        *,
        rls_engine: Engine,
        episodic: EpisodicStore,
        edition: Edition,
        tasks: TaskStore,
    ) -> None:
        self._engine = rls_engine
        self._episodic = episodic
        self._edition = edition
        self._tasks = tasks

    async def ask(self, proposal: ActionProposal) -> None:
        await self._post("ask", proposal)

    async def reconfirm(self, proposal: ActionProposal) -> None:
        await self._post("reconfirm", proposal)

    async def clarify(self, proposal: ActionProposal) -> None:
        await self._post("clarify", proposal)

    async def remind(self, proposal: ActionProposal) -> None:
        await self._post("remind", proposal)

    async def expired(self, proposal: ActionProposal) -> None:
        await self._post("expired", proposal)

    async def _post(self, kind: str, proposal: ActionProposal) -> None:
        tag = resolve_persona_tag(self._engine, proposal.persona_id)
        if tag is None:  # deleted persona → nothing to voice
            _logger.info("approval notify skipped (no persona tag)", persona_id=proposal.persona_id)
            return
        try:
            conversation_id = self._tasks.get(proposal.owner_id, proposal.task_id).conversation_id
        except TaskNotFoundError:
            conversation_id = None
        await _originate_on_conversation(
            engine=self._engine,
            episodic=self._episodic,
            edition=self._edition,
            persona=tag,
            owner_id=proposal.owner_id,
            content=render_approval_message(kind, proposal),
            conversation_id=conversation_id or "",
            now=datetime.now(UTC),
        )


class OriginatorUpdateSender:
    """Deliver an A4 digest update as an originated C0 message (the ``UpdateSender`` plug).

    The production plug for :class:`~persona_api.tasks.updates.TaskUpdatePublisher`: a task's
    milestone progress reaches the user as a persona-voiced, name-tagged message on the contract's
    preferred channel (``channel`` feeds the router; ``None`` → home), persisted to the task's
    conversation. Granularity gating already happened in the publisher — this only delivers.
    """

    def __init__(
        self,
        *,
        rls_engine: Engine,
        memory_backend: Backend,
        edition: Edition,
        audit_root: Path,
        audit_logger: AuditLogger | None = None,
        sessions: LiveSessionRegistry | None = None,
    ) -> None:
        self._engine = rls_engine
        self._edition = edition
        self._sessions = sessions
        # R5-D-2: backend-selected audit when supplied (worker parity);
        # audit_root stays the byte-unchanged JSONL fallback.
        self._episodic = EpisodicStore(
            backend=memory_backend, audit_logger=audit_logger or JSONLAuditLogger(audit_root)
        )

    async def send(
        self,
        *,
        persona: PersonaIdentityTag,
        owner_id: str,
        content: str,
        channel: str | None,
        conversation_id: str | None,
        priority: MessagePriority,  # noqa: ARG002 — gating happened upstream; kept for the Protocol
        now: datetime,
    ) -> None:
        """Deliver the update on ``channel`` (or home), persisted to ``conversation_id``."""
        await _originate_on_conversation(
            engine=self._engine,
            episodic=self._episodic,
            edition=self._edition,
            persona=persona,
            owner_id=owner_id,
            content=content,
            conversation_id=conversation_id or "",
            now=now,
            channel=channel,
            sessions=self._sessions,
        )

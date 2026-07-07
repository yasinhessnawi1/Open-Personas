"""The ``delegated_turn`` A0 handler — voice's ask executed on the chat pipeline (Spec A9, T5).

Under the A9 delegation architecture voice never executes with the mid model: a confirmed spoken
task/schedule/autonomy ask crosses as a durable ``delegated_turn`` job (T4), and THIS handler runs
it through the **one audited path** — ``RuntimeFactory.build_conversation_loop`` on the frontier
tier (real tools / K4 / approvals / R7), the same ``ConversationLoop`` an interactive chat turn uses
(A9-D-7). The mid model never parses-for-execution; the frontier re-parses the **verbatim ask**.

The confirmation the chat flow would normally get from a typed "yes" was already given **by voice**
(the spoken echo + "yes", ``provenance=voice``). So the handler drives ``loop.turn(verbatim_ask)``
once — the full pipeline — and:

* if the frontier recognised a **standing intent** (it echoed a pending contract proposal), the
  handler creates it through the **unchanged** ``OriginationService.originate`` (the one create door
  chat uses) — voice's confirmation stands in for the chat "yes"; **no fake user turn**, loop.py
  untouched;
* if the ask was a **now-action**, the loop executed it inline (or blocked on an approval/budget
  gate).

The **outcome** (``succeeded`` / ``blocked_on_approval`` / ``failed``) is recorded durably as a
final assistant message on the call conversation — carrying ``delegation_outcome`` +
``provenance=voice`` + the ``delegation_key`` — BOTH the audit turn AND T6's grounded text (A9-D-7
condition 1: what was ACTUALLY done, never a replay of the echo; condition 2: a blocked turn hands
back the honest-incomplete line, never a fake done). A ``delegated_turn.execute`` ``audit_log`` row
(actor ``voice_delegated``, mirroring A10's ``user_via_ui``) records the consent-vs-execution trail.

The handler is idempotent (A0 at-least-once): the origination key is derived from the delegation key
(stable across worker retries), so a re-run converges on one task; the outcome message is keyed by
``delegation_key`` too. Registered in ``worker_root`` as the task-leg tenant's sibling.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

from persona.jobs import (
    DELEGATED_TURN_JOB_TYPE,
    LONG_LEASE,
    DelegatedTurnPayload,
    JobTypeSpec,
    RetryPolicy,
    delegated_turn_idempotency_key,
)
from persona.logging import get_logger
from persona.schema.conversation import Conversation, ConversationMessage
from persona_runtime.task_origination import ContractDraft, build_task_originated_event
from sqlalchemy import insert, select, text

from persona_api.db.models import conversations as conversations_t
from persona_api.db.models import messages as messages_t
from persona_api.services import audit_service
from persona_api.services.origination_service import OriginationService, OriginationStatus

if TYPE_CHECKING:
    from persona.jobs import JobContext, JobRegistry
    from sqlalchemy import Engine

    from persona_api.services.runtime_factory import RuntimeFactory
    from persona_api.services.task_steering_service import TaskSteeringService

__all__ = [
    "DelegatedTurnHandler",
    "DelegatedTurnOutcome",
    "register_delegated_turn_handler",
]

_log = get_logger("api.delegated_turn")

#: The audit-log actor for a voice delegation (mirrors A10's ``user_via_ui`` — A9-D-7 condition 3).
_AUDIT_ACTOR = "voice_delegated"
#: The message ``channel`` modality marking a delegated turn's outcome (attributable, distinct from
#: the V9 ``voice`` transcript turns on the same conversation — the concurrency guard).
_MODALITY = "voice_delegated"
#: Deterministic user-before-assistant ordering offset (mirrors the V9 transcript writer).
_ONE_MICROSECOND = timedelta(microseconds=1)


class DelegatedTurnOutcome(StrEnum):
    """The durable outcome of a delegated turn (A9-D-7 — T6 reads this to speak the hand-back)."""

    SUCCEEDED = "succeeded"
    BLOCKED_ON_APPROVAL = "blocked_on_approval"
    FAILED = "failed"


class DelegatedTurnHandler:
    """Runs a confirmed spoken ask through the frontier chat pipeline (A9-D-7).

    Args:
        runtime_factory: The app runtime factory — builds the SAME ``ConversationLoop`` an
            interactive chat turn uses (the one audited path; owner-scoped via the executor's
            ``current_user_id`` binding).
        origination_service: The unchanged A4 create door (idempotent + failure-visible).
        steering_service: The unchanged A4 steering door — applies a delegated pause/resume (T7).
        rls_engine: The owner-scoped engine (reads the executor's GUC) for loading the conversation,
            persisting the outcome message, and reading the persona name.
    """

    def __init__(
        self,
        *,
        runtime_factory: RuntimeFactory,
        origination_service: OriginationService,
        steering_service: TaskSteeringService,
        rls_engine: Engine,
    ) -> None:
        self._factory = runtime_factory
        self._origination = origination_service
        self._steering = steering_service
        self._engine = rls_engine

    async def handle(self, payload: DelegatedTurnPayload, context: JobContext) -> None:
        """Execute the delegated turn on the frontier + record the durable outcome (A9-D-7).

        The executor has already bound ``current_user_id`` to the job owner, so the loop's stores +
        the engine reads below are owner-scoped. At-least-once safe: origination is keyed on the
        delegation, and the outcome message is idempotent on ``delegation_key``.
        """
        owner_id = context.owner_id
        conversation_id = payload.conversation_id
        persona_id = payload.persona_id
        delegation_key = delegated_turn_idempotency_key(payload)

        outcome = DelegatedTurnOutcome.FAILED
        grounded = "I ran into a problem setting that up, so nothing was changed."
        try:
            loop = await self._factory.build_conversation_loop(persona_id)
            conversation = self._load_conversation(conversation_id, persona_id)

            events: list[dict[str, object]] = []
            reply: list[str] = []

            async def _on_event(event: object) -> None:
                data = getattr(event, "model_dump", None)
                events.append(data(mode="json") if callable(data) else {"type": str(event)})

            async for chunk in loop.turn(conversation, payload.verbatim_ask, _on_event):
                delta = getattr(chunk, "delta", None)
                if delta:
                    reply.append(delta)

            outcome, grounded = await self._resolve_outcome(
                conversation=conversation,
                events=events,
                reply_text="".join(reply).strip(),
                owner_id=owner_id,
                conversation_id=conversation_id,
                persona_id=persona_id,
                delegation_key=delegation_key,
            )
        except Exception as exc:  # noqa: BLE001 — a delegated-turn failure must be recorded, never crash the worker
            _log.error(
                "delegated turn failed conversation={cid}: {err}", cid=conversation_id, err=str(exc)
            )
            outcome = DelegatedTurnOutcome.FAILED

        self._record_outcome(
            owner_id=owner_id,
            conversation_id=conversation_id,
            persona_id=persona_id,
            delegation_key=delegation_key,
            outcome=outcome,
            grounded=grounded,
            verbatim_ask=payload.verbatim_ask,
        )

    async def _resolve_outcome(
        self,
        *,
        conversation: Conversation,
        events: list[dict[str, object]],
        reply_text: str,
        owner_id: str,
        conversation_id: str,
        persona_id: str,
        delegation_key: str,
    ) -> tuple[DelegatedTurnOutcome, str]:
        """Apply steering / create a standing intent / read a now-action — the delegated outcome."""
        # A blocked approval/budget gate ⇒ honest incomplete (A9-D-7 cond. 2), never a fake done.
        if _blocked_on_approval(events):
            name = self._persona_name(persona_id)
            return (
                DelegatedTurnOutcome.BLOCKED_ON_APPROVAL,
                f"I've started it — it needs your OK in your chat with {name}.",
            )
        # A9-T7: a spoken STEERING ask — the frontier emitted a ``task_steering`` (pause/resume)
        # that rides the SAME delegation crossing. Apply it through the unchanged steering door.
        steer = _steering_event(events)
        if steer is not None:
            await self._steering.steer(
                {
                    **steer,
                    "owner_id": owner_id,
                    "conversation_id": conversation_id,
                    "persona_id": persona_id,
                }
            )
            verb = str(steer.get("verb", "updated"))
            return DelegatedTurnOutcome.SUCCEEDED, f"Done — I've {_verb_past(verb)} that task."
        # A cancel / reschedule the frontier surfaced as a pending confirmation (destructive, so it
        # asks first). Voice is a poor medium for that confirm — hand back the honest-incomplete
        # line so the user resolves it in chat (A9-D-4 applied to destructive steering).
        if _pending_confirm(conversation):
            return (
                DelegatedTurnOutcome.BLOCKED_ON_APPROVAL,
                "I've teed that change up — confirm it in your chat with "
                f"{self._persona_name(persona_id)} and I'll apply it.",
            )
        # A standing intent: the frontier echoed a pending contract proposal. Voice already
        # confirmed it (provenance=voice), so create through the unchanged origination door.
        draft = _pending_contract_proposal(conversation)
        if draft is not None:
            event = build_task_originated_event(
                draft=draft,
                owner_id=owner_id,
                persona_id=persona_id,
                persona_name=self._persona_name(persona_id),
                conversation_id=conversation_id,
                # Stable anchor: keyed on the delegation, so a worker retry converges on one task.
                assistant_message_id=delegation_key,
            )
            result = await self._origination.originate(dict(event.data))
            if result.status in (OriginationStatus.CREATED, OriginationStatus.IDEMPOTENT):
                goal = result.task.contract.goal if result.task is not None else draft.goal
                return DelegatedTurnOutcome.SUCCEEDED, f"Done — I've set that up: {goal}."
            return (
                DelegatedTurnOutcome.FAILED,
                "I couldn't set that up just now, so nothing was scheduled.",
            )
        # A now-action executed inline — the loop's own reply is the grounded result.
        if reply_text:
            return DelegatedTurnOutcome.SUCCEEDED, reply_text
        return DelegatedTurnOutcome.SUCCEEDED, "Done."

    def _load_conversation(self, conversation_id: str, persona_id: str) -> Conversation:
        """Materialise the call's conversation from the DB (owner-scoped by the executor GUC)."""
        with self._engine.begin() as conn:
            conv = (
                conn.execute(select(conversations_t).where(conversations_t.c.id == conversation_id))
                .mappings()
                .first()
            )
            rows = (
                conn.execute(
                    select(messages_t)
                    .where(messages_t.c.conversation_id == conversation_id)
                    .order_by(messages_t.c.created_at.asc())
                )
                .mappings()
                .all()
            )
        messages = [
            ConversationMessage(
                role=str(r["role"]),  # type: ignore[arg-type]
                content=str(r["content"]),
                created_at=_aware(r["created_at"]),
            )
            for r in rows
            if str(r["role"]) in ("user", "assistant", "system")
        ]
        return Conversation(
            conversation_id=conversation_id,
            persona_id=str(conv["persona_id"]) if conv is not None else persona_id,
            messages=messages,
            compacted_summary=str(conv["compacted_summary"]) if conv is not None else "",
            compacted_up_to=int(conv["compacted_up_to"]) if conv is not None else 0,
        )

    def _record_outcome(
        self,
        *,
        owner_id: str,
        conversation_id: str,
        persona_id: str,
        delegation_key: str,
        outcome: DelegatedTurnOutcome,
        grounded: str,
        verbatim_ask: str,
    ) -> None:
        """Persist the outcome message (T6's grounded text) + the ``delegated_turn.execute`` row."""
        self._persist_outcome_message(
            conversation_id=conversation_id,
            content=grounded,
            outcome=outcome,
            delegation_key=delegation_key,
        )
        audit_service.record(
            engine=self._engine,
            user_id=owner_id,
            action="delegated_turn.execute",
            target=conversation_id,
            metadata={
                "actor": _AUDIT_ACTOR,
                "provenance": "voice",
                "persona_id": persona_id,
                "outcome": outcome.value,
                "delegation_key": delegation_key,
                "verbatim_ask": verbatim_ask[:500],
            },
        )

    def _persist_outcome_message(
        self,
        *,
        conversation_id: str,
        content: str,
        outcome: DelegatedTurnOutcome,
        delegation_key: str,
    ) -> None:
        """Append the final assistant outcome message (idempotent on ``delegation_key``).

        Distinct ``msg_{uuid4}`` id + a monotonic timestamp (the delegated turn runs after the voice
        turns; a per-write offset keeps ordering deterministic) + a ``voice_delegated`` modality on
        ``channel`` — so it never collides with the V9 ``VoiceTranscriptWriter`` turns on the same
        live conversation (distinct ids, ordered ts, attributable — the concurrency guard).
        """
        now = datetime.now(UTC)
        channel = {
            "modality": _MODALITY,
            "delegation_outcome": outcome.value,
            "provenance": "voice",
            "delegation_key": delegation_key,
        }
        with self._engine.begin() as conn:
            # Idempotent: skip if this delegation already recorded an outcome (worker retry).
            existing = conn.execute(
                text(
                    "SELECT 1 FROM messages WHERE conversation_id = :c "
                    "AND channel->>'delegation_key' = :k LIMIT 1"
                ),
                {"c": conversation_id, "k": delegation_key},
            ).first()
            if existing is not None:
                return
            conn.execute(
                insert(messages_t).values(
                    id=f"msg_{uuid.uuid4().hex}",
                    conversation_id=conversation_id,
                    role="assistant",
                    content=content,
                    created_at=now + _ONE_MICROSECOND,
                    channel=channel,
                )
            )

    def _persona_name(self, persona_id: str) -> str:
        """Best-effort persona display name from the ``personas`` row (fallback: the id)."""
        try:
            import yaml as _yaml

            with self._engine.begin() as conn:
                row = (
                    conn.execute(text("SELECT yaml FROM personas WHERE id = :p"), {"p": persona_id})
                    .mappings()
                    .first()
                )
            if row is not None:
                raw = _yaml.safe_load(str(row["yaml"])) or {}
                name = (raw.get("identity") or {}).get("name")
                if isinstance(name, str) and name:
                    return name
        except Exception:  # noqa: BLE001 — a name lookup must never break the delegated turn
            _log.warning("delegated-turn persona name lookup failed persona={p}", p=persona_id)
        return persona_id


def _pending_contract_proposal(conversation: Conversation) -> ContractDraft | None:
    """The pending A4 contract proposal on the last assistant turn, if the frontier echoed one.

    Mirrors the chat loop's ``_pending_contract_draft`` read (the ``contract_proposal`` metadata the
    contract gate sets), decoupled so this handler does not import a private loop helper.
    """
    for message in reversed(conversation.messages):
        if message.role != "assistant":
            continue
        raw = message.metadata.get("contract_proposal")
        return ContractDraft.model_validate_json(raw) if isinstance(raw, str) else None
    return None


def _blocked_on_approval(events: list[dict[str, object]]) -> bool:
    """Whether the turn blocked on an approval / budget gate (A3/R7 → honest incomplete)."""
    for event in events:
        if event.get("type") == "asking_user":
            return True
        data = event.get("data")
        if isinstance(data, dict) and data.get("status") == "awaiting_approval":
            return True
    return False


def _steering_event(events: list[dict[str, object]]) -> dict[str, object] | None:
    """The ``task_steering`` event data (verb + task_id) the frontier emitted, if any (A9-T7)."""
    for event in events:
        if event.get("type") == "task_steering":
            data = event.get("data")
            if isinstance(data, dict):
                return dict(data)
    return None


def _pending_confirm(conversation: Conversation) -> bool:
    """Whether the frontier left a destructive steering confirm pending (cancel / reschedule).

    A cancel asks a consequence-aware confirmation (``cancel_proposal``) and a reschedule re-echoes
    the new clause (``reschedule_proposal``) before applying — both need a "yes" the headless
    delegated turn does not supply, so they hand back the honest-incomplete line (A9-T7).
    """
    for message in reversed(conversation.messages):
        if message.role != "assistant":
            continue
        return bool(
            message.metadata.get("cancel_proposal") or message.metadata.get("reschedule_proposal")
        )
    return False


def _verb_past(verb: str) -> str:
    """A spoken past-tense for a steering verb (``pause`` → ``paused``)."""
    return {"pause": "paused", "resume": "resumed", "cancel": "cancelled"}.get(verb, "updated")


def _aware(value: object) -> datetime:
    """Coerce a DB timestamp to tz-aware UTC (community SQLite returns naive)."""
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return datetime.now(UTC)


def register_delegated_turn_handler(
    registry: JobRegistry,
    *,
    runtime_factory: RuntimeFactory,
    origination_service: OriginationService,
    steering_service: TaskSteeringService,
    rls_engine: Engine,
) -> None:
    """Register the ``delegated_turn`` tenant (A9-D-5/D-7) — voice's ask on the chat pipeline."""
    registry.register(
        JobTypeSpec(
            type=DELEGATED_TURN_JOB_TYPE,
            payload_model=DelegatedTurnPayload,
            handler=DelegatedTurnHandler(
                runtime_factory=runtime_factory,
                origination_service=origination_service,
                steering_service=steering_service,
                rls_engine=rls_engine,
            ),
            idempotency_key=delegated_turn_idempotency_key,
            retry=RetryPolicy(max_attempts=3),
            lease=LONG_LEASE,
        )
    )

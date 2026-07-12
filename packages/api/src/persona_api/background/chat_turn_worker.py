"""Detached chat-turn worker + in-process event bus (spec P1, T1+T2b, D-P1-detached-execution).

A chat turn executes as a detached ``asyncio.Task`` so a client disconnect
(navigate / reload / tab close) **no longer cancels it** — the persona keeps
working until it decides to stop. Mirrors ``RunRegistry`` (``run_worker.py``)
1:1; the chat-shaped differences:

- the live queue carries ``("event", RunEvent)`` / ``("chunk", StreamChunk)`` /
  ``("done", payload)`` / ``("error", payload)`` items + a ``None`` sentinel, so
  the SSE tail interleaves tool events and text deltas in true emission order and
  ends with the same ``done`` payload shape the old inline ``stream_chat`` sent;
- the ``tier`` event is **captured into the ``done`` payload** (not a frame),
  matching the inline contract (the router's real choice rides ``done``);
- there is **no** ``responses`` queue (no ask-user) and **no** ``CancelToken``
  (``ConversationLoop.turn`` exposes none — an explicit cancel is a task-level
  ``cancel()``, D-P1-cancel);
- the registry is keyed by **``conversation_id``** (one-active-turn — D-P1-one-active-turn).

Persistence is the injected :class:`ChatTurnSink` (``messages``-backed —
``MessagesTurnSink``). Checkpoints are **throttled** (D-P1-cadence): a tool event
flushes immediately; text deltas debounce by char-count / wall-time (never
per-token). On CLEAN completion the worker finalizes, **deducts credits** (the
D-08-6 revision — bill regardless of client presence, D-P1-billing-contract),
runs the best-effort ``on_complete`` hook (auto-title), and emits ``done``;
cancel/error finalize the partial WITHOUT billing. In-process + single-worker
(D-08-5); a restart loses the task — the checkpointed partial remains, reconciled
to ``interrupted`` by the startup sweep (D-P1-restart-sweep).
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import time
from decimal import ROUND_CEILING, Decimal
from typing import TYPE_CHECKING, Protocol

from persona.logging import get_logger

from persona_api.errors import CreditsExhaustedError, TurnAlreadyActiveError
from persona_api.middleware.rls_context import current_user_id
from persona_api.sandbox import (
    SandboxRequestContext,
    reset_sandbox_request_context,
    set_sandbox_request_context,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping
    from typing import Any

    from persona.backends import StreamChunk
    from persona.schema.conversation import Conversation
    from persona_runtime.agentic.events import RunEvent
    from persona_runtime.loop import ConversationLoop
    from sqlalchemy import Engine

    from persona_api.editions.credits_policy import CreditsPolicy
    from persona_api.initiative.verb_service import InitiativeVerbService
    from persona_api.jobs.queue import JobQueue
    from persona_api.services.origination_service import OriginationService
    from persona_api.services.task_reschedule_service import TaskRescheduleService
    from persona_api.services.task_steering_service import TaskSteeringService

_log = get_logger("api.chat_turn_worker")

__all__ = ["ChatTurnHandle", "ChatTurnRegistry", "ChatTurnSink"]

# Throttle knobs (D-P1-cadence): flush a text checkpoint at most this often.
# Never per-token; a tool event always flushes immediately regardless.
_CHECKPOINT_CHAR_THRESHOLD = 256
_CHECKPOINT_INTERVAL_S = 1.0


class ChatTurnSink(Protocol):
    """The persistence seam the worker calls (``MessagesTurnSink`` is the impl).

    Both methods are **sync** (the worker runs them inline, like ``RunRegistry``'s
    ``_persist_*``). ``finalize``'s ``status`` ∈ {complete, cancelled, error}.
    """

    def checkpoint(
        self,
        *,
        conversation_id: str,
        assistant_message_id: str,
        content: str,
        events: list[dict[str, object]],
    ) -> None:
        """Persist the in-progress partial (throttled cadence is the worker's call)."""
        ...

    def finalize(
        self,
        *,
        conversation_id: str,
        assistant_message_id: str,
        conversation: Conversation,
        status: str,
        content: str,
        events: list[dict[str, object]],
        tier: str | None = None,
    ) -> None:
        """Write the terminal turn (``complete`` finalizes + updates conversation state)."""
        ...


class ChatTurnHandle:
    """The in-process state of one running chat turn (one per conversation)."""

    def __init__(self, conversation_id: str, owner_id: str, assistant_message_id: str) -> None:
        self.conversation_id = conversation_id
        self.owner_id = owner_id
        self.assistant_message_id = assistant_message_id
        #: Live tail: events / chunks / done / error in emission order, ``None``-terminated.
        self.events: asyncio.Queue[tuple[str, object] | None] = asyncio.Queue()
        self.task: asyncio.Task[None] | None = None
        #: Spec R7 (R7-D-4): the durable long-op concurrency slot token held for this
        #: turn's lifetime. Reserved in ``start_chat_turn`` (pre-persist) and released
        #: in ``_run_turn``'s terminal ``finally`` — so a client disconnect (the turn
        #: keeps running, detached) never frees the slot early, and a crash/error/cancel
        #: still releases it. ``None`` = no cap wired (community / uncapped).
        self.op_token: str | None = None
        #: Accumulating partial response (the checkpoint source of truth).
        self._content: list[str] = []
        self.event_log: list[dict[str, object]] = []
        #: Set by :meth:`ChatTurnRegistry.request_cancel` to distinguish an
        #: explicit user cancel (→ finalize ``cancelled``) from a shutdown cancel.
        self.cancel_requested = False
        # Throttle accounting (D-P1-cadence).
        self._chars_since_flush = 0
        self._last_flush = 0.0

    @property
    def content(self) -> str:
        return "".join(self._content)


class ChatTurnRegistry:
    """App-scoped registry of in-flight chat turns. Single-worker, in-process (D-08-5).

    Keyed by ``conversation_id`` — the one-active-turn invariant (D-P1-one-active-turn).
    ``credits_policy`` (+ ``rls_engine`` for its scope) wires the deduct on the
    detached completion path (D-P1-billing-contract); ``None`` → no billing
    (the unit-test / community-unmetered shape).
    """

    def __init__(
        self,
        *,
        sink: ChatTurnSink,
        rls_engine: Engine | None = None,
        credits_policy: CreditsPolicy | None = None,
        credits_per_turn: int = 1,
        proportional_credits: bool = True,
        job_queue: JobQueue | None = None,
        origination_service: OriginationService | None = None,
        task_steering_service: TaskSteeringService | None = None,
        task_reschedule_service: TaskRescheduleService | None = None,
        initiative_verb_service: InitiativeVerbService | None = None,
    ) -> None:
        self._sink = sink
        self._engine = rls_engine
        self._credits_policy = credits_policy
        self._credits_per_turn = credits_per_turn
        # Spec M2 (D-M2-5): proportional billing — max(floor, ceil(cost_cents))
        # at 1 credit = 1¢, computed in ``_turn_charge`` from the loop's
        # recorded turn cost. False = the pre-M2 flat charge (the approved
        # rollback hatch, PERSONA_API_PROPORTIONAL_CREDITS).
        self._proportional_credits = proportional_credits
        # Spec K2 (T8d): off-critical-path synthesis enqueue at the turn boundary.
        # Relocated from the old inline ``stream_turn`` to the detached worker's
        # clean-completion path; ``None`` → no-op (D-K2-2).
        self._job_queue = job_queue
        # Spec A4 (A4-D-X): on a confirm turn the loop emits a ``task_originated`` event;
        # this service creates the A2 task + A1 schedule on the clean-completion path.
        # ``None`` → no-op (the unmetered/unit-test shape).
        self._origination_service = origination_service
        # Spec A4 (T9b): apply a conversational steering verb (pause/resume/cancel) to a live task.
        self._task_steering_service = task_steering_service
        # Spec A8 (T6): apply a user-confirmed conversational reschedule through the CAS door.
        self._task_reschedule_service = task_reschedule_service
        # Spec A5 (T10): applies the initiative-verb family (dial + ledger-anchored
        # confirm/decline). None → the events are ignored (the pre-activation posture).
        self._initiative_verb_service = initiative_verb_service
        self._handles: dict[str, ChatTurnHandle] = {}

    def get(self, conversation_id: str) -> ChatTurnHandle | None:
        return self._handles.get(conversation_id)

    def start(
        self,
        *,
        conversation_id: str,
        owner_id: str,
        assistant_message_id: str,
        loop: ConversationLoop,
        conversation: Conversation,
        user_message: str,
        on_complete: Callable[[], Awaitable[None]] | None = None,
        op_token: str | None = None,
        **turn_kwargs: object,
    ) -> ChatTurnHandle:
        """Create a handle and launch the turn as a detached ``asyncio.Task``.

        ``on_complete`` is an optional best-effort async hook run AFTER a clean
        completion (e.g. auto-title); its failure never affects the turn. ``op_token``
        (R7-D-4) is the durable long-op concurrency slot reserved by the caller
        pre-persist; the worker releases it in its terminal ``finally``. Raises
        :class:`TurnAlreadyActiveError` if a turn is already running for this
        conversation (block, don't queue — D-P1-one-active-turn).
        """
        if conversation_id in self._handles:
            raise TurnAlreadyActiveError(
                "a turn is already running for this conversation",
                context={"conversation_id": conversation_id},
            )
        handle = ChatTurnHandle(conversation_id, owner_id, assistant_message_id)
        handle.op_token = op_token
        self._handles[conversation_id] = handle
        handle.task = asyncio.create_task(
            self._run_turn(handle, loop, conversation, user_message, on_complete, turn_kwargs)
        )
        return handle

    def request_cancel(self, conversation_id: str) -> bool:
        """Explicit user cancel (mirrors ``/runs/{id}/cancel``): flag + cancel the task.

        Returns ``True`` if a live turn was cancelled. The flag tells ``_run_turn``
        to finalize ``cancelled`` (vs a shutdown cancel, which does not finalize).
        """
        handle = self._handles.get(conversation_id)
        if handle is None or handle.task is None or handle.task.done():
            return False
        handle.cancel_requested = True
        handle.task.cancel()
        return True

    async def _run_turn(
        self,
        handle: ChatTurnHandle,
        loop: ConversationLoop,
        conversation: Conversation,
        user_message: str,
        on_complete: Callable[[], Awaitable[None]] | None,
        turn_kwargs: dict[str, object],
    ) -> None:
        """Drive the loop, stream to the queue, checkpoint, finalize, bill, end the stream.

        Runs OUTSIDE any request scope, so it binds the owner's RLS contextvar
        (else the checkpoint/finalize/deduct writes scope to '' and affect 0 rows)
        and a per-conversation sandbox context for the file/code tools — the
        ``run_worker._run`` discipline verbatim. Both are reset in ``finally``.
        """
        token = current_user_id.set(handle.owner_id)
        sandbox_token = set_sandbox_request_context(
            SandboxRequestContext(owner_id=handle.owner_id, conversation_id=handle.conversation_id)
        )
        handle._last_flush = time.monotonic()
        # R9-020: snapshot the PRE-turn message count now — the loop appends this
        # turn's user+assistant pair to ``conversation.messages`` while it runs, so
        # a post-loop ``len()`` is the new total, not the prior one. The durable
        # truth after a clean completion is exactly ``prior + 2`` (open_turn's two
        # rows, finalized), which is what the title-refresh crossing window needs.
        prior_message_count = len(conversation.messages)
        tier = "frontier"  # fallback; replaced by the router's real choice (tier event)
        routing: dict[str, object] | None = None
        last_chunk: StreamChunk | None = None
        error_message: str | None = None
        originated: Mapping[str, Any] | None = None
        steered: Mapping[str, Any] | None = None
        rescheduled: Mapping[str, Any] | None = None
        initiative_verbed: Mapping[str, Any] | None = None

        async def _on_event(event: RunEvent) -> None:
            nonlocal tier, routing, originated, steered, rescheduled, initiative_verbed
            if event.type == "tier":
                # The router's tier choice rides the terminal `done` payload — it
                # is NOT a frame and NOT in the persisted event-log (it lives on
                # the `tier_used` column via finalize).
                tier = str(event.data.get("tier", tier))
                routing = event.data.get("routing")  # Spec 31; may be None
                return
            if event.type == "task_originated":
                # Spec A4: an internal create-instruction, not an SSE frame — captured here,
                # acted on the clean-completion path (after the confirm turn is persisted). The
                # runtime loop knows neither the tenant nor the message id; the worker injects
                # both from its handle (owner_id = RLS scope; assistant_message_id = the primary
                # idempotency anchor — this confirm turn's stable id).
                originated = {
                    **event.data,
                    "owner_id": handle.owner_id,
                    "assistant_message_id": handle.assistant_message_id,
                }
                return
            if event.type == "task_steering":
                # Spec A4 (T9b): an internal pause/resume/cancel instruction — the worker injects
                # the tenant + conversation/persona (so a failed cancel can surface an
                # un-suppressible account on this conversation) and applies it on completion.
                steered = {
                    **event.data,
                    "owner_id": handle.owner_id,
                    "conversation_id": handle.conversation_id,
                    "persona_id": conversation.persona_id,
                }
                return
            if event.type == "initiative_verb":
                # Spec A5 (T10): a dial/confirm/decline instruction — the worker injects the
                # tenant + persona and applies it on the clean-completion path.
                initiative_verbed = {
                    **event.data,
                    "owner_id": handle.owner_id,
                    "persona_id": conversation.persona_id,
                }
                return
            if event.type == "task_rescheduled":
                # Spec A8 (T6): a user-confirmed conversational reschedule — the worker injects the
                # tenant and applies it through the CAS door on the clean-completion path.
                rescheduled = {**event.data, "owner_id": handle.owner_id}
                return
            handle.event_log.append(event.model_dump(mode="json"))
            await handle.events.put(("event", event))
            self._checkpoint(handle, force=True)  # tool events flush immediately

        status = "complete"
        try:
            try:
                # ``turn_kwargs`` (turn_has_image / images / documents /
                # document_context) is built + validated by ``start_chat_turn`` and
                # forwarded opaquely; the worker stays loop-signature-agnostic.
                async for chunk in loop.turn(conversation, user_message, _on_event, **turn_kwargs):  # type: ignore[arg-type]
                    last_chunk = chunk
                    if chunk.delta:
                        handle._content.append(chunk.delta)
                        handle.event_log.append({"kind": "text", "delta": chunk.delta})
                        handle._chars_since_flush += len(chunk.delta)
                    await handle.events.put(("chunk", chunk))
                    self._checkpoint(handle, force=False)  # throttled
            except asyncio.CancelledError:
                # Explicit user cancel → finalize the partial as ``cancelled`` (no
                # bill). A shutdown cancel (aclose) is NOT a user cancel: re-raise
                # so the task ends without a terminal write.
                if not handle.cancel_requested:
                    raise
                status = "cancelled"
            except Exception as exc:  # noqa: BLE001 — a background task must never crash silently
                _log.error(
                    "chat turn {cid} failed: {err}", cid=handle.conversation_id, err=str(exc)
                )
                status = "error"
                error_message = str(exc)

            self._sink.finalize(
                conversation_id=handle.conversation_id,
                assistant_message_id=handle.assistant_message_id,
                conversation=conversation,
                status=status,
                content=handle.content,
                events=handle.event_log,
                tier=tier,
            )
            if status == "complete":
                self._deduct(handle, loop)
                self._enqueue_synthesis(handle, conversation)
                self._enqueue_title_refresh(handle, prior_message_count)
                await self._originate_task(originated)
                await self._apply_steering(steered)
                await self._apply_reschedule(rescheduled)
                await self._apply_initiative_verb(initiative_verbed)
                await self._run_on_complete(on_complete, handle)
                await handle.events.put(
                    ("done", self._done_payload(loop, last_chunk, tier, routing))
                )
            elif status == "error":
                await handle.events.put(
                    ("error", {"error": "turn_failed", "message": error_message or "turn failed"})
                )
        finally:
            reset_sandbox_request_context(sandbox_token)
            # Spec R7 (R7-D-4): free the durable long-op slot on EVERY terminal path
            # (clean / cancel / error / shutdown) so the per-user cap doesn't ratchet
            # shut. Best-effort — a release failure must never crash the worker teardown
            # (the TTL staleness sweep in ``admit_long_op`` is the crash-safety backstop).
            self._release_op_slot(handle)
            current_user_id.reset(token)
            self._handles.pop(handle.conversation_id, None)
            await handle.events.put(None)  # end-of-stream sentinel for the SSE tail

    def _release_op_slot(self, handle: ChatTurnHandle) -> None:
        if self._engine is None or handle.op_token is None:
            return
        from persona.concurrency import release_long_op  # noqa: PLC0415

        try:
            release_long_op(rls_engine=self._engine, user_id=handle.owner_id, op_id=handle.op_token)
        except Exception as exc:  # noqa: BLE001 — teardown must never crash
            _log.warning(
                "chat turn concurrency-slot release failed cid={cid}: {err}",
                cid=handle.conversation_id,
                err=str(exc),
            )

    def _checkpoint(self, handle: ChatTurnHandle, *, force: bool) -> None:
        """Persist the partial — immediately when ``force`` (tool event), else throttled."""
        now = time.monotonic()
        if (
            not force
            and handle._chars_since_flush < _CHECKPOINT_CHAR_THRESHOLD
            and (now - handle._last_flush) < _CHECKPOINT_INTERVAL_S
        ):
            return
        self._sink.checkpoint(
            conversation_id=handle.conversation_id,
            assistant_message_id=handle.assistant_message_id,
            content=handle.content,
            events=handle.event_log,
        )
        handle._chars_since_flush = 0
        handle._last_flush = now

    def _turn_charge(self, loop: ConversationLoop) -> tuple[int, str]:
        """The credits amount + ledger reason for one completed turn (Spec M2, D-M2-5).

        PROPORTIONAL (owner-ruled): ``max(credits_per_turn, ceil(cost_cents))``
        at 1 credit = 1 cent, where ``cost_cents`` is the loop's recorded cost
        for the just-completed turn (``last_turn_cost_cents`` — the SAME number
        the TurnLog row persisted, actual or estimate per ``cost_basis``).
        The flat floor arms:

        * kill-switch OFF (``PERSONA_API_PROPORTIONAL_CREDITS=false``) — the
          pre-M2 flat charge, byte-identical (the rollback hatch);
        * ``unpriced`` basis / no recorded turn (legacy loops, the R1 bypass
          path) — a priceless turn is never guessed at, it costs the floor;
        * defensively, a non-finite recorded number (money path: never let a
          bad float overcharge).

        The ceil is ``Decimal(str(...))``-based: it ceils the number as
        PRINTED, so a genuinely-fractional cost (``1.000001`` → 2) rounds up
        per ceil semantics while binary-float representation noise can never
        manufacture an extra credit. The ledger reason carries the basis
        (``"chat_turn:<basis>"``) so audit rows record amount AND provenance
        (the R7 constraint); flat-floor charges keep the bare ``"chat_turn"``.
        """
        if not self._proportional_credits:
            return self._credits_per_turn, "chat_turn"
        cost = getattr(loop, "last_turn_cost_cents", None)
        basis = getattr(loop, "last_turn_cost_basis", None)
        if (
            not isinstance(cost, (int, float))
            or not math.isfinite(cost)
            or not isinstance(basis, str)
            or basis == "unpriced"
        ):
            return self._credits_per_turn, "chat_turn"
        ceiled = int(Decimal(str(cost)).to_integral_value(rounding=ROUND_CEILING))
        return max(self._credits_per_turn, ceiled), f"chat_turn:{basis}"

    def _deduct(self, handle: ChatTurnHandle, loop: ConversationLoop) -> None:
        """Bill one turn on clean completion (D-P1-billing-contract; D-08-6 revision).

        Fires regardless of client presence (the turn ran), via the owner's bound
        RLS scope. ``None`` policy/engine → no billing (unit / community-unmetered).

        Spec M2 (D-M2-5): the amount is PROPORTIONAL to the turn's recorded
        cost (``_turn_charge`` — floor ``credits_per_turn``, 1 credit = 1¢),
        read from the loop the worker just drove (the ``budget_snapshot``
        loop-owned-state precedent). ``MeteredCreditsPolicy`` is UNTOUCHED —
        it was always amount-agnostic, and the R7 day-cap books this same
        amount atomically inside the core deduct by construction.

        Spec R2 F-04: the decrement is a conditional atomic floor that raises
        :class:`CreditsExhaustedError` rather than driving the balance negative.
        This is **post-success** billing of an already-completed turn, so an
        exhausted balance must NOT discard the work — the floor already kept the
        balance >= 0; we log and let the turn finish (``done`` + synthesis). The
        pre-flight :func:`require_credits` gate still refuses the NEXT turn.
        """
        if self._credits_policy is None or self._engine is None:
            return
        amount, reason = self._turn_charge(loop)
        try:
            self._credits_policy.deduct(
                rls_engine=self._engine,
                user_id=handle.owner_id,
                amount=amount,
                reason=reason,
            )
        except CreditsExhaustedError:
            _log.warning(
                "post-turn billing skipped: insufficient credits to bill the completed turn "
                "(balance floored at 0); owner={owner} conversation={conv} amount={amount}",
                owner=handle.owner_id,
                conv=handle.conversation_id,
                amount=amount,
            )

    def _enqueue_synthesis(self, handle: ChatTurnHandle, conversation: Conversation) -> None:
        """Enqueue off-critical-path conversation synthesis at the turn boundary (K2 T8d).

        Relocated from the old inline ``stream_turn`` persist-after-final block to
        the detached worker's clean-completion path (D-P1-detached-execution): the
        K2 turn-end trigger still fires exactly once per completed turn, now
        regardless of client presence. Additive + no-op without a queue; the
        durable job re-reads the marker and synthesises the delta (D-K2-2). NEVER
        blocks/affects the reply already streamed.

        ``message_count`` scopes the idempotency key: ``conversation.messages`` is
        the prior count (loaded before ``open_turn`` appended the turn), so the
        completed turn is ``+1`` — mirroring the old ``prior_msg_count + 1``.
        """
        from persona_api.services.synthesis_trigger import (  # noqa: PLC0415
            enqueue_conversation_synthesis,
        )

        enqueue_conversation_synthesis(
            self._job_queue,
            owner_id=handle.owner_id,
            conversation_id=handle.conversation_id,
            persona_id=conversation.persona_id,
            message_count=len(conversation.messages) + 1,
        )

    def _enqueue_title_refresh(self, handle: ChatTurnHandle, prior_message_count: int) -> None:
        """Enqueue a whole-conversation title refresh at threshold crossings (R9-020).

        Same clean-completion boundary as synthesis; additive + no-op without a
        queue. ``prior_message_count`` is the pre-turn snapshot from ``_run_turn``
        (taken before the loop mutated the conversation), so the completed turn
        grew the durable total ``prior → prior + 2`` — the crossing window the
        trigger evaluates against ``{4, 10, 24, 50, 100}``. NEVER blocks/affects
        the reply already streamed.
        """
        from persona_api.services.title_trigger import (  # noqa: PLC0415
            enqueue_conversation_title_refresh,
        )

        enqueue_conversation_title_refresh(
            self._job_queue,
            owner_id=handle.owner_id,
            conversation_id=handle.conversation_id,
            previous_count=prior_message_count,
            new_count=prior_message_count + 2,
        )

    async def _run_on_complete(
        self, on_complete: Callable[[], Awaitable[None]] | None, handle: ChatTurnHandle
    ) -> None:
        """Best-effort post-completion hook (auto-title). Never fails the turn."""
        if on_complete is None:
            return
        try:
            await on_complete()
        except Exception as exc:  # noqa: BLE001 — the hook (auto-title) is best-effort
            _log.warning(
                "chat turn on_complete hook failed cid={cid}: {err}",
                cid=handle.conversation_id,
                err=str(exc),
            )

    async def _originate_task(self, originated: Mapping[str, Any] | None) -> None:
        """Spec A4: create the confirmed standing task (idempotent + failure-visible).

        The service owns the invariants (idempotency, failure-visibility); this is the thin
        clean-completion call. ``None`` event or unconfigured service → no-op. The service does
        not raise on a create failure (it surfaces a FAILURE-class account instead), so a stray
        exception here is a bug we log rather than letting it crash the turn's terminal events.
        """
        if originated is None or self._origination_service is None:
            return
        try:
            await self._origination_service.originate(originated)
        except Exception as exc:  # noqa: BLE001 — the service self-reports failures; never crash the turn
            _log.error("task origination raised unexpectedly: {err}", err=str(exc))

    async def _apply_initiative_verb(self, verbed: Mapping[str, Any] | None) -> None:
        """Spec A5 (T10): apply a captured initiative verb (dial / confirm / decline)."""
        if verbed is None or self._initiative_verb_service is None:
            return
        try:
            await self._initiative_verb_service.apply(
                owner_id=str(verbed["owner_id"]),
                persona_id=str(verbed["persona_id"]),
                verb=str(verbed["verb"]),
                notice_id=(str(verbed["notice_id"]) if verbed.get("notice_id") else None),
            )
        except Exception as exc:  # noqa: BLE001 — a verb miss must not crash the turn
            _log.warning("initiative verb application failed: {err}", err=str(exc))

    async def _apply_steering(self, steered: Mapping[str, Any] | None) -> None:
        """Spec A4 (T9b): apply a captured pause/resume/cancel to the live task (best-effort)."""
        if steered is None or self._task_steering_service is None:
            return
        try:
            await self._task_steering_service.steer(steered)
        except Exception as exc:  # noqa: BLE001 — a steering miss (e.g. already terminal) must not crash the turn
            _log.warning("task steering failed: {err}", err=str(exc))

    async def _apply_reschedule(self, rescheduled: Mapping[str, Any] | None) -> None:
        """Spec A8 (T6): apply a user-confirmed reschedule through the CAS door (best-effort).

        The service is sync (a fast CAS update + audit); run it off the event loop. It self-guards
        a missing task/schedule as a logged no-op, so a stray exception here is only logged.
        """
        if rescheduled is None or self._task_reschedule_service is None:
            return
        try:
            await asyncio.to_thread(self._task_reschedule_service.reschedule, rescheduled)
        except Exception as exc:  # noqa: BLE001 — a reschedule miss must not crash the turn
            _log.warning("task reschedule failed: {err}", err=str(exc))

    @staticmethod
    def _done_payload(
        loop: ConversationLoop,
        last_chunk: StreamChunk | None,
        tier: str,
        routing: dict[str, object] | None,
    ) -> dict[str, object]:
        """Build the terminal ``done`` payload (parity with the old inline stream_chat)."""
        usage = last_chunk.usage if last_chunk is not None else None
        done: dict[str, object] = {
            "usage": (
                {"prompt_tokens": usage.prompt_tokens, "completion_tokens": usage.completion_tokens}
                if usage is not None
                else {}
            ),
            "tier": tier,
            "format_hints": {},  # D-08-3: the API echoes empty; connectors populate
        }
        if routing is not None:
            done["routing"] = routing
        snapshot_fn = getattr(loop, "budget_snapshot", None)
        budget = snapshot_fn() if callable(snapshot_fn) else None
        if budget is not None:
            done["budget"] = budget
        return done

    async def aclose(self) -> None:
        """Cancel all in-flight turn tasks on shutdown (D-08-5: lost, but checkpointed).

        Shutdown cancellation is NOT a user cancel — the tasks end without a
        terminal finalize; the startup sweep (D-P1-restart-sweep) reconciles the
        ``running`` rows to ``interrupted`` on next boot.
        """
        for handle in list(self._handles.values()):
            if handle.task is not None and not handle.task.done():
                handle.task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await handle.task

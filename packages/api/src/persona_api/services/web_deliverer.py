"""The web-app deliverer for originated messages (Spec C0, T5).

:class:`WebAppDeliverer` is the first concrete implementation of persona-core's
``MessageDeliverer`` boundary (D-C0-1 / criterion 3) — the proof that the
one-pipe-many-deliverers seam is real before C1's connectors land. It honours the
**no-push-broker** scope seam (D-C0-X-no-push-broker):

* **Within-runtime (criterion 7):** when the origination happens inside a live run
  whose SSE stream the owner already has open, the message is pushed **inline onto
  that run's own open stream** → :data:`DeliveryOutcome.DELIVERED`. The
  :class:`LiveSessionRegistry` is the lookup for currently-open live run streams —
  in-process, request-scoped, populated/cleared by the run lifecycle (wired in a
  later task). It is **NOT a push broker**: it can only push onto a stream the
  client *already* holds open, never to an idle client.
* **No open session:** the recorder (T4) has **already durably persisted** the
  message into the conversation, so it is **saved and reachable** → this returns
  :data:`DeliveryOutcome.PENDING` (never ``FAILED`` — nothing is dropped, D-C0-4)
  and records the outcome via the api audit log (D-C0-5). "Saved and reachable" is
  the whole claim: since R9-120 this deliverer is also the fallback for a connector
  channel that did not take, and "present on next open" quietly assumed a reader who
  comes back to the conversation, which a chat-app user has no reason to do.

A general out-of-band push to a *not-currently-streaming* client is deliberately
out of scope here — that needs durable push infrastructure and is connector (C1) /
direction-4 territory. This module is where that boundary is made explicit + logged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from persona.delivery import DeliveryOutcome, DeliveryResult

from persona_api.services import audit_service

if TYPE_CHECKING:
    from collections.abc import Callable

    from persona.schema.origination import OriginatedMessage
    from sqlalchemy import Engine

__all__ = [
    "LiveSessionRegistry",
    "LiveSessionSink",
    "WebAppDeliverer",
]


@runtime_checkable
class LiveSessionSink(Protocol):
    """An open live stream the owner is currently connected to (a run's SSE sink)."""

    async def push(self, message: OriginatedMessage) -> None:
        """Push the originated message onto this already-open stream."""
        ...


@runtime_checkable
class LiveSessionRegistry(Protocol):
    """Lookup for currently-open live run streams (NOT a durable push channel).

    Returns the open sink for the message's owner/conversation if one exists right
    now, else ``None`` (→ present-on-next-open). Populated/cleared by the run
    lifecycle in a later task; here it is a seam the deliverer consults.
    """

    def lookup(self, message: OriginatedMessage) -> LiveSessionSink | None:
        """Return the owner's currently-open sink for this message, or ``None``."""
        ...


class WebAppDeliverer:
    """Deliver an originated message to the web app (the first ``MessageDeliverer``).

    Args:
        rls_engine: The per-app engine the audit write runs on (``audit_log`` is
            not RLS-scoped — it records ``user_id`` explicitly).
        sessions: Lookup for currently-open live run streams (the no-push-broker
            seam — only an already-open stream is delivered to inline).
        record: The audit sink (injected for testability; defaults to the api
            audit service). Records the delivery outcome (D-C0-5).
    """

    _CHANNEL = "web"

    def __init__(
        self,
        *,
        rls_engine: Engine,
        sessions: LiveSessionRegistry,
        record: Callable[..., None] = audit_service.record,
    ) -> None:
        self._engine = rls_engine
        self._sessions = sessions
        self._record = record

    async def deliver(self, message: OriginatedMessage) -> DeliveryResult:
        """Deliver inline on an open live stream if one exists, else mark pending.

        Never raises for the no-session case and never returns ``FAILED`` here:
        an undeliverable originated message is already persisted (T4) and present
        on next open — :data:`DeliveryOutcome.PENDING`, recorded, not dropped.
        """
        sink = self._sessions.lookup(message)
        if sink is not None:
            await sink.push(message)
            return self._record_outcome(
                message,
                DeliveryOutcome.DELIVERED,
                detail="inline on the open live run stream",
            )
        return self._record_outcome(
            message,
            DeliveryOutcome.PENDING,
            # Says what our RECORDS are, never what the person will do. The old text was
            # "present on next open", which is true of a web conversation people return to
            # and close to untrue once this is the fallback for a chat app nobody reopens.
            detail="no open web session; saved to the conversation and not yet seen",
        )

    def _record_outcome(
        self, message: OriginatedMessage, outcome: DeliveryOutcome, *, detail: str
    ) -> DeliveryResult:
        self._record(
            engine=self._engine,
            user_id=message.owner_user_id,
            action=f"origination.delivery.{outcome.value}",
            target=message.conversation_id or "",
            metadata={
                "persona_id": message.persona.persona_id,
                "channel": self._CHANNEL,
                "detail": detail,
            },
        )
        return DeliveryResult(outcome=outcome, channel=self._CHANNEL, detail=detail)

"""The voice-side delegation dispatcher — enqueue off-loop, watch for the result, fail-soft (T10).

The confirmed-ask sink the reply producer fires (A9-D-5/D-7). It ties the two off-loop halves of a
delegation together with a clean fail-soft contract:

* it enqueues the durable ``delegated_turn`` job OFF the event loop (``asyncio.to_thread`` — the
  voice loop never blocks on the DB);
* **on success** it registers the delegation's key with the hand-back poller (T6) so the result
  comes back to the call;
* **on an enqueue failure** (T10) it does NOT track (nothing to poll — no infinite loop) and speaks
  a brief fail-soft line, so the user who just heard "I'm setting that up in the background" is told
  it couldn't be set up. **No half-created state is possible:** voice never creates anything itself;
  the create is the worker's, keyed idempotently — a failed enqueue simply means the durable job
  never landed, so nothing was created.

Fire-and-forget: :meth:`dispatch` returns immediately; the enqueue + tracking run on a
session-scoped task, cancelled at teardown.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from persona.jobs import delegated_turn_idempotency_key, make_delegated_turn_payload
from persona.logging import get_logger

from persona_voice.session.delegation_enqueue import enqueue_delegated_turn

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy import Engine

    from persona_voice.model.origination_gate import DelegatedTurnIntent
    from persona_voice.session.delegation_handback import DelegationHandbackPoller

__all__ = ["DelegationDispatcher"]

_logger = get_logger("voice.delegation_dispatch")


class DelegationDispatcher:
    """Enqueues a confirmed delegated ask off-loop, tracks it for the hand-back, fails soft (T10).

    Args:
        engine: The session RLS engine (owner-scoped by its checkout GUC).
        owner_id: The caller's user id (the job owner).
        poller: The hand-back poller — tracked with the delegation key on a successful enqueue.
        on_failed: The fail-soft narration sink (the orchestrator's floor-gated narration). Called
            with no args when the enqueue fails, so the user hears it couldn't be set up. MUST NOT
            raise.
    """

    def __init__(
        self,
        *,
        engine: Engine,
        owner_id: str,
        poller: DelegationHandbackPoller,
        on_failed: Callable[[], Awaitable[None]],
    ) -> None:
        self._engine = engine
        self._owner_id = owner_id
        self._poller = poller
        self._on_failed = on_failed
        self._tasks: set[asyncio.Task[None]] = set()

    def dispatch(self, intent: DelegatedTurnIntent) -> None:
        """Fire-and-forget: spawn the off-loop enqueue-+-track task (returns immediately)."""
        task = asyncio.create_task(self._run(intent), name="delegated-turn-dispatch")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, intent: DelegatedTurnIntent) -> None:
        """Enqueue off-loop; on success track the hand-back, on failure speak the fail-soft line."""
        # The delegation key is deterministic from the intent — the SAME key the enqueue mints, so
        # the poller watches the exact job row this delegation writes.
        key = delegated_turn_idempotency_key(
            make_delegated_turn_payload(
                conversation_id=intent.conversation_id,
                verbatim_ask=intent.verbatim_ask,
                persona_id=intent.persona_id,
                provenance=intent.provenance,
            )
        )
        try:
            await asyncio.to_thread(
                enqueue_delegated_turn,
                self._engine,
                owner_id=self._owner_id,
                conversation_id=intent.conversation_id,
                verbatim_ask=intent.verbatim_ask,
                persona_id=intent.persona_id,
                provenance=intent.provenance,
            )
        except Exception:  # noqa: BLE001 — an enqueue failure must fail soft, never crash the call
            _logger.warning(
                "delegated-turn enqueue failed (conversation={cid}); no job landed, none created",
                cid=intent.conversation_id,
            )
            await self._on_failed()  # tell the user it couldn't be set up (no half-created state)
            return
        # The durable job landed (a fresh enqueue OR an ON CONFLICT dedup — either way the job
        # exists) → watch its terminal row for the grounded hand-back.
        self._poller.track(key)

    async def join(self) -> None:
        """Await all in-flight dispatch tasks to completion (a drain helper; used in tests)."""
        while self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    async def shutdown(self) -> None:
        """Cancel any in-flight dispatch task (call teardown)."""
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

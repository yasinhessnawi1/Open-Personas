"""The voice-side delegation hand-back poller (Spec A9, T6; A9-D-5/D-7).

Voice delegated a confirmed spoken ask to the chat pipeline (T4) and said "I'm preparing that in the
background." When the api-side handler (T5) finishes, the result must come BACK to the live call —
and voice is an ephemeral, per-call, single-session process with no addressable consumer, so the
hand-back is a **voice-side poll** (A9-D-5, the ratified direction), not a worker→voice push:

* voice watches the ``delegated_turn`` job's **terminal row** (the done-signal), keyed by the
  idempotency key it minted at enqueue (no new projection ⇒ migration stays NONE);
* on terminal it reads the **outcome message** by ``delegation_key`` — the grounded record the
  handler wrote (T5, Option a) — and speaks it at the next idle floor via the orchestrator's
  floor-gated narration (the same seam the async-artifact lane uses; never over the user):
  - ``succeeded`` → the message's **grounded** content (what was ACTUALLY done — A9-D-7 condition 1,
    never a replay of the echo);
  - ``blocked_on_approval`` → the honest-incomplete line the handler wrote ("I've started it — it
    needs your OK in your chat with {persona}" — A9-D-7 condition 2, never a fake done);
  - ``failed`` → a brief fail-soft line.

Polling runs **only while a delegation is pending** (the loop stops when the set drains, restarts on
a new ``track``). The result is **durable**, so it self-heals across a missed poll, a reconnect, or
a call-end: an un-consumed outcome still persists in the conversation (the user sees it in chat).
The DB reads run OFF the event loop (``asyncio.to_thread``) — the voice loop never blocks on the DB.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from persona.jobs import DELEGATED_TURN_JOB_TYPE  # noqa: F401 — documents the job type this polls
from persona.logging import get_logger
from sqlalchemy import text

from persona_voice.loop.streaming import Transcript

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy import Engine

__all__ = ["DEFAULT_HANDBACK_POLL_INTERVAL_S", "DelegationHandbackPoller"]

_logger = get_logger("voice.delegation_handback")

#: How often to poll a pending delegation's terminal row (seconds). The delegated turn runs in the
#: worker (seconds), and polling is bounded to while-pending, so a short interval is cheap.
DEFAULT_HANDBACK_POLL_INTERVAL_S: float = 2.0

#: The A0 terminal states — the done-signal for a delegation (mirrors ``TERMINAL_STATES``
#: as raw strings, since this reads the ``jobs.state`` text column directly).
_TERMINAL_STATES: frozenset[str] = frozenset({"succeeded", "failed", "dead"})

_OUTCOME_SUCCEEDED = "succeeded"
_OUTCOME_BLOCKED = "blocked_on_approval"

_JOB_STATE_SQL = text(
    "SELECT state FROM jobs WHERE owner_id = :owner AND idempotency_key = :key LIMIT 1"
)
_OUTCOME_SQL = text(
    "SELECT content, channel->>'delegation_outcome' AS outcome FROM messages "
    "WHERE conversation_id = :conv AND channel->>'delegation_key' = :key LIMIT 1"
)


@dataclass(frozen=True)
class _Resolution:
    """A resolved delegation: its recorded outcome + the grounded text to voice."""

    outcome: str
    content: str


class DelegationHandbackPoller:
    """Polls pending delegations and speaks the grounded hand-back at the next idle floor (T6).

    Construct once per voice session. :meth:`track` registers the ``delegation_key`` voice minted at
    enqueue; a background loop resolves each pending delegation and fires ``on_handback`` (the
    orchestrator's floor-gated narration) with a persona-voiced nudge from the durable outcome.

    Args:
        engine: The session RLS engine (owner-scoped by its checkout GUC).
        owner_id: The caller's user id (the job owner — the ``jobs`` lookup key half).
        conversation_id: The call's conversation (the outcome message lives here).
        on_handback: The floor-gated narration sink — the orchestrator's ``notify_artifact_ready``.
            Called once per resolved delegation with the spoken nudge. MUST NOT raise.
        poll_interval_s: How often to poll while a delegation is pending.
        scheduler: Spawns the background poll loop (default ``asyncio.create_task``; injectable).
    """

    def __init__(
        self,
        *,
        engine: Engine,
        owner_id: str,
        conversation_id: str,
        on_handback: Callable[[Transcript], Awaitable[None]],
        poll_interval_s: float = DEFAULT_HANDBACK_POLL_INTERVAL_S,
    ) -> None:
        self._engine = engine
        self._owner_id = owner_id
        self._conversation_id = conversation_id
        self._on_handback = on_handback
        self._interval = poll_interval_s
        self._pending: set[str] = set()
        self._task: asyncio.Task[None] | None = None

    def track(self, delegation_key: str) -> None:
        """Register a pending delegation (idempotent) and ensure the poll loop is running."""
        self._pending.add(delegation_key)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="delegation-handback-poll")

    async def poll_once(self) -> list[str]:
        """One resolution pass: speak + drop every pending delegation that has reached terminal.

        Returns the keys resolved this pass (for tests). Each DB read runs off the event loop; a
        still-pending delegation is left for the next pass. A hand-back-sink failure never breaks
        the poll (it degrades that one delegation to logged, then drops it — the result is durable).
        """
        resolved: list[str] = []
        for key in tuple(self._pending):
            resolution = await asyncio.to_thread(self._resolve, key)
            if resolution is None:
                continue  # still pending — try next pass
            self._pending.discard(key)
            resolved.append(key)
            try:
                await self._on_handback(self._narration(resolution))
            except Exception:  # noqa: BLE001 — a narration-sink failure must not break the poll loop
                _logger.warning("delegation hand-back narration failed key={k}", k=key)
        return resolved

    async def shutdown(self) -> None:
        """Cancel the poll loop (call teardown). The durable result self-heals — nothing is lost."""
        task = self._task
        self._task = None
        self._pending.clear()
        if task is not None and not task.done():
            task.cancel()
            try:  # noqa: SIM105 — suppress needs the import; the try/except is clearer here
                await task
            except asyncio.CancelledError:
                pass

    # ----- internals ---------------------------------------------------

    async def _run(self) -> None:
        """Poll on the interval while any delegation is pending; stop when the set drains."""
        try:
            while self._pending:
                await self.poll_once()
                if not self._pending:
                    break
                await asyncio.sleep(self._interval)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — the poll loop must never crash the call
            _logger.warning("delegation hand-back poll loop errored; stopping (results durable)")

    def _resolve(self, delegation_key: str) -> _Resolution | None:
        """Read the job's terminal state; if terminal, read the outcome message (sync — off-loop).

        ``None`` ⇒ still pending (no terminal job row yet). A terminal job with no outcome message
        is the executor-error edge → treated as ``failed`` (fail-soft), never a fake done.
        """
        with self._engine.begin() as conn:
            state = conn.execute(
                _JOB_STATE_SQL, {"owner": self._owner_id, "key": delegation_key}
            ).scalar()
            if state is None or str(state) not in _TERMINAL_STATES:
                return None
            row = (
                conn.execute(_OUTCOME_SQL, {"conv": self._conversation_id, "key": delegation_key})
                .mappings()
                .first()
            )
        if row is None:
            return _Resolution(outcome="failed", content="")
        return _Resolution(
            outcome=str(row["outcome"] or "failed"), content=str(row["content"] or "")
        )

    def _narration(self, resolution: _Resolution) -> Transcript:
        """Build the persona-voiced hand-back nudge for this outcome (A9-D-7).

        ``succeeded``/``blocked_on_approval`` voice the handler's grounded content (the honest line
        is already the content for a block); ``failed`` degrades to a brief detail-free fail-soft
        line. The model authors the spoken sentence — grounded in the real outcome, never the echo.
        """
        content = resolution.content.strip()
        if resolution.outcome == _OUTCOME_SUCCEEDED and content:
            nudge = (
                "A task the user asked for by voice just finished in the background. Here is what "
                f'was actually done: "{content}". In one short, natural sentence, let them know '
                "it's ready."
            )
        elif resolution.outcome == _OUTCOME_BLOCKED and content:
            nudge = (
                "A task the user asked for by voice has been started but needs their approval "
                f'before it can finish. Tell them, in one short sentence: "{content}".'
            )
        else:  # failed / missing outcome — fail-soft, no details leaked
            nudge = (
                "A task the user asked for by voice could not be completed. In one short, kind "
                "sentence, let them know you weren't able to set it up."
            )
        return Transcript(is_final=True, text=nudge, confidence=1.0)

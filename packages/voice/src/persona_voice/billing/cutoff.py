"""Mid-call cutoff on credit exhaustion (Spec M3, T6b-2 — D-M3-7 / D-M3-R3).

When a real per-turn deduct exhausts the caller's balance (the meter's
``capture_up_to`` captures less than charged, or the balance hits 0), the call
must end — a persona cannot keep talking on credit it doesn't have. This is the
terminal half of D-M3-7 (voice "deducts per-turn/min and terminates the call on
exhaustion"); the per-turn metering that DETECTS exhaustion is T6b-1's
:class:`~persona_voice.billing.turn_meter.VoiceTurnBillingMeter`.

The cutoff is **fire-once** (one call end, even if several turns detect
exhaustion), speaks **one** brief grounded notice under a **bounded grace** (so
the caller hears WHY, but the end never blocks indefinitely on the audio), then
issues the authoritative :meth:`LiveKitAPI.room.delete_room` — which disconnects
BOTH parties, so the agent's own room-disconnect handler drives the normal
teardown (finalizing the durable call-record + the LiveKit infra tick). If the
server call fails, it falls back to the agent-leave path (setting the session's
``ended`` event, which run() awaits → ``_teardown`` → the agent leaves the room).

Everything is **best-effort** — a cutoff hiccup must never raise into the turn or
audio path (it is reached from the meter's ``on_exhausted``, itself fired inside
the recorder's commit-``finally``).
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from persona.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

__all__ = ["VoiceExhaustionCutoff"]

_LOG = get_logger("voice.cutoff")

#: Default seconds allowed for the exhaustion notice to play before the room is
#: deleted — bounded so the end never hangs on the audio (D-M3-R3).
_DEFAULT_GRACE_S = 3.0


class VoiceExhaustionCutoff:
    """Ends the call once the caller's credit is exhausted (fire-once, best-effort).

    Args:
        delete_room: The authoritative room-termination call (a closure over the
            LiveKit ``RoomServiceClient``/``LiveKitAPI`` for THIS call's room) —
            disconnects both parties. Injected as a bare awaitable so the boundary
            is mockable in tests and the SDK never leaks into this module.
        speak_notice: Optional — emits ONE brief grounded spoken notice (the
            floor-gated narration seam). Awaited under ``grace_s`` so the caller
            hears why; a slow/blocked notice never delays the cutoff past the grace.
        on_fallback: Called if ``delete_room`` fails — the agent-leave path
            (typically ``ended.set``), so teardown still runs and the agent leaves.
        grace_s: Upper bound (seconds) on the notice before the room is deleted.
    """

    def __init__(
        self,
        *,
        delete_room: Callable[[], Awaitable[None]],
        speak_notice: Callable[[], Awaitable[None]] | None = None,
        on_fallback: Callable[[], None] | None = None,
        grace_s: float = _DEFAULT_GRACE_S,
    ) -> None:
        self._delete_room = delete_room
        self._speak_notice = speak_notice
        self._on_fallback = on_fallback
        self._grace_s = grace_s
        self._fired = False

    async def trigger(self) -> None:
        """End the call once (idempotent): notice (bounded) → delete_room → fallback.

        MUST NOT raise — reached from the meter's ``on_exhausted`` inside the turn
        commit path. Re-entry after the first fire is a no-op (fire-once, D-M3-R3).
        """
        if self._fired:
            return
        self._fired = True
        _LOG.info("voice credit exhausted; ending the call")
        # One brief grounded notice, bounded so the end never hangs on the audio.
        if self._speak_notice is not None:
            with contextlib.suppress(Exception):  # incl. TimeoutError (an Exception)
                await asyncio.wait_for(self._speak_notice(), timeout=self._grace_s)
        # The authoritative cutoff — disconnects both parties; the agent's own
        # room-disconnect handler then drives teardown (record + infra tick).
        try:
            await self._delete_room()
        except Exception as exc:  # noqa: BLE001 — never raise into the audio path
            _LOG.warning(
                "delete_room failed on exhaustion cutoff; falling back to agent-leave: {err}",
                err=repr(exc)[:200],
            )
            if self._on_fallback is not None:
                with contextlib.suppress(Exception):
                    self._on_fallback()

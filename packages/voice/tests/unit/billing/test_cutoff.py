"""Mid-call exhaustion cutoff (Spec M3, T6b-2).

Proves the fire-once cutoff: one brief notice under a bounded grace, then the
authoritative ``delete_room``; a ``delete_room`` failure falls back to the
agent-leave path; re-entry is a no-op. The exhaustion→cutoff REAL chain (via the
per-turn meter) is proven in test_turn_meter.py.
"""

from __future__ import annotations

import asyncio

import pytest
from persona_voice.billing import VoiceExhaustionCutoff


class TestVoiceExhaustionCutoff:
    def test_speaks_notice_then_deletes_room(self) -> None:
        order: list[str] = []

        async def _notice() -> None:
            order.append("notice")

        async def _delete() -> None:
            order.append("delete")

        cutoff = VoiceExhaustionCutoff(delete_room=_delete, speak_notice=_notice)
        asyncio.run(cutoff.trigger())
        assert order == ["notice", "delete"]  # the caller hears WHY before the room dies

    def test_fire_once_second_trigger_is_a_noop(self) -> None:
        deletes: list[int] = []

        async def _delete() -> None:
            deletes.append(1)

        cutoff = VoiceExhaustionCutoff(delete_room=_delete)

        async def _run() -> None:
            await cutoff.trigger()
            await cutoff.trigger()  # re-entry — a call ends exactly once

        asyncio.run(_run())
        assert deletes == [1]

    def test_delete_room_failure_falls_back_to_agent_leave(self) -> None:
        fallback: list[str] = []

        async def _delete() -> None:
            raise RuntimeError("livekit unreachable")

        cutoff = VoiceExhaustionCutoff(
            delete_room=_delete, on_fallback=lambda: fallback.append("ended")
        )
        asyncio.run(cutoff.trigger())  # must not raise
        assert fallback == ["ended"]  # the ended-event / agent-leave path

    def test_no_notice_goes_straight_to_delete_room(self) -> None:
        deletes: list[int] = []

        async def _delete() -> None:
            deletes.append(1)

        cutoff = VoiceExhaustionCutoff(delete_room=_delete)  # no notice wired
        asyncio.run(cutoff.trigger())
        assert deletes == [1]

    def test_slow_notice_is_bounded_and_never_blocks_the_cutoff(self) -> None:
        deletes: list[int] = []

        async def _hung_notice() -> None:
            await asyncio.sleep(10.0)  # would hang the end without the grace bound

        async def _delete() -> None:
            deletes.append(1)

        cutoff = VoiceExhaustionCutoff(delete_room=_delete, speak_notice=_hung_notice, grace_s=0.05)

        async def _run() -> None:
            await asyncio.wait_for(cutoff.trigger(), timeout=2.0)  # cutoff itself never hangs

        asyncio.run(_run())
        assert deletes == [1]  # delete_room still fired after the bounded grace

    def test_notice_failure_does_not_block_delete_room(self) -> None:
        deletes: list[int] = []

        async def _raising_notice() -> None:
            raise RuntimeError("narration failed")

        async def _delete() -> None:
            deletes.append(1)

        cutoff = VoiceExhaustionCutoff(delete_room=_delete, speak_notice=_raising_notice)
        asyncio.run(cutoff.trigger())
        assert deletes == [1]  # a broken notice never stops the authoritative cutoff


@pytest.mark.asyncio
async def test_trigger_is_awaitable_and_idempotent_under_concurrency() -> None:
    deletes: list[int] = []

    async def _delete() -> None:
        deletes.append(1)

    cutoff = VoiceExhaustionCutoff(delete_room=_delete)
    await asyncio.gather(cutoff.trigger(), cutoff.trigger())
    assert deletes == [1]  # the fire-once guard holds even fired twice in one tick

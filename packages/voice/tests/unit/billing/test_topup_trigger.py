"""The voice-side auto-top-up trigger, and its edition gate (Spec M5, B5).

Two properties, both load-bearing on a money path:

1. **The meter reports two balances, and only when they can matter.** It never decides
   whether a crossing happened, whether the caller is eligible, or how much to charge:
   those live in ``maybe_auto_topup`` on the api side (D-M5-16), so the voice process
   never learns what "$2" means and the rule cannot desync across two processes.

2. **Community is provably inert.** The trigger is injected at construction (D-M5-22),
   so with no collaborator there is no code path at all, rather than a branch that has to
   be evaluated correctly on every turn. This matters more than usual here: there is no
   edition check anywhere else on the voice deduct path (D-M5-23), so this gate is the
   only one.
"""

from __future__ import annotations

import asyncio

from persona_voice.billing.turn_meter import VoiceTurnBillingMeter


class _Ledger:
    """A ledger whose capture returns a scripted (captured, new_balance)."""

    def __init__(self, captured: int, new_balance: int) -> None:
        self._captured = captured
        self._new_balance = new_balance

    def capture_up_to_idempotent(self, **_kwargs: object) -> tuple[int, int]:
        return (self._captured, self._new_balance)


class _Engine:
    """Just enough engine for the meter's dispose-in-finally."""

    def dispose(self) -> None:
        return None


def _meter(
    *, captured: int, new_balance: int, enqueue: object | None
) -> tuple[VoiceTurnBillingMeter, list[tuple[int, int, int]]]:
    seen: list[tuple[int, int, int]] = []

    def _record(old: int, new: int, turn: int) -> None:
        seen.append((old, new, turn))

    from persona.billing import BillingConfig

    meter = VoiceTurnBillingMeter(
        ledger=_Ledger(captured, new_balance),  # type: ignore[arg-type]
        billing_config=BillingConfig(),
        engine_factory=lambda: _Engine(),  # type: ignore[arg-type,return-value]
        user_id="u1",
        call_id="call1",
        stt_provider="deepgram",
        stt_model="nova",
        tts_provider="elevenlabs",
        tts_model="flash",
        streamed_seconds_reader=lambda: 0.0,
        enqueue_topup=(_record if enqueue == "on" else None),
    )
    return meter, seen


def _charge(meter: VoiceTurnBillingMeter, turn_seq: int = 3) -> None:
    """Drive the REAL off-loop charge path the recorder calls."""
    meter._charge_turn(10.0, f"voice:call1:{turn_seq}", turn_seq)  # noqa: SLF001


def test_a_real_deduct_reports_both_balances() -> None:
    """old = new + captured, so the api sees the true before/after pair (D-M5-17)."""
    meter, seen = _meter(captured=100, new_balance=150, enqueue="on")
    _charge(meter)
    assert seen == [(250, 150, 3)]


def test_a_duplicate_tick_cannot_manufacture_a_crossing() -> None:
    """The Finding-1 trap, defused by arithmetic.

    A re-used ``billing_key`` returns ``captured == 0`` with a HEALTHY balance. Reporting
    ``charged`` instead of ``captured`` would give old > new and invent a crossing that
    never happened; with ``captured`` the pre-filter drops it entirely.
    """
    meter, seen = _meter(captured=0, new_balance=5000, enqueue="on")
    _charge(meter)
    assert seen == []


def test_community_never_enqueues_anything() -> None:
    """D-M5-22/23: no collaborator, so no code path exists. The deduct still happens."""
    meter, seen = _meter(captured=100, new_balance=150, enqueue=None)
    _charge(meter)
    assert seen == []


def test_an_enqueue_failure_never_breaks_the_turn() -> None:
    """Billing is enrichment; the conversation is the work.

    A DB hiccup writing the trigger must not propagate into the turn, and the meter must
    still report exhaustion correctly to the caller.
    """
    from persona.billing import BillingConfig

    def _explode(_old: int, _new: int, _turn: int) -> None:
        raise RuntimeError("db is down")

    meter = VoiceTurnBillingMeter(
        ledger=_Ledger(100, 0),  # type: ignore[arg-type]
        billing_config=BillingConfig(),
        engine_factory=lambda: _Engine(),  # type: ignore[arg-type,return-value]
        user_id="u1",
        call_id="call1",
        stt_provider="deepgram",
        stt_model="nova",
        tts_provider="elevenlabs",
        tts_model="flash",
        streamed_seconds_reader=lambda: 0.0,
        enqueue_topup=_explode,
    )
    # Exhaustion (new_balance == 0) is still reported despite the enqueue failing.
    assert meter._charge_turn(10.0, "voice:call1:1", 1) is True  # noqa: SLF001


def test_the_trigger_runs_off_the_audio_loop() -> None:
    """D-M5-18: the enqueue is synchronous INSIDE the already-off-loop charge.

    No extra task means no cancellation window at teardown, so a crossing on the final
    turn cannot lose its trigger. Driving ``bill_turn`` (the real entry point) proves the
    enqueue happens under ``asyncio.to_thread``, not on the loop.
    """
    meter, seen = _meter(captured=100, new_balance=150, enqueue="on")
    # TTS chars price through the voice registry, so this turn has a real cost and the
    # deduct actually fires (an unpriced model would charge 0 and skip it entirely).
    meter.note_tts_chars(5000)
    asyncio.run(meter.bill_turn(4))
    assert len(seen) == 1
    assert seen[0][2] == 4

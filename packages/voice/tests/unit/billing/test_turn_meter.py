"""Voice per-turn owner billing meter (Spec M3, T6b-1).

Proves the metering substrate + off-loop idempotent deduct: the char accumulator
totals synthesized chars, LLM usage is summed and priced, STT is a since-last
delta, the served provider is priced at its OWN rate (attribution), the deduct
rides ``capture_up_to_idempotent`` off the loop with a per-turn key, a no-cost
turn charges nothing, LiveKit infra bills at call end, and billing is fail-soft
(a ledger raise never escapes ``bill_turn`` / ``bill_call_infra``).
"""

from __future__ import annotations

import asyncio
import math

import pytest
from persona.billing import BillingConfig
from persona_voice.billing import VoiceTurnAccumulator, VoiceTurnBillingMeter


def _amount(kw: dict[str, object]) -> int:
    amount = kw["amount"]
    assert isinstance(amount, int)
    return amount


class _FakeEngine:
    def __init__(self) -> None:
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True


class _RecordingLedger:
    """A LedgerPort double recording every idempotent capture call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def capture_up_to_idempotent(self, **kw: object) -> tuple[int, int]:
        self.calls.append(kw)
        return _amount(kw), 100

    # Unused arms of the port (present so the double satisfies the Protocol shape).
    def capture_up_to(self, **kw: object) -> tuple[int, int]:  # pragma: no cover
        self.calls.append(kw)
        return _amount(kw), 100

    def deduct(self, **_kw: object) -> int:  # pragma: no cover
        return 0

    def deduct_idempotent(self, **_kw: object) -> int:  # pragma: no cover
        return 0


class _RaisingLedger(_RecordingLedger):
    def capture_up_to_idempotent(self, **_kw: object) -> tuple[int, int]:
        raise RuntimeError("ledger exploded")


def _meter(
    ledger: object,
    *,
    stt_provider: str = "gladia",
    stt_model: str = "solaria-1",
    tts_provider: str = "elevenlabs",
    tts_model: str = "eleven_flash_v2_5",
    streamed_seconds: list[float] | None = None,
    engines: list[_FakeEngine] | None = None,
    on_exhausted: object | None = None,
) -> VoiceTurnBillingMeter:
    seconds_box = streamed_seconds if streamed_seconds is not None else [0.0]
    engines_out = engines if engines is not None else []

    def _factory() -> _FakeEngine:
        eng = _FakeEngine()
        engines_out.append(eng)
        return eng

    return VoiceTurnBillingMeter(
        ledger=ledger,  # type: ignore[arg-type]
        billing_config=BillingConfig(),
        engine_factory=_factory,
        user_id="owner-1",
        call_id="call1",
        stt_provider=stt_provider,
        stt_model=stt_model,
        tts_provider=tts_provider,
        tts_model=tts_model,
        streamed_seconds_reader=lambda: seconds_box[0],
        floor=1,
        on_exhausted=on_exhausted,  # type: ignore[arg-type]
    )


# ----- accumulator ----------------------------------------------------------


class TestVoiceTurnAccumulator:
    def test_tts_chars_total_across_the_turn(self) -> None:
        acc = VoiceTurnAccumulator(streamed_seconds_reader=lambda: 0.0)
        acc.note_tts_chars(120)
        acc.note_tts_chars(80)  # a second synthesized chunk in the same turn
        assert acc.take_turn().tts_chars == 200

    def test_stt_is_a_since_last_delta(self) -> None:
        box = [0.0]
        acc = VoiceTurnAccumulator(streamed_seconds_reader=lambda: box[0])
        box[0] = 30.0
        assert acc.take_turn().stt_streamed_seconds == pytest.approx(30.0)
        box[0] = 75.0  # cumulative reader; the second turn bills only the new 45s
        assert acc.take_turn().stt_streamed_seconds == pytest.approx(45.0)

    def test_llm_usage_sums_across_rounds(self) -> None:
        acc = VoiceTurnAccumulator(streamed_seconds_reader=lambda: 0.0)
        acc.note_llm_usage(
            prompt_tokens=100, completion_tokens=50, cost_usd=0.01, provider="openrouter", model="m"
        )
        acc.note_llm_usage(
            prompt_tokens=40, completion_tokens=20, cost_usd=0.02, provider="openrouter", model="m"
        )
        usage = acc.take_turn()
        assert usage.llm_prompt_tokens == 140
        assert usage.llm_completion_tokens == 70
        assert usage.llm_cost_usd == pytest.approx(0.03)  # summed, both rounds reported one

    def test_cost_usd_is_none_when_any_round_lacks_it(self) -> None:
        acc = VoiceTurnAccumulator(streamed_seconds_reader=lambda: 0.0)
        acc.note_llm_usage(
            prompt_tokens=100, completion_tokens=50, cost_usd=0.01, provider="openrouter", model="m"
        )
        acc.note_llm_usage(
            prompt_tokens=40, completion_tokens=20, cost_usd=None, provider="openrouter", model="m"
        )
        assert acc.take_turn().llm_cost_usd is None

    def test_take_turn_resets_state(self) -> None:
        acc = VoiceTurnAccumulator(streamed_seconds_reader=lambda: 0.0)
        acc.note_tts_chars(500)
        acc.note_llm_usage(
            prompt_tokens=10, completion_tokens=10, cost_usd=0.01, provider="p", model="m"
        )
        acc.take_turn()
        second = acc.take_turn()
        assert second.tts_chars == 0
        assert second.llm_prompt_tokens == 0
        assert second.llm_cost_usd is None


# ----- per-turn deduct ------------------------------------------------------


class TestBillTurn:
    def test_primary_turn_sums_served_costs_and_deducts_off_loop(self) -> None:
        ledger = _RecordingLedger()
        engines: list[_FakeEngine] = []
        meter = _meter(ledger, streamed_seconds=[60.0], engines=engines)  # 60s streamed
        meter.note_tts_chars(1000)  # ElevenLabs Flash: 1000 chars × 5.0¢/1k = 5.0¢
        meter.note_llm_usage(
            prompt_tokens=100,
            completion_tokens=100,
            cost_usd=0.02,  # OpenRouter actual → 2.0¢
            provider="openrouter",
            model="anthropic/claude",
        )
        asyncio.run(meter.bill_turn(1))
        # STT gladia 60s = 1min × 1.25 = 1.25; TTS 5.0; LLM 2.0 → 8.25¢ → ceil = 9 credits.
        assert len(ledger.calls) == 1
        kw = ledger.calls[0]
        assert kw["amount"] == 9
        assert kw["cost_cents"] == pytest.approx(8.25)
        assert kw["cost_basis"] == "provider_meter"
        assert kw["reason"] == "voice:provider_meter"
        assert kw["billing_key"] == "voice:call1:1"
        assert kw["user_id"] == "owner-1"
        assert engines  # a fresh engine was created for the off-loop deduct
        assert engines[0].disposed  # and disposed in finally

    def test_served_fallback_provider_is_priced_at_the_fallback_rate(self) -> None:
        ledger = _RecordingLedger()
        # Fallback STT (deepgram) + no TTS/LLM → 60s × 0.77 = 0.77¢ → ceil = 1 credit.
        meter = _meter(
            ledger, stt_provider="deepgram", stt_model="nova-3-streaming", streamed_seconds=[60.0]
        )
        asyncio.run(meter.bill_turn(1))
        assert ledger.calls[0]["cost_cents"] == pytest.approx(0.77)
        assert ledger.calls[0]["amount"] == 1  # ceil(0.77) floored to the min 1

    def test_per_turn_billing_key_is_unique_per_turn(self) -> None:
        ledger = _RecordingLedger()
        box = [0.0]
        meter = _meter(ledger, streamed_seconds=box)
        box[0] = 60.0
        meter.note_tts_chars(1000)
        asyncio.run(meter.bill_turn(1))
        box[0] = 120.0
        meter.note_tts_chars(1000)
        asyncio.run(meter.bill_turn(2))
        keys = [c["billing_key"] for c in ledger.calls]
        assert keys == ["voice:call1:1", "voice:call1:2"]

    def test_no_metered_cost_turn_charges_nothing(self) -> None:
        ledger = _RecordingLedger()
        meter = _meter(ledger, streamed_seconds=[0.0])  # no STT, no TTS, no LLM
        asyncio.run(meter.bill_turn(1))
        assert ledger.calls == []

    def test_fail_soft_ledger_raise_does_not_escape(self) -> None:
        ledger = _RaisingLedger()
        meter = _meter(ledger, streamed_seconds=[60.0])
        meter.note_tts_chars(1000)
        # Must not raise — a billing failure can never break the turn/audio path.
        asyncio.run(meter.bill_turn(1))

    def test_take_turn_runs_even_when_charge_is_skipped(self) -> None:
        # A no-cost turn still advances the STT delta baseline (take_turn happened),
        # so the NEXT turn bills only its own new audio.
        ledger = _RecordingLedger()
        box = [10.0]
        meter = _meter(ledger, streamed_seconds=box)
        asyncio.run(meter.bill_turn(1))  # 10s STT gladia = 0.208¢ → charges (ceil→1)
        box[0] = 20.0
        meter.note_tts_chars(0)
        asyncio.run(meter.bill_turn(2))  # only the new 10s
        assert ledger.calls[-1]["cost_cents"] == pytest.approx(10.0 / 60.0 * 1.25)


# ----- call-end LiveKit infra ------------------------------------------------


class TestBillCallInfra:
    def test_livekit_infra_billed_per_minute_at_call_end(self) -> None:
        ledger = _RecordingLedger()
        meter = _meter(ledger)
        asyncio.run(meter.bill_call_infra(duration_s=180))  # 3 min × 1.0¢ = 3.0¢
        assert len(ledger.calls) == 1
        kw = ledger.calls[0]
        assert kw["amount"] == 3
        assert kw["cost_cents"] == pytest.approx(0.0)  # zero provider cost — infra only
        assert kw["cost_basis"] == "infra_flat"
        assert kw["reason"] == "voice:infra_flat"
        assert kw["billing_key"] == "voice:call1:livekit"

    def test_zero_duration_charges_nothing(self) -> None:
        ledger = _RecordingLedger()
        meter = _meter(ledger)
        asyncio.run(meter.bill_call_infra(duration_s=0))
        assert ledger.calls == []

    def test_infra_billing_is_fail_soft(self) -> None:
        ledger = _RaisingLedger()
        meter = _meter(ledger)
        asyncio.run(meter.bill_call_infra(duration_s=180))  # must not raise


def test_charged_amount_matches_the_credit_formula() -> None:
    # Sanity: the meter's charge is ceil(provider_cents) at markup 1.0 / floor 1.
    ledger = _RecordingLedger()
    meter = _meter(ledger, streamed_seconds=[120.0])  # gladia 2min = 2.5¢
    meter.note_tts_chars(2000)  # ElevenLabs Flash 2000 × 5.0/1k = 10.0¢
    asyncio.run(meter.bill_turn(7))
    kw = ledger.calls[0]
    assert kw["cost_cents"] == pytest.approx(12.5)
    assert kw["amount"] == math.ceil(12.5)


# ----- exhaustion → on_exhausted (Spec M3, T6b-2) ---------------------------


class _FixedLedger:
    """A LedgerPort whose capture returns a fixed (captured, new_balance)."""

    def __init__(self, *, captured: int, new_balance: int) -> None:
        self._captured = captured
        self._new_balance = new_balance

    def capture_up_to_idempotent(self, **_kw: object) -> tuple[int, int]:
        return self._captured, self._new_balance

    def capture_up_to(self, **_kw: object) -> tuple[int, int]:  # pragma: no cover
        return self._captured, self._new_balance

    def deduct(self, **_kw: object) -> int:  # pragma: no cover
        return 0

    def deduct_idempotent(self, **_kw: object) -> int:  # pragma: no cover
        return 0


class _BalanceLedger:
    """A LedgerPort with a REAL balance that capture_up_to_idempotent decrements.

    captures ``min(amount, balance)`` (never overdraws), returns the new balance,
    and no-ops on a re-seen key — exactly the semantics the meter's exhaustion
    detection reads (``captured < charged`` or ``new_balance <= 0``).
    """

    def __init__(self, balance: int) -> None:
        self.balance = balance
        self._seen: set[str] = set()

    def capture_up_to_idempotent(self, **kw: object) -> tuple[int, int]:
        key = kw["billing_key"]
        assert isinstance(key, str)
        if key in self._seen:
            return 0, self.balance
        self._seen.add(key)
        amount = kw["amount"]
        assert isinstance(amount, int)
        captured = min(amount, max(0, self.balance))
        self.balance -= captured
        return captured, self.balance

    def capture_up_to(self, **kw: object) -> tuple[int, int]:  # pragma: no cover
        return self.capture_up_to_idempotent(**kw)

    def deduct(self, **_kw: object) -> int:  # pragma: no cover
        return 0

    def deduct_idempotent(self, **_kw: object) -> int:  # pragma: no cover
        return 0


class TestExhaustionSignal:
    def test_fires_on_partial_capture(self) -> None:
        fired: list[int] = []

        async def _on_exhausted() -> None:
            fired.append(1)

        # charged 9 (gladia 60s 1.25 + flash 1000ch 5.0 + 0 llm → 6.25¢→7? recompute):
        # gladia 60s=1.25, flash 1000=5.0 → 6.25¢ → ceil 7. Capture only 3 → short.
        meter = _meter(
            _FixedLedger(captured=3, new_balance=5),
            streamed_seconds=[60.0],
            on_exhausted=_on_exhausted,
        )
        meter.note_tts_chars(1000)
        asyncio.run(meter.bill_turn(1))
        assert fired == [1]  # captured < charged → exhausted

    def test_fires_when_balance_hits_zero_even_if_fully_captured(self) -> None:
        fired: list[int] = []

        async def _on_exhausted() -> None:
            fired.append(1)

        meter = _meter(
            _FixedLedger(captured=7, new_balance=0),
            streamed_seconds=[60.0],
            on_exhausted=_on_exhausted,
        )
        meter.note_tts_chars(1000)
        asyncio.run(meter.bill_turn(1))
        assert fired == [1]  # new_balance == 0 → exhausted

    def test_does_not_fire_when_affordable(self) -> None:
        fired: list[int] = []

        async def _on_exhausted() -> None:
            fired.append(1)

        meter = _meter(
            _FixedLedger(captured=7, new_balance=93),
            streamed_seconds=[60.0],
            on_exhausted=_on_exhausted,
        )
        meter.note_tts_chars(1000)
        asyncio.run(meter.bill_turn(1))
        assert fired == []  # fully captured, balance remains → no cutoff

    def test_fires_exactly_once_across_multiple_exhausted_turns(self) -> None:
        fired: list[int] = []

        async def _on_exhausted() -> None:
            fired.append(1)

        box = [60.0]
        meter = _meter(
            _FixedLedger(captured=1, new_balance=0),
            streamed_seconds=box,
            on_exhausted=_on_exhausted,
        )

        async def _run() -> None:
            meter.note_tts_chars(1000)
            await meter.bill_turn(1)
            box[0] = 120.0
            meter.note_tts_chars(1000)
            await meter.bill_turn(2)  # also exhausted, but the cutoff already fired

        asyncio.run(_run())
        assert fired == [1]


def test_real_deduct_chain_drives_the_cutoff_delete_room() -> None:
    """The REAL path: low balance → real per-turn deduct exhausts → on_exhausted →
    the VoiceExhaustionCutoff deletes the room. delete_room is mocked at the SDK
    boundary only; it is reached through the real deduct→exhaustion chain, never
    a hand-forced call (the synthetic-harness lesson)."""
    from persona_voice.billing import VoiceExhaustionCutoff

    deleted: list[str] = []

    async def _delete_room() -> None:
        deleted.append("deleted")

    cutoff = VoiceExhaustionCutoff(delete_room=_delete_room)  # no notice/fallback here
    ledger = _BalanceLedger(balance=5)  # only 5 credits — a single real turn drains it
    box = [0.0]
    meter = _meter(ledger, streamed_seconds=box, on_exhausted=cutoff.trigger)

    async def _run() -> None:
        # Turn 1: gladia 60s (1.25¢) + flash 1000 chars (5.0¢) = 6.25¢ → charge 7;
        # balance 5 → captured 5 < 7 AND balance 0 → real exhaustion.
        box[0] = 60.0
        meter.note_tts_chars(1000)
        await meter.bill_turn(1)
        # Turn 2: still billing, but the call already ended (fire-once).
        box[0] = 120.0
        meter.note_tts_chars(1000)
        await meter.bill_turn(2)

    asyncio.run(_run())
    assert deleted == ["deleted"]  # reached delete_room once, through the real chain
    assert ledger.balance == 0  # the real capture drained the wallet, never negative

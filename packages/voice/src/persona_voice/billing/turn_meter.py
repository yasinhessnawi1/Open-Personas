"""Voice per-turn owner billing — the metering substrate + off-loop deduct (Spec M3, T6b-1).

A voice call bills the **caller (== owner)** its real provider cost incrementally,
one deduct per committed turn plus a LiveKit infra tick at call end:

    per turn  = STT(served × real streamed audio-seconds) + TTS(served × real chars)
                + LLM(turn tokens, priced via the seam)          → basis provider_meter
    call end  = LiveKit infra/min (real call duration)            → basis infra_flat

The **served** provider is whatever backend actually served (there is no runtime
provider-outage failover today, so served ≡ configured); each quantity is priced
at that provider's own registry rate through the pure ``persona.billing`` helpers
(model-fallback attribution). The deduct rides
:meth:`~persona.billing.metered.MeteredBilling.charge` in ``capture`` mode with a
per-turn idempotency ``billing_key`` (``voice:{call_id}:{turn_seq}``) so a
re-fired tick never double-charges.

**Two hard constraints (D-M3-R3):**

* The DB deduct runs **off the audio loop** (``asyncio.to_thread`` + a fresh
  short-lived RLS engine, disposed in ``finally`` — the exact ``_on_call_complete``
  idiom). A DB round-trip on the audio thread manifests as a false "provider
  failure" (the event-loop-starvation lesson).
* Billing is **fail-soft everywhere** — a pricing/DB error must NEVER raise into
  the turn or audio path (the deduct is enrichment; the conversation is the work).

The mid-call cutoff on exhaustion (terminating the call when the balance runs out)
is **T6b-2** — this meter only bills; it never ends the call.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from persona.billing import (
    BillingConfig,
    MeteredBilling,
    livekit_infra_cents,
    voice_stt_cents,
    voice_tts_cents,
)
from persona.logging import get_logger
from persona_runtime.cost import compute_turn_cost

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from persona.billing import LedgerPort
    from persona_runtime.cost import CostSource
    from sqlalchemy import Engine

__all__ = ["TurnUsage", "VoiceTurnAccumulator", "VoiceTurnBillingMeter"]

_LOG = get_logger("voice.billing")

_SECONDS_PER_MINUTE = 60.0


@dataclass(frozen=True)
class TurnUsage:
    """One turn's summed metered quantities (the snapshot the meter prices)."""

    stt_streamed_seconds: float
    tts_chars: int
    llm_prompt_tokens: int
    llm_completion_tokens: int
    llm_cost_usd: float | None
    llm_provider: str
    llm_model: str


class VoiceTurnAccumulator:
    """Accumulates the CURRENT turn's metered quantities; snapshot-and-reset each turn.

    STT is read as a **delta**: the seam adapter's ``streamed_seconds`` is a
    session-cumulative counter (V8 rebase), so each turn bills the audio streamed
    *since the previous turn*. TTS chars + LLM usage are summed across the turn's
    rounds (a tool round + re-prompt is one turn, two model calls). ``cost_usd`` is
    the summed OpenRouter actual **iff every round reported one**, else ``None``
    (then the turn prices from tokens) — mirrors the T5 usage-collector discipline.
    """

    def __init__(self, *, streamed_seconds_reader: Callable[[], float]) -> None:
        self._read_streamed_seconds = streamed_seconds_reader
        self._last_streamed = 0.0
        self._tts_chars = 0
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._costs: list[float | None] = []
        self._provider = ""
        self._model = ""

    def note_tts_chars(self, count: int) -> None:
        """Add characters sent to the TTS backend for synthesis this turn."""
        self._tts_chars += max(0, count)

    def note_llm_usage(
        self,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        cost_usd: float | None,
        provider: str,
        model: str,
    ) -> None:
        """Add one model round's token usage (summed across the turn's rounds)."""
        self._prompt_tokens += max(0, prompt_tokens)
        self._completion_tokens += max(0, completion_tokens)
        self._costs.append(cost_usd)
        self._provider = provider  # representative served model (last round)
        self._model = model

    def take_turn(self) -> TurnUsage:
        """Snapshot this turn's totals (STT as a since-last delta) and reset for the next."""
        now = max(0.0, self._read_streamed_seconds())
        stt_delta = max(0.0, now - self._last_streamed)
        self._last_streamed = now
        cost_usd = (
            sum(c for c in self._costs if c is not None)
            if self._costs and all(c is not None for c in self._costs)
            else None
        )
        usage = TurnUsage(
            stt_streamed_seconds=stt_delta,
            tts_chars=self._tts_chars,
            llm_prompt_tokens=self._prompt_tokens,
            llm_completion_tokens=self._completion_tokens,
            llm_cost_usd=cost_usd,
            llm_provider=self._provider,
            llm_model=self._model,
        )
        self._tts_chars = 0
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._costs = []
        self._provider = ""
        self._model = ""
        return usage


class VoiceTurnBillingMeter:
    """Prices a turn's real cost and deducts it from the owner, off-loop + idempotent.

    Injected once per session over the served STT/TTS provider identities (read from
    the live backends) + a fresh-RLS-engine factory. The producer feeds it
    (:meth:`note_tts_chars` / :meth:`note_llm_usage`); the turn recorder fires
    :meth:`bill_turn` on each committed turn; teardown fires :meth:`bill_call_infra`.
    """

    def __init__(
        self,
        *,
        ledger: LedgerPort,
        billing_config: BillingConfig,
        engine_factory: Callable[[], Engine],
        user_id: str,
        call_id: str,
        stt_provider: str,
        stt_model: str,
        tts_provider: str,
        tts_model: str,
        streamed_seconds_reader: Callable[[], float],
        cost_source: CostSource | None = None,
        floor: int = 1,
        on_exhausted: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._billing = MeteredBilling(ledger=ledger, config=billing_config)
        self._billing_config = billing_config
        self._accumulator = VoiceTurnAccumulator(streamed_seconds_reader=streamed_seconds_reader)
        self._engine_factory = engine_factory
        self._user_id = user_id
        self._call_id = call_id
        self._stt_provider = stt_provider
        self._stt_model = stt_model
        self._tts_provider = tts_provider
        self._tts_model = tts_model
        self._cost_source = cost_source
        self._floor = floor
        # Spec M3 (T6b-2): fired ONCE when a per-turn deduct exhausts the balance
        # (captured < charged, or balance hit 0) — the mid-call cutoff. Late-bound
        # via ``set_on_exhausted`` (the cutoff needs the orchestrator + room, built
        # after this meter). ``None`` ⇒ metering-only (no cutoff).
        self._on_exhausted = on_exhausted
        self._exhausted_fired = False

    def set_on_exhausted(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Late-bind the exhaustion cutoff (Spec M3, T6b-2).

        The cutoff (speak-notice + delete-room) depends on the orchestrator + the
        room, built after this meter, so the composition root injects it here.
        """
        self._on_exhausted = callback

    # ----- producer feeds (called on the loop; cheap, no I/O) ----------------

    def note_tts_chars(self, count: int) -> None:
        """Record characters sent to TTS this turn (delegates to the accumulator)."""
        self._accumulator.note_tts_chars(count)

    def note_llm_usage(
        self,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        cost_usd: float | None,
        provider: str,
        model: str,
    ) -> None:
        """Record one model round's usage this turn (delegates to the accumulator)."""
        self._accumulator.note_llm_usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost_usd,
            provider=provider,
            model=model,
        )

    # ----- pricing (pure) ----------------------------------------------------

    def _price_turn(self, usage: TurnUsage) -> float:
        """The turn's real provider cost in cents = STT(served) + TTS(served) + LLM."""
        stt_cents, _ = voice_stt_cents(
            self._stt_provider, streamed_seconds=usage.stt_streamed_seconds, model=self._stt_model
        )
        tts_cents, _ = voice_tts_cents(
            self._tts_provider, chars=usage.tts_chars, model=self._tts_model
        )
        llm_cents, _ = compute_turn_cost(
            provider=usage.llm_provider,
            model=usage.llm_model,
            prompt_tokens=usage.llm_prompt_tokens,
            completion_tokens=usage.llm_completion_tokens,
            actual_cost_usd=usage.llm_cost_usd,
            source=self._cost_source,
        )
        return stt_cents + tts_cents + llm_cents

    # ----- deducts (off the audio loop, fail-soft) ---------------------------

    async def bill_turn(self, turn_seq: int) -> None:
        """Bill one committed turn's real cost to the owner, off-loop + idempotent.

        MUST NOT raise (the turn recorder runs this in its commit ``finally``). A
        turn with no metered cost (a fully-synthetic turn — no STT/TTS/LLM) charges
        nothing; otherwise the ``provider_meter`` deduct captures what's affordable.
        """
        try:
            usage = self._accumulator.take_turn()
            provider_cents = self._price_turn(usage)
            if provider_cents <= 0.0:
                return  # no real metered cost this turn → charge nothing
            billing_key = f"voice:{self._call_id}:{turn_seq}"
            exhausted = await asyncio.to_thread(self._charge_turn, provider_cents, billing_key)
            # Spec M3 (T6b-2): the balance ran out on THIS deduct → end the call,
            # once. Fired on the loop (the cutoff speaks + deletes the room); the
            # DB write already happened off-loop above.
            if exhausted and self._on_exhausted is not None and not self._exhausted_fired:
                self._exhausted_fired = True
                await self._on_exhausted()
        except Exception as exc:  # noqa: BLE001 — billing must never break the turn/audio path
            _LOG.warning(
                "voice per-turn billing failed (fail-soft) call={call} turn={seq}: {err}",
                call=self._call_id,
                seq=turn_seq,
                err=repr(exc)[:200],
            )

    def _charge_turn(self, provider_cents: float, billing_key: str) -> bool:
        """Capture the turn's charge off-loop; return whether it EXHAUSTED the balance.

        Exhausted = the capture landed less than the charge (balance was short) OR
        the balance is now at/below zero — either way the caller is out of credit.
        """
        engine = self._engine_factory()
        try:
            result = self._billing.charge(
                rls_engine=engine,
                user_id=self._user_id,
                provider_cents=provider_cents,
                infra_flat_cents=0.0,  # voice STT/TTS carry no infra add (LiveKit is its own tick)
                basis="provider_meter",
                reason="voice:provider_meter",
                floor=self._floor,
                mode="capture",  # a completed turn captures what's affordable, never negative
                billing_key=billing_key,
            )
        finally:
            engine.dispose()
        short = result.captured is not None and result.captured < result.charged
        return short or result.new_balance <= 0

    async def bill_call_infra(self, duration_s: int) -> None:
        """Bill the call's LiveKit infra (per-min) at teardown, off-loop + idempotent.

        MUST NOT raise (runs in the teardown suppress). Keyed once per call so a
        re-run of teardown is a clean no-op.
        """
        try:
            minutes = max(0, duration_s) / _SECONDS_PER_MINUTE
            infra_cents, _ = livekit_infra_cents(self._billing_config, minutes=minutes)
            if infra_cents <= 0.0:
                return
            billing_key = f"voice:{self._call_id}:livekit"
            await asyncio.to_thread(self._charge_infra, infra_cents, billing_key)
        except Exception as exc:  # noqa: BLE001 — billing must never break teardown
            _LOG.warning(
                "voice LiveKit infra billing failed (fail-soft) call={call}: {err}",
                call=self._call_id,
                err=repr(exc)[:200],
            )

    def _charge_infra(self, infra_cents: float, billing_key: str) -> None:
        engine = self._engine_factory()
        try:
            self._billing.charge(
                rls_engine=engine,
                user_id=self._user_id,
                provider_cents=0.0,
                infra_flat_cents=infra_cents,
                basis="infra_flat",
                reason="voice:infra_flat",
                floor=self._floor,
                mode="capture",
                billing_key=billing_key,
            )
        finally:
            engine.dispose()

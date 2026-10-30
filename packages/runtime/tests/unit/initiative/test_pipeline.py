"""Unit tests — the initiative pipeline (Spec A5, T7; the one enforced path).

Deterministic fakes for every seam. Pins: the GATE ORDER (grounding →
wellbeing-subject → dial → envelope → value → acceptance → decline →
arbitration → caps → delivery); no silent skips (every discard audited); the
p(accept) suppressor order (value first, both observable via audit reasons);
over-cap and quiet-hold HOLD (never drop); the flush's three staleness legs
(hold-age, why-now, grounding-dissolved ⇒ expire_stale — T3 ruling 3); and
fail-soft (a seam error discards + audits, never raises, never blocks the batch).
"""

# ruff: noqa: ARG002 — fakes deliberately ignore some args
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from persona.initiative import (
    CandidateSource,
    CitationKind,
    GroundingCitation,
    InitiativeCandidate,
    InitiativeDial,
    InitiativeSettings,
    InitiativeTrigger,
    PlannedStep,
    Urgency,
)
from persona.initiative.restraint import CadenceCounts
from persona.schedules.quiet_hours import QuietHours
from persona.tools.categories import ActionCategory
from persona_runtime.initiative import InitiativePipeline
from persona_runtime.initiative.grounding import GroundingRejection, GroundingVerdict

_NOW = datetime.now(UTC)
_SETTINGS = InitiativeSettings()


def _candidate(**overrides: Any) -> InitiativeCandidate:  # noqa: ANN401 — fixture override bag
    fields: dict[str, Any] = {
        "observation": "The hearing is Friday and nothing is drafted.",
        "citations": (GroundingCitation(kind=CitationKind.NODE, ref="node-91"),),
        "trigger": InitiativeTrigger.APPROACHING_COMMITMENT,
        "why_now": "The date entered the horizon.",
        "plan": (PlannedStep(description="draft", categories=frozenset({ActionCategory.DRAFT})),),
        "next_step": "Draft the letter.",
        "value": 0.9,
        "acceptance": 0.8,
        "urgency": Urgency.BATCH,
        "source": CandidateSource.SCAN,
        "owner_id": "u1",
        "persona_id": "p1",
        "prompt_version": "a5-scan-v11",
        "scanned_at": _NOW,
    }
    fields.update(overrides)
    return InitiativeCandidate(**fields)


class _Trace:
    """The shared gate-order recorder every fake appends to."""

    def __init__(self) -> None:
        self.calls: list[str] = []


class _FakeGrounding:
    def __init__(self, trace: _Trace, *, admit: bool = True, raises: bool = False) -> None:
        self._trace = trace
        self._admit = admit
        self._raises = raises

    async def check(self, candidate: InitiativeCandidate, **_: object) -> GroundingVerdict:
        self._trace.calls.append("grounding")
        if self._raises:
            msg = "grounding infra down"
            raise RuntimeError(msg)
        if self._admit:
            return GroundingVerdict(admitted=True, supporting_quote="the hearing is Friday")
        return GroundingVerdict(admitted=False, rejection=GroundingRejection.NOT_ENTAILED)


class _FakeWellbeing:
    def __init__(
        self,
        trace: _Trace,
        tagged: set[str] | None = None,
        *,
        unsynthesised: set[str] | None = None,
    ) -> None:
        self._trace = trace
        self._tagged = tagged or set()
        self._unsynthesised = unsynthesised or set()
        self.asked: list[tuple[list[str], bool]] = []

    def tagged_refs(
        self,
        owner_id: str,
        citations: tuple[GroundingCitation, ...],
        *,
        include_unsynthesised: bool,
    ) -> set[str]:
        self._trace.calls.append("wellbeing")
        refs = [c.ref for c in citations]
        self.asked.append((refs, include_unsynthesised))
        gated = self._tagged | (self._unsynthesised if include_unsynthesised else set())
        return gated & set(refs)


class _FakeDeclines:
    def __init__(self, trace: _Trace, declined: set[str] | None = None) -> None:
        self._trace = trace
        self._declined = declined or set()

    def suppressed_keys(self, owner_id: str, keys: list[str]) -> set[str]:
        self._trace.calls.append("declines")
        return self._declined & set(keys)


class _Claim:
    def __init__(self, notice_id: str, key: str) -> None:
        self.id = notice_id
        self.opportunity_key = key


class _Held:
    def __init__(self, candidate: InitiativeCandidate, *, created_at: datetime) -> None:
        self.id = "n-held"
        self.persona_id = candidate.persona_id
        self.candidate = candidate
        self.created_at = created_at


class _FakeLedger:
    def __init__(
        self,
        trace: _Trace,
        *,
        counts: CadenceCounts | None = None,
        claim_wins: bool = True,
        held: list[_Held] | None = None,
    ) -> None:
        self._trace = trace
        self._counts = counts or CadenceCounts(persona_day=0, persona_week=0, user_day=0)
        self._claim_wins = claim_wins
        self._held = held or []
        self.claims: list[tuple[str, str]] = []  # (opportunity_key, voicer)
        self.delivered: list[tuple[str, str]] = []  # (notice_id, envelope_action)
        self.expired: list[tuple[str, str]] = []  # (notice_id, reason)

    def try_claim_held(
        self,
        candidate: InitiativeCandidate,
        *,
        voicer_persona_id: str,
        held_until: datetime | None,
        now: datetime,
    ) -> _Claim | None:
        self._trace.calls.append("claim")
        self.claims.append((candidate.opportunity_key, voicer_persona_id))
        return _Claim("n-1", candidate.opportunity_key) if self._claim_wins else None

    def mark_delivered(
        self, owner_id: str, notice_id: str, *, envelope_action: str, now: datetime
    ) -> bool:
        self.delivered.append((notice_id, envelope_action))
        return True

    def expire_stale(self, owner_id: str, notice_id: str, *, reason: str, now: datetime) -> bool:
        self.expired.append((notice_id, reason))
        return True

    def held_for_owner(self, owner_id: str) -> list[_Held]:
        return list(self._held)

    def delivered_counts(self, owner_id: str, *, persona_id: str, now: datetime) -> CadenceCounts:
        self._trace.calls.append("counts")
        return self._counts


class _Context:
    def __init__(self, timezone: str = "UTC", quiet: QuietHours | None = None) -> None:
        self.timezone = timezone
        self.quiet_hours = quiet


class _FakeUsers:
    def __init__(self, trace: _Trace, context: _Context | None = None) -> None:
        self._trace = trace
        self._context = context or _Context()

    def user_context(self, owner_id: str) -> _Context:
        self._trace.calls.append("user_context")
        return self._context


class _FakeProvenance:
    def __init__(self, trace: _Trace, personas: list[str] | None = None) -> None:
        self._trace = trace
        self._personas = personas or []

    def citation_personas(self, owner_id: str, node_refs: list[str]) -> list[str]:
        self._trace.calls.append("provenance")
        return list(self._personas)

    def activity_rank(self, owner_id: str) -> dict[str, int]:
        return {}


class _FakeAuditor:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, str]] = []  # (event, key, detail)

    def record(self, owner_id: str, event: str, opportunity_key: str, detail: str) -> None:
        self.events.append((event, opportunity_key, detail))


class _FakeDelivery:
    def __init__(self, *, succeed: bool = True) -> None:
        self._succeed = succeed
        self.calls: list[tuple[str, str]] = []

    async def deliver(self, owner_id: str, notice_id: str, action: object) -> bool:
        self.calls.append((notice_id, str(action)))
        return self._succeed


class _Harness:
    """One assembled pipeline with every fake reachable for assertions."""

    def __init__(self, **overrides: Any) -> None:  # noqa: ANN401 — fixture override bag
        self.trace = _Trace()
        self.grounding = overrides.get("grounding") or _FakeGrounding(self.trace)
        self.wellbeing = overrides.get("wellbeing") or _FakeWellbeing(self.trace)
        self.declines = overrides.get("declines") or _FakeDeclines(self.trace)
        self.ledger = overrides.get("ledger") or _FakeLedger(self.trace)
        self.users = overrides.get("users") or _FakeUsers(self.trace)
        self.provenance = overrides.get("provenance") or _FakeProvenance(self.trace)
        self.auditor = _FakeAuditor()
        self.delivery = overrides.get("delivery")
        self.deferred = overrides.get("deferred_flush")
        dial = overrides.get("dial", InitiativeDial.PROPOSE_ONLY)
        extra: dict[str, Any] = {}
        if "clock" in overrides:
            extra["clock"] = overrides["clock"]
        self.pipeline = InitiativePipeline(
            grounding=self.grounding,
            wellbeing=self.wellbeing,
            declines=self.declines,
            ledger=self.ledger,
            users=self.users,
            provenance=self.provenance,
            auditor=self.auditor,
            dial_reader=lambda _o, _p: dial,
            settings=overrides.get("settings", _SETTINGS),
            delivery=self.delivery,
            deferred_flush=self.deferred,
            **extra,
        )

    def audit_events(self) -> list[str]:
        return [e for e, _k, _d in self.auditor.events]


class TestGateOrder:
    @pytest.mark.asyncio
    async def test_the_pinned_order_holds_on_the_happy_path(self) -> None:
        h = _Harness()
        await h.pipeline.submit((_candidate(),))
        assert h.trace.calls == [
            "user_context",  # the user's local date, for the judge (third review LOW 2)
            "grounding",
            "wellbeing",
            "declines",
            "provenance",
            "counts",
            "user_context",
            "claim",
        ]

    @pytest.mark.asyncio
    async def test_ungrounded_discards_before_wellbeing_is_consulted(self) -> None:
        h = _Harness()
        h.grounding = _FakeGrounding(h.trace, admit=False)
        h.pipeline._grounding = h.grounding  # noqa: SLF001 — rewire the one fake
        await h.pipeline.submit((_candidate(),))
        assert h.trace.calls == ["user_context", "grounding"]  # nothing after the first gate
        assert h.audit_events() == ["initiative.discard_ungrounded"]
        assert h.ledger.claims == []

    @pytest.mark.asyncio
    async def test_wellbeing_subject_discards_before_the_dial(self) -> None:
        h = _Harness(wellbeing=None)
        h.wellbeing = _FakeWellbeing(h.trace, tagged={"node-91"})
        h.pipeline._wellbeing = h.wellbeing  # noqa: SLF001
        await h.pipeline.submit((_candidate(),))
        assert h.trace.calls == ["user_context", "grounding", "wellbeing"]
        assert h.audit_events() == ["initiative.discard_wellbeing_subject"]
        assert h.ledger.claims == []


class TestDialAndEnvelope:
    @pytest.mark.asyncio
    async def test_dial_off_at_pipeline_time_discards_audited(self) -> None:
        h = _Harness(dial=InitiativeDial.OFF)
        await h.pipeline.submit((_candidate(),))
        assert h.audit_events() == ["initiative.discard_dial_off"]
        assert h.ledger.claims == []


class TestSuppressorOrder:
    @pytest.mark.asyncio
    async def test_value_is_read_before_acceptance(self) -> None:
        """A candidate failing BOTH thresholds discards as LOW VALUE — value first."""
        h = _Harness()
        await h.pipeline.submit((_candidate(value=0.1, acceptance=0.1),))
        assert h.audit_events() == ["initiative.discard_low_value"]

    @pytest.mark.asyncio
    async def test_acceptance_is_a_suppressor_only(self) -> None:
        """High value + low acceptance ⇒ suppressed (never rescued the other way)."""
        h = _Harness()
        await h.pipeline.submit((_candidate(value=0.95, acceptance=0.1),))
        assert h.audit_events() == ["initiative.discard_low_acceptance"]
        assert h.ledger.claims == []


class TestDeclineAndArbitration:
    @pytest.mark.asyncio
    async def test_live_decline_discards_before_any_claim(self) -> None:
        key = _candidate().opportunity_key
        h = _Harness(declines=None)
        h.declines = _FakeDeclines(h.trace, declined={key})
        h.pipeline._declines = h.declines  # noqa: SLF001
        await h.pipeline.submit((_candidate(),))
        assert h.audit_events() == ["initiative.discard_declined"]
        assert h.ledger.claims == []

    @pytest.mark.asyncio
    async def test_arbitration_hands_voicing_to_the_most_grounded_persona(self) -> None:
        h = _Harness(provenance=None)
        h.provenance = _FakeProvenance(h.trace, personas=["p2", "p2", "p1"])
        h.pipeline._provenance = h.provenance  # noqa: SLF001
        await h.pipeline.submit((_candidate(persona_id="p1"),))
        assert h.ledger.claims == [(_candidate().opportunity_key, "p2")]  # A5-D-6

    @pytest.mark.asyncio
    async def test_arbitration_loss_is_a_quiet_noop_here(self) -> None:
        """The store audits the duplicate; the pipeline just stops."""
        h = _Harness(ledger=None)
        h.ledger = _FakeLedger(h.trace, claim_wins=False)
        h.pipeline._ledger = h.ledger  # noqa: SLF001
        await h.pipeline.submit((_candidate(),))
        assert h.ledger.delivered == []


class TestRestraintHolds:
    @pytest.mark.asyncio
    async def test_over_cap_holds_never_drops(self) -> None:
        h = _Harness(ledger=None, delivery=_FakeDelivery())
        h.ledger = _FakeLedger(
            h.trace, counts=CadenceCounts(persona_day=1, persona_week=1, user_day=1)
        )
        h.pipeline._ledger = h.ledger  # noqa: SLF001
        await h.pipeline.submit((_candidate(),))
        assert len(h.ledger.claims) == 1  # claimed HELD
        assert h.ledger.delivered == []  # not delivered — waits for the flush
        assert h.audit_events() == []  # a hold is a disposition, not a discard

    @pytest.mark.asyncio
    async def test_batch_urgency_holds_even_with_an_executor(self) -> None:
        delivery = _FakeDelivery()
        h = _Harness(delivery=delivery)
        await h.pipeline.submit((_candidate(urgency=Urgency.BATCH),))
        assert len(h.ledger.claims) == 1
        assert delivery.calls == []  # batch-by-default: the morning flush delivers

    @pytest.mark.asyncio
    async def test_interrupt_claim_without_mechanical_deadline_batches(self) -> None:
        """The v1 posture (state.md flag): no deadline source ⇒ INTERRUPT resolves BATCH."""
        delivery = _FakeDelivery()
        h = _Harness(delivery=delivery)
        await h.pipeline.submit((_candidate(urgency=Urgency.INTERRUPT),))
        assert len(h.ledger.claims) == 1
        assert delivery.calls == []


class TestFlush:
    def _held(self, *, age_days: int = 0) -> _Held:
        return _Held(_candidate(), created_at=datetime.now(UTC) - timedelta(days=age_days))

    @pytest.mark.asyncio
    async def test_hold_age_expires_stale(self) -> None:
        h = _Harness(ledger=None, delivery=_FakeDelivery())
        h.ledger = _FakeLedger(h.trace, held=[self._held(age_days=8)])
        h.pipeline._ledger = h.ledger  # noqa: SLF001
        await h.pipeline.flush("u1")
        assert h.ledger.expired == [("n-held", "hold_age")]
        assert h.ledger.delivered == []

    @pytest.mark.asyncio
    async def test_dissolved_grounding_expires_stale(self) -> None:
        """The T7 re-grounding bar: citations dissolved ⇒ expire, never deliver."""
        h = _Harness(delivery=_FakeDelivery())
        h.grounding = _FakeGrounding(h.trace, admit=False)
        h.ledger = _FakeLedger(h.trace, held=[self._held()])
        h.pipeline._grounding = h.grounding  # noqa: SLF001
        h.pipeline._ledger = h.ledger  # noqa: SLF001
        await h.pipeline.flush("u1")
        assert h.ledger.expired == [("n-held", "grounding_dissolved")]
        assert h.ledger.delivered == []

    @pytest.mark.asyncio
    async def test_still_capped_stays_held(self) -> None:
        h = _Harness(delivery=_FakeDelivery())
        h.ledger = _FakeLedger(
            h.trace,
            held=[self._held()],
            counts=CadenceCounts(persona_day=1, persona_week=1, user_day=1),
        )
        h.pipeline._ledger = h.ledger  # noqa: SLF001
        await h.pipeline.flush("u1")
        assert h.ledger.expired == []
        assert h.ledger.delivered == []  # held for the NEXT flush — never dropped

    @pytest.mark.asyncio
    async def test_survivor_delivers_and_promotes(self) -> None:
        delivery = _FakeDelivery()
        h = _Harness(delivery=delivery)
        h.ledger = _FakeLedger(h.trace, held=[self._held()])
        h.pipeline._ledger = h.ledger  # noqa: SLF001
        await h.pipeline.flush("u1")
        assert delivery.calls == [("n-held", "propose")]  # StrEnum str() is the value
        assert h.ledger.delivered == [("n-held", "propose")]

    @pytest.mark.asyncio
    async def test_no_executor_stays_held_nothing_lost(self) -> None:
        h = _Harness(delivery=None)
        h.ledger = _FakeLedger(h.trace, held=[self._held()])
        h.pipeline._ledger = h.ledger  # noqa: SLF001
        await h.pipeline.flush("u1")
        assert h.ledger.expired == []
        assert h.ledger.delivered == []


class _FakeDeferredFlush:
    def __init__(self, *, raises: bool = False) -> None:
        self.calls: list[tuple[str, datetime]] = []  # (owner_id, release_at)
        self._raises = raises

    def schedule_flush(self, owner_id: str, *, release_at: datetime) -> None:
        self.calls.append((owner_id, release_at))
        if self._raises:
            msg = "queue unavailable"
            raise RuntimeError(msg)


class _BrokenUsers:
    def user_context(self, owner_id: str) -> _Context:
        msg = "users table unavailable"
        raise RuntimeError(msg)


class _Clock:
    """A settable "now" so one pipeline can live through the 07:00 and 08:00 flushes."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class TestFlushHonoursQuietHours:
    """R9-237: a held notice is never delivered inside the user's quiet hours."""

    # 2026-09-26 05:00 UTC = 07:00 in Oslo (CEST, UTC+2): the default scan hour.
    _SCAN_FIRE = datetime(2026, 9, 26, 5, 0, tzinfo=UTC)
    # 08:00 Oslo the same morning: the end of a 22:00 to 08:00 window.
    _WINDOW_END = datetime(2026, 9, 26, 6, 0, tzinfo=UTC)
    _NIGHT = QuietHours(start_minute=22 * 60, end_minute=8 * 60)

    def _harness(
        self,
        clock: _Clock,
        *,
        timezone: str = "Europe/Oslo",
        quiet: QuietHours | None = None,
        held: list[_Held] | None = None,
    ) -> tuple[_Harness, _FakeDelivery, _FakeDeferredFlush, _Context]:
        delivery = _FakeDelivery()
        deferred = _FakeDeferredFlush()
        context = _Context(timezone=timezone, quiet=quiet)
        h = _Harness(delivery=delivery, deferred_flush=deferred, clock=clock)
        h.ledger = _FakeLedger(
            h.trace,
            held=held
            if held is not None
            else [_Held(_candidate(), created_at=clock.now - timedelta(days=1))],
        )
        h.pipeline._ledger = h.ledger  # noqa: SLF001
        h.users = _FakeUsers(h.trace, context)
        h.pipeline._users = h.users  # noqa: SLF001
        return h, delivery, deferred, context

    @pytest.mark.asyncio
    async def test_held_through_the_0700_flush_and_delivered_by_the_0800_one(self) -> None:
        clock = _Clock(self._SCAN_FIRE)
        h, delivery, deferred, _ = self._harness(clock, quiet=self._NIGHT)

        await h.pipeline.flush("u1")
        assert delivery.calls == []
        assert h.ledger.delivered == []
        assert h.ledger.expired == []
        assert "grounding" not in h.trace.calls  # nothing spent inside the window
        assert deferred.calls == [("u1", self._WINDOW_END)]

        clock.now = deferred.calls[0][1]  # the deferred flush runs at its release instant
        await h.pipeline.flush("u1")
        assert delivery.calls == [("n-held", "propose")]
        assert h.ledger.delivered == [("n-held", "propose")]
        assert deferred.calls == [("u1", self._WINDOW_END)]  # nothing deferred again

    @pytest.mark.asyncio
    async def test_no_quiet_hours_delivers_at_0700_as_before(self) -> None:
        h, delivery, deferred, _ = self._harness(_Clock(self._SCAN_FIRE), quiet=None)
        await h.pipeline.flush("u1")
        assert delivery.calls == [("n-held", "propose")]
        assert h.ledger.delivered == [("n-held", "propose")]
        assert deferred.calls == []

    @pytest.mark.asyncio
    async def test_a_window_crossing_midnight_is_read_in_the_users_own_zone(self) -> None:
        # 16:30 UTC on 26 Sep is 01:30 on 27 Sep in Tokyo: inside 23:00 to 06:00 there,
        # outside it on a UTC clock. The window ends at 06:00 Tokyo = 21:00 UTC on 26 Sep.
        clock = _Clock(datetime(2026, 9, 26, 16, 30, tzinfo=UTC))
        tokyo_night = QuietHours(start_minute=23 * 60, end_minute=6 * 60)
        h, delivery, deferred, _ = self._harness(clock, timezone="Asia/Tokyo", quiet=tokyo_night)

        await h.pipeline.flush("u1")
        assert delivery.calls == []
        assert deferred.calls == [("u1", datetime(2026, 9, 26, 21, 0, tzinfo=UTC))]

        clock.now = deferred.calls[0][1]
        await h.pipeline.flush("u1")
        assert delivery.calls == [("n-held", "propose")]

    @pytest.mark.asyncio
    async def test_two_held_notices_ask_for_one_deferred_flush(self) -> None:
        clock = _Clock(self._SCAN_FIRE)
        first = _Held(_candidate(), created_at=clock.now - timedelta(days=1))
        second = _Held(_candidate(persona_id="p2"), created_at=clock.now - timedelta(days=2))
        second.id = "n-held-2"
        h, delivery, deferred, _ = self._harness(clock, quiet=self._NIGHT, held=[first, second])

        await h.pipeline.flush("u1")
        assert delivery.calls == []
        assert deferred.calls == [("u1", self._WINDOW_END)]

        clock.now = self._WINDOW_END
        await h.pipeline.flush("u1")
        assert delivery.calls == [("n-held", "propose"), ("n-held-2", "propose")]

    @pytest.mark.asyncio
    async def test_a_window_extended_meanwhile_still_holds_at_the_deferred_flush(self) -> None:
        clock = _Clock(self._SCAN_FIRE)
        h, delivery, deferred, context = self._harness(clock, quiet=self._NIGHT)
        await h.pipeline.flush("u1")

        context.quiet_hours = QuietHours(start_minute=22 * 60, end_minute=9 * 60)
        clock.now = self._WINDOW_END
        await h.pipeline.flush("u1")
        assert delivery.calls == []
        assert deferred.calls == [
            ("u1", self._WINDOW_END),
            ("u1", datetime(2026, 9, 26, 7, 0, tzinfo=UTC)),  # 09:00 Oslo
        ]

    @pytest.mark.asyncio
    async def test_without_a_deferral_seam_the_batch_stays_held(self) -> None:
        h, delivery, _, _ = self._harness(_Clock(self._SCAN_FIRE), quiet=self._NIGHT)
        h.pipeline._deferred_flush = None  # noqa: SLF001
        await h.pipeline.flush("u1")
        assert delivery.calls == []
        assert h.ledger.expired == []

    @pytest.mark.asyncio
    async def test_a_failed_enqueue_holds_the_batch_and_does_not_raise(self) -> None:
        h, delivery, _, _ = self._harness(_Clock(self._SCAN_FIRE), quiet=self._NIGHT)
        failing = _FakeDeferredFlush(raises=True)
        h.pipeline._deferred_flush = failing  # noqa: SLF001
        await h.pipeline.flush("u1")  # returns normally
        assert failing.calls == [("u1", self._WINDOW_END)]
        assert delivery.calls == []
        assert h.ledger.delivered == []
        assert h.ledger.expired == []

    @pytest.mark.asyncio
    async def test_unknown_quiet_hours_hold_the_batch_and_do_not_raise(self) -> None:
        h, delivery, deferred, _ = self._harness(_Clock(self._SCAN_FIRE), quiet=None)
        h.pipeline._users = _BrokenUsers()  # noqa: SLF001
        await h.pipeline.flush("u1")  # fails closed: never "deliver" on an unknown window
        assert delivery.calls == []
        assert h.ledger.delivered == []
        assert deferred.calls == []

    @pytest.mark.asyncio
    async def test_nothing_held_reads_no_quiet_hours_and_defers_nothing(self) -> None:
        h, _, deferred, _ = self._harness(_Clock(self._SCAN_FIRE), quiet=self._NIGHT, held=[])
        await h.pipeline.flush("u1")
        assert deferred.calls == []
        assert "user_context" not in h.trace.calls


class TestFailSoft:
    @pytest.mark.asyncio
    async def test_a_seam_error_discards_audited_and_the_batch_continues(self) -> None:
        h = _Harness()
        h.grounding = _FakeGrounding(h.trace, raises=True)
        h.pipeline._grounding = h.grounding  # noqa: SLF001
        # T4's checker never raises in production; the pipeline's own guard is
        # the second net — and one bad candidate must not block the rest.
        await h.pipeline.submit((_candidate(), _candidate()))
        assert h.audit_events() == ["initiative.discard_error", "initiative.discard_error"]


class TestWellbeingSubjectOverConversations:
    """R9-249 and its review: the subject rule judges EVERY cited item, of every kind."""

    @pytest.mark.asyncio
    async def test_a_candidate_citing_a_disclosing_conversation_is_discarded(self) -> None:
        h = _Harness(wellbeing=None)
        h.wellbeing = _FakeWellbeing(h.trace, tagged={"conv-7"})
        h.pipeline._wellbeing = h.wellbeing  # noqa: SLF001
        conv_only = _candidate(
            citations=(GroundingCitation(kind=CitationKind.CONVERSATION, ref="conv-7"),)
        )
        await h.pipeline.submit((conv_only,))
        assert h.audit_events() == ["initiative.discard_wellbeing_subject"]
        assert h.ledger.claims == []

    @pytest.mark.asyncio
    async def test_every_cited_item_is_asked_whatever_its_kind(self) -> None:
        """Nodes, conversations AND tasks (review MEDIUM-1: a task begun in a disclosure)."""
        h = _Harness(wellbeing=None)
        h.wellbeing = _FakeWellbeing(h.trace)
        h.pipeline._wellbeing = h.wellbeing  # noqa: SLF001
        mixed = _candidate(
            citations=(
                GroundingCitation(kind=CitationKind.NODE, ref="node-91"),
                GroundingCitation(kind=CitationKind.CONVERSATION, ref="conv-7"),
                GroundingCitation(kind=CitationKind.TASK, ref="task-3"),
            )
        )
        await h.pipeline.submit((mixed,))
        assert h.wellbeing.asked == [(["node-91", "conv-7", "task-3"], True)]

    @pytest.mark.asyncio
    async def test_a_gated_task_citation_discards_the_candidate(self) -> None:
        h = _Harness(wellbeing=None)
        h.wellbeing = _FakeWellbeing(h.trace, tagged={"task-3"})
        h.pipeline._wellbeing = h.wellbeing  # noqa: SLF001
        task_only = _candidate(citations=(GroundingCitation(kind=CitationKind.TASK, ref="task-3"),))
        await h.pipeline.submit((task_only,))
        assert h.audit_events() == ["initiative.discard_wellbeing_subject"]
        assert h.ledger.claims == []

    @pytest.mark.asyncio
    async def test_a_scan_candidate_is_held_to_the_synthesis_freshness_gate(self) -> None:
        """A conversation synthesis has not tagged yet is not the subject of a SCAN notice."""
        h = _Harness(wellbeing=None)
        h.wellbeing = _FakeWellbeing(h.trace, unsynthesised={"conv-new"})
        h.pipeline._wellbeing = h.wellbeing  # noqa: SLF001
        fresh = _candidate(
            citations=(GroundingCitation(kind=CitationKind.CONVERSATION, ref="conv-new"),)
        )
        await h.pipeline.submit((fresh,))
        assert h.audit_events() == ["initiative.discard_wellbeing_subject"]

    @pytest.mark.asyncio
    async def test_an_event_candidate_is_not_held_to_the_freshness_gate(self) -> None:
        """A7 events fire as a message arrives, before synthesis: freshness is scan-only."""
        h = _Harness(wellbeing=None)
        h.wellbeing = _FakeWellbeing(h.trace, unsynthesised={"conv-new"})
        h.pipeline._wellbeing = h.wellbeing  # noqa: SLF001
        event = _candidate(
            source=CandidateSource.EVENT,
            citations=(GroundingCitation(kind=CitationKind.CONVERSATION, ref="conv-new"),),
        )
        await h.pipeline.submit((event,))
        assert h.wellbeing.asked == [(["conv-new"], False)]
        assert "initiative.discard_wellbeing_subject" not in h.audit_events()


class _RaisingWellbeing:
    def tagged_refs(
        self,
        owner_id: str,
        citations: tuple[GroundingCitation, ...],
        *,
        include_unsynthesised: bool,
    ) -> set[str]:
        msg = "graph unavailable"
        raise RuntimeError(msg)


class TestFlushReappliesTheSubjectRule:
    """R9-249 review HIGH-A: every notice is BATCH-held in v1 and goes out at a flush, so
    the wellbeing rule and the prompt-version floor are re-applied there, before delivery."""

    def _harness(
        self,
        *,
        candidate: InitiativeCandidate | None = None,
        wellbeing: Any = None,  # noqa: ANN401
    ) -> tuple[_Harness, _FakeDelivery]:
        delivery = _FakeDelivery()
        h = _Harness(delivery=delivery)
        held = _Held(candidate or _candidate(), created_at=datetime.now(UTC))
        h.ledger = _FakeLedger(h.trace, held=[held])
        h.pipeline._ledger = h.ledger  # noqa: SLF001
        if wellbeing is not None:
            h.wellbeing = wellbeing
            h.pipeline._wellbeing = wellbeing  # noqa: SLF001
        return h, delivery

    @pytest.mark.asyncio
    async def test_a_conversation_disclosed_after_the_hold_expires_the_notice(self) -> None:
        """Monday a clean node is held; Tuesday its conversation discloses; Wednesday's
        flush must not deliver it."""
        h, delivery = self._harness()
        h.pipeline._wellbeing = _FakeWellbeing(h.trace, tagged={"node-91"})  # noqa: SLF001
        await h.pipeline.flush("u1")
        assert h.ledger.expired == [("n-held", "wellbeing_subject")]
        assert delivery.calls == []
        assert "grounding" not in h.trace.calls  # no judge spend on a notice that never goes out

    @pytest.mark.asyncio
    async def test_a_scan_notice_whose_source_synthesis_has_not_caught_up_stays_held(
        self,
    ) -> None:
        h, delivery = self._harness()
        h.pipeline._wellbeing = _FakeWellbeing(h.trace, unsynthesised={"node-91"})  # noqa: SLF001
        await h.pipeline.flush("u1")
        assert h.ledger.expired == []
        assert delivery.calls == []

    @pytest.mark.asyncio
    async def test_an_event_notice_is_not_held_to_freshness_at_the_flush(self) -> None:
        h, delivery = self._harness(candidate=_candidate(source=CandidateSource.EVENT))
        h.pipeline._wellbeing = _FakeWellbeing(h.trace, unsynthesised={"node-91"})  # noqa: SLF001
        await h.pipeline.flush("u1")
        assert delivery.calls == [("n-held", "propose")]

    @pytest.mark.asyncio
    async def test_a_failing_subject_check_keeps_the_notice_held(self) -> None:
        h, delivery = self._harness(wellbeing=_RaisingWellbeing())
        await h.pipeline.flush("u1")
        assert h.ledger.expired == []
        assert delivery.calls == []

    @pytest.mark.parametrize(
        "version",
        [
            "a5-scan-v1",
            "a5-scan-v4",
            "a5-scan-v5",
            "a5-scan-v6",
            "a5-scan-v7",
            "a5-scan-v8",
            "a5-scan-v9",
            "a5-scan-v10",
            "a7-event-candidate-v1",
            "a7-event-candidate-v2",
            "a7-event-candidate-v3",
            "a7-event-candidate-v4",
            "a7-event-candidate-v5",
            "a7-event-candidate-v6",
            "a7-event-candidate-v7",
            "v1",
            "scripted",
            "",
        ],
    )
    @pytest.mark.asyncio
    async def test_a_notice_from_a_superseded_prompt_expires_and_never_flushes(
        self, version: str
    ) -> None:
        stale = _candidate().model_copy(update={"prompt_version": version})
        h, delivery = self._harness(candidate=stale)
        await h.pipeline.flush("u1")
        assert h.ledger.expired == [("n-held", "prompt_superseded")]
        assert delivery.calls == []

    @pytest.mark.parametrize("version", ["a5-scan-v11", "a5-scan-v12", "a7-event-candidate-v8"])
    @pytest.mark.asyncio
    async def test_a_notice_from_a_current_prompt_flushes(self, version: str) -> None:
        current = _candidate().model_copy(update={"prompt_version": version})
        h, delivery = self._harness(candidate=current)
        await h.pipeline.flush("u1")
        assert delivery.calls == [("n-held", "propose")]

    def test_the_scan_prompt_in_force_is_deliverable(self) -> None:
        from persona.initiative import prompt_version_deliverable
        from persona_runtime.initiative import INITIATIVE_SCAN_PROMPT_VERSION

        assert prompt_version_deliverable(INITIATIVE_SCAN_PROMPT_VERSION)


class TestTheVoiceStage:
    """R9-256 and the re-review (LOW 2): the voice guard runs in the pipeline, for scans and
    A7 events alike, BEFORE grounding, and a void is audited so the rate is visible."""

    @pytest.mark.parametrize("source", [CandidateSource.SCAN, CandidateSource.EVENT])
    @pytest.mark.asyncio
    async def test_a_safe_object_is_rewritten_before_grounding_and_the_claim(
        self, source: CandidateSource
    ) -> None:
        seen: list[InitiativeCandidate] = []
        h = _Harness()
        real = h.grounding.check

        async def _check(candidate: InitiativeCandidate, **_: object) -> GroundingVerdict:
            seen.append(candidate)
            return await real(candidate)

        h.grounding.check = _check  # type: ignore[method-assign]
        third = _candidate(
            source=source,
            why_now="The user's hearing is Friday.",
            next_step="draft the letter and show it to the user for approval",
        )
        await h.pipeline.submit((third,))
        assert seen[0].why_now == "Your hearing is Friday."  # judged as the user reads it
        assert seen[0].next_step == "draft the letter and show it to you for approval"
        assert h.ledger.claims  # and claimed (stored) that way

    @pytest.mark.parametrize(
        ("field", "text"),
        [
            ("why_now", "The user expressed anxiety today."),
            ("next_step", "send the user manual"),
        ],
    )
    @pytest.mark.asyncio
    async def test_an_unsafe_third_person_text_is_voided_and_audited(
        self, field: str, text: str
    ) -> None:
        h = _Harness()
        await h.pipeline.submit((_candidate(**{field: text}),))
        assert h.audit_events() == ["initiative.discard_voice"]
        assert h.auditor.events[0][2] == "third_person"
        assert "grounding" not in h.trace.calls  # no judge spend on a void
        assert h.ledger.claims == []


class TestTooManyNodeCitations:
    """Re-review LOW 4: past the bounded node read the subject is gated whole, audited
    under its own reason (never mistaken for a wellbeing tag), with no read at all."""

    @pytest.mark.asyncio
    async def test_more_cited_nodes_than_the_bound_are_discarded_by_name(self) -> None:
        from persona.initiative import MAX_CITED_NODE_READS

        h = _Harness()
        many = _candidate(
            citations=tuple(
                GroundingCitation(kind=CitationKind.NODE, ref=f"node-{i}")
                for i in range(MAX_CITED_NODE_READS + 1)
            )
        )
        await h.pipeline.submit((many,))
        assert h.auditor.events == [
            (
                "initiative.discard_wellbeing_subject",
                many.opportunity_key,
                "too_many_node_citations",
            )
        ]
        assert h.wellbeing.asked == []
        assert h.ledger.claims == []

    @pytest.mark.asyncio
    async def test_the_bound_itself_is_still_judged(self) -> None:
        from persona.initiative import MAX_CITED_NODE_READS

        h = _Harness()
        refs = [f"node-{i}" for i in range(MAX_CITED_NODE_READS)]
        at_bound = _candidate(
            citations=tuple(GroundingCitation(kind=CitationKind.NODE, ref=r) for r in refs)
        )
        await h.pipeline.submit((at_bound,))
        assert h.wellbeing.asked == [(refs, True)]


class TestThirdReviewFlushOrderAndLocalDay:
    @pytest.mark.asyncio
    async def test_a_waiting_notice_still_ages_out(self) -> None:
        """LOW 6: hold age runs BEFORE the subject rule, so a notice the rule keeps
        waiting (synthesis behind) cannot hold its duplicate slot forever."""
        delivery = _FakeDelivery()
        h = _Harness(delivery=delivery)
        old = _Held(_candidate(), created_at=datetime.now(UTC) - timedelta(days=8))
        h.ledger = _FakeLedger(h.trace, held=[old])
        h.pipeline._ledger = h.ledger  # noqa: SLF001
        h.pipeline._wellbeing = _FakeWellbeing(h.trace, unsynthesised={"node-91"})  # noqa: SLF001
        await h.pipeline.flush("u1")
        assert h.ledger.expired == [("n-held", "hold_age")]
        assert delivery.calls == []

    @pytest.mark.asyncio
    async def test_the_judge_gets_the_users_local_date_and_zone(self) -> None:
        """LOW 2: 23:30 UTC on 25 September is already 26 September in Oslo."""
        seen: list[dict[str, object]] = []
        clock_now = datetime(2026, 9, 25, 23, 30, tzinfo=UTC)
        h = _Harness(users=None, clock=lambda: clock_now, delivery=_FakeDelivery())
        h.users = _FakeUsers(h.trace, _Context(timezone="Europe/Oslo"))
        h.pipeline._users = h.users  # noqa: SLF001
        real = h.grounding.check

        async def _check(candidate: InitiativeCandidate, **kwargs: object) -> GroundingVerdict:
            seen.append(kwargs)
            return await real(candidate)

        h.grounding.check = _check  # type: ignore[method-assign]
        await h.pipeline.submit((_candidate(),))
        assert seen == [{"today": date(2026, 9, 26), "timezone": "Europe/Oslo"}]

    @pytest.mark.asyncio
    async def test_an_unreadable_timezone_judges_in_utc(self) -> None:
        seen: list[dict[str, object]] = []

        class _BrokenUsers:
            def user_context(self, owner_id: str) -> _Context:
                msg = "users table unavailable"
                raise RuntimeError(msg)

        h = _Harness(clock=lambda: datetime(2026, 9, 25, 23, 30, tzinfo=UTC))
        h.pipeline._users = _BrokenUsers()  # noqa: SLF001
        real = h.grounding.check

        async def _check(candidate: InitiativeCandidate, **kwargs: object) -> GroundingVerdict:
            seen.append(kwargs)
            return await real(candidate)

        h.grounding.check = _check  # type: ignore[method-assign]
        await h.pipeline.submit((_candidate(),))
        assert seen[0] == {"today": date(2026, 9, 25), "timezone": "UTC"}


def test_the_scan_floor_is_the_version_in_force() -> None:
    """Third review LOW 5: the deliverable floor is the scan prompt in force (a5-scan-v11)."""
    from persona.initiative import MIN_DELIVERABLE_PROMPT_VERSIONS, prompt_version_deliverable
    from persona_runtime.initiative import INITIATIVE_SCAN_PROMPT_VERSION

    assert INITIATIVE_SCAN_PROMPT_VERSION == "a5-scan-v11"
    assert MIN_DELIVERABLE_PROMPT_VERSIONS["a5-scan"] == 11
    assert prompt_version_deliverable(INITIATIVE_SCAN_PROMPT_VERSION)
    assert not prompt_version_deliverable("a5-scan-v10")


class _FingerprintGrounding:
    """Admits with a fixed fingerprint and records what each call was given."""

    def __init__(self, *, kept: bool = False) -> None:
        self.kwargs: list[dict[str, object]] = []
        self._kept = kept

    async def check(self, candidate: InitiativeCandidate, **kwargs: object) -> GroundingVerdict:
        self.kwargs.append(kwargs)
        return GroundingVerdict(
            admitted=True, fingerprint="fp-admitted", served_model="m-head", kept=self._kept
        )


class _ContextDownGrounding:
    """The judge's context read failed (a transient database error)."""

    async def check(self, candidate: InitiativeCandidate, **kwargs: object) -> GroundingVerdict:  # noqa: ARG002
        return GroundingVerdict(admitted=False, rejection=GroundingRejection.CONTEXT_UNAVAILABLE)


class TestATransientContextErrorKeepsTheNoticeHeld:
    """Review LOW-5: at the flush a failed context read keeps the notice held for the next
    flush (the hold age still bounds it); at submit it is discarded, audited."""

    @pytest.mark.asyncio
    async def test_the_flush_keeps_it_held(self) -> None:
        delivery = _FakeDelivery()
        h = _Harness(delivery=delivery)
        held = _Held(_candidate(), created_at=datetime.now(UTC))
        h.ledger = _FakeLedger(h.trace, held=[held])
        h.pipeline._ledger = h.ledger  # noqa: SLF001
        h.pipeline._grounding = _ContextDownGrounding()  # noqa: SLF001
        await h.pipeline.flush("u1")
        assert h.ledger.expired == []
        assert delivery.calls == []

    @pytest.mark.asyncio
    async def test_the_submit_discards_it(self) -> None:
        h = _Harness()
        h.pipeline._grounding = _ContextDownGrounding()  # noqa: SLF001
        await h.pipeline.submit((_candidate(),))
        assert h.auditor.events[0][0] == "initiative.discard_ungrounded"
        assert h.auditor.events[0][2] == "context_unavailable"


class TestTheFingerprintRidesOnTheHeldNotice:
    @pytest.mark.asyncio
    async def test_the_claimed_candidate_carries_its_grounding_fingerprint(self) -> None:
        h = _Harness()
        claimed: list[InitiativeCandidate] = []
        real = h.ledger.try_claim_held

        def _claim(candidate: InitiativeCandidate, **kwargs: Any) -> Any:  # noqa: ANN401
            claimed.append(candidate)
            return real(candidate, **kwargs)

        h.ledger.try_claim_held = _claim  # type: ignore[method-assign]
        h.pipeline._grounding = _FingerprintGrounding()  # noqa: SLF001
        await h.pipeline.submit((_candidate(),))
        assert [c.grounding_fingerprint for c in claimed] == ["fp-admitted"]
        assert [c.grounding_served_model for c in claimed] == ["m-head"]

    @pytest.mark.asyncio
    async def test_the_flush_passes_the_stored_fingerprint_to_the_check(self) -> None:
        grounding = _FingerprintGrounding()
        h = _Harness(delivery=_FakeDelivery())
        stored = _candidate().model_copy(
            update={"grounding_fingerprint": "fp-stored", "grounding_served_model": "m-stored"}
        )
        h.ledger = _FakeLedger(h.trace, held=[_Held(stored, created_at=datetime.now(UTC))])
        h.pipeline._ledger = h.ledger  # noqa: SLF001
        h.pipeline._grounding = grounding  # noqa: SLF001
        await h.pipeline.flush("u1")
        assert grounding.kwargs[0]["admitted_fingerprint"] == "fp-stored"
        assert grounding.kwargs[0]["admitted_served_model"] == "m-stored"
        assert "initiative.grounding_kept" not in h.audit_events()  # it was judged

    @pytest.mark.asyncio
    async def test_a_kept_verdict_is_audited_at_the_flush(self) -> None:
        """Fifth review LOW-1: each keep is visible, so a re-run can count them."""
        h = _Harness(delivery=_FakeDelivery())
        stored = _candidate().model_copy(update={"grounding_fingerprint": "fp-admitted"})
        h.ledger = _FakeLedger(h.trace, held=[_Held(stored, created_at=datetime.now(UTC))])
        h.pipeline._ledger = h.ledger  # noqa: SLF001
        h.pipeline._grounding = _FingerprintGrounding(kept=True)  # noqa: SLF001
        await h.pipeline.flush("u1")
        assert h.auditor.events == [
            ("initiative.grounding_kept", stored.opportunity_key, "unchanged_head_undated")
        ]


class TestTheFlushLapsesAPassedDate:
    """v6: a held notice expires once the latest date its message states is before the
    user's local today (the why-now time half, anchored mechanically)."""

    def _harness(
        self,
        why_now: str,
        now: datetime,
        *,
        next_step: str = "Draft the letter.",
        scanned_at: datetime = datetime(2026, 9, 26, 5, 0, tzinfo=UTC),
    ) -> tuple[_Harness, _FakeDelivery]:
        delivery = _FakeDelivery()
        h = _Harness(delivery=delivery, clock=lambda: now, users=None)
        h.users = _FakeUsers(h.trace, _Context(timezone="Europe/Oslo"))
        h.pipeline._users = h.users  # noqa: SLF001
        held = _candidate(why_now=why_now, next_step=next_step, scanned_at=scanned_at)
        h.ledger = _FakeLedger(h.trace, held=[_Held(held, created_at=now - timedelta(hours=24))])
        h.pipeline._ledger = h.ledger  # noqa: SLF001
        return h, delivery

    @pytest.mark.asyncio
    async def test_a_date_already_past_in_the_users_zone_expires_the_notice(self) -> None:
        # 23:30 UTC on 1 October is 01:30 on 2 October in Oslo: 1 October has passed there.
        h, delivery = self._harness(
            "Your quote is valid until Thursday 1 October.",
            datetime(2026, 10, 1, 23, 30, tzinfo=UTC),
        )
        await h.pipeline.flush("u1")
        assert h.ledger.expired == [("n-held", "why_now_lapsed")]
        assert delivery.calls == []
        assert "grounding" not in h.trace.calls  # no judge spend on a lapsed notice

    @pytest.mark.asyncio
    async def test_a_date_still_today_or_ahead_delivers(self) -> None:
        h, delivery = self._harness(
            "Your quote is valid until Thursday 1 October.",
            datetime(2026, 10, 1, 5, 0, tzinfo=UTC),
        )
        await h.pipeline.flush("u1")
        assert h.ledger.expired == []
        assert delivery.calls == [("n-held", "propose")]

    @pytest.mark.asyncio
    async def test_a_message_with_no_date_never_lapses_by_time(self) -> None:
        h, delivery = self._harness(
            "Your quote is still open.", datetime(2026, 12, 1, 5, 0, tzinfo=UTC)
        )
        h.pipeline._settings = InitiativeSettings(hold_max_days=400)  # noqa: SLF001
        await h.pipeline.flush("u1")
        assert delivery.calls == [("n-held", "propose")]

    @pytest.mark.asyncio
    async def test_the_earliest_upcoming_date_is_the_moment(self) -> None:
        """Fourth review LOW 3: the outbound flight has left, so the notice lapses even
        though the return is still ahead."""
        h, delivery = self._harness(
            "You fly out on Saturday 3 October and back on Saturday 10 October.",
            datetime(2026, 10, 4, 5, 0, tzinfo=UTC),
        )
        await h.pipeline.flush("u1")
        assert h.ledger.expired == [("n-held", "why_now_lapsed")]
        assert delivery.calls == []

    @pytest.mark.asyncio
    async def test_a_date_in_the_offer_counts(self) -> None:
        h, delivery = self._harness(
            "Your quote is still open.",
            datetime(2026, 10, 1, 5, 0, tzinfo=UTC),
            next_step="book the fitter before 30 September",
        )
        await h.pipeline.flush("u1")
        assert h.ledger.expired == [("n-held", "why_now_lapsed")]
        assert delivery.calls == []

    @pytest.mark.asyncio
    async def test_a_date_already_past_when_written_is_not_the_moment(self) -> None:
        h, delivery = self._harness(
            "Power dropped the price on 25 September; the offer runs until 29 September.",
            datetime(2026, 9, 28, 5, 0, tzinfo=UTC),
        )
        await h.pipeline.flush("u1")
        assert h.ledger.expired == []
        assert delivery.calls == [("n-held", "propose")]

    @pytest.mark.asyncio
    async def test_the_writing_day_is_the_users_local_day(self) -> None:
        # Scanned 23:30 UTC on 25 September: already 26 September in Oslo, so the
        # 25 September date is history and 30 September is the moment.
        h, delivery = self._harness(
            "The price dropped on 25 September; the offer runs until 30 September.",
            datetime(2026, 9, 27, 5, 0, tzinfo=UTC),
            scanned_at=datetime(2026, 9, 25, 23, 30, tzinfo=UTC),
        )
        await h.pipeline.flush("u1")
        assert h.ledger.expired == []
        assert delivery.calls == [("n-held", "propose")]


_POLICY = (
    "The car insurance renews on Sunday 4 October 2026 (04.10.2026, 4/10/2026, 2026-10-04); "
    "the policy runs 12 months; the renewal notice was sent in May."
)


class _PolicySource:
    """The real checker's resolution surface: node-91 holds the policy excerpt."""

    def node_content(self, owner_id: str, node_id: str) -> str | None:  # noqa: ARG002
        return _POLICY if node_id == "node-91" else None

    def conversation_content(self, owner_id: str, conversation_id: str) -> str | None:  # noqa: ARG002
        return None

    def task_content(self, owner_id: str, task_id: str) -> str | None:  # noqa: ARG002
        return None

    def node_context(self, owner_id: str, node_id: str, *, limit: int) -> list[Any]:  # noqa: ARG002
        return []


class _NoJudge:
    """A judge that now says NO, counting its calls (asked only when not kept)."""

    provider_name = "openrouter"
    model_name = "openai/gpt-oss-120b"
    supports_native_tools = False
    supports_vision = False

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages: Any, **_: Any) -> Any:  # noqa: ANN401, ARG002
        from persona.backends.types import ChatResponse, TokenUsage

        self.calls += 1
        return ChatResponse(
            content='{"entailed": false}',
            model=self.model_name,
            provider=self.provider_name,
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            latency_ms=1.0,
        )


class TestEachTimeFamilyIsJudgedAgainAtTheFlush:
    """Fifth review LOW-4: a real GroundingChecker in the pipeline, the grounds unchanged
    and the head model's YES stored; a message naming a time in any family is still
    re-judged (here the judge now says no, so the notice dissolves), and an undated one
    is kept without a model call."""

    _NOW = datetime(2026, 9, 27, 5, 0, tzinfo=UTC)

    def _flush(self, why_now: str) -> tuple[_Harness, _NoJudge]:
        from persona_runtime.initiative.grounding import GroundingChecker, excerpts_fingerprint

        judge = _NoJudge()
        h = _Harness(delivery=_FakeDelivery(), clock=lambda: self._NOW)
        held = _candidate(
            why_now=why_now,
            scanned_at=datetime(2026, 9, 26, 5, 0, tzinfo=UTC),
            grounding_fingerprint=excerpts_fingerprint([_POLICY]),
            grounding_served_model="openrouter/openai/gpt-oss-120b",
        )
        h.ledger = _FakeLedger(h.trace, held=[_Held(held, created_at=self._NOW)])
        h.pipeline._ledger = h.ledger  # noqa: SLF001
        h.pipeline._grounding = GroundingChecker(source=_PolicySource(), backend=judge)  # noqa: SLF001  # type: ignore[arg-type]
        return h, judge

    @pytest.mark.parametrize(
        "why_now",
        [
            "Your car insurance renews on Sunday 4 October.",  # weekday
            "Your car insurance renews on Sun 4 Oct.",  # abbreviations
            "Your car insurance renews in October.",  # month
            "Your car insurance renews in Oct.",  # month abbreviation
            "The renewal notice was sent in May.",  # May in context
            "Your car insurance renews on the 4th of October.",  # ordinal day
            "Your car insurance renews on 2026-10-04.",  # ISO
            "Your car insurance renews on 04.10.2026.",  # dd.mm.yyyy
            "Your car insurance renews on 4/10/2026.",  # d/m/yyyy
            "Your car insurance policy runs 12 months.",  # counted duration
            "This year's car insurance renewal is due.",  # exempt possessive period
            "Your car insurance renews in a day or two.",  # exempt "in a day or two"
        ],
    )
    @pytest.mark.asyncio
    async def test_a_dated_message_is_judged_again(self, why_now: str) -> None:
        h, judge = self._flush(why_now)
        await h.pipeline.flush("u1")
        assert judge.calls == 1
        assert h.ledger.expired == [("n-held", "grounding_dissolved")]

    @pytest.mark.asyncio
    async def test_a_relative_phrase_is_refused_without_a_model_call(self) -> None:
        h, judge = self._flush("Your car insurance renews tomorrow.")
        await h.pipeline.flush("u1")
        assert judge.calls == 0
        assert h.ledger.expired == [("n-held", "grounding_dissolved")]

    @pytest.mark.asyncio
    async def test_an_undated_message_is_kept_without_a_model_call(self) -> None:
        h, judge = self._flush("Your car insurance renewal is worth a look.")
        await h.pipeline.flush("u1")
        assert judge.calls == 0
        assert h.ledger.expired == []
        assert "initiative.grounding_kept" in h.audit_events()


class TestTheRelativeDayIsNormalised:
    """B9 (ruling after the third MID set): "tomorrow" or "today" directly before the
    full date it names, in the owner's zone at scan time, is removed before the judge, so
    the judged, fingerprinted, stored and delivered text all carry the date alone; each
    removal is audited (the word, never content). Anything else reaches the refusal."""

    class _SeeingGrounding(_FakeGrounding):
        def __init__(self, trace: _Trace) -> None:
            super().__init__(trace)
            self.seen: list[InitiativeCandidate] = []

        async def check(self, candidate: InitiativeCandidate, **kwargs: object) -> GroundingVerdict:
            self.seen.append(candidate)
            return await super().check(candidate, **kwargs)

    class _StoringLedger(_FakeLedger):
        def __init__(self, trace: _Trace) -> None:
            super().__init__(trace)
            self.stored: list[InitiativeCandidate] = []

        def try_claim_held(self, candidate: InitiativeCandidate, **kwargs: Any) -> _Claim | None:  # noqa: ANN401
            self.stored.append(candidate)
            return super().try_claim_held(candidate, **kwargs)

    async def _submit(
        self, why_now: str, *, zone: str, now: datetime
    ) -> tuple[_Harness, TestTheRelativeDayIsNormalised._SeeingGrounding]:
        trace = _Trace()
        grounding = self._SeeingGrounding(trace)
        h = _Harness(
            grounding=grounding,
            ledger=self._StoringLedger(trace),
            users=_FakeUsers(trace, _Context(timezone=zone)),
            clock=lambda: now,
        )
        await h.pipeline.submit((_candidate(why_now=why_now),))
        return h, grounding

    @pytest.mark.asyncio
    async def test_the_matching_date_is_kept_and_the_word_removed(self) -> None:
        now = datetime(2027, 3, 11, 6, 0, tzinfo=UTC)
        text = "Your choir concert is tomorrow, Friday 12 March 2027, at the town hall."
        h, grounding = await self._submit(text, zone="Europe/Oslo", now=now)
        expected = "Your choir concert is on Friday 12 March 2027, at the town hall."
        assert [c.why_now for c in grounding.seen] == [expected]
        assert [c.why_now for c in h.ledger.stored] == [expected]  # type: ignore[attr-defined]
        normalised = [e for e in h.auditor.events if e[0] == "initiative.relative_day_normalised"]
        assert [e[2] for e in normalised] == ["tomorrow"]

    @pytest.mark.asyncio
    async def test_a_mismatching_date_is_left_for_the_refusal(self) -> None:
        now = datetime(2027, 3, 11, 6, 0, tzinfo=UTC)
        text = "Your choir concert is tomorrow, Saturday 13 March 2027."
        h, grounding = await self._submit(text, zone="Europe/Oslo", now=now)
        assert [c.why_now for c in grounding.seen] == [text]
        assert not [e for e in h.auditor.events if e[0] == "initiative.relative_day_normalised"]

    @pytest.mark.parametrize(
        ("zone", "normalised"),
        [("Europe/Oslo", True), ("UTC", False)],
    )
    @pytest.mark.asyncio
    async def test_tomorrow_is_read_in_the_owners_zone(self, zone: str, normalised: bool) -> None:
        """23:30 UTC on 11 March is already 12 March in Oslo, so there "tomorrow" is the
        13th; in UTC it is still the 12th, which the text does not name."""
        now = datetime(2027, 3, 11, 23, 30, tzinfo=UTC)
        text = "Your choir concert is tomorrow, Saturday 13 March 2027."
        h, grounding = await self._submit(text, zone=zone, now=now)
        expected = "Your choir concert is on Saturday 13 March 2027." if normalised else text
        assert [c.why_now for c in grounding.seen] == [expected]

    @pytest.mark.asyncio
    async def test_the_offer_is_normalised_too(self) -> None:
        trace = _Trace()
        grounding = self._SeeingGrounding(trace)
        h = _Harness(
            grounding=grounding,
            ledger=self._StoringLedger(trace),
            users=_FakeUsers(trace, _Context(timezone="UTC")),
            clock=lambda: datetime(2027, 3, 11, 6, 0, tzinfo=UTC),
        )
        offer = "book seats for the concert tomorrow, Friday 12 March 2027"
        await h.pipeline.submit((_candidate(next_step=offer),))
        assert [c.next_step for c in grounding.seen] == [
            "book seats for the concert Friday 12 March 2027"
        ]
        normalised = [e for e in h.auditor.events if e[0] == "initiative.relative_day_normalised"]
        assert [e[2] for e in normalised] == ["tomorrow"]

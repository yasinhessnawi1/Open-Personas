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

from datetime import UTC, datetime, timedelta
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
from persona.schedules.quiet_hours import QuietHours  # noqa: TC002 — runtime default arg
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
        "prompt_version": "a5-scan-v1",
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

    async def check(self, candidate: InitiativeCandidate) -> GroundingVerdict:
        self._trace.calls.append("grounding")
        if self._raises:
            msg = "grounding infra down"
            raise RuntimeError(msg)
        if self._admit:
            return GroundingVerdict(admitted=True, supporting_quote="the hearing is Friday")
        return GroundingVerdict(admitted=False, rejection=GroundingRejection.NOT_ENTAILED)


class _FakeWellbeing:
    def __init__(self, trace: _Trace, tagged: set[str] | None = None) -> None:
        self._trace = trace
        self._tagged = tagged or set()

    def tagged_refs(self, owner_id: str, node_refs: list[str]) -> set[str]:
        self._trace.calls.append("wellbeing")
        return self._tagged & set(node_refs)


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
        dial = overrides.get("dial", InitiativeDial.PROPOSE_ONLY)
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
        )

    def audit_events(self) -> list[str]:
        return [e for e, _k, _d in self.auditor.events]


class TestGateOrder:
    @pytest.mark.asyncio
    async def test_the_pinned_order_holds_on_the_happy_path(self) -> None:
        h = _Harness()
        await h.pipeline.submit((_candidate(),))
        assert h.trace.calls == [
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
        assert h.trace.calls == ["grounding"]  # nothing after the first gate
        assert h.audit_events() == ["initiative.discard_ungrounded"]
        assert h.ledger.claims == []

    @pytest.mark.asyncio
    async def test_wellbeing_subject_discards_before_the_dial(self) -> None:
        h = _Harness(wellbeing=None)
        h.wellbeing = _FakeWellbeing(h.trace, tagged={"node-91"})
        h.pipeline._wellbeing = h.wellbeing  # noqa: SLF001
        await h.pipeline.submit((_candidate(),))
        assert h.trace.calls == ["grounding", "wellbeing"]
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

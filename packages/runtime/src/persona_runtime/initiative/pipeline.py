"""The initiative pipeline — the ONE enforced path (Spec A5, T7; spec §2).

Every candidate — scan-sourced today, A7's event-sourced tomorrow (the
``CandidateSource`` tag; A7 plugs in without touching a single gate) — passes
the SAME gates in the SAME pinned order:

    1. grounding (T4: mechanical resolution + entailment — no grounding, no
       candidate)
    2. the K4/wellbeing-subject rule (a candidate citing ANY wellbeing-tagged
       node is discarded — subject exclusion, A5-D-X-k4-initiative-side;
       handling stays K4's, through the normal persona runtime at delivery)
    3. the dial (OFF ⇒ nothing; the envelope converts under PROPOSE_ONLY)
    4. the envelope (T2: act within, propose at the gates, borderline proposes)
    5. restraint (T2 policy + T3 stores): value threshold → acceptance floor
       (the SUPPRESSOR — read only after the structural anti-engagement
       checks, i.e. the closed trigger catalogue enforced at construction;
       A5-D-X-paccept-suppressor) → decline check (a live decline binds ALL
       personas) → arbitration (A5-D-6, before the insert) → cadence caps
       (over-cap HOLDS, never drops) → delivery resolution (quiet hours
       ABSOLUTE).

Dispositions are persisted + audited at every exit — a candidate leaves this
pipeline as a ledger row (held/delivered) or as an audited discard; there are
NO silent skips. The flush (ruling 4: flush-on-next-scan) re-validates every
held notice BOTH ways a hold can rot (T3 ruling 3 + the T7 bar): the why-now
time half (T2's pure predicate), the citations-still-resolve half (the T4
check re-run), and a hold-age bound — a stale hold is EXPIRED with the
``suppressed_stale`` disposition + audit row, never delivered.

Fail-soft is absolute: any stage error discards the candidate (audited) —
silence is the safe state; the pipeline never raises into the scan job.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from persona.initiative import InitiativeCandidate, InitiativeDial, Urgency
from persona.initiative.arbitration import arbitrate_voicer
from persona.initiative.envelope import EnvelopeAction, decide_envelope
from persona.initiative.restraint import (
    DeliveryKind,
    cadence_exhausted,
    meets_acceptance_floor,
    meets_value_threshold,
    resolve_delivery,
    why_now_lapsed,
)
from persona.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from persona.initiative import InitiativeSettings
    from persona.initiative.restraint import CadenceCounts, DeliveryResolution
    from persona.schedules.quiet_hours import QuietHours

    from persona_runtime.initiative.grounding import GroundingChecker

__all__ = [
    "HeldNotice",
    "InitiativePipeline",
    "NoticeClaim",
    "NoticeLedger",
    "PipelineAuditor",
    "UserContext",
    "UserContextReader",
    "WellbeingSubjectCheck",
]

_logger = get_logger("runtime.initiative.pipeline")


# --- the injected seams (api implements; T7's wiring) --------------------------


@runtime_checkable
class WellbeingSubjectCheck(Protocol):
    """The subject-exclusion read: which cited node refs are wellbeing-tagged."""

    def tagged_refs(self, owner_id: str, node_refs: Sequence[str]) -> set[str]:
        """The subset of ``node_refs`` carrying ANY wellbeing category."""
        ...


@runtime_checkable
class DeclineReader(Protocol):
    """The live-decline check (T3's DeclineStore surface, narrowed)."""

    def suppressed_keys(self, owner_id: str, keys: Sequence[str]) -> set[str]:
        """The subset of ``keys`` with a LIVE decline (user-level, binds all personas)."""
        ...


class NoticeClaim(Protocol):
    """The post-claim confirmation the ledger returns (T3's NoticeRecord, narrowed)."""

    @property
    def id(self) -> str: ...
    @property
    def opportunity_key(self) -> str: ...


class HeldNotice(Protocol):
    """A held ledger row the flush re-validates (T3's NoticeRecord, narrowed)."""

    @property
    def id(self) -> str: ...
    @property
    def persona_id(self) -> str: ...
    @property
    def candidate(self) -> InitiativeCandidate: ...
    @property
    def created_at(self) -> datetime: ...


@runtime_checkable
class NoticeLedger(Protocol):
    """The T3 ledger surface the pipeline drives (api: ``InitiativeLedger``)."""

    def try_claim_held(
        self,
        candidate: InitiativeCandidate,
        *,
        voicer_persona_id: str,
        held_until: datetime | None,
        now: datetime,
    ) -> NoticeClaim | None:
        """Claim the opportunity slot as HELD; ``None`` = arbitration lost (audited)."""
        ...

    def mark_delivered(
        self, owner_id: str, notice_id: str, *, envelope_action: str, now: datetime
    ) -> bool:
        """Promote a held notice to delivered."""
        ...

    def expire_stale(self, owner_id: str, notice_id: str, *, reason: str, now: datetime) -> bool:
        """Expire a held notice — WRITES the disposition + audit row (T3 ruling 3)."""
        ...

    def held_for_owner(self, owner_id: str) -> Sequence[HeldNotice]:
        """The owner's held notices, oldest first (the flush read)."""
        ...

    def delivered_counts(self, owner_id: str, *, persona_id: str, now: datetime) -> CadenceCounts:
        """The A5-D-3 cap inputs (trailing windows, counted at delivery)."""
        ...


class UserContext(Protocol):
    """The user's restraint context (tz + quiet window)."""

    @property
    def timezone(self) -> str: ...
    @property
    def quiet_hours(self) -> QuietHours | None: ...


@runtime_checkable
class UserContextReader(Protocol):
    """Reads the user's tz + quiet window (A8's shared definitions, api-resolved)."""

    def user_context(self, owner_id: str) -> UserContext:
        """The owner's resolved timezone + quiet window (off-until-set)."""
        ...


@runtime_checkable
class ProvenanceReader(Protocol):
    """The arbitration signal: cited nodes' provenance persona-ids (A5-D-6)."""

    def citation_personas(self, owner_id: str, node_refs: Sequence[str]) -> list[str]:
        """One entry per provenance contribution carrying a persona_id."""
        ...

    def activity_rank(self, owner_id: str) -> Mapping[str, int]:
        """Persona → recent-activity count (the tie-break)."""
        ...


@runtime_checkable
class PipelineAuditor(Protocol):
    """The no-silent-skips sink: every pre-claim discard leaves an audit row."""

    def record(self, owner_id: str, event: str, opportunity_key: str, detail: str) -> None:
        """Append one audit event (best-effort; a failure never breaks the pipeline)."""
        ...


@runtime_checkable
class DeliveryExecutor(Protocol):
    """Executes a delivery — act-then-report (T8) or propose-first (T9).

    T7 wires ``None`` (deliver-now degrades to held-for-flush — nothing is
    lost); T8/T9 fill it.
    """

    async def deliver(self, owner_id: str, notice_id: str, action: EnvelopeAction) -> bool:
        """Deliver one notice; True = delivered (the ledger is then promoted).

        False keeps the notice HELD (retained for the next flush — a failed or
        not-yet-wired delivery loses nothing and never double-sends).
        """
        ...


# --- the pipeline ---------------------------------------------------------------


class InitiativePipeline:
    """The one enforced path from candidate to disposition (a ``CandidateSink``)."""

    def __init__(
        self,
        *,
        grounding: GroundingChecker,
        wellbeing: WellbeingSubjectCheck,
        declines: DeclineReader,
        ledger: NoticeLedger,
        users: UserContextReader,
        provenance: ProvenanceReader,
        auditor: PipelineAuditor,
        dial_reader: Callable[[str, str], InitiativeDial],
        settings: InitiativeSettings,
        delivery: DeliveryExecutor | None = None,
    ) -> None:
        """Inject every seam (api wires the real stores at the worker root)."""
        self._grounding = grounding
        self._wellbeing = wellbeing
        self._declines = declines
        self._ledger = ledger
        self._users = users
        self._provenance = provenance
        self._auditor = auditor
        self._dial_reader = dial_reader
        self._settings = settings
        self._delivery = delivery

    # --- the CandidateSink surface (T6's seam, filled) ------------------------

    async def submit(self, candidates: tuple[InitiativeCandidate, ...]) -> None:
        """Gate every candidate; never raises (silence is the safe state)."""
        for candidate in candidates:
            try:
                await self._gate_one(candidate)
            except Exception:  # noqa: BLE001 — one bad candidate never blocks the rest
                _logger.warning("pipeline stage failed; discarding candidate (fail-soft)")
                self._audit(candidate, "initiative.discard_error", "pipeline_error")

    async def _gate_one(self, candidate: InitiativeCandidate) -> None:
        now = datetime.now(UTC)
        owner = candidate.owner_id

        # (1) grounding — T4's two mechanical layers. No grounding, no candidate.
        verdict = await self._grounding.check(candidate)
        if not verdict.admitted:
            self._audit(
                candidate,
                "initiative.discard_ungrounded",
                verdict.rejection.value if verdict.rejection else "unknown",
            )
            return

        # (2) the wellbeing-subject rule — ANY tagged citation disqualifies the
        # SUBJECT (all five categories; stronger than K4's chat gate — ruling 6).
        node_refs = [c.ref for c in candidate.citations if c.kind.value == "node"]
        if node_refs and self._wellbeing.tagged_refs(owner, node_refs):
            self._audit(candidate, "initiative.discard_wellbeing_subject", "tagged_citation")
            return

        # (3) the dial — re-read at pipeline time (one source of truth; a dial
        # turned OFF between scan and pipeline still silences).
        dial = self._read_dial(owner, candidate.persona_id)
        # (4) the envelope — T2's fail-closed decision (borderline proposes).
        action = decide_envelope(candidate.category_footprint, dial)
        if action is EnvelopeAction.NONE:
            self._audit(candidate, "initiative.discard_dial_off", dial.value)
            return

        # (5) restraint — value first; the acceptance SUPPRESSOR only after the
        # structural anti-engagement checks (the closed catalogue enforced at
        # construction) and never before value (A5-D-X-paccept-suppressor).
        if not meets_value_threshold(candidate.value, self._settings):
            self._audit(candidate, "initiative.discard_low_value", f"value={candidate.value}")
            return
        if not meets_acceptance_floor(candidate.acceptance, self._settings):
            self._audit(
                candidate, "initiative.discard_low_acceptance", f"acc={candidate.acceptance}"
            )
            return

        # Decline memory — user-level, binds all personas (A5-D-4).
        if self._declines.suppressed_keys(owner, [candidate.opportunity_key]):
            self._audit(candidate, "initiative.discard_declined", "live_decline")
            return

        # Arbitration BEFORE the insert (A5-D-6); the ledger's unique makes any
        # remaining race harmless (the loser is audited by the store).
        voicer = arbitrate_voicer(
            candidate.persona_id,
            citation_provenance_personas=self._provenance.citation_personas(owner, node_refs),
            activity_rank=self._provenance.activity_rank(owner),
        )

        # Cadence caps — over-cap HOLDS to the next flush, never drops (A5-D-3).
        counts = self._ledger.delivered_counts(owner, persona_id=voicer, now=now)
        if cadence_exhausted(counts, self._settings):
            self._claim_held(candidate, voicer, held_until=None, now=now)
            return

        # Delivery resolution — quiet hours ABSOLUTE (the Phase-3 pin); v1 has no
        # mechanical deadline source, so interrupt claims resolve to BATCH (the
        # conservative default — flagged at the T7 gate).
        context = self._users.user_context(owner)
        resolution = resolve_delivery(
            interrupt_claimed=candidate.urgency is Urgency.INTERRUPT,
            deadline=None,  # v1: no mechanical deadline source (state.md, T7 flag)
            now=now,
            timezone_name=context.timezone,
            quiet=context.quiet_hours,
            settings=self._settings,
        )
        await self._claim_and_route(candidate, voicer, action, resolution, now=now)

    async def _claim_and_route(
        self,
        candidate: InitiativeCandidate,
        voicer: str,
        action: EnvelopeAction,
        resolution: DeliveryResolution,
        *,
        now: datetime,
    ) -> None:
        """Claim the slot FIRST (win the race durably), then deliver if due now.

        A claim that loses is the audited duplicate (the store's no-op). A
        deliver-now with no executor (T7's staging) or a failed delivery stays
        HELD — the flush retries; nothing is lost, nothing double-sends.
        """
        claim = self._ledger.try_claim_held(
            candidate, voicer_persona_id=voicer, held_until=None, now=now
        )
        if claim is None:
            return  # duplicate — one user-level notice per opportunity (audited by the store)
        if resolution.kind is not DeliveryKind.DELIVER_NOW or self._delivery is None:
            return  # held for the flush (quiet-hold / batch / staging posture)
        delivered = await self._delivery.deliver(candidate.owner_id, claim.id, action)
        if delivered:
            self._ledger.mark_delivered(
                candidate.owner_id, claim.id, envelope_action=action.value, now=now
            )

    # --- the flush (ruling 4 + the T7 re-grounding bar) ------------------------

    async def flush(self, owner_id: str) -> None:
        """Release the held batch — re-validated BOTH ways a hold can rot.

        Runs at the daily scan fire (the morning window). Per held notice:
        (a) hold-age bound — a buffer, not a queue; (b) the why-now time half
        (T2's pure predicate; anchor time is None in v1 so this never trips —
        wired for when a mechanical deadline source exists); (c) the T4 check
        re-run — citations that dissolved (K7 merged/superseded, a deleted
        conversation) or no longer entail EXPIRE the notice. Survivors deliver
        under the same cadence caps (counted at delivery).
        """
        now = datetime.now(UTC)
        for notice in self._ledger.held_for_owner(owner_id):
            try:
                await self._flush_one(owner_id, notice, now=now)
            except Exception:  # noqa: BLE001 — one rotten hold never blocks the batch
                _logger.warning("flush of one held notice failed; leaving it held")

    async def _flush_one(self, owner_id: str, notice: HeldNotice, now: datetime) -> None:
        candidate = notice.candidate
        max_age = timedelta(days=self._settings.hold_max_days)
        if now - notice.created_at > max_age:
            self._ledger.expire_stale(owner_id, notice.id, reason="hold_age", now=now)
            return
        if why_now_lapsed(anchor_time=None, now=now):  # v1: no mechanical anchor time
            self._ledger.expire_stale(owner_id, notice.id, reason="why_now_lapsed", now=now)
            return
        verdict = await self._grounding.check(candidate)
        if not verdict.admitted:
            self._ledger.expire_stale(owner_id, notice.id, reason="grounding_dissolved", now=now)
            return
        counts = self._ledger.delivered_counts(owner_id, persona_id=notice.persona_id, now=now)
        if cadence_exhausted(counts, self._settings):
            return  # still capped — stays held for the next flush (never dropped)
        if self._delivery is None:
            return  # staging posture — held until T8/T9 wire the executor
        dial = self._read_dial(owner_id, notice.persona_id)
        action = decide_envelope(candidate.category_footprint, dial)
        if action is EnvelopeAction.NONE:
            return  # dial turned off since the hold — stays held, silent
        delivered = await self._delivery.deliver(owner_id, notice.id, action)
        if delivered:
            self._ledger.mark_delivered(owner_id, notice.id, envelope_action=action.value, now=now)

    # --- helpers ----------------------------------------------------------------

    def _claim_held(
        self,
        candidate: InitiativeCandidate,
        voicer: str,
        *,
        held_until: datetime | None,
        now: datetime,
    ) -> None:
        self._ledger.try_claim_held(
            candidate, voicer_persona_id=voicer, held_until=held_until, now=now
        )

    def _read_dial(self, owner_id: str, persona_id: str) -> InitiativeDial:
        return self._dial_reader(owner_id, persona_id)

    def _audit(self, candidate: InitiativeCandidate, event: str, detail: str) -> None:
        try:
            self._auditor.record(candidate.owner_id, event, candidate.opportunity_key, detail)
        except Exception:  # noqa: BLE001 — audit is best-effort, never breaks the gate
            _logger.warning("pipeline audit failed (best-effort)")

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
``suppressed_stale`` disposition + audit row, never delivered. Quiet hours bind
the flush too (R9-237): a flush that lands inside the user's window delivers
nothing and asks the injected :class:`DeferredFlushScheduler` for one flush at
the window's end.

Fail-soft is absolute: any stage error discards the candidate (audited) —
silence is the safe state; the pipeline never raises into the scan job.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from persona.initiative import (
    MAX_CITED_NODE_READS,
    CandidateSource,
    InitiativeCandidate,
    InitiativeDial,
    Urgency,
    address_user,
    prompt_version_deliverable,
)
from persona.initiative.arbitration import arbitrate_voicer
from persona.initiative.envelope import EnvelopeAction, decide_envelope
from persona.initiative.restraint import (
    DeliveryKind,
    cadence_exhausted,
    meets_acceptance_floor,
    meets_value_threshold,
    quiet_edge_release_at,
    quiet_hold_edge_minute,
    resolve_delivery,
    why_now_lapsed,
)
from persona.logging import get_logger

from persona_runtime.initiative.grounding import GroundingRejection
from persona_runtime.initiative.specifics import earliest_upcoming_date, normalise_relative_days

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from persona.initiative import GroundingCitation, InitiativeSettings
    from persona.initiative.restraint import CadenceCounts, DeliveryResolution
    from persona.schedules.quiet_hours import QuietHours

    from persona_runtime.initiative.grounding import GroundingChecker

__all__ = [
    "DeferredFlushScheduler",
    "HeldNotice",
    "HeldSubject",
    "InitiativePipeline",
    "NoticeClaim",
    "NoticeLedger",
    "PipelineAuditor",
    "UserContext",
    "UserContextReader",
    "WellbeingSubjectCheck",
    "held_notice_subject",
]

_logger = get_logger("runtime.initiative.pipeline")


# --- the injected seams (api implements; T7's wiring) --------------------------


@runtime_checkable
class WellbeingSubjectCheck(Protocol):
    """The subject-exclusion read over EVERY cited item (R9-249 and its review).

    An item is gated when it, or the interaction it came from, is. A conversation is
    gated when a wellbeing-tagged node was sourced from it (the rule A7's event door
    uses, one shared definition) and, when ``include_unsynthesised`` is set, also when
    K2 synthesis has not yet caught up with it: a disclosure made today cannot have
    been tagged before synthesis runs (precision first). A node is gated when it
    carries any wellbeing category or was learned from a gated conversation; a task
    when its id or its originating conversation is gated.
    """

    def tagged_refs(
        self,
        owner_id: str,
        citations: Sequence[GroundingCitation],
        *,
        include_unsynthesised: bool,
    ) -> set[str]:
        """The refs among ``citations`` that are gated."""
        ...


@runtime_checkable
class DeclineReader(Protocol):
    """The live-decline check (T3's DeclineStore surface, narrowed)."""

    def suppressed_keys(self, owner_id: str, keys: Sequence[str]) -> set[str]:
        """The subset of ``keys`` with a LIVE decline (user-level, binds all personas).

        A decline binds the anchor under any trigger (owner ruling 2026-10-30): a key
        is suppressed when a live decline names its anchor, whatever its trigger.
        """
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


@runtime_checkable
class DeferredFlushScheduler(Protocol):
    """Arranges one later flush for an owner (R9-237; api: the A0 queue).

    The daily scan fire is the flush's only other trigger, and it fires at the
    scan hour, which a quiet window can cover every day. A flush inside the
    window therefore hands the release to this seam instead of delivering.
    """

    def schedule_flush(self, owner_id: str, *, release_at: datetime) -> None:
        """Run :meth:`InitiativePipeline.flush` for ``owner_id`` at ``release_at``.

        Idempotent per owner per ``release_at``: every scan fire of the same
        morning asks for the same instant, and the owner gets ONE flush.
        """
        ...


class HeldSubject(StrEnum):
    """The wellbeing subject rule's answer for a held notice (R9-249 review HIGH-A)."""

    CLEAR = "clear"
    DISCLOSED = "disclosed"
    WAIT = "wait"


def notice_lapse_anchor(candidate: InitiativeCandidate, zone: str) -> datetime | None:
    """The instant the notice's moment has passed in the user's zone.

    The moment is the earliest date the message or the offer states on or after the
    day it was written (:func:`earliest_upcoming_date`); the anchor is the start of
    the day AFTER it, local. ``None`` when they state no such date (the notice then
    never lapses by time; its re-grounding still runs).
    """
    written_on = candidate.scanned_at.astimezone(ZoneInfo(zone)).date()
    moment = earliest_upcoming_date(candidate.why_now, candidate.next_step, written_on=written_on)
    if moment is None:
        return None
    return datetime.combine(moment + timedelta(days=1), time(0), ZoneInfo(zone)).astimezone(UTC)


def held_notice_subject(
    check: WellbeingSubjectCheck, owner_id: str, candidate: InitiativeCandidate
) -> HeldSubject:
    """The wellbeing subject rule for a HELD notice (the flush and the digest, one rule).

    DISCLOSED when a citation is gated by a disclosure (the notice must never go out);
    WAIT when a scan candidate is gated only because synthesis has not caught up, or
    when the check itself fails (fail closed, retried at the next flush); CLEAR
    otherwise. Freshness is scan-only, as at submit.
    """
    try:
        if check.tagged_refs(owner_id, candidate.citations, include_unsynthesised=False):
            return HeldSubject.DISCLOSED
        if candidate.source is CandidateSource.SCAN and check.tagged_refs(
            owner_id, candidate.citations, include_unsynthesised=True
        ):
            return HeldSubject.WAIT
    except Exception:  # noqa: BLE001 (an unknown subject never means "deliver")
        _logger.opt(exception=True).warning("held-notice subject check failed; stays held")
        return HeldSubject.WAIT
    return HeldSubject.CLEAR


def _address_user(candidate: InitiativeCandidate) -> InitiativeCandidate | None:
    """The candidate with its user-facing texts addressed to "you", or ``None`` (void)."""
    why_now = address_user(candidate.why_now)
    next_step = address_user(candidate.next_step)
    if why_now is None or next_step is None:
        return None
    if (why_now, next_step) == (candidate.why_now, candidate.next_step):
        return candidate
    return candidate.model_copy(update={"why_now": why_now, "next_step": next_step})


def _utc_now() -> datetime:
    return datetime.now(UTC)


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
        deferred_flush: DeferredFlushScheduler | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        """Inject every seam (api wires the real stores at the worker root).

        ``deferred_flush`` (R9-237) receives the release of a flush that fell
        inside quiet hours; ``None`` leaves such a batch held for the next scan
        fire (logged). ``clock`` is the pipeline's one source of "now".
        """
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
        self._deferred_flush = deferred_flush
        self._clock = clock

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
        now = self._clock()
        owner = candidate.owner_id

        # (0) the voice (R9-256): why_now and next_step are read BY the user. A safe
        # rewrite ("for the user to review" to "for you to review") is applied here,
        # BEFORE grounding, so the judged text is the delivered text; any other
        # third-person reference voids the candidate, audited so the rate is visible.
        voiced = _address_user(candidate)
        if voiced is None:
            self._audit(candidate, "initiative.discard_voice", "third_person")
            return
        candidate = voiced

        # (0b) B9 (ruling after the third MID set): "tomorrow" directly before the full
        # date it names, in the owner's zone at scan time, is removed and the date
        # kept, so the judged, fingerprinted, stored and delivered text carry the date alone.
        # Any other relative word reaches the refusal unchanged.
        today, zone = self._local_day(owner, now)
        candidate = self._normalise_relative_days(candidate, today)

        # (1) grounding: T4's mechanical layers and the judge. No grounding, no candidate. The
        # judge is told the user's local date, so a relative claim can be verified.
        verdict = await self._grounding.check(candidate, today=today, timezone=zone)
        if not verdict.admitted:
            self._audit(
                candidate,
                "initiative.discard_ungrounded",
                verdict.rejection.value if verdict.rejection else "unknown",
            )
            return
        # The fingerprint of what the judge admitted, and the model that admitted it, ride
        # on the held notice: the flush keeps the YES only on unchanged grounds, from the
        # head of the judge's chain, for an undated message (GroundingChecker.check).
        candidate = candidate.model_copy(
            update={
                "grounding_fingerprint": verdict.fingerprint,
                "grounding_served_model": verdict.served_model,
            }
        )

        # (2) the wellbeing-subject rule: ANY gated citation disqualifies the SUBJECT
        # (all five categories; stronger than K4's chat gate, ruling 6). Every cited
        # item, judged with the interaction it came from (R9-249 and its review: a
        # conversation, an untagged node learned from a disclosing conversation, a
        # task begun in one). The synthesis-freshness half applies to SCAN
        # candidates: an A7 event fires as its message arrives, before synthesis
        # could run, and A7's own handler already applies the disclosed rule to it.
        node_refs = [c.ref for c in candidate.citations if c.kind.value == "node"]
        if len(set(node_refs)) > MAX_CITED_NODE_READS:
            # Too many cited nodes to read their sources: the subject is gated whole
            # (fail closed), under its own reason so it is never mistaken for a tag.
            self._audit(
                candidate, "initiative.discard_wellbeing_subject", "too_many_node_citations"
            )
            return
        if self._wellbeing.tagged_refs(
            owner,
            candidate.citations,
            include_unsynthesised=candidate.source is CandidateSource.SCAN,
        ):
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
        (0) a notice produced under a superseded prompt version expires
        (``prompt_superseded``: it may cite a conversation or speak of "the user");
        (0b) the wellbeing subject rule, re-applied (R9-249 review HIGH-A: a
        conversation can become disclosing AFTER the notice was held): gated by a
        disclosure it expires (``wellbeing_subject``); gated only because synthesis
        has not caught up, or the check failed, it stays held;
        (a) hold-age bound — a buffer, not a queue; (b) the why-now time half
        (T2's pure predicate), anchored mechanically: the notice expires once the
        earliest date its message or offer states (on or after the day it was
        written) is before the user's local today (the texts state dates
        absolutely since a5-scan-v6); (c) the T4 check
        re-run: citations that dissolved (K7 merged/superseded, a deleted
        conversation) EXPIRE the notice, and the mechanical specifics check re-runs.
        The admitted verdict stands without a model call only when the grounds are
        unchanged (the stored ``grounding_fingerprint`` matches, prompt version
        included), the YES came from the head of the judge's chain (the stored
        ``grounding_served_model``), and the message and the offer name no time
        (no date, weekday or month): so the judge cannot flip on identical,
        undated material (the A5-R-1 re-run, 2026-10-23), and a dated message is
        re-read against the new TODAY. Otherwise the judge is asked again.
        Survivors deliver under the same cadence caps (counted at delivery).

        Quiet hours first (R9-237): inside the user's window NOTHING is
        delivered, expired or re-grounded; the whole batch waits for one flush
        at the window's end (:meth:`_defer_past_quiet_hours`). That later flush
        comes back through here and re-checks, so a window edited meanwhile
        still holds.

        Raising: the quiet-hours step never raises (a failed read or enqueue
        holds the batch and logs), and one rotten notice never blocks the rest.
        A failure of the held-notice read itself propagates; both callers (the
        scan handler and the deferred-flush handler) are fail-soft around this.
        """
        now = self._clock()
        held = self._ledger.held_for_owner(owner_id)
        if not held:
            return
        if self._defer_past_quiet_hours(owner_id, now=now):
            return
        for notice in held:
            try:
                await self._flush_one(owner_id, notice, now=now)
            except Exception:  # noqa: BLE001 — one rotten hold never blocks the batch
                _logger.warning("flush of one held notice failed; leaving it held")

    def _defer_past_quiet_hours(self, owner_id: str, *, now: datetime) -> bool:
        """True when the batch must not go out now (the release is then scheduled).

        The same rule the interrupt path applies (:func:`quiet_hold_edge_minute`,
        in the user's zone); the window's end minute becomes the absolute instant
        the deferred flush runs at. Fails CLOSED: a user-context read that
        fails holds the batch (the next scan fire retries), and a failed enqueue
        holds it too; neither raises.
        """
        try:
            context = self._users.user_context(owner_id)
        except Exception:  # noqa: BLE001, unknown quiet hours must never mean "deliver"
            _logger.opt(exception=True).warning(
                "quiet-hours read failed for a flush; batch stays held"
            )
            return True
        edge = quiet_hold_edge_minute(
            now=now, timezone_name=context.timezone, quiet=context.quiet_hours
        )
        if edge is None:
            return False
        release_at = quiet_edge_release_at(now, context.timezone, edge)
        if self._deferred_flush is None:
            _logger.warning("flush inside quiet hours with no deferral seam; batch stays held")
            return True
        try:
            self._deferred_flush.schedule_flush(owner_id, release_at=release_at)
        except Exception:  # noqa: BLE001, a lost deferral holds the batch; it never delivers
            _logger.opt(exception=True).warning(
                "deferred flush enqueue failed; batch stays held until the next scan fire"
            )
        return True

    async def _flush_one(self, owner_id: str, notice: HeldNotice, now: datetime) -> None:
        candidate = notice.candidate
        if not prompt_version_deliverable(candidate.prompt_version):
            self._ledger.expire_stale(owner_id, notice.id, reason="prompt_superseded", now=now)
            return
        # Hold age BEFORE the subject rule (third review LOW 6): a notice the subject
        # rule keeps waiting (synthesis behind, or a failing check) still ages out, so
        # it can never hold its opportunity's duplicate slot forever.
        max_age = timedelta(days=self._settings.hold_max_days)
        if now - notice.created_at > max_age:
            self._ledger.expire_stale(owner_id, notice.id, reason="hold_age", now=now)
            return
        subject = held_notice_subject(self._wellbeing, owner_id, candidate)
        if subject is HeldSubject.DISCLOSED:
            self._ledger.expire_stale(owner_id, notice.id, reason="wellbeing_subject", now=now)
            return
        if subject is HeldSubject.WAIT:
            return  # synthesis not caught up, or the check failed: stays held
        today, zone = self._local_day(owner_id, now)
        if why_now_lapsed(anchor_time=notice_lapse_anchor(candidate, zone), now=now):
            self._ledger.expire_stale(owner_id, notice.id, reason="why_now_lapsed", now=now)
            return
        verdict = await self._grounding.check(
            candidate,
            today=today,
            timezone=zone,
            admitted_fingerprint=candidate.grounding_fingerprint,
            admitted_served_model=candidate.grounding_served_model,
        )
        if verdict.rejection is GroundingRejection.CONTEXT_UNAVAILABLE:
            return  # a transient read error: stays held for the next flush (review LOW-5)
        if not verdict.admitted:
            self._ledger.expire_stale(owner_id, notice.id, reason="grounding_dissolved", now=now)
            return
        if verdict.kept:  # visible in the audit, so a re-run can count the keeps
            self._audit(candidate, "initiative.grounding_kept", "unchanged_head_undated")
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

    def _local_day(self, owner_id: str, now: datetime) -> tuple[date, str]:
        """The user's local date and timezone name, for the judge (UTC if unreadable)."""
        try:
            zone = self._users.user_context(owner_id).timezone
            return now.astimezone(ZoneInfo(zone)).date(), zone
        except Exception:  # noqa: BLE001 (the judge is then told UTC, and says so)
            _logger.opt(exception=True).warning("user timezone read failed; judging in UTC")
            return now.astimezone(UTC).date(), "UTC"

    def _normalise_relative_days(
        self, candidate: InitiativeCandidate, today: date
    ) -> InitiativeCandidate:
        """The candidate with each self-dating "tomorrow" removed; each removal
        is audited by the word alone (``initiative.relative_day_normalised``)."""
        why_now, from_reason = normalise_relative_days(candidate.why_now, today=today)
        next_step, from_offer = normalise_relative_days(candidate.next_step, today=today)
        for word in (*from_reason, *from_offer):
            self._audit(candidate, "initiative.relative_day_normalised", word.lower())
        if not (from_reason or from_offer):
            return candidate
        return candidate.model_copy(update={"why_now": why_now, "next_step": next_step})

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

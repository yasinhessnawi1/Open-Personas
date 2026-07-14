"""The ``initiative_scan`` job + schedule provisioning (Spec A5, T6; the real chain).

The scan is a durable A0 tenant fired by a real A1 schedule — never a
hand-invoked step (the A4 anti-inertness lesson):

    A1 tick (due row) → A0 enqueue (``sched:{schedule_id}:{fire_time}`` dedup)
    → claim → owner GUC (the RLS chokepoint the executor already owns)
    → this handler → dial read → scan → candidates → the sink (T7's pipeline).

Composition posture: registered at the worker root ONLY when
``PERSONA_INITIATIVE_ENABLED`` is on (default OFF — the criterion-9 gate);
dial ``OFF`` is a HANDLER EXIT (the schedule stays — one source of truth,
Phase-1 ruling). R7 composition per the tension-5 ruling: the scan is
synthesis-class background work — bounded by cadence + the per-scan token
budget knob, metered via the synthesis-style ``context.meter`` audit row
(``credits_charged=0``, cost visible — A5-R-4's real-data source), never a
parallel budget. Fail-soft: the scanner cannot raise (T5); this handler's own
guard degrades any residual failure to silence + the metering row.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — Pydantic needs runtime access (payload field)
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from persona.errors import ScheduleNotFoundError
from persona.initiative import DEFAULT_INITIATIVE_DIAL, InitiativeDial
from persona.jobs import MEDIUM_LEASE, JobPayload, JobTypeSpec, RetryPolicy
from persona.logging import get_logger
from persona.schedules import MissedFirePolicy, RecurrenceFreq, RecurrenceRule, Schedule
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from persona_api.approvals import AutonomyPauseCheck, never_paused
from persona_api.db.engine import rls_connection
from persona_api.db.models import personas as personas_t
from persona_api.schedules.tombstones import TombstoneAction
from persona_api.services import audit_service

if TYPE_CHECKING:
    from collections.abc import Callable

    from persona.initiative import InitiativeCandidate, InitiativeSettings
    from persona.jobs import JobContext, JobRegistry
    from persona_runtime.initiative import InitiativeScanner
    from sqlalchemy import Engine

    from persona_api.schedules import ScheduleStore
    from persona_api.schedules.tombstones import ScheduleTombstoneStore

__all__ = [
    "INITIATIVE_SCAN_JOB_TYPE",
    "CandidateSink",
    "EnsureScheduleResult",
    "InitiativeScanHandler",
    "InitiativeScanPayload",
    "ensure_initiative_schedule",
    "initiative_schedule_id",
    "read_initiative_dial",
    "register_initiative_scan_handler",
    "set_initiative_dial",
]

INITIATIVE_SCAN_JOB_TYPE = "initiative_scan"

_log = get_logger("api.initiative.handler")


class InitiativeScanPayload(JobPayload):
    """A schedule fire targeting the scan — the A1 anchor + the persona (the template)."""

    persona_id: str
    schedule_id: str
    fire_time: datetime


@runtime_checkable
class CandidateSink(Protocol):
    """Where surviving scan output goes — T7's pipeline fills this seam.

    T6 registers the tenant with a ``None`` sink (candidates are produced,
    metered, and logged; nothing is delivered) — the staging posture until the
    pipeline lands; the T6 composition test proves registration, T7's tests
    prove consumption.
    """

    async def submit(self, candidates: tuple[InitiativeCandidate, ...]) -> None:
        """Run the pipeline over one scan's candidates."""
        ...


def read_initiative_dial(engine: Engine, owner_id: str, persona_id: str) -> InitiativeDial:
    """The persona's dial (RLS read); a missing row/value reads as the default.

    Fail-soft in the conservative direction: an unknown stored value (a
    hand-edited row) reads as the ratified PROPOSE_ONLY default, never as
    act-within-envelope.
    """
    with rls_connection(engine, owner_id) as conn:
        row = conn.execute(
            select(personas_t.c.initiative_dial).where(personas_t.c.id == persona_id)
        ).first()
    if row is None:
        return InitiativeDial.OFF  # persona gone (or another tenant's) — nothing to scan
    try:
        return InitiativeDial(str(row[0]))
    except ValueError:
        return DEFAULT_INITIATIVE_DIAL


def set_initiative_dial(
    engine: Engine, owner_id: str, persona_id: str, dial: InitiativeDial, *, now: datetime
) -> bool:
    """The SINGLE durable dial-write path (A5-D-5) — write ``initiative_dial`` (+ ts) + audit.

    Both the T10 dial verb (:meth:`InitiativeVerbService._apply_dial`) and the A6 autonomy-controls
    route call this — never a second UPDATE. RLS-scoped: returns whether a persona row matched
    (``False`` = missing/foreign under RLS). Audits ``initiative.dial_set`` on a real write. The
    durable level persists here regardless of whether initiative is globally enabled; the lazy
    schedule-ensure (A5-D-1) is the verb-path's own concern layered on top by the caller.
    """
    with rls_connection(engine, owner_id) as conn:
        result = conn.execute(
            update(personas_t)
            .where(personas_t.c.id == persona_id)
            .values(initiative_dial=dial.value, initiative_dial_updated_at=now)
        )
    if result.rowcount == 0:
        return False
    audit_service.record(
        engine=engine,
        user_id=owner_id,
        action="initiative.dial_set",
        target=persona_id,
        metadata={"dial": dial.value},
    )
    return True


class InitiativeScanHandler:
    """Dial gate → scan → meter → sink. Every failure path is silence + the audit row."""

    def __init__(
        self,
        *,
        scanner: InitiativeScanner,
        dial_reader: Callable[[str, str], InitiativeDial],
        sink: CandidateSink | None = None,
        pause_check: AutonomyPauseCheck = never_paused,
    ) -> None:
        """Inject the runtime scanner, the dial read (owner, persona → dial), the T7 sink.

        The composition root binds ``dial_reader`` to :func:`read_initiative_dial`
        over the RLS engine; tests inject a plain callable (DI, no DB).

        ``pause_check`` (A6-D-8 completeness) — the per-owner autonomy-pause gate the scan
        consults BEFORE reading the dial or spending: a paused owner originates no initiative,
        even for personas whose dial is on. Default :func:`never_paused` (green pre-A6); the
        worker composition binds ``KillSwitchStore.is_owner_autonomy_paused``.
        """
        self._scanner = scanner
        self._dial_reader = dial_reader
        self._sink = sink
        self._pause_check = pause_check

    async def handle(self, payload: InitiativeScanPayload, context: JobContext) -> None:
        """One scan fire; never raises a user-facing error (silence is the safe state)."""
        if self._pause_check(context.owner_id):
            # A6-D-8: the owner paused all autonomy — no scan, no spend, no proposal. Checked
            # before the dial so a paused owner leaks nothing (completeness is non-negotiable).
            _log.info("owner autonomy paused; scan exits", persona_id=payload.persona_id)
            return
        dial = self._dial_reader(context.owner_id, payload.persona_id)
        if dial is InitiativeDial.OFF:
            # The handler exit (Phase-1 ruling): the schedule stays; the dial is
            # the one source of truth. No scan, no spend, no message.
            _log.info("initiative dial off; scan exits", persona_id=payload.persona_id)
            return
        candidates = await self._scanner.scan(
            context.owner_id, payload.persona_id, fire_time=payload.fire_time
        )
        # The synthesis-style metering row (tension-5 ruling ADD): system-initiated,
        # credits_charged=0, cost visible — the A5-R-4 economics accumulate from
        # day one, and the row doubles as the real-fire proof's durable marker.
        context.meter(
            amount_micros=0,
            kind="model",
            detail={
                "surface": "initiative_scan",
                "persona_id": payload.persona_id,
                "fire_time": payload.fire_time.isoformat(),
                "candidates": str(len(candidates)),
            },
        )
        if not candidates:
            return  # a thin scan is success (criterion 1) — silence, already metered.
        if self._sink is None:
            _log.info(
                "initiative scan produced candidates; no sink wired (T7 pending)",
                count=len(candidates),
            )
            return
        try:
            await self._sink.submit(candidates)
        except Exception:  # noqa: BLE001 — the pipeline must not crash the job into retries
            _log.warning("initiative sink failed; degrading to silence (fail-soft)")


def register_initiative_scan_handler(
    registry: JobRegistry, *, handler: InitiativeScanHandler
) -> None:
    """Register the scan tenant with its declared idempotency (D-A0-2's hard gate).

    The declared recipe is ``initiative_scan:{persona}:{fire_time}`` (the ratified
    key). The A1 tick enqueues with its own ``sched:{schedule_id}:{fire_time}`` —
    equivalent dedup identity, since the schedule id is deterministic per persona
    (:func:`initiative_schedule_id`); both collapse a duplicate fire to one job.
    """
    registry.register(
        JobTypeSpec(
            type=INITIATIVE_SCAN_JOB_TYPE,
            payload_model=InitiativeScanPayload,
            handler=handler,
            idempotency_key=lambda p: f"initiative_scan:{p.persona_id}:{p.fire_time.isoformat()}",
            retry=RetryPolicy(max_attempts=2),
            lease=MEDIUM_LEASE,
        )
    )


def initiative_schedule_id(persona_id: str) -> str:
    """The deterministic per-persona scan-schedule id (the idempotent-ensure key)."""
    return f"initsched:{persona_id}"


class EnsureScheduleResult(BaseModel):
    """The outcome of one :func:`ensure_initiative_schedule` call.

    ``created`` is ``False`` for BOTH the ordinary already-exists no-op and a
    tombstone-refused attempt — ``refused`` disambiguates the two for a caller
    that cares (the provisioner's own counting; tests).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schedule_id: str
    created: bool
    refused: bool = False


def ensure_initiative_schedule(
    store: ScheduleStore,
    *,
    owner_id: str,
    persona_id: str,
    timezone: str,
    settings: InitiativeSettings,
    now: datetime,
    tombstones: ScheduleTombstoneStore | None = None,
    tombstone_window_days: int = 30,
    seam: str = "initiative_ensure",
) -> EnsureScheduleResult:
    """Idempotently ensure the persona's daily scan schedule exists (A5-D-1).

    Lazy provisioning: called when initiative is enabled for a persona (persona
    create / dial write — T10 wires the call sites). Deterministic id ⇒ the
    ensure is a get-or-create; a concurrent create loses to the PK and reads
    back (race-safe without a new mechanism). The schedule fires daily at the
    user's morning hour in THEIR zone (A8's resolved tz), targeting this job
    type with the persona in the template.

    **R9-037.** Before creating a MISSING row, consult ``tombstones`` (when
    wired — ``None`` is the pre-R9-037 behaviour, e.g. in tests that don't care):
    a schedule with this exact id that the user deleted within
    ``tombstone_window_days`` refuses the re-creation (audited
    ``schedule.recreate_refused``) instead of silently resurrecting it. This is
    the confirmed fix for the owner's report — this function is called both by
    the hourly (and immediate-on-every-worker-restart) ``InitiativeProvisioner``
    sweep AND by the initiative dial chat-verb
    (``InitiativeVerbService._apply_dial``); neither call is a fresh, explicit
    user ask for THIS schedule, so a tombstone match is a hard refusal, never a
    silent re-creation. ``seam`` names the caller in the audit row (observability).
    """
    schedule_id = initiative_schedule_id(persona_id)
    try:
        store.get(owner_id, schedule_id)
    except ScheduleNotFoundError:
        pass
    else:
        return EnsureScheduleResult(schedule_id=schedule_id, created=False)
    if tombstones is not None:
        match = tombstones.find_recent(
            owner_id,
            schedule_id=schedule_id,
            window_days=tombstone_window_days,
            now=now,
            action=TombstoneAction.DELETED,
        )
        if match is not None:
            _log.info(
                "initiative schedule creation refused — recently user-deleted "
                "schedule_id={sid} persona_id={pid}",
                sid=schedule_id,
                pid=persona_id,
            )
            tombstones.audit_refusal(owner_id, schedule_id=schedule_id, seam=seam, tombstone=match)
            return EnsureScheduleResult(schedule_id=schedule_id, created=False, refused=True)
    schedule = Schedule(
        id=schedule_id,
        owner_id=owner_id,
        timezone=timezone,
        recurrence=RecurrenceRule(
            freq=RecurrenceFreq.DAILY,
            byhour=(settings.scan_hour,),
            byminute=(0,),
        ),
        target_job_type=INITIATIVE_SCAN_JOB_TYPE,
        payload_template={"persona_id": persona_id},
        missed_fire_policy=MissedFirePolicy.SKIP_AND_NOTE,
        created_at=now,
        updated_at=now,
    )
    try:
        store.create(schedule, now=now)
    except IntegrityError:
        # A concurrent ensure won the PK race — the schedule exists; converge.
        _log.info("initiative schedule ensure raced; existing row wins")
        return EnsureScheduleResult(schedule_id=schedule_id, created=False)
    return EnsureScheduleResult(schedule_id=schedule_id, created=True)

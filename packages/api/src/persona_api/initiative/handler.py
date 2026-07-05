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
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from persona_api.db.engine import rls_connection
from persona_api.db.models import personas as personas_t

if TYPE_CHECKING:
    from collections.abc import Callable

    from persona.initiative import InitiativeCandidate, InitiativeSettings
    from persona.jobs import JobContext, JobRegistry
    from persona_runtime.initiative import InitiativeScanner
    from sqlalchemy import Engine

    from persona_api.schedules import ScheduleStore

__all__ = [
    "INITIATIVE_SCAN_JOB_TYPE",
    "CandidateSink",
    "InitiativeScanHandler",
    "InitiativeScanPayload",
    "ensure_initiative_schedule",
    "initiative_schedule_id",
    "read_initiative_dial",
    "register_initiative_scan_handler",
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


class InitiativeScanHandler:
    """Dial gate → scan → meter → sink. Every failure path is silence + the audit row."""

    def __init__(
        self,
        *,
        scanner: InitiativeScanner,
        dial_reader: Callable[[str, str], InitiativeDial],
        sink: CandidateSink | None = None,
    ) -> None:
        """Inject the runtime scanner, the dial read (owner, persona → dial), the T7 sink.

        The composition root binds ``dial_reader`` to :func:`read_initiative_dial`
        over the RLS engine; tests inject a plain callable (DI, no DB).
        """
        self._scanner = scanner
        self._dial_reader = dial_reader
        self._sink = sink

    async def handle(self, payload: InitiativeScanPayload, context: JobContext) -> None:
        """One scan fire; never raises a user-facing error (silence is the safe state)."""
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


def ensure_initiative_schedule(
    store: ScheduleStore,
    *,
    owner_id: str,
    persona_id: str,
    timezone: str,
    settings: InitiativeSettings,
    now: datetime,
) -> str:
    """Idempotently ensure the persona's daily scan schedule exists (A5-D-1).

    Lazy provisioning: called when initiative is enabled for a persona (persona
    create / dial write — T10 wires the call sites). Deterministic id ⇒ the
    ensure is a get-or-create; a concurrent create loses to the PK and reads
    back (race-safe without a new mechanism). The schedule fires daily at the
    user's morning hour in THEIR zone (A8's resolved tz), targeting this job
    type with the persona in the template.
    """
    schedule_id = initiative_schedule_id(persona_id)
    try:
        store.get(owner_id, schedule_id)
    except ScheduleNotFoundError:
        pass
    else:
        return schedule_id
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
    return schedule_id

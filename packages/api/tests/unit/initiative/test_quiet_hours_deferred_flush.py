"""Unit tests, held initiative waits out the user's quiet hours (R9-237).

The daily scan fire flushes the held batch at 07:00 local. For a user whose
quiet hours run 22:00 to 08:00 that fire is inside the window, so it must
deliver nothing and enqueue ONE deferred flush for 08:00, and that deferred
flush (a real job handler) must repeat the scan fire's gates before it
delivers. Drives the REAL scan handler, the REAL pipeline, the REAL deferred
handler and the REAL ledger on the community engine; the durable disposition
is the assertion, not only the delivery fake.
"""

# ruff: noqa: ARG002, the pipeline-seam fakes deliberately ignore some args
from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

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
from persona.schedules.quiet_hours import QuietHours
from persona.tools.categories import ActionCategory
from persona_api.background.worker_root import build_worker_registry
from persona_api.config import APIConfig
from persona_api.db.community import (
    create_community_schema,
    ensure_owner,
    make_community_engine,
)
from persona_api.db.models import personas as personas_t
from persona_api.initiative.deferred_flush import (
    INITIATIVE_DEFERRED_FLUSH_JOB_TYPE,
    DeferredFlushPayload,
    InitiativeDeferredFlushHandler,
    QueueDeferredFlushScheduler,
    deferred_flush_idempotency_key,
)
from persona_api.initiative.handler import (
    INITIATIVE_SCAN_JOB_TYPE,
    InitiativeScanHandler,
    InitiativeScanPayload,
)
from persona_api.initiative.pipeline_wiring import LedgerAdapter
from persona_api.initiative.store import InitiativeLedger, NoticeDisposition
from persona_runtime.initiative import InitiativePipeline
from persona_runtime.initiative.grounding import GroundingVerdict
from pydantic import ValidationError
from sqlalchemy import create_engine, insert

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

    from persona.initiative.envelope import EnvelopeAction
    from sqlalchemy import Engine

_OWNER = "user_alice"
_PERSONA = "astrid"
_OTHER_PERSONA = "bjorn"
_SETTINGS = InitiativeSettings()
_OSLO = "Europe/Oslo"
# 05:00 UTC = 07:00 Oslo (CEST): the default scan hour, inside 22:00 to 08:00.
_SCAN_FIRE = datetime(2026, 9, 26, 5, 0, tzinfo=UTC)
# 06:00 UTC = 08:00 Oslo: the end of that window.
_WINDOW_END = datetime(2026, 9, 26, 6, 0, tzinfo=UTC)
_NIGHT = QuietHours(start_minute=22 * 60, end_minute=8 * 60)


@pytest.fixture
def engine(tmp_path: Any) -> Iterator[Engine]:  # noqa: ANN401, pytest tmp_path
    eng = make_community_engine(tmp_path / "quiet.db")
    create_community_schema(eng)
    ensure_owner(eng, owner_id=_OWNER, email="alice@example.com")
    with eng.begin() as conn:
        for persona in (_PERSONA, _OTHER_PERSONA):
            conn.execute(
                insert(personas_t).values(
                    id=persona,
                    owner_id=_OWNER,
                    yaml=f"name: {persona}",
                    initiative_dial=InitiativeDial.PROPOSE_ONLY.value,
                )
            )
    yield eng
    eng.dispose()


def _candidate(persona_id: str = _PERSONA, ref: str = "node-91") -> InitiativeCandidate:
    return InitiativeCandidate(
        observation="The hearing is Friday and nothing is drafted.",
        citations=(GroundingCitation(kind=CitationKind.NODE, ref=ref),),
        trigger=InitiativeTrigger.APPROACHING_COMMITMENT,
        why_now="The date entered the horizon.",
        plan=(PlannedStep(description="draft", categories=frozenset({ActionCategory.DRAFT})),),
        next_step="Draft the letter.",
        value=0.9,
        acceptance=0.8,
        urgency=Urgency.BATCH,
        source=CandidateSource.SCAN,
        owner_id=_OWNER,
        persona_id=persona_id,
        prompt_version="a5-scan-v1",
        scanned_at=_SCAN_FIRE - timedelta(days=1),
    )


def _hold(ledger: InitiativeLedger, candidate: InitiativeCandidate) -> str:
    record = ledger.try_claim(
        candidate,
        voicer_persona_id=candidate.persona_id,
        disposition=NoticeDisposition.HELD,
        envelope_action=None,
        held_until=None,
        now=_SCAN_FIRE - timedelta(days=1),
    )
    assert record is not None
    return record.id


def _disposition(ledger: InitiativeLedger, notice_id: str) -> NoticeDisposition | None:
    record = ledger.get_notice(_OWNER, notice_id)
    return None if record is None else record.disposition


class _FakeGrounding:
    async def check(self, candidate: InitiativeCandidate) -> GroundingVerdict:
        return GroundingVerdict(admitted=True, supporting_quote="the hearing is Friday")


class _FakeWellbeing:
    def tagged_refs(self, owner_id: str, node_refs: Sequence[str]) -> set[str]:
        return set()


class _FakeDeclines:
    def suppressed_keys(self, owner_id: str, keys: Sequence[str]) -> set[str]:
        return set()


class _UserContext:
    def __init__(self, quiet: QuietHours | None) -> None:
        self.timezone = _OSLO
        self.quiet_hours = quiet


class _FakeUsers:
    def __init__(self, quiet: QuietHours | None) -> None:
        self.context = _UserContext(quiet)

    def user_context(self, owner_id: str) -> _UserContext:
        return self.context


class _FakeProvenance:
    def citation_personas(self, owner_id: str, node_refs: Sequence[str]) -> list[str]:
        return []

    def activity_rank(self, owner_id: str) -> dict[str, int]:
        return {}


class _FakeAuditor:
    def record(self, owner_id: str, event: str, opportunity_key: str, detail: str) -> None:
        return


class _FakeDelivery:
    def __init__(self) -> None:
        self.delivered: list[str] = []
        self.succeeds = True

    async def deliver(self, owner_id: str, notice_id: str, action: EnvelopeAction) -> bool:
        if self.succeeds:
            self.delivered.append(notice_id)
        return self.succeeds


class _FakeScanner:
    def __init__(self) -> None:
        self.calls = 0

    async def scan(
        self, owner_id: str, persona_id: str, *, fire_time: datetime
    ) -> tuple[InitiativeCandidate, ...]:
        self.calls += 1
        return ()


class _FakeQueue:
    """The A0 queue's contract: one row per (owner, idempotency_key); a duplicate is a no-op."""

    def __init__(self, *, raises: bool = False) -> None:
        self.jobs: dict[tuple[str, str], dict[str, Any]] = {}
        self.enqueue_calls = 0
        self._raises = raises

    def enqueue(self, **kwargs: Any) -> object:  # noqa: ANN401, the queue's kwargs surface
        self.enqueue_calls += 1
        if self._raises:
            msg = "queue unavailable"
            raise RuntimeError(msg)
        key = (kwargs["owner_id"], kwargs["idempotency_key"])
        if key in self.jobs:
            return None
        self.jobs[key] = kwargs
        return object()


class _FakeJobContext:
    def __init__(self) -> None:
        self.meters: list[dict[str, Any]] = []

    @property
    def owner_id(self) -> str:
        return _OWNER

    @property
    def job_id(self) -> str:
        return "job-1"

    @contextlib.contextmanager
    def connection(self) -> Iterator[object]:
        yield object()

    def meter(
        self, *, amount_micros: int, kind: str, detail: Mapping[str, str] | None = None
    ) -> None:
        self.meters.append({"amount_micros": amount_micros, "detail": dict(detail or {})})


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class _World:
    """The real scan handler, pipeline, deferred handler and ledger, one owner."""

    def __init__(
        self,
        engine: Engine,
        *,
        quiet: QuietHours | None,
        paused: Callable[[str], bool] = lambda _o: False,
        dial_for: Callable[[str], InitiativeDial] = lambda _p: InitiativeDial.PROPOSE_ONLY,
        queue: _FakeQueue | None = None,
    ) -> None:
        self.ledger = InitiativeLedger(engine)
        self.delivery = _FakeDelivery()
        self.queue = queue or _FakeQueue()
        self.scanner = _FakeScanner()
        self.clock = _Clock(_SCAN_FIRE)
        self.users = _FakeUsers(quiet)
        pipeline = InitiativePipeline(
            grounding=_FakeGrounding(),  # type: ignore[arg-type]  # protocol-shaped fake
            wellbeing=_FakeWellbeing(),
            declines=_FakeDeclines(),
            ledger=LedgerAdapter(self.ledger),
            users=self.users,
            provenance=_FakeProvenance(),
            auditor=_FakeAuditor(),
            dial_reader=lambda _o, p: dial_for(p),
            settings=_SETTINGS,
            delivery=self.delivery,  # type: ignore[arg-type]  # protocol-shaped fake
            deferred_flush=QueueDeferredFlushScheduler(self.queue),  # type: ignore[arg-type]
            clock=self.clock,
        )
        self.scan = InitiativeScanHandler(
            scanner=self.scanner,  # type: ignore[arg-type]  # protocol-shaped fake
            dial_reader=lambda _o, p: dial_for(p),
            sink=pipeline,
            held_batch=pipeline,
        )
        self.deferred = InitiativeDeferredFlushHandler(
            held_batch=pipeline,
            held_personas=lambda owner: {n.persona_id for n in self.ledger.held_for_owner(owner)},
            dial_reader=lambda _o, p: dial_for(p),
            pause_check=paused,
        )

    async def scan_fire(
        self, persona_id: str = _PERSONA, context: _FakeJobContext | None = None
    ) -> None:
        payload = InitiativeScanPayload(
            persona_id=persona_id, schedule_id=f"initsched:{persona_id}", fire_time=_SCAN_FIRE
        )
        await self.scan.handle(payload, context or _FakeJobContext())  # type: ignore[arg-type]

    def only_job(self) -> dict[str, Any]:
        assert len(self.queue.jobs) == 1
        return next(iter(self.queue.jobs.values()))

    async def run_deferred_job(self, context: _FakeJobContext | None = None) -> None:
        job = self.only_job()
        payload = DeferredFlushPayload.model_validate(job["payload"])
        self.clock.now = job["scheduled_at"]  # A0 claims it no earlier than scheduled_at
        await self.deferred.handle(payload, context or _FakeJobContext())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_the_0700_fire_holds_and_the_0800_deferred_flush_delivers(engine: Engine) -> None:
    world = _World(engine, quiet=_NIGHT)
    notice_id = _hold(world.ledger, _candidate())

    await world.scan_fire()
    assert world.delivery.delivered == []
    assert _disposition(world.ledger, notice_id) is NoticeDisposition.HELD
    job = world.only_job()
    assert job["type"] == INITIATIVE_DEFERRED_FLUSH_JOB_TYPE
    assert job["owner_id"] == _OWNER
    assert job["scheduled_at"] == _WINDOW_END

    context = _FakeJobContext()
    await world.run_deferred_job(context)
    assert world.delivery.delivered == [notice_id]
    assert _disposition(world.ledger, notice_id) is NoticeDisposition.DELIVERED
    assert [m["detail"]["surface"] for m in context.meters] == ["initiative_deferred_flush"]


@pytest.mark.asyncio
async def test_no_quiet_hours_delivers_at_the_0700_fire_as_before(engine: Engine) -> None:
    world = _World(engine, quiet=None)
    notice_id = _hold(world.ledger, _candidate())

    await world.scan_fire()
    assert world.delivery.delivered == [notice_id]
    assert _disposition(world.ledger, notice_id) is NoticeDisposition.DELIVERED
    assert world.queue.jobs == {}


@pytest.mark.asyncio
async def test_two_held_notices_and_two_persona_fires_make_one_deferred_job(
    engine: Engine,
) -> None:
    world = _World(engine, quiet=_NIGHT)
    first = _hold(world.ledger, _candidate(_PERSONA, "node-91"))
    second = _hold(world.ledger, _candidate(_OTHER_PERSONA, "node-92"))

    await world.scan_fire(_PERSONA)
    await world.scan_fire(_OTHER_PERSONA)  # every persona's fire lands at 07:00 too
    assert world.queue.enqueue_calls == 2
    assert len(world.queue.jobs) == 1  # one key per owner per release instant

    await world.run_deferred_job()
    assert sorted(world.delivery.delivered) == sorted([first, second])


@pytest.mark.asyncio
async def test_a_pause_pressed_before_the_deferred_flush_delivers_nothing(
    engine: Engine,
) -> None:
    paused = {"on": False}
    world = _World(engine, quiet=_NIGHT, paused=lambda _o: paused["on"])
    notice_id = _hold(world.ledger, _candidate())
    await world.scan_fire()

    paused["on"] = True  # the owner pauses autonomy between 07:00 and 08:00
    context = _FakeJobContext()
    await world.run_deferred_job(context)
    assert world.delivery.delivered == []
    assert _disposition(world.ledger, notice_id) is NoticeDisposition.HELD
    assert context.meters == []  # no flush ran, nothing was spent


@pytest.mark.asyncio
async def test_a_dial_turned_off_before_the_deferred_flush_delivers_nothing(
    engine: Engine,
) -> None:
    world = _World(engine, quiet=_NIGHT)
    notice_id = _hold(world.ledger, _candidate())
    await world.scan_fire()

    world.deferred._dial_reader = lambda _o, _p: InitiativeDial.OFF  # noqa: SLF001
    context = _FakeJobContext()
    await world.run_deferred_job(context)
    assert world.delivery.delivered == []
    assert _disposition(world.ledger, notice_id) is NoticeDisposition.HELD
    assert context.meters == []


@pytest.mark.asyncio
async def test_the_deferred_flush_rechecks_a_window_edited_meanwhile(engine: Engine) -> None:
    world = _World(engine, quiet=_NIGHT)
    notice_id = _hold(world.ledger, _candidate())
    await world.scan_fire()

    world.users.context.quiet_hours = QuietHours(start_minute=22 * 60, end_minute=9 * 60)
    await world.run_deferred_job()
    assert world.delivery.delivered == []
    assert _disposition(world.ledger, notice_id) is NoticeDisposition.HELD
    later = [j["scheduled_at"] for j in world.queue.jobs.values()]
    assert later == [_WINDOW_END, datetime(2026, 9, 26, 7, 0, tzinfo=UTC)]  # 09:00 Oslo


@pytest.mark.asyncio
async def test_fires_seconds_apart_still_make_one_deferred_job(engine: Engine) -> None:
    world = _World(engine, quiet=_NIGHT)
    _hold(world.ledger, _candidate(_PERSONA, "node-91"))
    _hold(world.ledger, _candidate(_OTHER_PERSONA, "node-92"))

    await world.scan_fire(_PERSONA)
    world.clock.now = _SCAN_FIRE + timedelta(seconds=4, microseconds=217_000)
    await world.scan_fire(_OTHER_PERSONA)
    assert world.queue.enqueue_calls == 2
    assert len(world.queue.jobs) == 1
    assert world.only_job()["scheduled_at"] == _WINDOW_END


@pytest.mark.asyncio
async def test_one_persona_on_and_one_off_delivers_only_the_on_personas_notice(
    engine: Engine,
) -> None:
    def dial_for(persona_id: str) -> InitiativeDial:
        return InitiativeDial.OFF if persona_id == _OTHER_PERSONA else InitiativeDial.PROPOSE_ONLY

    world = _World(engine, quiet=_NIGHT, dial_for=dial_for)
    on_notice = _hold(world.ledger, _candidate(_PERSONA, "node-91"))
    off_notice = _hold(world.ledger, _candidate(_OTHER_PERSONA, "node-92"))
    await world.scan_fire(_PERSONA)

    await world.run_deferred_job()
    assert world.delivery.delivered == [on_notice]
    assert _disposition(world.ledger, on_notice) is NoticeDisposition.DELIVERED
    assert _disposition(world.ledger, off_notice) is NoticeDisposition.HELD


class _BillingRecorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> None:  # noqa: ANN401, bill_background_llm's kwargs
        self.calls.append(kwargs)


@pytest.mark.asyncio
async def test_the_deferred_flush_bills_under_one_key_per_owner_and_release(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    import persona_api.initiative.deferred_flush as deferred_mod

    billing = _BillingRecorder()
    monkeypatch.setattr(deferred_mod, "bill_background_llm", billing)
    world = _World(engine, quiet=_NIGHT)
    notice_id = _hold(world.ledger, _candidate())
    await world.scan_fire()

    world.delivery.succeeds = False  # the first attempt spends, then fails to deliver
    await world.run_deferred_job()
    assert _disposition(world.ledger, notice_id) is NoticeDisposition.HELD
    world.delivery.succeeds = True
    await world.run_deferred_job()  # the retry of the same job
    assert _disposition(world.ledger, notice_id) is NoticeDisposition.DELIVERED
    keys = [call["billing_key"] for call in billing.calls]
    expected = f"initiative_deferred_flush:{_OWNER}:2026-09-26T06:00:00+00:00"
    assert keys == [expected, expected]
    assert [call["surface"] for call in billing.calls] == ["initiative_deferred_flush"] * 2
    assert [call["owner_id"] for call in billing.calls] == [_OWNER, _OWNER]


@pytest.mark.asyncio
async def test_a_failing_enqueue_still_lets_the_scan_run_and_bill(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    import persona_api.initiative.handler as handler_mod

    billing = _BillingRecorder()
    monkeypatch.setattr(handler_mod, "bill_background_llm", billing)
    world = _World(engine, quiet=_NIGHT, queue=_FakeQueue(raises=True))
    notice_id = _hold(world.ledger, _candidate())

    context = _FakeJobContext()
    await world.scan_fire(context=context)
    assert world.queue.enqueue_calls == 1  # the deferral was attempted and failed
    assert world.scanner.calls == 1
    assert [call["billing_key"] for call in billing.calls] == [
        f"initiative_scan:{_PERSONA}:{_SCAN_FIRE.isoformat()}"
    ]
    assert [m["detail"]["surface"] for m in context.meters] == ["initiative_scan"]
    assert world.delivery.delivered == []  # a lost deferral never delivers inside the window
    assert _disposition(world.ledger, notice_id) is NoticeDisposition.HELD


def test_one_instant_in_any_offset_is_one_key_and_one_job() -> None:
    queue = _FakeQueue()
    scheduler = QueueDeferredFlushScheduler(queue)  # type: ignore[arg-type]
    oslo_summer = timezone(timedelta(hours=2))
    scheduler.schedule_flush(_OWNER, release_at=_WINDOW_END)
    scheduler.schedule_flush(_OWNER, release_at=_WINDOW_END.astimezone(oslo_summer))
    assert queue.enqueue_calls == 2
    assert list(queue.jobs) == [(_OWNER, "initiative_deferred_flush:2026-09-26T06:00:00+00:00")]
    only = next(iter(queue.jobs.values()))
    assert only["scheduled_at"] == _WINDOW_END
    assert only["scheduled_at"].utcoffset() == timedelta(0)
    payload = DeferredFlushPayload(release_at=_WINDOW_END.astimezone(oslo_summer))
    assert deferred_flush_idempotency_key(payload) == (
        "initiative_deferred_flush:2026-09-26T06:00:00+00:00"
    )


def test_a_naive_release_instant_is_refused() -> None:
    with pytest.raises(ValidationError):
        DeferredFlushPayload(release_at=datetime(2026, 9, 26, 6, 0))  # noqa: DTZ001


def test_production_builds_the_pipeline_with_the_deferral_and_its_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No dark code, behaviourally: the REAL composition root, initiative enabled."""

    class _Tiers:
        configured_tier_names = ("small",)

        def get(self, _tier: str) -> object:
            return object()

    monkeypatch.setenv("PERSONA_INITIATIVE_ENABLED", "true")
    registry = build_worker_registry(
        rls_engine=create_engine("postgresql+psycopg://nobody@localhost/none"),
        embedder=None,  # type: ignore[arg-type]
        tier_registry=_Tiers(),  # type: ignore[arg-type]
        free_tier_registry=None,
        config=APIConfig(
            database_url="postgresql+psycopg://super@localhost/p",
            app_database_url="postgresql+psycopg://persona_app@localhost/p",
        ),
        synthesis_tier="small",
    )
    pipeline = registry.get(INITIATIVE_SCAN_JOB_TYPE).handler._held_batch  # noqa: SLF001
    assert isinstance(pipeline, InitiativePipeline)
    assert isinstance(pipeline._deferred_flush, QueueDeferredFlushScheduler)  # noqa: SLF001
    deferred = registry.get(INITIATIVE_DEFERRED_FLUSH_JOB_TYPE).handler
    assert isinstance(deferred, InitiativeDeferredFlushHandler)
    assert deferred._held_batch is pipeline  # noqa: SLF001

"""Unit tests, the held batch is released by the real scan fire (R9-183).

Phase-1 ruling 4 is flush-on-next-scan: a candidate held for quiet hours, a
cadence cap, or the batch default is released at the owner's NEXT daily scan
fire, re-validated both ways a hold can rot. :meth:`InitiativePipeline.flush`
implemented that and every caller was a test, nothing in production ever
released a hold, so a quiet-hours proposal sat in the ledger until its hold-age
bound, which is itself only checked inside the flush.

These tests drive the REAL scan handler (the job the A1 schedule fires) over the
REAL pipeline and the REAL ledger on the community engine, and assert the
flush's observable effect: the held row is DELIVERED afterwards.
"""

# ruff: noqa: ARG002, the pipeline-seam fakes deliberately ignore some args
from __future__ import annotations

import contextlib
import inspect
from datetime import UTC, datetime, timedelta
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
from persona.tools.categories import ActionCategory
from persona_api.background import worker_root
from persona_api.db.community import (
    create_community_schema,
    ensure_owner,
    make_community_engine,
)
from persona_api.db.models import personas as personas_t
from persona_api.initiative.handler import InitiativeScanHandler, InitiativeScanPayload
from persona_api.initiative.pipeline_wiring import LedgerAdapter
from persona_api.initiative.store import InitiativeLedger, NoticeDisposition
from persona_runtime.initiative import InitiativePipeline
from persona_runtime.initiative.grounding import GroundingVerdict
from sqlalchemy import insert

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    from persona.initiative.envelope import EnvelopeAction
    from sqlalchemy import Engine

_OWNER = "user_alice"
_PERSONA = "astrid"
_FIRE = datetime.now(UTC)
_SETTINGS = InitiativeSettings()


@pytest.fixture
def engine(tmp_path: Any) -> Iterator[Engine]:  # noqa: ANN401, pytest tmp_path
    eng = make_community_engine(tmp_path / "flush.db")
    create_community_schema(eng)
    ensure_owner(eng, owner_id=_OWNER, email="alice@example.com")
    with eng.begin() as conn:
        conn.execute(
            insert(personas_t).values(
                id=_PERSONA,
                owner_id=_OWNER,
                yaml="name: Astrid",
                initiative_dial=InitiativeDial.PROPOSE_ONLY.value,
            )
        )
    yield eng
    eng.dispose()


def _candidate() -> InitiativeCandidate:
    return InitiativeCandidate(
        observation="The hearing is Friday and nothing is drafted.",
        citations=(GroundingCitation(kind=CitationKind.NODE, ref="node-91"),),
        trigger=InitiativeTrigger.APPROACHING_COMMITMENT,
        why_now="The date entered the horizon.",
        plan=(PlannedStep(description="draft", categories=frozenset({ActionCategory.DRAFT})),),
        next_step="Draft the letter.",
        value=0.9,
        acceptance=0.8,
        urgency=Urgency.BATCH,
        source=CandidateSource.SCAN,
        owner_id=_OWNER,
        persona_id=_PERSONA,
        prompt_version="a5-scan-v1",
        scanned_at=_FIRE,
    )


def _hold(ledger: InitiativeLedger, *, at: datetime) -> str:
    """Put a HELD notice in the ledger, quiet hours, a cadence cap, or the batch default."""
    record = ledger.try_claim(
        _candidate(),
        voicer_persona_id=_PERSONA,
        disposition=NoticeDisposition.HELD,
        envelope_action=None,
        held_until=None,
        now=at,
    )
    assert record is not None
    return record.id


def _disposition(ledger: InitiativeLedger, notice_id: str) -> NoticeDisposition | None:
    record = ledger.get_notice(_OWNER, notice_id)
    return None if record is None else record.disposition


# --------------------------------------------------------------------------- #
# the seams the pipeline needs that are not the ledger                        #
# --------------------------------------------------------------------------- #


class _FakeGrounding:
    async def check(self, candidate: InitiativeCandidate) -> GroundingVerdict:
        return GroundingVerdict(admitted=True, supporting_quote="the hearing is Friday")


class _FakeWellbeing:
    def tagged_refs(self, owner_id: str, node_refs: Sequence[str]) -> set[str]:
        return set()


class _FakeDeclines:
    def suppressed_keys(self, owner_id: str, keys: Sequence[str]) -> set[str]:
        return set()


class _Context:
    timezone = "UTC"
    quiet_hours = None


class _FakeUsers:
    def user_context(self, owner_id: str) -> _Context:
        return _Context()


class _FakeProvenance:
    def citation_personas(self, owner_id: str, node_refs: Sequence[str]) -> list[str]:
        return []

    def activity_rank(self, owner_id: str) -> dict[str, int]:
        return {}


class _FakeAuditor:
    def record(self, owner_id: str, event: str, opportunity_key: str, detail: str) -> None:
        return


class _FakeDelivery:
    """The delivery seam, records what the flush actually put in front of the user."""

    def __init__(self, *, succeeds: bool = True) -> None:
        self._succeeds = succeeds
        self.delivered: list[str] = []

    async def deliver(self, owner_id: str, notice_id: str, action: EnvelopeAction) -> bool:
        self.delivered.append(notice_id)
        return self._succeeds


class _FakeScanner:
    """A thin scan, a scan that finds nothing is success, and still owes the flush."""

    def __init__(self) -> None:
        self.calls = 0

    async def scan(
        self, owner_id: str, persona_id: str, *, fire_time: datetime
    ) -> tuple[InitiativeCandidate, ...]:
        self.calls += 1
        return ()


class _FakeJobContext:
    def __init__(self, owner_id: str = _OWNER) -> None:
        self._owner_id = owner_id
        self.meters: list[dict[str, Any]] = []

    @property
    def owner_id(self) -> str:
        return self._owner_id

    @property
    def job_id(self) -> str:
        return "job-1"

    @contextlib.contextmanager
    def connection(self) -> Iterator[object]:
        yield object()

    def meter(
        self, *, amount_micros: int, kind: str, detail: Mapping[str, str] | None = None
    ) -> None:
        self.meters.append(
            {"amount_micros": amount_micros, "kind": kind, "detail": dict(detail or {})}
        )


def _pipeline(engine: Engine, delivery: _FakeDelivery) -> InitiativePipeline:
    """The REAL pipeline over the REAL ledger (only the model-touching seams are fake)."""
    return InitiativePipeline(
        grounding=_FakeGrounding(),  # type: ignore[arg-type]  # protocol-shaped fake
        wellbeing=_FakeWellbeing(),
        declines=_FakeDeclines(),
        ledger=LedgerAdapter(InitiativeLedger(engine)),
        users=_FakeUsers(),
        provenance=_FakeProvenance(),
        auditor=_FakeAuditor(),
        dial_reader=lambda _o, _p: InitiativeDial.PROPOSE_ONLY,
        settings=_SETTINGS,
        delivery=delivery,  # type: ignore[arg-type]  # protocol-shaped fake
    )


def _handler(
    engine: Engine,
    delivery: _FakeDelivery,
    *,
    scanner: _FakeScanner,
    dial: InitiativeDial = InitiativeDial.PROPOSE_ONLY,
    pause_check: Any = None,  # noqa: ANN401, optional seam
) -> InitiativeScanHandler:
    pipeline = _pipeline(engine, delivery)
    kwargs: dict[str, Any] = {}
    if pause_check is not None:
        kwargs["pause_check"] = pause_check
    return InitiativeScanHandler(
        scanner=scanner,  # type: ignore[arg-type]  # protocol-shaped fake
        dial_reader=lambda _o, _p: dial,
        sink=pipeline,
        held_batch=pipeline,
        **kwargs,
    )


def _payload() -> InitiativeScanPayload:
    return InitiativeScanPayload(
        persona_id=_PERSONA, schedule_id=f"initsched:{_PERSONA}", fire_time=_FIRE
    )


# --------------------------------------------------------------------------- #
# 1. the real scan fire releases the batch                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_scan_fire_releases_a_held_notice(engine: Engine) -> None:
    """The whole point: nothing but the scan fire is touched, and the hold goes out."""
    ledger = InitiativeLedger(engine)
    notice_id = _hold(ledger, at=_FIRE - timedelta(hours=8))
    delivery = _FakeDelivery()

    await _handler(engine, delivery, scanner=_FakeScanner()).handle(_payload(), _FakeJobContext())  # type: ignore[arg-type]

    assert delivery.delivered == [notice_id]
    assert _disposition(ledger, notice_id) is NoticeDisposition.DELIVERED


@pytest.mark.asyncio
async def test_a_hold_past_its_age_bound_is_expired_by_the_scan_fire(engine: Engine) -> None:
    """The other half of the flush: a rotten hold is written stale, never delivered."""
    ledger = InitiativeLedger(engine)
    stale_at = _FIRE - timedelta(days=_SETTINGS.hold_max_days + 1)
    notice_id = _hold(ledger, at=stale_at)
    delivery = _FakeDelivery()

    await _handler(engine, delivery, scanner=_FakeScanner()).handle(_payload(), _FakeJobContext())  # type: ignore[arg-type]

    assert delivery.delivered == []
    assert _disposition(ledger, notice_id) is NoticeDisposition.SUPPRESSED_STALE


@pytest.mark.asyncio
async def test_a_thin_scan_still_flushes(engine: Engine) -> None:
    """A scan that finds nothing returns early, the flush must run before that exit."""
    scanner = _FakeScanner()
    ledger = InitiativeLedger(engine)
    notice_id = _hold(ledger, at=_FIRE - timedelta(hours=8))
    delivery = _FakeDelivery()

    await _handler(engine, delivery, scanner=scanner).handle(_payload(), _FakeJobContext())  # type: ignore[arg-type]

    assert scanner.calls == 1  # the scan really did run and really did find nothing
    assert delivery.delivered == [notice_id]


# --------------------------------------------------------------------------- #
# 2. the flush obeys the same gates the fire does                             #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_dial_off_fire_flushes_nothing(engine: Engine) -> None:
    """The Phase-1 ruling exit: no scan, no spend, no message, the flush spends too."""
    ledger = InitiativeLedger(engine)
    notice_id = _hold(ledger, at=_FIRE - timedelta(hours=8))
    delivery = _FakeDelivery()

    handler = _handler(engine, delivery, scanner=_FakeScanner(), dial=InitiativeDial.OFF)
    await handler.handle(_payload(), _FakeJobContext())  # type: ignore[arg-type]

    assert delivery.delivered == []
    assert _disposition(ledger, notice_id) is NoticeDisposition.HELD


@pytest.mark.asyncio
async def test_a_paused_owner_flushes_nothing(engine: Engine) -> None:
    """A6-D-8 completeness: a paused owner originates nothing, held batch included."""
    ledger = InitiativeLedger(engine)
    notice_id = _hold(ledger, at=_FIRE - timedelta(hours=8))
    delivery = _FakeDelivery()

    handler = _handler(engine, delivery, scanner=_FakeScanner(), pause_check=lambda _o: True)
    await handler.handle(_payload(), _FakeJobContext())  # type: ignore[arg-type]

    assert delivery.delivered == []
    assert _disposition(ledger, notice_id) is NoticeDisposition.HELD


@pytest.mark.asyncio
async def test_a_failing_flush_never_crashes_the_scan_job() -> None:
    """Fail-soft is absolute: a broken flush still leaves a scan behind it."""

    class _BrokenFlush:
        async def flush(self, owner_id: str) -> None:
            msg = "ledger down"
            raise RuntimeError(msg)

    scanner = _FakeScanner()
    context = _FakeJobContext()
    handler = InitiativeScanHandler(
        scanner=scanner,  # type: ignore[arg-type]  # protocol-shaped fake
        dial_reader=lambda _o, _p: InitiativeDial.PROPOSE_ONLY,
        sink=None,
        held_batch=_BrokenFlush(),
    )

    await handler.handle(_payload(), context)  # type: ignore[arg-type]

    assert scanner.calls == 1
    assert context.meters  # the fire was metered; the flush error did not propagate


@pytest.mark.asyncio
async def test_an_unwired_flush_seam_is_a_clean_no_op() -> None:
    """The default stays harmless, every pre-existing composition keeps working."""
    scanner = _FakeScanner()
    handler = InitiativeScanHandler(
        scanner=scanner,  # type: ignore[arg-type]  # protocol-shaped fake
        dial_reader=lambda _o, _p: InitiativeDial.PROPOSE_ONLY,
        sink=None,
        held_batch=None,
    )

    await handler.handle(_payload(), _FakeJobContext())  # type: ignore[arg-type]

    assert scanner.calls == 1


# --------------------------------------------------------------------------- #
# 3. reachability: the composition root wires the seam                        #
# --------------------------------------------------------------------------- #


def test_the_worker_root_hands_the_scan_handler_its_flush() -> None:
    """No dark code: the production handler is built WITH the pipeline's flush."""
    assert "held_batch=pipeline" in inspect.getsource(worker_root)

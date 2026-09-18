"""Unit tests — the two silent ways a person declines an initiative (Spec A5, T3/T10).

The restraint ledger typed three decline sources from the start and only ever
wrote one of them. These tests drive the other two through their REAL paths on a
real store (the community engine, the same tables cloud runs):

- a STOP verb ("stop suggesting things") declines what the persona has actually
  put in front of the user, through the same store method the reply path uses;
- a proposal the user simply never answers is declined at the expiry sweep, once
  and only once however many times the sweep runs;

and the consumer half in the same test: an ``ignored_expiry`` record changes the
pipeline's next decision exactly as a ``declined_reply`` record does — the
suppression is source-blind on purpose (the source is provenance, not weight).
"""

# ruff: noqa: ARG002 — the pipeline-seam fakes deliberately ignore some args
from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

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
from persona.jobs import JobRegistry
from persona.tools.categories import ActionCategory
from persona_api.background import worker_root
from persona_api.db.community import (
    create_community_schema,
    ensure_owner,
    make_community_engine,
)
from persona_api.db.models import initiative_declines as declines_t
from persona_api.db.models import initiative_notices as notices_t
from persona_api.db.models import personas as personas_t
from persona_api.initiative.ignored_sweep import IgnoredProposalSweeper
from persona_api.initiative.store import (
    DeclineSource,
    DeclineStore,
    InitiativeLedger,
    NoticeDisposition,
)
from persona_api.initiative.verb_service import InitiativeVerbService
from persona_api.jobs import Worker
from persona_api.schedules import ScheduleStore
from persona_runtime.initiative import InitiativePipeline
from persona_runtime.initiative.grounding import GroundingVerdict
from sqlalchemy import insert, select

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator, Sequence

    from sqlalchemy import Engine

_OWNER = "user_alice"
_PERSONA = "astrid"
# Anchored on the real clock: the verb service stamps its own ``datetime.now``, so a
# frozen past instant would put every delivered notice outside the answerable window.
_NOW = datetime.now(UTC)
_SETTINGS = InitiativeSettings()


# --------------------------------------------------------------------------- #
# the real store on the community engine                                      #
# --------------------------------------------------------------------------- #


@pytest.fixture
def engine(tmp_path: Any) -> Iterator[Engine]:  # noqa: ANN401 — pytest tmp_path
    eng = make_community_engine(tmp_path / "declines.db")
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


def _candidate(**overrides: Any) -> InitiativeCandidate:  # noqa: ANN401 — override bag
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
        "owner_id": _OWNER,
        "persona_id": _PERSONA,
        "prompt_version": "a5-scan-v1",
        "scanned_at": _NOW,
    }
    fields.update(overrides)
    return InitiativeCandidate(**fields)


def _deliver(
    ledger: InitiativeLedger,
    candidate: InitiativeCandidate,
    *,
    at: datetime,
    action: str = "propose",
) -> str:
    """Put a DELIVERED notice in the ledger — what the user was actually shown."""
    record = ledger.try_claim(
        candidate,
        voicer_persona_id=_PERSONA,
        disposition=NoticeDisposition.DELIVERED,
        envelope_action=action,
        held_until=None,
        now=at,
    )
    assert record is not None
    return record.id


def _decline_rows(engine: Engine) -> list[dict[str, Any]]:
    with engine.begin() as conn:
        return [
            dict(r)
            for r in conn.execute(
                select(
                    declines_t.c.opportunity_key,
                    declines_t.c.source,
                    declines_t.c.persona_id,
                    declines_t.c.revived_at,
                )
            ).mappings()
        ]


def _superseded_at(engine: Engine, notice_id: str) -> Any:  # noqa: ANN401 — driver-typed
    with engine.begin() as conn:
        return conn.execute(
            select(notices_t.c.superseded_at).where(notices_t.c.id == notice_id)
        ).scalar_one()


class _FakeExecutor:
    """The confirm door — never reached by any test here."""

    async def execute_confirmed(self, owner_id: str, notice_id: str) -> bool:
        raise AssertionError("confirm must not run in a decline test")


def _verb_service(engine: Engine) -> InitiativeVerbService:
    ledger = InitiativeLedger(engine)
    return InitiativeVerbService(
        rls_engine=engine,
        schedules=ScheduleStore(engine),
        ledger=ledger,
        declines=DeclineStore(engine),
        executor=_FakeExecutor(),  # type: ignore[arg-type]  # protocol-shaped fake
        settings=_SETTINGS,
    )


# --------------------------------------------------------------------------- #
# 1. the stop verb                                                            #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_stop_verb_declines_what_the_persona_put_in_front_of_the_user(
    engine: Engine,
) -> None:
    ledger = InitiativeLedger(engine)
    candidate = _candidate()
    _deliver(ledger, candidate, at=_NOW - timedelta(days=1))

    await _verb_service(engine).apply(
        owner_id=_OWNER, persona_id=_PERSONA, verb="dial_off", notice_id=None
    )

    rows = _decline_rows(engine)
    assert [r["source"] for r in rows] == [DeclineSource.STOP_VERB.value]
    assert rows[0]["opportunity_key"] == candidate.opportunity_key
    assert rows[0]["persona_id"] == _PERSONA
    # the slot is freed the same way the reply path frees it
    assert (
        ledger.delivered_in_window(
            _OWNER, _PERSONA, max_age_days=_SETTINGS.hold_max_days, now=datetime.now(UTC)
        )
        == []
    )


@pytest.mark.asyncio
async def test_stop_verb_still_writes_the_dial(engine: Engine) -> None:
    ledger = InitiativeLedger(engine)
    _deliver(ledger, _candidate(), at=_NOW - timedelta(days=1))

    await _verb_service(engine).apply(
        owner_id=_OWNER, persona_id=_PERSONA, verb="dial_off", notice_id=None
    )

    with engine.begin() as conn:
        dial = conn.execute(
            select(personas_t.c.initiative_dial).where(personas_t.c.id == _PERSONA)
        ).scalar_one()
    assert dial == InitiativeDial.OFF.value


@pytest.mark.asyncio
async def test_stop_verb_with_nothing_shown_declines_nothing(engine: Engine) -> None:
    await _verb_service(engine).apply(
        owner_id=_OWNER, persona_id=_PERSONA, verb="dial_off", notice_id=None
    )
    assert _decline_rows(engine) == []


@pytest.mark.asyncio
async def test_a_non_stop_dial_verb_declines_nothing(engine: Engine) -> None:
    ledger = InitiativeLedger(engine)
    _deliver(ledger, _candidate(), at=_NOW - timedelta(days=1))

    await _verb_service(engine).apply(
        owner_id=_OWNER, persona_id=_PERSONA, verb="dial_propose_only", notice_id=None
    )
    assert _decline_rows(engine) == []


# --------------------------------------------------------------------------- #
# 2. the ignored proposal                                                     #
# --------------------------------------------------------------------------- #


class _FakeLeader:
    def __init__(self, *, wins: bool = True) -> None:
        self._wins = wins
        self.resigned = False

    def try_become_leader(self) -> bool:
        return self._wins

    def resign(self) -> None:
        self.resigned = True


def _sweeper(engine: Engine, *, wins: bool = True) -> IgnoredProposalSweeper:
    return IgnoredProposalSweeper(
        dispatch_engine=engine,
        declines=DeclineStore(engine),
        ledger=InitiativeLedger(engine),
        settings=_SETTINGS,
        leader_factory=lambda: _FakeLeader(wins=wins),
    )


def test_an_ignored_proposal_is_declined_at_expiry(engine: Engine) -> None:
    ledger = InitiativeLedger(engine)
    candidate = _candidate()
    stale = _NOW - timedelta(days=_SETTINGS.hold_max_days + 1)
    _deliver(ledger, candidate, at=stale)

    assert _sweeper(engine).run_once(now=_NOW) == 1

    rows = _decline_rows(engine)
    assert [r["source"] for r in rows] == [DeclineSource.IGNORED_EXPIRY.value]
    assert rows[0]["opportunity_key"] == candidate.opportunity_key


def test_the_sweep_writes_one_record_however_often_it_runs(engine: Engine) -> None:
    """The idempotency guard: the swept notice leaves the candidate set for good."""
    ledger = InitiativeLedger(engine)
    stale = _NOW - timedelta(days=_SETTINGS.hold_max_days + 1)
    notice_id = _deliver(ledger, _candidate(), at=stale)

    sweeper = _sweeper(engine)
    first = sweeper.run_once(now=_NOW)
    assert _superseded_at(engine, notice_id) is not None  # out of the query, permanently
    second = sweeper.run_once(now=_NOW + timedelta(hours=1))

    assert (first, second) == (1, 0)
    assert len(_decline_rows(engine)) == 1


def test_a_later_notice_on_an_already_declined_topic_adds_no_second_record(
    engine: Engine,
) -> None:
    """The store-level convergence behind the guard: one LIVE decline per topic."""
    ledger = InitiativeLedger(engine)
    candidate = _candidate()
    stale = _NOW - timedelta(days=_SETTINGS.hold_max_days + 1)
    _deliver(ledger, candidate, at=stale)
    sweeper = _sweeper(engine)
    assert sweeper.run_once(now=_NOW) == 1

    # the slot re-opened, so a fresh notice can claim the same topic and age out too
    _deliver(ledger, candidate, at=stale)
    assert sweeper.run_once(now=_NOW) == 0
    assert len(_decline_rows(engine)) == 1


def test_a_proposal_still_inside_its_window_is_left_alone(engine: Engine) -> None:
    ledger = InitiativeLedger(engine)
    _deliver(ledger, _candidate(), at=_NOW - timedelta(hours=2))

    assert _sweeper(engine).run_once(now=_NOW) == 0
    assert _decline_rows(engine) == []


def test_a_non_leader_sweep_is_a_clean_no_op(engine: Engine) -> None:
    ledger = InitiativeLedger(engine)
    stale = _NOW - timedelta(days=_SETTINGS.hold_max_days + 1)
    _deliver(ledger, _candidate(), at=stale)

    assert _sweeper(engine, wins=False).run_once(now=_NOW) == 0
    assert _decline_rows(engine) == []


def test_an_acted_report_is_not_declined_by_silence(engine: Engine) -> None:
    """Silence answers a question; an act-then-report asked none (the honest scope)."""
    ledger = InitiativeLedger(engine)
    stale = _NOW - timedelta(days=_SETTINGS.hold_max_days + 1)
    _deliver(ledger, _candidate(), at=stale, action="act")

    assert _sweeper(engine).run_once(now=_NOW) == 0
    assert _decline_rows(engine) == []


# --------------------------------------------------------------------------- #
# 3. the consumer: restraint learns the same way from all three sources        #
# --------------------------------------------------------------------------- #


class _FakeGrounding:
    async def check(self, candidate: InitiativeCandidate) -> GroundingVerdict:
        return GroundingVerdict(admitted=True, supporting_quote="the hearing is Friday")


class _FakeWellbeing:
    def tagged_refs(self, owner_id: str, node_refs: Sequence[str]) -> set[str]:
        return set()


class _FakeLedgerSeam:
    """The pipeline's ledger seam — records whether the opportunity was claimed."""

    def __init__(self) -> None:
        self.claims: list[str] = []

    def try_claim_held(
        self,
        candidate: InitiativeCandidate,
        *,
        voicer_persona_id: str,
        held_until: datetime | None,
        now: datetime,
    ) -> None:
        self.claims.append(candidate.opportunity_key)
        return

    def mark_delivered(
        self, owner_id: str, notice_id: str, *, envelope_action: str, now: datetime
    ) -> bool:
        return True

    def expire_stale(self, owner_id: str, notice_id: str, *, reason: str, now: datetime) -> bool:
        return True

    def held_for_owner(self, owner_id: str) -> list[Any]:
        return []

    def delivered_counts(self, owner_id: str, *, persona_id: str, now: datetime) -> CadenceCounts:
        return CadenceCounts(persona_day=0, persona_week=0, user_day=0)


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
    def __init__(self) -> None:
        self.events: list[str] = []

    def record(self, owner_id: str, event: str, opportunity_key: str, detail: str) -> None:
        self.events.append(event)


def _pipeline(engine: Engine, seam: _FakeLedgerSeam, auditor: _FakeAuditor) -> InitiativePipeline:
    """The REAL pipeline reading the REAL decline store (the consumer under test)."""
    return InitiativePipeline(
        grounding=_FakeGrounding(),  # type: ignore[arg-type]  # protocol-shaped fake
        wellbeing=_FakeWellbeing(),
        declines=DeclineStore(engine),
        ledger=seam,  # type: ignore[arg-type]  # protocol-shaped fake
        users=_FakeUsers(),
        provenance=_FakeProvenance(),
        auditor=auditor,
        dial_reader=lambda _o, _p: InitiativeDial.PROPOSE_ONLY,
        settings=_SETTINGS,
    )


async def _declined_reply(engine: Engine, candidate: InitiativeCandidate) -> None:
    ledger = InitiativeLedger(engine)
    notice_id = _deliver(ledger, candidate, at=_NOW - timedelta(hours=1))
    await _verb_service(engine).apply(
        owner_id=_OWNER, persona_id=_PERSONA, verb="decline_proposal", notice_id=notice_id
    )


async def _stop_verb(engine: Engine, candidate: InitiativeCandidate) -> None:
    _deliver(InitiativeLedger(engine), candidate, at=_NOW - timedelta(hours=1))
    await _verb_service(engine).apply(
        owner_id=_OWNER, persona_id=_PERSONA, verb="dial_off", notice_id=None
    )


async def _ignored_expiry(engine: Engine, candidate: InitiativeCandidate) -> None:
    stale = _NOW - timedelta(days=_SETTINGS.hold_max_days + 1)
    _deliver(InitiativeLedger(engine), candidate, at=stale)
    assert _sweeper(engine).run_once(now=_NOW) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("producer", "expected_source"),
    [
        (_declined_reply, DeclineSource.DECLINED_REPLY),
        (_stop_verb, DeclineSource.STOP_VERB),
        (_ignored_expiry, DeclineSource.IGNORED_EXPIRY),
    ],
    ids=["declined_reply", "stop_verb", "ignored_expiry"],
)
async def test_restraint_backs_off_identically_whichever_way_the_user_declined(
    engine: Engine,
    producer: Callable[[Engine, InitiativeCandidate], Awaitable[None]],
    expected_source: DeclineSource,
) -> None:
    candidate = _candidate()
    await producer(engine, candidate)
    assert [r["source"] for r in _decline_rows(engine)] == [expected_source.value]

    seam, auditor = _FakeLedgerSeam(), _FakeAuditor()
    await _pipeline(engine, seam, auditor).submit((candidate,))

    assert seam.claims == []  # the opportunity is never raised again
    assert "initiative.discard_declined" in auditor.events


@pytest.mark.asyncio
async def test_without_a_decline_the_same_candidate_is_raised(engine: Engine) -> None:
    """The control: the suppression above is the decline record, not the harness."""
    seam, auditor = _FakeLedgerSeam(), _FakeAuditor()
    await _pipeline(engine, seam, auditor).submit((_candidate(),))

    assert seam.claims == [_candidate().opportunity_key]
    assert "initiative.discard_declined" not in auditor.events


# --------------------------------------------------------------------------- #
# 4. reachability: the sweep is driven by the real worker loop                 #
# --------------------------------------------------------------------------- #


def _worker(**kw: Any) -> Worker:  # noqa: ANN401 — the Worker kwarg bag
    return Worker(
        dispatch_engine=MagicMock(),
        rls_engine=MagicMock(),
        registry=JobRegistry(),
        worker_id="w-test",
        **kw,
    )


def test_the_sweep_is_a_no_op_on_a_worker_without_initiative() -> None:
    asyncio.run(_worker()._maybe_run_ignored_proposal_sweep())  # must not raise  # noqa: SLF001


def test_the_worker_runs_the_sweep_then_respects_its_cadence() -> None:
    sweep = MagicMock()
    sweep.run_once = MagicMock(return_value=0)
    worker = _worker(ignored_proposal_sweep=sweep, ignored_proposal_sweep_interval_seconds=10_000.0)

    asyncio.run(worker._maybe_run_ignored_proposal_sweep())  # noqa: SLF001
    assert sweep.run_once.call_count == 1
    asyncio.run(worker._maybe_run_ignored_proposal_sweep())  # noqa: SLF001
    assert sweep.run_once.call_count == 1  # inside the interval → skipped


def test_a_failing_sweep_never_crashes_the_worker_loop() -> None:
    sweep = MagicMock()
    sweep.run_once = MagicMock(side_effect=RuntimeError("boom"))
    worker = _worker(ignored_proposal_sweep=sweep, ignored_proposal_sweep_interval_seconds=10_000.0)

    asyncio.run(worker._maybe_run_ignored_proposal_sweep())  # noqa: SLF001
    assert sweep.run_once.call_count == 1


def test_the_loop_body_and_the_composition_root_actually_reach_the_sweep() -> None:
    """No dark code: the loop calls it and the worker root builds it (Spec A5, T3)."""
    assert "_maybe_run_ignored_proposal_sweep()" in inspect.getsource(Worker.run)
    root = inspect.getsource(worker_root)
    assert "ignored_proposal_sweep_builder=_ignored_proposal_sweep_builder" in root
    assert "IgnoredProposalSweeper(" in root

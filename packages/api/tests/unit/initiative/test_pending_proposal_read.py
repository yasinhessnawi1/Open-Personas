"""Unit tests, the pending-proposal read on the community engine (R9-182).

The confirm/decline floors (Spec A5, T10) ask the LEDGER, once per turn,
"is a proposal pending for this owner+persona?". The read compares the stored
``delivered_at`` against ``datetime.now(UTC)`` in Python, so the instant has to
come back tz-aware. The community SQLite engine stores no tzinfo and hands it
back naive, which made that subtraction a ``TypeError``, swallowed by the
provider's fail-soft guard, so every live proposal read as "nothing pending"
and neither verb could ever fire in the community edition.

These tests drive the REAL store on the REAL community engine and then the REAL
closure the runtime factory hands the loop, down to the pure resolver the loop
calls with its answer.
"""

from __future__ import annotations

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
from persona.tools.categories import ActionCategory
from persona_api.db.community import (
    create_community_schema,
    ensure_owner,
    make_community_engine,
)
from persona_api.db.models import personas as personas_t
from persona_api.initiative.store import InitiativeLedger, NoticeDisposition
from persona_api.middleware.rls_context import current_user_id
from persona_api.services.runtime_factory import RuntimeFactory
from persona_runtime.initiative.verbs import InitiativeVerb, resolve_verb_for_pending
from sqlalchemy import insert

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from sqlalchemy import Engine

_OWNER = "user_alice"
_PERSONA = "astrid"
_NOW = datetime.now(UTC)
_SETTINGS = InitiativeSettings()


@pytest.fixture
def engine(tmp_path: Any) -> Iterator[Engine]:  # noqa: ANN401, pytest tmp_path
    eng = make_community_engine(tmp_path / "pending.db")
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
        scanned_at=_NOW,
    )


def _deliver(ledger: InitiativeLedger, *, at: datetime) -> str:
    record = ledger.try_claim(
        _candidate(),
        voicer_persona_id=_PERSONA,
        disposition=NoticeDisposition.DELIVERED,
        envelope_action="propose",
        held_until=None,
        now=at,
    )
    assert record is not None
    return record.id


def test_a_delivered_proposal_is_pending_on_the_community_engine(engine: Engine) -> None:
    """The bug's core: the read must not blow up on a naive stored instant."""
    ledger = InitiativeLedger(engine)
    notice_id = _deliver(ledger, at=_NOW - timedelta(hours=2))

    pending = ledger.latest_pending_proposal(_OWNER, _PERSONA, max_age_days=_SETTINGS.hold_max_days)

    assert pending is not None
    assert pending.id == notice_id


def test_every_instant_the_read_returns_is_tz_aware(engine: Engine) -> None:
    """The fix is the whole row, not one column, a naive sibling is the next bug."""
    ledger = InitiativeLedger(engine)
    notice_id = _deliver(ledger, at=_NOW - timedelta(hours=2))

    record = ledger.get_notice(_OWNER, notice_id)

    assert record is not None
    assert record.delivered_at is not None
    assert record.delivered_at.tzinfo is not None
    assert record.created_at.tzinfo is not None


def test_a_proposal_past_its_window_is_still_not_pending(engine: Engine) -> None:
    """The negative the arithmetic exists for: expiry must survive the fix."""
    ledger = InitiativeLedger(engine)
    _deliver(ledger, at=_NOW - timedelta(days=_SETTINGS.hold_max_days + 1))

    assert (
        ledger.latest_pending_proposal(_OWNER, _PERSONA, max_age_days=_SETTINGS.hold_max_days)
        is None
    )


def _pending_provider(engine: Engine) -> Callable[[], str | None]:
    """The REAL closure the runtime factory hands the conversation loop."""
    factory = RuntimeFactory(
        rls_engine=engine,
        embedder=MagicMock(),
        tier_registry=MagicMock(),
        turn_log_writer=MagicMock(),
        audit_root=MagicMock(),
    )
    provider = factory._build_initiative_pending_provider(_PERSONA)  # noqa: SLF001
    assert provider is not None
    return provider


@pytest.mark.parametrize(
    ("reply", "expected"),
    [("yes", InitiativeVerb.CONFIRM_PROPOSAL), ("no", InitiativeVerb.DECLINE_PROPOSAL)],
    ids=["confirm", "decline"],
)
def test_the_confirm_and_decline_floors_see_the_pending_proposal(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    reply: str,
    expected: InitiativeVerb,
) -> None:
    """The consumer half: the loop's floors resolve a verb against the real read.

    The provider swallows every error by design (a pending read must never break
    a turn), so the naive-instant ``TypeError`` surfaced here as silence, both
    verbs fell through and the proposal could never be answered.
    """
    monkeypatch.setenv("PERSONA_INITIATIVE_ENABLED", "true")
    notice_id = _deliver(InitiativeLedger(engine), at=_NOW - timedelta(hours=2))
    provider = _pending_provider(engine)

    token = current_user_id.set(_OWNER)
    try:
        resolved = resolve_verb_for_pending(reply, provider())
    finally:
        current_user_id.reset(token)

    assert resolved == (expected, notice_id)

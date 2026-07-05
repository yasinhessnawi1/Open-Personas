"""A5 T3 — DeclineStore + InitiativeLedger on the real RLS stack (A5-D-3/D-4/D-6).

Real Postgres under the ``persona_app`` non-superuser role. Proves:

* **Non-vacuous RLS** — BOTH tenants have decline + notice rows; each sees only
  its own through every read surface (the standing adversarial bar).
* **The arbitration race is harmless** — two claims for one opportunity: exactly
  one LIVE notice; the loser no-ops and the duplicate is AUDITED.
* **Decline lifecycle (A5-D-4)** — live decline suppresses; ``revive`` is an
  explicit UPDATE (history kept) after which a fresh decline records again.
* **Ruling 3** — ``expire_stale`` WRITES the ``suppressed_stale`` disposition
  AND the audit row (never a silent drop).
* **Trailing-window cadence counts** — delivered-only, persona/day + persona/week
  + user/day (the pile-on guard counts across personas).
"""

# ruff: noqa: ARG001 — ``migrated_engine`` is a dependency-ordering fixture param.
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from persona.initiative import (
    CandidateSource,
    CitationKind,
    GroundingCitation,
    InitiativeCandidate,
    InitiativeTrigger,
    PlannedStep,
    Urgency,
)
from persona.tools.categories import ActionCategory
from persona_api.initiative import (
    DeclineSource,
    DeclineStore,
    InitiativeLedger,
    NoticeDisposition,
)
from sqlalchemy import create_engine, text

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 7, 4, 6, 0, tzinfo=UTC)


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (non-superuser role) not set; skipping RLS test")
    engine = create_engine(app_url.replace("+asyncpg", "+psycopg"))
    yield engine
    engine.dispose()


def _seed_user(engine: Engine, uid: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:u, :e) ON CONFLICT DO NOTHING"),
            {"u": uid, "e": f"{uid}@example.com"},
        )


def _candidate(
    owner: str,
    *,
    persona: str = "persona_a",
    ref: str = "node-hearing-1",
    trigger: InitiativeTrigger = InitiativeTrigger.APPROACHING_COMMITMENT,
) -> InitiativeCandidate:
    return InitiativeCandidate(
        observation="The hearing is Friday and no response letter exists.",
        citations=(GroundingCitation(kind=CitationKind.NODE, ref=ref),),
        trigger=trigger,
        why_now="The date entered the horizon.",
        plan=(
            PlannedStep(
                description="draft the letter",
                categories=frozenset({ActionCategory.DRAFT}),
            ),
        ),
        next_step="Draft the response letter.",
        value=0.9,
        acceptance=0.8,
        urgency=Urgency.BATCH,
        source=CandidateSource.SCAN,
        owner_id=owner,
        persona_id=persona,
        prompt_version="v1",
        scanned_at=_NOW,
    )


def _audit_actions(engine: Engine, user_id: str, action: str) -> int:
    with engine.begin() as conn:
        return conn.execute(
            text("SELECT count(*) FROM audit_log WHERE user_id = :u AND action = :a"),
            {"u": user_id, "a": action},
        ).scalar_one()


class TestRlsNonVacuous:
    def test_both_tenants_have_rows_each_sees_only_its_own(
        self, migrated_engine: Engine, app_engine: Engine
    ) -> None:
        declines = DeclineStore(app_engine)
        ledger = InitiativeLedger(app_engine)
        _seed_user(migrated_engine, "user_a")
        _seed_user(migrated_engine, "user_b")

        for owner in ("user_a", "user_b"):
            candidate = _candidate(owner, ref=f"node-{owner}")
            assert (
                declines.record_decline(
                    owner,
                    opportunity_key=f"declined:{owner}",
                    trigger="conflict",
                    source=DeclineSource.DECLINED_REPLY,
                    persona_id=None,
                    now=_NOW,
                )
                is not None
            )
            assert (
                ledger.try_claim(
                    candidate,
                    voicer_persona_id="persona_a",
                    disposition=NoticeDisposition.HELD,
                    envelope_action=None,
                    held_until=None,
                    now=_NOW,
                )
                is not None
            )

        # Each tenant's reads see exactly their own rows — never the other's.
        assert declines.suppressed_keys("user_a", ["declined:user_a", "declined:user_b"]) == {
            "declined:user_a"
        }
        assert declines.suppressed_keys("user_b", ["declined:user_a", "declined:user_b"]) == {
            "declined:user_b"
        }
        held_a = ledger.held_for_owner("user_a")
        held_b = ledger.held_for_owner("user_b")
        assert [n.owner_id for n in held_a] == ["user_a"]
        assert [n.owner_id for n in held_b] == ["user_b"]
        # Cross-tenant mutation attempts hit zero rows (RLS, not app logic):
        # user_b tries to supersede user_a's (distinct-keyed) notice — no row moves.
        assert held_a[0].opportunity_key != held_b[0].opportunity_key
        assert ledger.supersede("user_b", held_a[0].opportunity_key, now=_NOW) is False
        assert [n.owner_id for n in ledger.held_for_owner("user_a")] == ["user_a"]


class TestArbitrationRace:
    def test_one_live_notice_per_opportunity_loser_audited(
        self, migrated_engine: Engine, app_engine: Engine
    ) -> None:
        ledger = InitiativeLedger(app_engine)
        _seed_user(migrated_engine, "user_race")
        candidate = _candidate("user_race", ref="node-shared-deadline")

        won = ledger.try_claim(
            candidate,
            voicer_persona_id="persona_a",
            disposition=NoticeDisposition.HELD,
            envelope_action=None,
            held_until=None,
            now=_NOW,
        )
        lost = ledger.try_claim(
            _candidate("user_race", persona="persona_b", ref="node-shared-deadline"),
            voicer_persona_id="persona_b",
            disposition=NoticeDisposition.HELD,
            envelope_action=None,
            held_until=None,
            now=_NOW,
        )
        assert won is not None
        assert lost is None  # the loser's claim no-ops — one user-level notice
        assert _audit_actions(app_engine, "user_race", "initiative.duplicate_suppressed") == 1

    def test_supersede_reopens_the_slot(self, migrated_engine: Engine, app_engine: Engine) -> None:
        ledger = InitiativeLedger(app_engine)
        _seed_user(migrated_engine, "user_slot")
        candidate = _candidate("user_slot", ref="node-slot")

        first = ledger.try_claim(
            candidate,
            voicer_persona_id="persona_a",
            disposition=NoticeDisposition.HELD,
            envelope_action=None,
            held_until=None,
            now=_NOW,
        )
        assert first is not None
        assert ledger.supersede("user_slot", candidate.opportunity_key, now=_NOW) is True
        second = ledger.try_claim(
            candidate,
            voicer_persona_id="persona_a",
            disposition=NoticeDisposition.HELD,
            envelope_action=None,
            held_until=None,
            now=_NOW,
        )
        assert second is not None  # revival re-opened the slot; history retained


class TestDeclineLifecycle:
    def test_decline_suppresses_revive_reopens_and_redecline_records(
        self, migrated_engine: Engine, app_engine: Engine
    ) -> None:
        declines = DeclineStore(app_engine)
        _seed_user(migrated_engine, "user_dl")
        key = "approaching_commitment:node/node-dl"

        first = declines.record_decline(
            "user_dl",
            opportunity_key=key,
            trigger="approaching_commitment",
            source=DeclineSource.DECLINED_REPLY,
            persona_id="persona_a",
            now=_NOW,
        )
        assert first is not None
        assert declines.is_suppressed("user_dl", key) is True
        # A duplicate decline of a LIVE topic converges (idempotent, no second row).
        assert (
            declines.record_decline(
                "user_dl",
                opportunity_key=key,
                trigger="approaching_commitment",
                source=DeclineSource.IGNORED_EXPIRY,
                persona_id=None,
                now=_NOW,
            )
            is None
        )

        assert declines.revive("user_dl", key, now=_NOW + timedelta(days=30)) is True
        assert declines.is_suppressed("user_dl", key) is False
        assert declines.revive("user_dl", key, now=_NOW) is False  # nothing live to revive

        # A fresh decline after revival records again (partial unique, history kept).
        again = declines.record_decline(
            "user_dl",
            opportunity_key=key,
            trigger="approaching_commitment",
            source=DeclineSource.STOP_VERB,
            persona_id="persona_a",
            now=_NOW + timedelta(days=31),
        )
        assert again is not None
        assert _audit_actions(app_engine, "user_dl", "initiative.decline") == 2
        assert _audit_actions(app_engine, "user_dl", "initiative.revive") == 1


class TestDispositions:
    def test_expire_stale_writes_disposition_and_audit_row(
        self, migrated_engine: Engine, app_engine: Engine
    ) -> None:
        """T3 gate ruling 3: expiry is a WRITTEN disposition + audit row, never a drop."""
        ledger = InitiativeLedger(app_engine)
        _seed_user(migrated_engine, "user_st")
        candidate = _candidate("user_st", ref="node-stale")

        held = ledger.try_claim(
            candidate,
            voicer_persona_id="persona_a",
            disposition=NoticeDisposition.HELD,
            envelope_action=None,
            held_until=None,
            now=_NOW,
        )
        assert held is not None
        assert ledger.expire_stale("user_st", held.id, reason="why_now_lapsed", now=_NOW) is True
        assert ledger.held_for_owner("user_st") == []  # no longer held...
        assert _audit_actions(app_engine, "user_st", "initiative.suppressed_stale") == 1
        # ...and a second expiry attempt no-ops (already terminal).
        assert ledger.expire_stale("user_st", held.id, reason="why_now_lapsed", now=_NOW) is False

    def test_mark_delivered_and_trailing_window_counts(
        self, migrated_engine: Engine, app_engine: Engine
    ) -> None:
        ledger = InitiativeLedger(app_engine)
        _seed_user(migrated_engine, "user_cc")

        # persona_a delivers one now; persona_b delivers one 3 days ago (inside the
        # week window, outside the day window). User-day counts across personas.
        recent = ledger.try_claim(
            _candidate("user_cc", ref="node-cc-1"),
            voicer_persona_id="persona_a",
            disposition=NoticeDisposition.HELD,
            envelope_action=None,
            held_until=None,
            now=_NOW,
        )
        assert recent is not None
        assert ledger.mark_delivered("user_cc", recent.id, envelope_action="propose", now=_NOW)

        older = ledger.try_claim(
            _candidate("user_cc", ref="node-cc-2", trigger=InitiativeTrigger.TASK_FOLLOWUP),
            voicer_persona_id="persona_b",
            disposition=NoticeDisposition.HELD,
            envelope_action=None,
            held_until=None,
            now=_NOW - timedelta(days=3),
        )
        assert older is not None
        assert ledger.mark_delivered(
            "user_cc", older.id, envelope_action="propose", now=_NOW - timedelta(days=3)
        )

        counts_a = ledger.delivered_counts("user_cc", persona_id="persona_a", now=_NOW)
        assert counts_a.persona_day == 1
        assert counts_a.persona_week == 1
        assert counts_a.user_day == 1  # persona_b's delivery is outside the 24h window

        counts_b = ledger.delivered_counts("user_cc", persona_id="persona_b", now=_NOW)
        assert counts_b.persona_day == 0
        assert counts_b.persona_week == 1  # 3 days ago is inside the trailing week
        assert counts_b.user_day == 1

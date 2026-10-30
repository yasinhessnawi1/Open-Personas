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
    kind: CitationKind = CitationKind.NODE,
    trigger: InitiativeTrigger = InitiativeTrigger.APPROACHING_COMMITMENT,
) -> InitiativeCandidate:
    return InitiativeCandidate(
        observation="The hearing is Friday and no response letter exists.",
        citations=(GroundingCitation(kind=kind, ref=ref),),
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
        prompt_version="a5-scan-v9",
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


class TestTheAnchorBindsUnderAnyTrigger:
    """Ruling 1 (2026-10-30 MID set, B4): a decline, and the one-live-notice slot, bind
    the ANCHOR, not trigger plus anchor. B4 declined "stale_open_loop" on the insurance
    note and fire 3 raised "approaching_commitment" on the same note: a new key, so it
    came back. Existing rows keep their keys; the check reads the anchor from them."""

    _NODE = "own_b4::node::n1"

    def test_a_decline_refuses_the_same_note_under_another_trigger(
        self, migrated_engine: Engine, app_engine: Engine
    ) -> None:
        declines = DeclineStore(app_engine)
        _seed_user(migrated_engine, "user_b4")
        declined = f"stale_open_loop:node/{self._NODE}"
        assert declines.record_decline(
            "user_b4",
            opportunity_key=declined,
            trigger="stale_open_loop",
            source=DeclineSource.DECLINED_REPLY,
            persona_id="persona_a",
            now=_NOW,
        )
        other_trigger = f"approaching_commitment:node/{self._NODE}"
        other_note = f"approaching_commitment:node/{self._NODE}0"
        assert declines.suppressed_keys("user_b4", [declined, other_trigger, other_note]) == {
            declined,
            other_trigger,
        }
        assert declines.is_suppressed("user_b4", f"conflict:node/{self._NODE}")
        assert declines.revive("user_b4", declined, now=_NOW)
        assert declines.suppressed_keys("user_b4", [declined, other_trigger]) == set()

    def test_the_anchor_is_matched_exactly_never_as_a_pattern(
        self, migrated_engine: Engine, app_engine: Engine
    ) -> None:
        declines = DeclineStore(app_engine)
        _seed_user(migrated_engine, "user_like")
        assert declines.record_decline(
            "user_like",
            opportunity_key="conflict:node/a_b%c",
            trigger="conflict",
            source=DeclineSource.DECLINED_REPLY,
            persona_id=None,
            now=_NOW,
        )
        lookalikes = ["conflict:node/aXbYc", "conflict:node/a_bZZc", "x:node/a_b%cd"]
        assert declines.suppressed_keys("user_like", lookalikes) == set()
        assert declines.suppressed_keys("user_like", ["task_followup:node/a_b%c"]) == {
            "task_followup:node/a_b%c"
        }

    def test_the_slot_refuses_the_same_note_under_another_trigger(
        self, migrated_engine: Engine, app_engine: Engine
    ) -> None:
        ledger = InitiativeLedger(app_engine)
        _seed_user(migrated_engine, "user_slot")
        first = ledger.try_claim(
            _candidate("user_slot", ref=self._NODE, trigger=InitiativeTrigger.STALE_OPEN_LOOP),
            voicer_persona_id="persona_a",
            disposition=NoticeDisposition.HELD,
            envelope_action=None,
            held_until=None,
            now=_NOW,
        )
        assert first is not None
        before = _audit_actions(migrated_engine, "user_slot", "initiative.duplicate_suppressed")
        second = ledger.try_claim(
            _candidate(
                "user_slot", ref=self._NODE, trigger=InitiativeTrigger.APPROACHING_COMMITMENT
            ),
            voicer_persona_id="persona_b",
            disposition=NoticeDisposition.HELD,
            envelope_action=None,
            held_until=None,
            now=_NOW,
        )
        assert second is None
        after = _audit_actions(migrated_engine, "user_slot", "initiative.duplicate_suppressed")
        assert after == before + 1
        elsewhere = ledger.try_claim(
            _candidate("user_slot", ref=f"{self._NODE}0", trigger=InitiativeTrigger.CONFLICT),
            voicer_persona_id="persona_a",
            disposition=NoticeDisposition.HELD,
            envelope_action=None,
            held_until=None,
            now=_NOW,
        )
        assert elsewhere is not None
        assert ledger.supersede("user_slot", first.opportunity_key, now=_NOW)
        reopened = ledger.try_claim(
            _candidate(
                "user_slot", ref=self._NODE, trigger=InitiativeTrigger.APPROACHING_COMMITMENT
            ),
            voicer_persona_id="persona_b",
            disposition=NoticeDisposition.HELD,
            envelope_action=None,
            held_until=None,
            now=_NOW,
        )
        assert reopened is not None

    def test_the_slot_matches_the_anchor_exactly_never_as_a_pattern(
        self, migrated_engine: Engine, app_engine: Engine
    ) -> None:
        """A live notice on "aXbYc" does not hold the slot for "a_b%c": the anchor is
        escaped, never read as a LIKE pattern."""
        ledger = InitiativeLedger(app_engine)
        _seed_user(migrated_engine, "user_slot_like")
        for ref, trigger in (
            ("aXbYc", InitiativeTrigger.CONFLICT),
            ("a_b%c", InitiativeTrigger.STALE_OPEN_LOOP),
        ):
            claim = ledger.try_claim(
                _candidate("user_slot_like", ref=ref, trigger=trigger),
                voicer_persona_id="persona_a",
                disposition=NoticeDisposition.HELD,
                envelope_action=None,
                held_until=None,
                now=_NOW,
            )
            assert claim is not None, ref


class TestWhatTheAnchorBinds:
    """Review of fix/initiative-mid-tier-2. MEDIUM-1: only a notice the user saw or may
    still see (held, delivered) holds a node's anchor under another trigger; an expired
    one never did. MEDIUM-2: only a NODE anchor binds across triggers; a conversation or
    task anchor keeps the exact-key behaviour. LOW-1: the slot rechecks the anchor."""

    @staticmethod
    def _claim(
        ledger: InitiativeLedger,
        owner: str,
        ref: str,
        trigger: InitiativeTrigger,
        kind: CitationKind = CitationKind.NODE,
    ) -> object:
        return ledger.try_claim(
            _candidate(owner, ref=ref, trigger=trigger, kind=kind),
            voicer_persona_id="persona_a",
            disposition=NoticeDisposition.HELD,
            envelope_action=None,
            held_until=None,
            now=_NOW,
        )

    def test_an_expired_notice_never_holds_the_anchor(
        self, migrated_engine: Engine, app_engine: Engine
    ) -> None:
        ledger = InitiativeLedger(app_engine)
        _seed_user(migrated_engine, "user_exp")
        old = self._claim(ledger, "user_exp", "n-exp", InitiativeTrigger.STALE_OPEN_LOOP)
        assert old is not None
        assert ledger.expire_stale("user_exp", old.id, reason="prompt_superseded", now=_NOW)
        fresh = self._claim(ledger, "user_exp", "n-exp", InitiativeTrigger.APPROACHING_COMMITMENT)
        assert fresh is not None

    def test_a_held_and_a_delivered_notice_hold_the_anchor(
        self, migrated_engine: Engine, app_engine: Engine
    ) -> None:
        ledger = InitiativeLedger(app_engine)
        _seed_user(migrated_engine, "user_hold")
        held = self._claim(ledger, "user_hold", "n-held", InitiativeTrigger.STALE_OPEN_LOOP)
        assert held is not None
        assert self._claim(ledger, "user_hold", "n-held", InitiativeTrigger.CONFLICT) is None
        sent = self._claim(ledger, "user_hold", "n-sent", InitiativeTrigger.STALE_OPEN_LOOP)
        assert sent is not None
        assert ledger.mark_delivered("user_hold", sent.id, envelope_action="propose", now=_NOW)
        assert self._claim(ledger, "user_hold", "n-sent", InitiativeTrigger.CONFLICT) is None

    @pytest.mark.parametrize("kind", [CitationKind.CONVERSATION, CitationKind.TASK])
    def test_a_conversation_or_task_anchor_keeps_the_exact_key(
        self, migrated_engine: Engine, app_engine: Engine, kind: CitationKind
    ) -> None:
        owner = f"user_exact_{kind.value}"
        _seed_user(migrated_engine, owner)
        ledger = InitiativeLedger(app_engine)
        declines = DeclineStore(app_engine)
        ref = f"{kind.value}-1"
        first = self._claim(ledger, owner, ref, InitiativeTrigger.STALE_OPEN_LOOP, kind)
        assert first is not None
        assert self._claim(ledger, owner, ref, InitiativeTrigger.CONFLICT, kind) is not None
        assert declines.record_decline(
            owner,
            opportunity_key=first.opportunity_key,
            trigger="stale_open_loop",
            source=DeclineSource.DECLINED_REPLY,
            persona_id=None,
            now=_NOW,
        )
        other = f"conflict:{kind.value}/{ref}"
        assert declines.suppressed_keys(owner, [first.opportunity_key, other]) == {
            first.opportunity_key
        }

    def test_a_node_anchor_binds_across_triggers_for_both_checks(
        self, migrated_engine: Engine, app_engine: Engine
    ) -> None:
        owner = "user_node_binds"
        _seed_user(migrated_engine, owner)
        declines = DeclineStore(app_engine)
        assert declines.record_decline(
            owner,
            opportunity_key="stale_open_loop:node/n-binds",
            trigger="stale_open_loop",
            source=DeclineSource.DECLINED_REPLY,
            persona_id=None,
            now=_NOW,
        )
        assert declines.suppressed_keys(owner, ["conflict:node/n-binds"]) == {
            "conflict:node/n-binds"
        }

    def test_the_slot_rechecks_the_anchor_it_found(
        self, migrated_engine: Engine, app_engine: Engine
    ) -> None:
        """A conversation ref ending in ":node/n-tail" ends its key with the node key's
        anchor, but its anchor is the conversation's: it holds no node slot."""
        ledger = InitiativeLedger(app_engine)
        _seed_user(migrated_engine, "user_tail")
        tail = self._claim(
            ledger,
            "user_tail",
            "c-x:node/n-tail",
            InitiativeTrigger.STALE_OPEN_LOOP,
            CitationKind.CONVERSATION,
        )
        assert tail is not None
        assert self._claim(ledger, "user_tail", "n-tail", InitiativeTrigger.CONFLICT) is not None

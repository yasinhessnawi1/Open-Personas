"""The autonomy controls — the owner-wide pause + the per-persona suspend (Spec A6, B4).

Drives the route handlers directly (they self-scope via the RLS engine, so no middleware GUC is
needed). Proves the switch bar for both: the pause/resume + suspend/resume write their
presence-based row and audit (``autonomy.owner_pause`` / ``owner_resume`` /
``autonomy.persona_suspend`` / ``persona_resume``); the operations are idempotent (a double-press
is a calm no-op that neither errors nor re-audits); the state read is RLS-scoped (a cross-owner
reach reads its own absence, never the neighbour's row); a foreign/absent persona 404s before any
suspension write (no stray row).
"""

# ruff: noqa: ARG001 — ``migrated_engine`` is a dependency-ordering fixture param.
from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
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
from persona_api.auth import AuthenticatedUser
from persona_api.initiative import InitiativeLedger, NoticeDisposition
from persona_api.routes.autonomy import (
    decline_initiative,
    get_autonomy_state,
    get_initiative_dial,
    get_persona_suspension,
    pause_autonomy,
    resume_autonomy,
    resume_persona,
    set_persona_initiative_dial,
    suspend_persona,
)
from persona_api.schemas.requests import InitiativeDialRequest
from sqlalchemy import text
from sqlalchemy.engine import Engine

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_USER = AuthenticatedUser(id="u", email=None)
_OTHER = AuthenticatedUser(id="v", email=None)
_NOW = datetime(2026, 7, 7, 9, 0, tzinfo=UTC)


def _candidate(owner: str, *, persona: str, ref: str = "node-1") -> InitiativeCandidate:
    return InitiativeCandidate(
        observation="The hearing is Friday and no response letter exists.",
        citations=(GroundingCitation(kind=CitationKind.NODE, ref=ref),),
        trigger=InitiativeTrigger.APPROACHING_COMMITMENT,
        why_now="The date entered the horizon.",
        plan=(
            PlannedStep(
                description="draft the letter", categories=frozenset({ActionCategory.DRAFT})
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


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:
    from sqlalchemy import create_engine

    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (non-superuser role) not set; skipping autonomy controls")
    engine = create_engine(app_url.replace("+asyncpg", "+psycopg"))
    yield engine
    engine.dispose()


def _request(engine: Engine) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(rls_engine=engine)))


def _seed(su: Engine) -> None:
    with su.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES ('u','u@x'),('v','v@x')"))


def _seed_personas(su: Engine) -> None:
    with su.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO personas (id, owner_id, yaml) "
                "VALUES ('kai','u','name: kai'),('vpersona','v','name: v')"
            )
        )


def _audit_actions(su: Engine, user_id: str, action: str) -> int:
    with su.begin() as conn:
        return conn.execute(
            text("SELECT count(*) FROM audit_log WHERE user_id = :u AND action = :a"),
            {"u": user_id, "a": action},
        ).scalar_one()


async def test_pause_is_idempotent_and_audited(migrated_engine: Engine, app_engine: Engine) -> None:
    _seed(migrated_engine)
    req = _request(app_engine)

    first = await pause_autonomy(req, _USER)
    assert first.paused is True
    assert first.changed is True
    assert _audit_actions(migrated_engine, "u", "autonomy.owner_pause") == 1

    # a second press (another tab, a double-click) → a calm no-op: no error, no second audit row.
    second = await pause_autonomy(req, _USER)
    assert second.paused is True
    assert second.changed is False
    assert _audit_actions(migrated_engine, "u", "autonomy.owner_pause") == 1  # not re-audited

    state = await get_autonomy_state(req, _USER)
    assert state.paused is True


async def test_resume_is_idempotent_and_audited(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed(migrated_engine)
    req = _request(app_engine)

    # resuming when not paused → a calm no-op, never a resume audit row.
    noop = await resume_autonomy(req, _USER)
    assert noop.paused is False
    assert noop.changed is False
    assert _audit_actions(migrated_engine, "u", "autonomy.owner_resume") == 0

    await pause_autonomy(req, _USER)
    resumed = await resume_autonomy(req, _USER)
    assert resumed.paused is False
    assert resumed.changed is True
    assert _audit_actions(migrated_engine, "u", "autonomy.owner_resume") == 1
    assert (await get_autonomy_state(req, _USER)).paused is False


async def test_state_read_is_rls_scoped(migrated_engine: Engine, app_engine: Engine) -> None:
    _seed(migrated_engine)
    req = _request(app_engine)

    await pause_autonomy(req, _USER)  # only u is paused

    assert (await get_autonomy_state(req, _USER)).paused is True
    # v reads their own absence — never u's pause (the row is RLS-scoped to its owner).
    assert (await get_autonomy_state(req, _OTHER)).paused is False


# --- second switch: per-persona suspend ------------------------------------------------------


async def test_persona_suspend_is_idempotent_and_audited(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed(migrated_engine)
    _seed_personas(migrated_engine)
    req = _request(app_engine)

    first = await suspend_persona("kai", req, _USER)
    assert first.suspended is True
    assert first.changed is True
    assert _audit_actions(migrated_engine, "u", "autonomy.persona_suspend") == 1

    # a second press → a calm no-op: no error, no second audit row.
    second = await suspend_persona("kai", req, _USER)
    assert second.suspended is True
    assert second.changed is False
    assert _audit_actions(migrated_engine, "u", "autonomy.persona_suspend") == 1  # not re-audited

    assert (await get_persona_suspension("kai", req, _USER)).suspended is True

    # resume is the mirror: idempotent, audited once.
    resumed = await resume_persona("kai", req, _USER)
    assert resumed.suspended is False
    assert resumed.changed is True
    assert _audit_actions(migrated_engine, "u", "autonomy.persona_resume") == 1
    noop = await resume_persona("kai", req, _USER)
    assert noop.changed is False
    assert _audit_actions(migrated_engine, "u", "autonomy.persona_resume") == 1


async def test_suspend_foreign_or_absent_persona_404s_without_writing(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed(migrated_engine)
    _seed_personas(migrated_engine)
    req = _request(app_engine)

    # a persona the caller doesn't own → 404, and NO suspension row (guarded before the write).
    with pytest.raises(HTTPException) as exc:
        await suspend_persona("vpersona", req, _USER)
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as missing:
        await suspend_persona("ghost", req, _USER)
    assert missing.value.status_code == 404
    with migrated_engine.begin() as conn:
        rows = conn.execute(
            text("SELECT count(*) FROM suspended_personas WHERE owner_id = 'u'")
        ).scalar_one()
    assert rows == 0


async def test_persona_suspension_read_is_rls_scoped(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed(migrated_engine)
    _seed_personas(migrated_engine)
    req = _request(app_engine)

    await suspend_persona("kai", req, _USER)  # only u's kai is suspended

    assert (await get_persona_suspension("kai", req, _USER)).suspended is True
    # v cannot even read kai's state (not their persona) — a 404, never a cross-tenant peek.
    with pytest.raises(HTTPException) as exc:
        await get_persona_suspension("kai", req, _OTHER)
    assert exc.value.status_code == 404


# --- third switch: the initiative dial + declines --------------------------------------------


async def test_initiative_dial_is_idempotent_audited_and_honest(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed(migrated_engine)
    _seed_personas(migrated_engine)
    req = _request(app_engine)

    # a fresh persona reads the ratified default (server_default 'propose_only').
    assert (await get_initiative_dial("kai", req, _USER)).dial == "propose_only"

    up = await set_persona_initiative_dial(
        "kai", InitiativeDialRequest(dial="act_within_envelope"), req, _USER
    )
    assert up.changed is True
    assert up.dial == "act_within_envelope"
    assert _audit_actions(migrated_engine, "u", "initiative.dial_set") == 1
    # honest UX: with initiative off platform-wide, the note says it won't act yet.
    if not up.initiative_enabled:
        assert up.note

    # the write is durable — the GET reflects the new level.
    assert (await get_initiative_dial("kai", req, _USER)).dial == "act_within_envelope"

    # re-setting the same level → a calm no-op: no error, no second audit row.
    again = await set_persona_initiative_dial(
        "kai", InitiativeDialRequest(dial="act_within_envelope"), req, _USER
    )
    assert again.changed is False
    assert _audit_actions(migrated_engine, "u", "initiative.dial_set") == 1


async def test_initiative_dial_foreign_persona_404s(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed(migrated_engine)
    _seed_personas(migrated_engine)
    req = _request(app_engine)
    with pytest.raises(HTTPException) as exc:
        await set_persona_initiative_dial("vpersona", InitiativeDialRequest(dial="off"), req, _USER)
    assert exc.value.status_code == 404


async def test_decline_is_ledger_anchored_idempotent_and_rls_scoped(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed(migrated_engine)
    _seed_personas(migrated_engine)
    ledger = InitiativeLedger(app_engine)
    notice = ledger.try_claim(
        _candidate("u", persona="kai"),
        voicer_persona_id="kai",
        disposition=NoticeDisposition.DELIVERED,
        envelope_action="propose",
        held_until=None,
        now=_NOW,
    )
    assert notice is not None
    req = _request(app_engine)

    first = await decline_initiative(notice.id, req, _USER)
    assert first.declined is True
    assert first.changed is True
    assert first.opportunity_key == notice.opportunity_key  # anchored on the durable ledger record
    assert _audit_actions(migrated_engine, "u", "initiative.decline") == 1

    # a second decline of the same topic → a calm no-op (already suppressed): no second decline row.
    second = await decline_initiative(notice.id, req, _USER)
    assert second.changed is False
    assert _audit_actions(migrated_engine, "u", "initiative.decline") == 1

    # v cannot decline u's notice — the ledger anchor read is RLS-scoped → 404 (no cross-tenant).
    with pytest.raises(HTTPException) as exc:
        await decline_initiative(notice.id, req, _OTHER)
    assert exc.value.status_code == 404

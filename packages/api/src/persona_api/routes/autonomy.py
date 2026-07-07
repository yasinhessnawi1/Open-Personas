"""The autonomy area endpoints (Spec A6) — the morning review (B5) + the controls (B4).

``GET /v1/autonomy/review`` serves the one shared morning digest, RLS-scoped. Opening the review
CONSUMES the deferred chatter atomically (build-from-RETURNING, A6-D-10) so it is delivered exactly
once across the review and C0's morning message; the digest's main sections never depend on it.

The controls are the owner-wide autonomy pause (B4, first switch): ``POST /pause`` and ``POST
/resume`` write/delete the presence-based ``owner_autonomy_pause`` row (the SUSPEND-ALL the origin
gates consult via :meth:`KillSwitchStore.is_owner_autonomy_paused`), and ``GET /state`` reads it.
Every command audits (``autonomy.owner_pause`` / ``owner_resume``); the state read is RLS-scoped;
the operations are idempotent — a double-press reflects the durable truth as a calm no-op.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request
from persona.initiative import InitiativeDial, InitiativeSettings
from sqlalchemy import select

from persona_api.approvals import KillSwitchStore
from persona_api.auth import AuthenticatedUser, get_current_user
from persona_api.db.engine import rls_connection
from persona_api.db.models import personas as personas_t
from persona_api.digest import DeferredDigestStore, MorningDigest, build_morning_digest
from persona_api.initiative.handler import read_initiative_dial, set_initiative_dial
from persona_api.initiative.store import DeclineSource, DeclineStore, InitiativeLedger
from persona_api.jobs.queue import JobQueue
from persona_api.schedules.store import ScheduleStore
from persona_api.schemas.requests import InitiativeDialRequest
from persona_api.schemas.responses import (
    AutonomyStateOut,
    InitiativeDeclineOut,
    InitiativeDialOut,
    PersonaSuspensionOut,
)
from persona_api.tasks.continuation import TaskContinuation
from persona_api.tasks.store import CheckpointStore, TaskStore

if TYPE_CHECKING:
    from sqlalchemy import Engine

router = APIRouter(prefix="/v1/autonomy", tags=["autonomy"])

#: The audited actor recorded on the owner-pause row + the audit_log for a UI-driven switch.
_ACTOR_USER_VIA_UI = "user_via_ui"


def _kill_switch(engine: Engine) -> KillSwitchStore:
    return KillSwitchStore(
        engine,
        continuation=TaskContinuation(
            task_store=TaskStore(engine),
            queue=JobQueue(engine),
            checkpoint_store=CheckpointStore(engine),
            schedule_store=ScheduleStore(engine),
        ),
    )


def _require_owned_persona(engine: Engine, owner_id: str, persona_id: str) -> None:
    """404 unless the persona is the caller's — an RLS-scoped ownership read (no cross-tenant peek).

    Guards BEFORE any suspension write: a foreign/absent persona never yields a stray
    ``suspended_personas`` row (which would be meaningless noise under the caller's owner_id).
    """
    with rls_connection(engine, owner_id) as conn:
        owned = conn.execute(
            select(personas_t.c.id).where(
                personas_t.c.id == persona_id,
                personas_t.c.owner_id == owner_id,
            )
        ).first()
    if owned is None:
        raise HTTPException(status_code=404, detail="persona not found")


@router.get("/review", response_model=MorningDigest)
async def get_review(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> MorningDigest:
    """The caller's morning review — waiting → stuck → done → initiatives + upcoming (A6-D-2)."""
    engine = request.app.state.rls_engine
    now = datetime.now(UTC)
    deferred = DeferredDigestStore(engine).consume_undelivered(user.id, now=now)
    return build_morning_digest(
        engine,
        owner_id=user.id,
        config=request.app.state.config,
        now=now,
        deferred=deferred,
    )


# --- B4: autonomy controls — the owner-wide pause (first switch) ------------------------------


@router.get("/state", response_model=AutonomyStateOut)
async def get_autonomy_state(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> AutonomyStateOut:
    """Is the caller's autonomy paused? The durable presence read, RLS-scoped (B4)."""
    engine = request.app.state.rls_engine
    return AutonomyStateOut(paused=_kill_switch(engine).is_owner_autonomy_paused(user.id))


@router.post("/pause", response_model=AutonomyStateOut)
async def pause_autonomy(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> AutonomyStateOut:
    """Pause ALL of the caller's autonomy (SUSPEND-ALL). Idempotent — re-pausing is a calm no-op."""
    engine = request.app.state.rls_engine
    switch = _kill_switch(engine)
    if switch.is_owner_autonomy_paused(user.id):  # already paused → reflect, don't re-audit
        return AutonomyStateOut(paused=True, changed=False, note="Autonomy is already paused.")
    # audits autonomy.owner_pause
    switch.pause_owner(user.id, actor=_ACTOR_USER_VIA_UI, now=datetime.now(UTC))
    return AutonomyStateOut(
        paused=True,
        changed=True,
        note="Autonomy paused — no persona will start new work until you resume.",
    )


@router.post("/resume", response_model=AutonomyStateOut)
async def resume_autonomy(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> AutonomyStateOut:
    """Resume the caller's autonomy. Idempotent — resuming when not paused is a calm no-op."""
    engine = request.app.state.rls_engine
    switch = _kill_switch(engine)
    if not switch.is_owner_autonomy_paused(user.id):  # not paused → reflect, don't re-audit
        return AutonomyStateOut(paused=False, changed=False, note="Autonomy isn't paused.")
    switch.resume_owner(user.id, now=datetime.now(UTC))  # audits owner_resume
    return AutonomyStateOut(paused=False, changed=True, note="Autonomy resumed.")


# --- B4: autonomy controls — per-persona suspend (second switch) ------------------------------


@router.get("/personas/{persona_id}/state", response_model=PersonaSuspensionOut)
async def get_persona_suspension(
    persona_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> PersonaSuspensionOut:
    """Is this persona's autonomy suspended? The durable presence read, RLS-scoped (B4)."""
    engine = request.app.state.rls_engine
    _require_owned_persona(engine, user.id, persona_id)
    suspended = _kill_switch(engine).is_persona_suspended(user.id, persona_id)
    return PersonaSuspensionOut(persona_id=persona_id, suspended=suspended)


@router.post("/personas/{persona_id}/suspend", response_model=PersonaSuspensionOut)
async def suspend_persona(
    persona_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> PersonaSuspensionOut:
    """Suspend one persona's autonomy — no new legs for its tasks. Idempotent calm no-op."""
    engine = request.app.state.rls_engine
    _require_owned_persona(engine, user.id, persona_id)
    switch = _kill_switch(engine)
    if switch.is_persona_suspended(
        user.id, persona_id
    ):  # already suspended → reflect, don't re-audit
        return PersonaSuspensionOut(
            persona_id=persona_id, suspended=True, changed=False, note="Already suspended."
        )
    # audits autonomy.persona_suspend
    switch.suspend_persona(user.id, persona_id, now=datetime.now(UTC))
    return PersonaSuspensionOut(
        persona_id=persona_id,
        suspended=True,
        changed=True,
        note="Suspended — this persona starts no new work until you resume it.",
    )


@router.post("/personas/{persona_id}/resume", response_model=PersonaSuspensionOut)
async def resume_persona(
    persona_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> PersonaSuspensionOut:
    """Resume one persona's autonomy — delete its suspension row. Idempotent calm no-op."""
    engine = request.app.state.rls_engine
    _require_owned_persona(engine, user.id, persona_id)
    switch = _kill_switch(engine)
    if not switch.is_persona_suspended(
        user.id, persona_id
    ):  # not suspended → reflect, don't re-audit
        return PersonaSuspensionOut(
            persona_id=persona_id, suspended=False, changed=False, note="Not suspended."
        )
    # audits autonomy.persona_resume
    switch.resume_persona(user.id, persona_id, now=datetime.now(UTC))
    return PersonaSuspensionOut(
        persona_id=persona_id, suspended=False, changed=True, note="Resumed."
    )


# --- B4: autonomy controls — the initiative dial + declines (third switch) --------------------


def _initiative_enabled() -> bool:
    """The platform ``PERSONA_INITIATIVE_ENABLED`` flag (honest UX, not a second gate)."""
    return bool(InitiativeSettings().enabled)


@router.get("/personas/{persona_id}/initiative", response_model=InitiativeDialOut)
async def get_initiative_dial(
    persona_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> InitiativeDialOut:
    """This persona's durable initiative restraint level + the honest platform flag (B4)."""
    engine = request.app.state.rls_engine
    _require_owned_persona(engine, user.id, persona_id)
    dial = read_initiative_dial(engine, user.id, persona_id)
    return InitiativeDialOut(
        persona_id=persona_id, dial=dial.value, initiative_enabled=_initiative_enabled()
    )


@router.put("/personas/{persona_id}/initiative", response_model=InitiativeDialOut)
async def set_persona_initiative_dial(
    persona_id: str,
    body: InitiativeDialRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> InitiativeDialOut:
    """Set this persona's restraint level (the dial). Idempotent — re-setting the same is a no-op.

    The level persists on the persona row regardless of the platform flag; when initiative is off,
    the note is honest that it won't act until enabled (a true durable write, not a lie).
    """
    engine = request.app.state.rls_engine
    _require_owned_persona(engine, user.id, persona_id)
    requested = InitiativeDial(body.dial)
    current = read_initiative_dial(engine, user.id, persona_id)
    enabled = _initiative_enabled()
    if current is requested:  # already at this level → reflect, don't re-write/re-audit
        return InitiativeDialOut(
            persona_id=persona_id,
            dial=current.value,
            changed=False,
            initiative_enabled=enabled,
            note="Already set to this level.",
        )
    # the single durable dial-write path (shared with the T10 dial verb); audits initiative.dial_set
    set_initiative_dial(engine, user.id, persona_id, requested, now=datetime.now(UTC))
    note = ""
    if not enabled and requested is not InitiativeDial.OFF:
        note = "Saved — initiative is off platform-wide, so this persona won't act until enabled."
    return InitiativeDialOut(
        persona_id=persona_id,
        dial=requested.value,
        changed=True,
        initiative_enabled=enabled,
        note=note,
    )


@router.post("/initiatives/{notice_id}/decline", response_model=InitiativeDeclineOut)
async def decline_initiative(
    notice_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> InitiativeDeclineOut:
    """Decline a surfaced initiative — user-level, LEDGER-anchored, suppresses it for all personas.

    Anchored on the durable A5 ledger notice (never conversation metadata — the same anchor-on-
    durable discipline as the resolver). Idempotent: a topic already live-declined is a calm no-op.
    """
    engine = request.app.state.rls_engine
    ledger = InitiativeLedger(engine)
    notice = ledger.get_notice(user.id, notice_id)  # RLS-scoped durable anchor; None → 404
    if notice is None:
        raise HTTPException(status_code=404, detail="initiative not found")
    now = datetime.now(UTC)
    # ride A5's decline store (the same calls the T10 decline verb makes); audits initiative.decline
    record = DeclineStore(engine).record_decline(
        user.id,
        opportunity_key=notice.opportunity_key,
        trigger=notice.trigger,
        source=DeclineSource.DECLINED_REPLY,
        persona_id=notice.persona_id,
        now=now,
    )
    ledger.supersede(user.id, notice.opportunity_key, now=now)  # free the slot (idempotent)
    changed = record is not None  # None = already live-declined (nothing added)
    return InitiativeDeclineOut(
        notice_id=notice_id,
        opportunity_key=notice.opportunity_key,
        declined=True,
        changed=changed,
        note="" if changed else "You'd already declined this — it stays suppressed.",
    )


__all__ = ["router"]

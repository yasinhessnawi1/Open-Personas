"""Auto-dispatch consent — the tri-state gate (spec 21 T09, D-21-7/13/17).

Consent for *autonomous* task dispatch is a tri-state on the ``personas`` row
(D-21-7): ``None`` = never asked / revoked-to-ask, ``True`` = granted,
``False`` = explicitly declined. The pure helpers below encode the state machine
the orchestrator (T10) consults; the DB helpers read/write the columns. Every
transition emits an ``AuditEvent`` at the route (where the user id lives).

Key invariants:
- **Re-read on every dispatch** (D-21-7): never cache consent in runtime/session
  state — :func:`read_consent` is called per dispatch.
- **Declined is stable** (D-21-17): ``False`` never auto-re-prompts; only a
  ``user``-initiated settings toggle resets it to ``None``.
- **Explicit runs are exempt** (D-21-13): this gate covers auto-dispatch only;
  an explicit ``POST /runs`` never consults it and never writes the column.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.errors import PersonaNotFoundError
from sqlalchemy import select, update

from persona_api.db.models import persona_complete
from persona_api.db.models import personas as personas_t

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.engine import Engine

__all__ = [
    "ConsentState",
    "can_auto_dispatch",
    "read_consent",
    "set_consent",
    "should_prompt_for_consent",
]

#: ``None`` = never asked / revoked-to-ask · ``True`` = granted · ``False`` = declined.
ConsentState = bool | None


def can_auto_dispatch(state: ConsentState) -> bool:
    """Whether a task may auto-dispatch without prompting (only when granted)."""
    return state is True


def should_prompt_for_consent(state: ConsentState) -> bool:
    """Whether the next autonomous dispatch should ask for consent (D-21-17).

    Only ``None`` (never asked / revoked) prompts. ``True`` auto-dispatches;
    ``False`` is a stable decline that proceeds as normal chat without ever
    re-prompting.
    """
    return state is None


def read_consent(*, rls_engine: Engine, persona_id: str) -> ConsentState:
    """Read the persona's current consent state (RLS-scoped; re-read per dispatch)."""
    with rls_engine.begin() as conn:
        row = (
            conn.execute(
                select(personas_t.c.consent_to_auto_dispatch).where(
                    personas_t.c.id == persona_id, persona_complete
                )
            )
            .mappings()
            .first()
        )
    if row is None:
        raise PersonaNotFoundError("persona not found", context={"id": persona_id})
    value = row["consent_to_auto_dispatch"]
    return bool(value) if value is not None else None


def set_consent(
    *, rls_engine: Engine, persona_id: str, granted: ConsentState, now: datetime
) -> ConsentState:
    """Set the consent state (grant ``True`` / decline ``False`` / revoke ``None``).

    Only ``user``-sourced settings writes call this (D-21-8 corollary —
    ``persona_self`` may never touch consent). Stamps ``consent_updated_at``.
    Returns the new state. The caller emits the ``AuditEvent`` for the transition.

    Raises:
        PersonaNotFoundError: No persona row for ``persona_id`` (RLS-scoped).
    """
    with rls_engine.begin() as conn:
        result = conn.execute(
            update(personas_t)
            .where(personas_t.c.id == persona_id, persona_complete)
            .values(consent_to_auto_dispatch=granted, consent_updated_at=now)
            .returning(personas_t.c.id)
        )
        if result.first() is None:
            raise PersonaNotFoundError("persona not found", context={"id": persona_id})
    return granted

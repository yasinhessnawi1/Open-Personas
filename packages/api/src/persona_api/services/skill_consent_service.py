"""Per-persona speciality (skill) consent — the real store (Spec S3, S3-D-1/D-2).

S1 shipped the ``SkillConsentPort`` protocol + a default-DENY stub
(``DenyUnvettedConsent``); S3 implements the real store against ``content_hash``
(S1-D-6). This module holds:

- :class:`PostgresSkillConsentStore` — the ``SkillConsentPort`` implementation the
  runtime loop consults before injecting an above-vetted skill. ``is_enabled`` is
  the latest consent event for ``(persona_id, skill_name, content_hash)`` with
  ``granted = true``; **no matching row → False** — so an empty store behaves
  identically to ``DenyUnvettedConsent`` (S3-D-2: swapping the stub for the real
  port can NEVER open access; consent is the only thing that opens it).
- :func:`record_consent` — append one consent EVENT (grant/revoke). Append-only:
  the history survives; a revoke is a new ``granted = false`` row, never a delete.
- :func:`consent_state_for` — the per-persona presented state
  (``not_required`` / ``granted`` / ``stale`` / ``none``) the specialities surface
  renders. ``stale`` = the latest event is a grant, but for an OLD body hash — a
  synced change re-gates (S1-D-5), never shown as still-consented.

Consent binds to the **server-known current** ``content_hash`` (S3-D-2 integrity):
callers resolve the hash from the catalog server-side; the client never supplies
it (nor the trust tier — source-assigned, S1-D-3). All reads/writes run on the
RLS-scoped engine (``persona_id`` FK-chain → the persona's owner).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import insert, select

from persona_api.db.models import skill_consents as skill_consents_t

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.engine import Engine

__all__ = [
    "CONSENT_GRANTED",
    "CONSENT_NONE",
    "CONSENT_NOT_REQUIRED",
    "CONSENT_STALE",
    "PostgresSkillConsentStore",
    "consent_state_for",
    "record_consent",
]

#: The four presented consent states (S3-D-3 / D-4). ``not_required`` = builtin/vetted
#: (activate freely); ``granted`` = consented at the current hash; ``stale`` = consented
#: at an old body hash → re-gate (S1-D-5); ``none`` = never/revoked.
CONSENT_NOT_REQUIRED = "not_required"
CONSENT_GRANTED = "granted"
CONSENT_STALE = "stale"
CONSENT_NONE = "none"


class PostgresSkillConsentStore:
    """The real ``SkillConsentPort`` (S1-D-6): consent bound to ``content_hash``.

    Runs on the RLS-scoped engine, so it only ever sees the owner's consent rows
    (cross-tenant reads return nothing → denied). Structurally satisfies the core
    ``SkillConsentPort`` protocol.
    """

    def __init__(self, rls_engine: Engine) -> None:
        self._engine = rls_engine

    def is_enabled(self, persona_id: str, skill_name: str, content_hash: str) -> bool:
        """Whether ``persona_id`` has a live consent for ``skill_name`` at ``content_hash``.

        The latest event for the exact ``(persona, skill, hash)`` triple with
        ``granted = true``. **No row → False** (default-deny). A body change moves the
        runtime's ``content_hash`` to a value with no matching row → denied → re-gate.
        """
        stmt = (
            select(skill_consents_t.c.granted)
            .where(
                (skill_consents_t.c.persona_id == persona_id)
                & (skill_consents_t.c.skill_name == skill_name)
                & (skill_consents_t.c.content_hash == content_hash)
            )
            .order_by(skill_consents_t.c.created_at.desc(), skill_consents_t.c.id.desc())
            .limit(1)
        )
        with self._engine.begin() as conn:
            row = conn.execute(stmt).first()
        return bool(row[0]) if row is not None else False


def record_consent(
    *,
    rls_engine: Engine,
    persona_id: str,
    skill_name: str,
    content_hash: str,
    granted: bool,
    now: datetime,
    granted_by: str = "user",
) -> None:
    """Append one consent EVENT (grant/revoke) — append-only, never an update.

    ``content_hash`` is the **server-known current** hash (S3-D-2); the caller
    resolves it from the catalog, never from the client. ``created_at`` is stamped
    explicitly (not the ``now()`` transaction-start default) so consecutive events
    order deterministically. RLS ``WITH CHECK`` rejects a write to another owner's
    persona (fail-closed).
    """
    with rls_engine.begin() as conn:
        conn.execute(
            insert(skill_consents_t).values(
                id=f"skc_{uuid.uuid4().hex}",
                persona_id=persona_id,
                skill_name=skill_name,
                content_hash=content_hash,
                granted=granted,
                granted_by=granted_by,
                created_at=now,
            )
        )


def consent_state_for(
    *,
    rls_engine: Engine,
    persona_id: str,
    skill_name: str,
    current_hash: str | None,
    requires_consent: bool,
) -> str:
    """The presented consent state for a speciality on a persona (S3-D-3).

    ``not_required`` for builtin/vetted (activate freely, S1-D-4). Otherwise the
    latest event for ``(persona, skill)`` decides: a grant at the current hash →
    ``granted``; a grant at an OLD hash → ``stale`` (the body changed since consent,
    S1-D-5 re-gate); no event or a revoke → ``none`` (default-deny).
    """
    if not requires_consent:
        return CONSENT_NOT_REQUIRED
    stmt = (
        select(skill_consents_t.c.granted, skill_consents_t.c.content_hash)
        .where(
            (skill_consents_t.c.persona_id == persona_id)
            & (skill_consents_t.c.skill_name == skill_name)
        )
        .order_by(skill_consents_t.c.created_at.desc(), skill_consents_t.c.id.desc())
        .limit(1)
    )
    with rls_engine.begin() as conn:
        row = conn.execute(stmt).first()
    if row is None or not row[0]:
        return CONSENT_NONE
    return CONSENT_GRANTED if row[1] == current_hash else CONSENT_STALE

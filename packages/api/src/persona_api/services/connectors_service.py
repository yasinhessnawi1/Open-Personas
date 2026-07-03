"""Connector management reads + unlink — the C1-designated "C6 backend".

Spec C6 (C6-D-0). The web's thin authenticated front-door over C1's linking
spine (D-C1-5 named this "the C6 backend"). RLS scopes every query to the
caller's tenant exactly like ``notifications`` / ``credits`` — the ``rls_engine``
connection carries ``app.current_user_id`` (set per-request from the auth
contextvar), so a caller sees ONLY their own bindings (criterion 11,
non-vacuously — a foreign row is invisible, not a guessable 404).

No linking logic lives here: issuing tokens + per-platform carriers are C1 + the
connector service; this module only reads persona-api's own
``connector_identities`` rows.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select, update

from persona_api.db.models import connector_identities as connector_identities_t

if TYPE_CHECKING:
    from sqlalchemy import Engine

_ACTIVE = "active"
_REVOKED = "revoked"


def list_connections(*, rls_engine: Engine) -> list[dict[str, object]]:
    """List the caller's ACTIVE connector bindings (RLS-scoped), newest-first.

    Read-only (CQS). Only ``status='active'`` rows — a revoked (disconnected)
    binding is not a connection. RLS hides other tenants' rows, so this is
    owner-only without an explicit ``owner_id`` filter (the security spine); a
    ``linked_at`` desc / ``id`` desc order is deterministic.
    """
    with rls_engine.begin() as conn:
        rows = (
            conn.execute(
                select(connector_identities_t)
                .where(connector_identities_t.c.status == _ACTIVE)
                .order_by(
                    connector_identities_t.c.linked_at.desc(),
                    connector_identities_t.c.id.desc(),
                )
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


def revoke_connection(*, rls_engine: Engine, platform: str, platform_identity: str) -> int:
    """Sever the caller's active binding for ``(platform, platform_identity)``.

    Drives C1's unlink semantics (D-C1-5 / ``LinkingService.unlink``): flips
    ``active`` → ``revoked`` + stamps ``revoked_at``, **keeping the row for audit**
    (not a hard delete) and freeing the partial-active unique so a later re-link is
    allowed. Returns rows touched — ``1`` when the caller's active binding was
    severed, ``0`` for the idempotent no-op (already revoked / never existed / not
    the caller's — RLS makes a foreign row invisible, so the UPDATE matches nothing
    and never leaks that it exists).

    After this commits, C1's ``resolve_owner`` for that identity raises
    ``IdentityNotLinkedError`` — the platform can no longer reach the persona
    (criterion 9, the real sever proven at the backend state, not a UI chip).
    """
    with rls_engine.begin() as conn:
        result = conn.execute(
            update(connector_identities_t)
            .where(
                connector_identities_t.c.platform == platform,
                connector_identities_t.c.platform_identity == platform_identity,
                connector_identities_t.c.status == _ACTIVE,
            )
            .values(status=_REVOKED, revoked_at=datetime.now(UTC))
        )
    return result.rowcount

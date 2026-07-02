"""JIT user provisioning + profile read/write (spec-09 + Spec K6).

A third-party provider (Clerk) issues JWTs; spec 08 deferred webhook
user-mirroring, so a freshly-authenticated user has no ``users`` row — yet
personas/conversations/runs/credits all FK ``users.id``. The auth dependency
calls :func:`ensure_user` on each request to idempotently create the row, run on
the **superuser** engine (a *system* action, not the user acting under RLS). The
production path is a provider webhook; this JIT upsert is the v0.1 equivalent.

Spec K6 adds the user's optional **name** as first-class data on ``users``
(:func:`get_user_profile` / :func:`update_user_profile`) — captured through our
own app step, our DB the source of truth (Clerk auth-only). The name is optional
+ null-safe everywhere; :func:`normalize_name` enforces the K6-D-8 posture
(strip control chars, whitespace-only → unset, defensive length cap).
"""

from __future__ import annotations

import unicodedata
from typing import TYPE_CHECKING

from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy import Engine

__all__ = [
    "NAME_MAX_LENGTH",
    "UNSET",
    "compose_display_name",
    "ensure_user",
    "get_user_profile",
    "normalize_name",
    "update_user_profile",
]

#: Defensive upper bound on a stored name field (K6-D-8). The request schema
#: rejects longer input at the boundary (fail-fast 422); this caps any other
#: caller (and truncates defensively) so a pathological value never reaches the DB.
NAME_MAX_LENGTH = 100


class _Unset:
    """Sentinel distinguishing "field omitted" from "field set to null" in a PATCH."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "UNSET"


#: Singleton sentinel: a PATCH that omits a name leaves it unchanged; a PATCH that
#: sends ``null`` clears it. ``UNSET`` is the "omitted" marker (``None`` = "clear").
UNSET = _Unset()


def normalize_name(raw: str | None) -> str | None:
    """Normalise a captured name to its stored form (K6-D-8), or ``None`` if empty.

    Strips Unicode control/format characters (newlines, tabs, C0/C1, bidi/format
    marks) so a name can never break the prompt structure it is later rendered into
    (self-injection defence — the name is always rendered as a delimited data
    value, but stripping controls keeps it tidy). Outer whitespace is trimmed; a
    whitespace-only or empty result is treated as **unset** (``None``) so the name
    is never a hard requirement. Ordinary spaces and Unicode letters/emoji/RTL are
    preserved. Result is capped at :data:`NAME_MAX_LENGTH` characters.
    """
    if raw is None:
        return None
    cleaned = "".join(ch for ch in raw if not unicodedata.category(ch).startswith("C"))
    cleaned = cleaned.strip()
    return cleaned[:NAME_MAX_LENGTH] or None


def compose_display_name(first_name: str | None, last_name: str | None) -> str | None:
    """Join the stored name parts into a display name, or ``None`` if both are unset.

    The name the persona speaks (Spec K6, K6-D-6) and the ``SELF`` node's label — a
    nameless account (both parts ``None``/blank) composes to ``None`` so the prompt
    omits the name line and no self node is materialised (null-safe throughout).
    """
    parts = [p for p in (first_name, last_name) if p]
    return " ".join(parts) or None


def ensure_user(
    engine: Engine,
    *,
    user_id: str,
    email: str | None,
    first_name: str | None = None,
    last_name: str | None = None,
) -> None:
    """Idempotently provision the ``users`` row, seeding the name once when null (K6-D-1).

    Uses a superuser engine (bypasses RLS — provisioning is a system action).
    ``email`` is NOT NULL + unique in the schema; falls back to a noreply address
    when the token carries none. Parameterised (no interpolation).

    ``first_name``/``last_name`` are the OPTIONAL identity-provider name claims
    (Clerk ``given_name``/``family_name``, present only when the session token is
    configured to emit them — K6-D-1 seed). They are normalised (:func:`normalize_name`
    — same control-char/length/whitespace hygiene as a PATCH, K6-D-8) and seeded
    **only when our column is still null** (``COALESCE(existing, seed)``): a user who
    typed their name at signup is not re-asked by the web step, yet **our DB stays
    authoritative** — a name already set here is never overwritten by a claim, and
    absent claims (``None``) are a graceful no-op. The ``WHERE`` skips the write
    entirely once both names are set (the steady-state per-request path).
    """
    resolved_email = email or f"{user_id}@users.noreply"
    seed_first = normalize_name(first_name)
    seed_last = normalize_name(last_name)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, email, first_name, last_name) "
                "VALUES (:id, :email, :first_name, :last_name) "
                "ON CONFLICT (id) DO UPDATE SET "
                "first_name = COALESCE(users.first_name, EXCLUDED.first_name), "
                "last_name = COALESCE(users.last_name, EXCLUDED.last_name) "
                "WHERE users.first_name IS NULL OR users.last_name IS NULL"
            ),
            {
                "id": user_id,
                "email": resolved_email,
                "first_name": seed_first,
                "last_name": seed_last,
            },
        )


def get_user_profile(engine: Engine, *, user_id: str) -> dict[str, object] | None:
    """Read the caller's profile row (``id, email, first_name, last_name, created_at``).

    Scoped to ``user_id`` (``users`` is not RLS-scoped — it is globally readable by
    id; the tenant RLS lives on the child tables). Returns ``None`` if no row exists
    (should not happen post-:func:`ensure_user`, but callers handle it defensively).
    """
    stmt = text("SELECT id, email, first_name, last_name, created_at FROM users WHERE id = :id")
    with engine.connect() as conn:
        row = conn.execute(stmt, {"id": user_id}).mappings().first()
    return dict(row) if row is not None else None


def update_user_profile(
    engine: Engine,
    *,
    user_id: str,
    first_name: str | None | _Unset = UNSET,
    last_name: str | None | _Unset = UNSET,
) -> dict[str, object] | None:
    """Set the caller's name fields and return the updated profile row.

    PATCH semantics: only fields that are *provided* are written — pass a value
    (``str`` to set, ``None`` to clear) to change a field, or leave it :data:`UNSET`
    to leave it unchanged. Provided values pass through :func:`normalize_name`
    (K6-D-8). With nothing provided this is a read (equivalent to
    :func:`get_user_profile`). Dialect-safe (``UPDATE`` then re-``SELECT``; no
    ``RETURNING`` so the community SQLite edition behaves identically). Scoped to
    ``WHERE id = :id`` — a caller can only ever touch their own row.
    """
    assignments: dict[str, str | None] = {}
    if not isinstance(first_name, _Unset):
        assignments["first_name"] = normalize_name(first_name)
    if not isinstance(last_name, _Unset):
        assignments["last_name"] = normalize_name(last_name)
    if assignments:
        # Column names are a fixed internal allowlist ({first_name, last_name}),
        # never user input — the f-string carries identifiers only; every value is
        # bound. (No SQL injection surface.)
        set_clause = ", ".join(f"{col} = :{col}" for col in assignments)
        stmt = text(f"UPDATE users SET {set_clause} WHERE id = :id")  # noqa: S608 - fixed identifiers
        with engine.begin() as conn:
            conn.execute(stmt, {**assignments, "id": user_id})
    return get_user_profile(engine, user_id=user_id)

"""Spec R8 — per-user MCP OAuth: token-lifecycle columns + ``mcp_oauth_states`` (R8-D-4/6).

R8 **extends** N4/Spec-30 (migration 009): storage + injection stay N4's; R8 adds the
OAuth *obtaining* + *refresh*. Two additive changes, both fail-closed and
byte-compatible with every pre-R8 row/edition:

1. **Four additive-nullable columns on ``user_mcp_servers``** (R8-D-4) — the OAuth
   token lifecycle: ``oauth_provider``, ``refresh_token_encrypted`` (Fernet),
   ``access_token_expires_at`` (TIMESTAMPTZ), ``oauth_scopes``. A BYO/PAT row
   (``auth_method`` ∈ none/bearer/header) is untouched — all four read NULL. The
   access token reuses the existing ``credentials_encrypted`` blob under the SAME
   Fernet key (one credential channel — no second key). Additive nullable columns
   inherit ``user_mcp_servers``' existing owner_id RLS (migration 009) unchanged.

2. **New ``mcp_oauth_states`` table** (R8-D-6) — the in-flight CSRF/PKCE binding:
   one short-lived row per started flow, minted at ``/authorize``, consumed once
   at ``/callback`` (one-time use → fail-closed). RLS lives ENTIRELY here (the
   009/011 pattern — so ``001``'s downgrade never ALTERs a later table), scoped
   ``owner_id = current_user``. The PKCE ``code_verifier`` is Fernet-encrypted at
   rest; the opaque ``state`` is the callback key; the redirect target is stored
   server-side, never encoded in ``state``.

Split-home (cf. 008 / 020 / 023 / 025): the same columns + table are declared on
the canonical ``persona_api.db.models``, so on a fresh DB (incl. the community
SQLite edition) ``001_initial``'s ``create_all`` already builds them and the
guarded ``ADD COLUMN IF NOT EXISTS`` / ``create(checkfirst=True)`` below are
harmless no-ops; on a previously-deployed Postgres they actually apply. Both agree.

Revision ID: 026_mcp_oauth
Revises: 025_avatar_source_provenance
Create Date: 2026-07-01
"""

from __future__ import annotations

from alembic import op
from persona_api.db.models import mcp_oauth_states

# PLACEHOLDER down_revision (R-19-1 chain numbering): chained off main's head at
# authoring time, ``025_avatar_source_provenance``. Other in-flight specs
# (R-/C-/N-/P-track) may land migrations before R8 merges, so this number +
# ``down_revision`` are RECOMPUTED at merge-back to preserve the single-head chain —
# do NOT rely on ``026`` / ``025`` surviving verbatim.
revision = "027_mcp_oauth"
down_revision = "026_skill_consents"
branch_labels = None
depends_on = None

# The R8-D-4 token-lifecycle columns + the T7 DCR client_id (name → type SQL). All
# additive + nullable. ``oauth_client_id`` (folded in per the checkpoint ruling) holds
# the RFC 7591-registered client_id for a discovered ``mcp-native`` server — it cannot be
# re-derived; the endpoints are re-discovered from ``url``.
_OAUTH_COLUMNS: tuple[tuple[str, str], ...] = (
    ("oauth_provider", "TEXT"),
    ("refresh_token_encrypted", "TEXT"),
    ("access_token_expires_at", "TIMESTAMPTZ"),
    ("oauth_scopes", "TEXT"),
    ("oauth_client_id", "TEXT"),
)

_CUR = "current_setting('app.current_user_id', true)"
_STATE_PREDICATE = f"owner_id = {_CUR}"


def upgrade() -> None:
    bind = op.get_bind()
    # 1. Additive-nullable OAuth columns on the existing user_mcp_servers row.
    #    ADD COLUMN IF NOT EXISTS: a no-op on a fresh 001 DB (create_all already
    #    built them from the live model), a genuine add on a pre-R8 Postgres.
    for name, sql_type in _OAUTH_COLUMNS:
        op.execute(f"ALTER TABLE user_mcp_servers ADD COLUMN IF NOT EXISTS {name} {sql_type}")

    # 2. The in-flight state table + its OWN RLS (009/011 pattern). checkfirst=True
    #    so a fresh 001 create_all (the table is now in canonical metadata) is a
    #    no-op; a pre-R8 DB gets it here.
    mcp_oauth_states.create(bind, checkfirst=True)
    op.execute("ALTER TABLE mcp_oauth_states ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE mcp_oauth_states FORCE ROW LEVEL SECURITY")
    # DROP IF EXISTS keeps this idempotent + avoids clashing with a policy a
    # fresh-install 001 already created (the table is in metadata).
    op.execute("DROP POLICY IF EXISTS user_isolation ON mcp_oauth_states")
    op.execute(
        "CREATE POLICY user_isolation ON mcp_oauth_states "
        f"USING ({_STATE_PREDICATE}) WITH CHECK ({_STATE_PREDICATE})"
    )


def downgrade() -> None:
    bind = op.get_bind()
    op.execute("DROP POLICY IF EXISTS user_isolation ON mcp_oauth_states")
    op.execute("ALTER TABLE mcp_oauth_states NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE mcp_oauth_states DISABLE ROW LEVEL SECURITY")
    mcp_oauth_states.drop(bind, checkfirst=True)
    for name, _sql_type in _OAUTH_COLUMNS:
        op.execute(f"ALTER TABLE user_mcp_servers DROP COLUMN IF EXISTS {name}")

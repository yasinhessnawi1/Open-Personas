"""R8 T2 — the OAuth storage migration: additive columns + ``mcp_oauth_states`` RLS.

Against real Postgres (migration 026 applied via ``migrated_engine``) this proves:

- the four R8-D-4 token-lifecycle columns exist on ``user_mcp_servers`` and a
  pre-R8-shaped BYO row reads them all NULL (byte-compatibility);
- ``mcp_oauth_states`` exists with RLS ENABLED **and FORCED** + a ``user_isolation``
  policy (the 009/011 own-your-RLS pattern);
- cross-tenant denial is **non-vacuous under the non-superuser ``persona_app`` role**:
  tenant B, with its own ``app.current_user_id`` bound, cannot see tenant A's state
  row (and the same insert IS visible to A — so the policy isn't just hiding
  everything). This is the ``test_rls_scope`` discipline (R8-D-6 / research §3.6).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

_OAUTH_COLUMNS = frozenset(
    {"oauth_provider", "refresh_token_encrypted", "access_token_expires_at", "oauth_scopes"}
)


def _ensure_user(engine: Engine, owner: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
            {"i": owner, "e": f"{owner}@x"},
        )


def test_oauth_columns_present_and_null_for_pre_r8_row(migrated_engine: Engine) -> None:
    # All four additive columns exist.
    with migrated_engine.connect() as conn:
        cols = {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'user_mcp_servers'"
                )
            )
        }
    assert cols >= _OAUTH_COLUMNS, f"missing OAuth columns: {_OAUTH_COLUMNS - cols}"

    # A pre-R8-shaped BYO row (no OAuth fields set) reads them all NULL.
    _ensure_user(migrated_engine, "u_pre_r8")
    with migrated_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO user_mcp_servers (id, owner_id, name, url, auth_method) "
                "VALUES ('s_pre', 'u_pre_r8', 'legacy', 'https://x.example/mcp', 'bearer')"
            )
        )
        row = (
            conn.execute(
                text(
                    "SELECT oauth_provider, refresh_token_encrypted, access_token_expires_at, "
                    "oauth_scopes FROM user_mcp_servers WHERE id = 's_pre'"
                )
            )
            .mappings()
            .first()
        )
    assert row is not None
    assert all(v is None for v in row.values()), "pre-R8 row must read OAuth columns as NULL"


def test_state_table_rls_enabled_and_forced(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as conn:
        rel = conn.execute(
            text(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE relname = 'mcp_oauth_states'"
            )
        ).first()
        policy = conn.execute(
            text("SELECT 1 FROM pg_policies WHERE tablename = 'mcp_oauth_states'")
        ).first()
    assert rel is not None, "mcp_oauth_states table missing"
    assert rel[0] is True, "RLS must be ENABLED on mcp_oauth_states"
    assert rel[1] is True, "RLS must be FORCED (owner is subject to policy too)"
    assert policy is not None, "user_isolation policy missing on mcp_oauth_states"


def test_state_cross_tenant_denial_nonvacuous_under_persona_app(migrated_engine: Engine) -> None:
    """Tenant B (persona_app, own GUC) cannot see tenant A's state row; A can."""
    app_url = migrated_engine.url.set(
        username="persona_app", password="persona_app"
    ).render_as_string(hide_password=False)
    engine = make_rls_engine(app_url)

    # Seed the two users + tenant A's server + a state row as superuser (bypasses RLS).
    _ensure_user(migrated_engine, "tenant_a")
    _ensure_user(migrated_engine, "tenant_b")
    expires = datetime.now(UTC) + timedelta(minutes=5)
    with migrated_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO user_mcp_servers "
                "(id, owner_id, name, url, auth_method, oauth_provider)"
                " VALUES ('srv_a', 'tenant_a', 'gh', "
                "'https://api.github.com/mcp', 'oauth', 'github')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO mcp_oauth_states "
                "(id, owner_id, server_id, state, code_verifier_encrypted, provider, expires_at) "
                "VALUES ('st_a', 'tenant_a', 'srv_a', 'STATE_A', 'enc', 'github', :exp)"
            ),
            {"exp": expires},
        )

    # Tenant A sees its own row (policy is not vacuously hiding everything).
    tok_a = current_user_id.set("tenant_a")
    try:
        with engine.begin() as conn:
            visible_a = conn.execute(
                text("SELECT state FROM mcp_oauth_states WHERE state = 'STATE_A'")
            ).first()
        assert visible_a is not None, "tenant A must see its own state row (non-vacuous)"
    finally:
        current_user_id.reset(tok_a)

    # Tenant B, with its own GUC bound, sees nothing — cross-tenant denial.
    tok_b = current_user_id.set("tenant_b")
    try:
        with engine.begin() as conn:
            visible_b = conn.execute(
                text("SELECT state FROM mcp_oauth_states WHERE state = 'STATE_A'")
            ).first()
        assert visible_b is None, "tenant B must NOT see tenant A's state row (RLS)"
    finally:
        current_user_id.reset(tok_b)

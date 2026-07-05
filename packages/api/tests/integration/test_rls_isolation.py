"""Adversarial RLS tenant-isolation tests (spec 07, T07, acceptance #4).

Written tests-first: insert rows for user_a AND user_b, then query under each
user's RLS context and assert ZERO cross-tenant rows across personas,
conversations, and memory_chunks.

The connection under test uses a NON-SUPERUSER role (``persona_app``).
Superusers bypass RLS entirely, and even ``FORCE ROW LEVEL SECURITY`` only binds
the table *owner* — a superuser still sees everything. So a meaningful isolation
test MUST connect as an unprivileged role, exactly as production does. The role
DSN is read from ``APP_DATABASE_URL``; the test skips if it is unset.

The user id is set via the production helper ``set_current_user`` (which uses
``set_config('app.current_user_id', :uid, true)`` — NOT ``SET LOCAL = :uid``,
a syntax error with a bound param).
"""

# ruff: noqa: ANN401, ARG001, ARG002
from __future__ import annotations

import os

import pytest
from persona_api.db.engine import rls_connection, set_current_user
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.integration


@pytest.fixture
def app_engine(migrated_engine: object) -> object:
    """A non-superuser engine for the RLS-under-test connection.

    Depends on ``migrated_engine`` so the schema (with RLS policies) is built
    and ``persona_app`` is granted before this fixture's connection is used.
    Returns a separate engine logged in as the unprivileged ``persona_app``
    role. Skips if ``APP_DATABASE_URL`` is unset (the role must be provisioned
    out-of-band — see the spec-07 closeout for the SQL).
    """
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (non-superuser role) not set; skipping RLS test")
    return create_engine(app_url.replace("+asyncpg", "+psycopg"))


def _seed_two_tenants(superuser_engine: object) -> None:
    # Seed as superuser (bypasses RLS) so both tenants' rows exist regardless of
    # policy — the test then proves the policy hides the other tenant's rows.
    with superuser_engine.begin() as conn:  # type: ignore[attr-defined]
        conn.execute(
            text(
                "INSERT INTO users (id, email) VALUES "
                "('user_a','a@example.com'),('user_b','b@example.com')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO personas (id, owner_id, yaml) VALUES "
                "('pa','user_a','name: a'),('pb','user_b','name: b')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO conversations (id, owner_id, persona_id) VALUES "
                "('ca','user_a','pa'),('cb','user_b','pb')"
            )
        )


def test_personas_isolated_per_tenant(migrated_engine: object, app_engine: object) -> None:
    _seed_two_tenants(migrated_engine)
    with rls_connection(app_engine, "user_a") as conn:  # type: ignore[arg-type]
        rows = conn.execute(text("SELECT id, owner_id FROM personas")).all()
    owners = {r.owner_id for r in rows}
    assert owners == {"user_a"}, f"RLS leak: user_a saw {owners}"


def test_conversations_isolated_per_tenant(migrated_engine: object, app_engine: object) -> None:
    _seed_two_tenants(migrated_engine)
    with rls_connection(app_engine, "user_b") as conn:  # type: ignore[arg-type]
        rows = conn.execute(text("SELECT id, owner_id FROM conversations")).all()
    owners = {r.owner_id for r in rows}
    assert owners == {"user_b"}, f"RLS leak: user_b saw {owners}"


def test_memory_chunks_isolated_via_persona_fk_chain(
    migrated_engine: object, app_engine: object
) -> None:
    _seed_two_tenants(migrated_engine)
    # Seed one memory chunk per persona as superuser (embedding is a zero vec).
    zero = "[" + ",".join(["0"] * 384) + "]"
    with migrated_engine.begin() as conn:  # type: ignore[attr-defined]
        for cid, pid in (("ma", "pa"), ("mb", "pb")):
            conn.execute(
                text(
                    "INSERT INTO memory_chunks "
                    "(id, persona_id, kind, text, embedding, content_hash) VALUES "
                    f"(:id, :pid, 'episodic', 'note', '{zero}', 'h')"
                ),
                {"id": cid, "pid": pid},
            )
    with rls_connection(app_engine, "user_a") as conn:  # type: ignore[arg-type]
        rows = conn.execute(text("SELECT id, persona_id FROM memory_chunks")).all()
    personas = {r.persona_id for r in rows}
    assert personas == {"pa"}, f"RLS leak via FK chain: user_a saw chunks for {personas}"


def test_episodic_gist_rows_inherit_the_persona_fk_policy(
    migrated_engine: object, app_engine: object
) -> None:
    """Spec K8 (K8-D-13): gist rows are ordinary memory_chunks under the SAME policy.

    Non-vacuous under the unprivileged ``persona_app`` role: both tenants get a
    ``kind='episodic_gist'`` row (with ``member_ids``); each tenant sees exactly
    their own. No new policy exists for the new kind — this proves none is needed.
    """
    _seed_two_tenants(migrated_engine)
    zero = "[" + ",".join(["0"] * 384) + "]"
    with migrated_engine.begin() as conn:  # type: ignore[attr-defined]
        for gid, pid, member in (("ga", "pa", "ma"), ("gb", "pb", "mb")):
            conn.execute(
                text(
                    "INSERT INTO memory_chunks "
                    "(id, persona_id, kind, text, embedding, content_hash, "
                    " fidelity_band, member_ids) VALUES "
                    f"(:id, :pid, 'episodic_gist', 'gist', '{zero}', 'h', 1, "
                    "ARRAY[:member]::text[])"
                ),
                {"id": gid, "pid": pid, "member": member},
            )
    with rls_connection(app_engine, "user_b") as conn:  # type: ignore[arg-type]
        rows = conn.execute(
            text("SELECT id, persona_id FROM memory_chunks WHERE kind = 'episodic_gist'")
        ).all()
    personas = {r.persona_id for r in rows}
    assert personas == {"pb"}, f"RLS leak on gist rows: user_b saw {personas}"


def test_cross_tenant_write_blocked_by_with_check(
    migrated_engine: object, app_engine: object
) -> None:
    # WITH CHECK must stop user_a from inserting a persona owned by user_b.
    _seed_two_tenants(migrated_engine)
    from sqlalchemy.exc import ProgrammingError

    with (
        rls_connection(app_engine, "user_a") as conn,  # type: ignore[arg-type]
        pytest.raises(ProgrammingError),
    ):
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES ('evil','user_b','name: evil')")
        )


def test_cannot_attach_conversation_to_other_tenants_persona(
    migrated_engine: object, app_engine: object
) -> None:
    # Security finding 1: the composite FK (persona_id, owner_id)->personas must
    # block user_a from attaching their conversation to user_b's persona, even
    # though owner_id passes the RLS WITH CHECK. Defence-in-depth beyond RLS.
    _seed_two_tenants(migrated_engine)
    from sqlalchemy.exc import IntegrityError

    with (
        rls_connection(app_engine, "user_a") as conn,  # type: ignore[arg-type]
        pytest.raises(IntegrityError),
    ):
        conn.execute(
            text(
                "INSERT INTO conversations (id, owner_id, persona_id) "
                "VALUES ('evil','user_a','pb')"  # pb is user_b's persona
            )
        )


def test_unset_user_sees_nothing_fail_closed(migrated_engine: object, app_engine: object) -> None:
    # No set_current_user call → current_setting(...,true) is NULL → zero rows.
    _seed_two_tenants(migrated_engine)
    with app_engine.begin() as conn:  # type: ignore[attr-defined]
        rows = conn.execute(text("SELECT id FROM personas")).all()
    assert rows == [], "RLS must fail closed when app.current_user_id is unset"


def test_set_current_user_is_parameterised_not_interpolated() -> None:
    # Guard against regressing to SET LOCAL string-interpolation. A value with a
    # quote must be handled as a bound param (no SQL injection / no syntax err).
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    engine = create_engine(app_url.replace("+asyncpg", "+psycopg"))
    with engine.begin() as conn:
        set_current_user(conn, "o'brien; DROP TABLE users;--")
        got = conn.execute(text("SELECT current_setting('app.current_user_id', true)")).scalar()
    assert got == "o'brien; DROP TABLE users;--"

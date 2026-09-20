"""Migration + RLS + constraint tests for the ``calls`` table (Spec V9, V9-D-5).

Runs against a real Postgres built by ``alembic upgrade head`` (so migration
``021_add_calls_table`` + its RLS policy are present). Concerns:

1. **Table + valid insert** — a call-record inserts cleanly with its FK parents
   (user → persona → conversation), ``ended_at``/``duration_s``/``end_reason``
   nullable while live.
2. **end_reason CHECK** — accepts every reason the voice runner writes
   (widened by migration 056 for R9-203) and rejects an out-of-vocabulary one.
3. **ON DELETE CASCADE** — deleting the conversation removes its call-records
   (v1 retention falls out of the FK, V9-D-5).
4. **RLS tenant isolation** (adversarial, the standing gate) — seed two tenants
   as superuser, then under each user's RLS context as the NON-SUPERUSER
   ``persona_app`` role assert zero cross-tenant rows + WITH CHECK blocks a
   cross-tenant insert. The non-superuser role is mandatory: superusers bypass
   RLS even under FORCE. ``APP_DATABASE_URL`` provides the role DSN; RLS tests
   skip if it is unset.
"""

# ruff: noqa: ARG001 — ``migrated_engine`` is a dependency-ordering fixture param.
from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from persona_api.db.engine import rls_connection
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, ProgrammingError

pytestmark = pytest.mark.integration


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:
    """A non-superuser (``persona_app``) engine for the RLS-under-test connection.

    Depends on ``migrated_engine`` so the migrated schema + grants exist first.
    Skips if ``APP_DATABASE_URL`` is unset. Disposed at teardown so no pooled
    connection lingers into the next test's DROP SCHEMA.
    """
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (non-superuser role) not set; skipping RLS test")
    engine = create_engine(app_url.replace("+asyncpg", "+psycopg"))
    yield engine
    engine.dispose()


def _seed_conversation(engine: Engine, *, user_id: str, persona_id: str, conv_id: str) -> None:
    """Seed the FK parents a ``calls`` row needs: user → persona → conversation."""
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:u, :e) ON CONFLICT DO NOTHING"),
            {"u": user_id, "e": f"{user_id}@example.com"},
        )
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :u, 'y')"),
            {"p": persona_id, "u": user_id},
        )
        conn.execute(
            text(
                "INSERT INTO conversations (id, owner_id, persona_id, origin) "
                "VALUES (:c, :u, :p, 'call')"
            ),
            {"c": conv_id, "u": user_id, "p": persona_id},
        )


# --- table + valid insert ---------------------------------------------------


def test_call_record_inserts_live_then_finalizes(migrated_engine: Engine) -> None:
    _seed_conversation(migrated_engine, user_id="user_a", persona_id="p_a", conv_id="c_a")
    with migrated_engine.begin() as conn:
        # live record: ended_at / duration_s / end_reason NULL.
        conn.execute(
            text(
                "INSERT INTO calls (call_id, conversation_id, persona_id, owner_id, started_at) "
                "VALUES ('call_1','c_a','p_a','user_a', now())"
            )
        )
        live = conn.execute(
            text(
                "SELECT ended_at, duration_s, end_reason, created_at "
                "FROM calls WHERE call_id='call_1'"
            )
        ).one()
        assert live.ended_at is None
        assert live.duration_s is None
        assert live.end_reason is None
        assert live.created_at is not None
        # finalize.
        conn.execute(
            text(
                "UPDATE calls SET ended_at = now(), duration_s = 125, end_reason = 'disconnect' "
                "WHERE call_id = 'call_1'"
            )
        )
        final = conn.execute(
            text("SELECT duration_s, end_reason FROM calls WHERE call_id='call_1'")
        ).one()
    assert final.duration_s == 125
    assert final.end_reason == "disconnect"


@pytest.mark.parametrize(
    "reason",
    ["user_hangup", "switched", "exhausted", "shutdown", "error", "disconnect"],
)
def test_end_reason_check_accepts_every_reason_the_runner_writes(
    migrated_engine: Engine, reason: str
) -> None:
    """R9-203 widened the vocabulary (migration 056), so the CHECK has to accept
    the three reasons the voice runner gained a producer for. A call ended by the
    credit cutoff or by a redeploy is no longer stored as a clean disconnect."""
    _seed_conversation(migrated_engine, user_id="user_a", persona_id="p_a", conv_id="c_a")
    with migrated_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO calls (call_id, conversation_id, persona_id, owner_id, started_at, "
                "end_reason) VALUES (:cid,'c_a','p_a','user_a', now(), :reason)"
            ),
            {"cid": f"call_{reason}", "reason": reason},
        )
        stored = conn.execute(
            text("SELECT end_reason FROM calls WHERE call_id = :cid"), {"cid": f"call_{reason}"}
        ).scalar_one()
    assert stored == reason


def test_end_reason_check_rejects_unknown(migrated_engine: Engine) -> None:
    _seed_conversation(migrated_engine, user_id="user_a", persona_id="p_a", conv_id="c_a")
    with pytest.raises(IntegrityError), migrated_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO calls (call_id, conversation_id, persona_id, owner_id, started_at, "
                "end_reason) VALUES ('call_bad','c_a','p_a','user_a', now(), 'exploded')"
            )
        )


def test_deleting_conversation_cascades_call_records(migrated_engine: Engine) -> None:
    _seed_conversation(migrated_engine, user_id="user_a", persona_id="p_a", conv_id="c_a")
    with migrated_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO calls (call_id, conversation_id, persona_id, owner_id, started_at) "
                "VALUES ('call_1','c_a','p_a','user_a', now())"
            )
        )
        conn.execute(text("DELETE FROM conversations WHERE id = 'c_a'"))
        remaining = conn.execute(
            text("SELECT count(*) FROM calls WHERE call_id='call_1'")
        ).scalar_one()
    assert remaining == 0


# --- RLS tenant isolation (adversarial) -------------------------------------


def _seed_two_tenants(superuser_engine: Engine) -> None:
    _seed_conversation(superuser_engine, user_id="user_a", persona_id="p_a", conv_id="c_a")
    _seed_conversation(superuser_engine, user_id="user_b", persona_id="p_b", conv_id="c_b")
    with superuser_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO calls (call_id, conversation_id, persona_id, owner_id, started_at) "
                "VALUES ('call_a','c_a','p_a','user_a', now()),"
                "('call_b','c_b','p_b','user_b', now())"
            )
        )


def test_calls_isolated_per_tenant(migrated_engine: Engine, app_engine: Engine) -> None:
    _seed_two_tenants(migrated_engine)
    with rls_connection(app_engine, "user_a") as conn:
        owners = {r.owner_id for r in conn.execute(text("SELECT owner_id FROM calls")).all()}
    assert owners == {"user_a"}, f"RLS leak on calls: user_a saw {owners}"


def test_calls_cross_tenant_write_blocked_by_with_check(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed_two_tenants(migrated_engine)
    with (
        rls_connection(app_engine, "user_a") as conn,
        pytest.raises(ProgrammingError),
    ):
        conn.execute(
            text(
                "INSERT INTO calls (call_id, conversation_id, persona_id, owner_id, started_at) "
                "VALUES ('evil','c_b','p_b','user_b', now())"
            )
        )

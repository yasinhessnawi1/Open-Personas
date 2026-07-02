"""Migration + RLS + constraint tests for the P6 ``notifications`` feed (P6-D-11).

Runs against a real Postgres built by ``alembic upgrade head`` (so migration
``028_notifications_feed`` and its RLS policy are present). Concerns:

1. **Table + valid insert** — a notification inserts cleanly with the server-side
   defaults (``read=false``, ``params={}``, ``created_at``).
2. **Constraints** — the ``kind`` / ``level`` checks; and the **idempotency key**
   ``UNIQUE (owner_id, kind, ref_id)`` — a duplicate server-authored write for the
   same (owner, kind, ref) is impossible, and ``ON CONFLICT DO NOTHING`` is a no-op
   (the once-only guard the run persist-final / persist-error / restart-sweep paths
   rely on, P6-D-11/D-P6-12).
3. **RLS tenant isolation** (adversarial, the standing gate) — two tenants, each
   sees ONLY its own rows under the ``persona_app`` non-superuser role, proven
   **non-vacuously in both directions** (A sees A's, not B's; B sees B's, not A's —
   ruling out an empty-table false pass); WITH CHECK blocks a cross-tenant insert;
   an unset GUC fails closed.

The non-superuser role is mandatory: superusers bypass RLS even under FORCE.
``APP_DATABASE_URL`` provides the role DSN; the RLS tests skip if it is unset.
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
    """A non-superuser (``persona_app``) engine for the RLS-under-test connection."""
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (non-superuser role) not set; skipping RLS test")
    engine = create_engine(app_url.replace("+asyncpg", "+psycopg"))
    yield engine
    engine.dispose()


def _seed_user(engine: Engine, user: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:u, :e)"),
            {"u": user, "e": f"{user}@example.com"},
        )


def _insert_notification(
    engine: Engine,
    *,
    nid: str,
    owner: str,
    kind: str = "run_terminal",
    ref: str | None = "run1",
    level: str = "success",
    key: str = "notifications.run.completed",
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO notifications "
                "(id, owner_id, kind, ref_id, level, message_key) "
                "VALUES (:i, :o, :k, :r, :l, :key)"
            ),
            {"i": nid, "o": owner, "k": kind, "r": ref, "l": level, "key": key},
        )


# --- table + valid insert ---------------------------------------------------


def test_notification_inserts_with_defaults(migrated_engine: Engine) -> None:
    _seed_user(migrated_engine, "user_a")
    _insert_notification(migrated_engine, nid="n1", owner="user_a")
    with migrated_engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT kind, ref_id, level, message_key, params, read, created_at "
                "FROM notifications WHERE id = 'n1'"
            )
        ).one()
    assert row.kind == "run_terminal"
    assert row.ref_id == "run1"
    assert row.level == "success"
    assert row.message_key == "notifications.run.completed"
    assert row.params == {}
    assert row.read is False
    assert row.created_at is not None


# --- constraints ------------------------------------------------------------


def test_kind_check_rejects_unknown(migrated_engine: Engine) -> None:
    _seed_user(migrated_engine, "user_a")
    with pytest.raises(IntegrityError):
        _insert_notification(migrated_engine, nid="n1", owner="user_a", kind="bogus")


def test_level_check_rejects_unknown(migrated_engine: Engine) -> None:
    _seed_user(migrated_engine, "user_a")
    with pytest.raises(IntegrityError):
        _insert_notification(migrated_engine, nid="n1", owner="user_a", level="bogus")


def test_idempotency_rejects_duplicate_owner_kind_ref(migrated_engine: Engine) -> None:
    _seed_user(migrated_engine, "user_a")
    _insert_notification(migrated_engine, nid="n1", owner="user_a", ref="run1")
    # A second row for the same (owner, kind, ref) is structurally impossible.
    with pytest.raises(IntegrityError):
        _insert_notification(migrated_engine, nid="n2", owner="user_a", ref="run1")


def test_idempotency_allows_different_ref_or_kind(migrated_engine: Engine) -> None:
    _seed_user(migrated_engine, "user_a")
    _insert_notification(migrated_engine, nid="n1", owner="user_a", kind="run_terminal", ref="run1")
    # Different ref → allowed.
    _insert_notification(migrated_engine, nid="n2", owner="user_a", kind="run_terminal", ref="run2")
    # Different kind (same owner) → allowed.
    _insert_notification(
        migrated_engine, nid="n3", owner="user_a", kind="persona_ready", ref="persona1"
    )
    with migrated_engine.begin() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM notifications WHERE owner_id='user_a'")
        ).scalar_one()
    assert count == 3


def test_on_conflict_do_nothing_is_noop(migrated_engine: Engine) -> None:
    # The exact once-only write pattern D4-c/d use: a re-run (persist-error /
    # restart-sweep) of the same server-authored write leaves a single row.
    _seed_user(migrated_engine, "user_a")
    _insert_notification(migrated_engine, nid="n1", owner="user_a", ref="run1")
    with migrated_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO notifications "
                "(id, owner_id, kind, ref_id, level, message_key) "
                "VALUES ('n2','user_a','run_terminal','run1','success','k') "
                "ON CONFLICT (owner_id, kind, ref_id) DO NOTHING"
            )
        )
        count = conn.execute(
            text("SELECT count(*) FROM notifications WHERE owner_id='user_a' AND ref_id='run1'")
        ).scalar_one()
    assert count == 1


# --- RLS tenant isolation (adversarial) -------------------------------------


def _seed_two_tenants(engine: Engine) -> None:
    _seed_user(engine, "user_a")
    _seed_user(engine, "user_b")
    _insert_notification(engine, nid="na", owner="user_a", ref="run_a")
    _insert_notification(engine, nid="nb", owner="user_b", ref="run_b")


def test_notifications_isolated_per_tenant_both_directions(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed_two_tenants(migrated_engine)
    # A sees only A's — and DOES see its own (non-vacuous, not an empty-table pass).
    with rls_connection(app_engine, "user_a") as conn:
        owners_a = {
            r.owner_id for r in conn.execute(text("SELECT owner_id FROM notifications")).all()
        }
    assert owners_a == {"user_a"}, f"RLS leak: user_a saw {owners_a}"
    # B sees only B's — the mirror direction, also non-vacuous.
    with rls_connection(app_engine, "user_b") as conn:
        owners_b = {
            r.owner_id for r in conn.execute(text("SELECT owner_id FROM notifications")).all()
        }
    assert owners_b == {"user_b"}, f"RLS leak: user_b saw {owners_b}"


def test_with_check_blocks_cross_tenant_insert(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed_two_tenants(migrated_engine)
    # user_a tries to write a row owned by user_b → WITH CHECK rejects it
    # (an RLS policy violation surfaces as InsufficientPrivilege / ProgrammingError).
    with pytest.raises(ProgrammingError), rls_connection(app_engine, "user_a") as conn:
        conn.execute(
            text(
                "INSERT INTO notifications "
                "(id, owner_id, kind, ref_id, level, message_key) "
                "VALUES ('x','user_b','run_terminal','run_x','success','k')"
            )
        )


def test_unset_guc_fails_closed(migrated_engine: Engine, app_engine: Engine) -> None:
    _seed_two_tenants(migrated_engine)
    # No set_current_user → current_setting(...) is NULL → predicate never true → zero rows.
    with app_engine.connect() as conn:
        rows = conn.execute(text("SELECT owner_id FROM notifications")).all()
    assert rows == [], f"fail-closed breach: unset GUC saw {rows}"

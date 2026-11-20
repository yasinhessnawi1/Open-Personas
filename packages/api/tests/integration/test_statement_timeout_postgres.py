"""R9-313 against a real Postgres: the engines' statement timeout cancels, and nothing is half-done.

The unit tests prove what the engines hand psycopg; these prove what Postgres does with it:
a statement past the bound is cancelled (SQLSTATE 57014), a money write cancelled mid-flight
rolls back whole, and the erasure step that must finish lifts the bound for its transaction.

Every wait in here is bounded, so a regression fails instead of hanging the run.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

import pytest
from persona.graph.maintenance_lock import take_graph_maintenance_lock
from persona_api.db.connection_bounds import (
    DEFAULT_STATEMENT_TIMEOUT_MS,
    DbConnectionBounds,
    lift_statement_timeout,
)
from persona_api.db.engine import create_db_engine
from persona_api.db.models import credit_transactions as credit_tx_t
from persona_api.db.models import credits as credits_t
from persona_api.middleware.rls_context import make_rls_engine
from persona_api.services import credits_service
from persona_api.services.erasure_graph import erase_persona_graph
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_USER = "u_stmt_timeout"
_BOUND_MS = 300


@pytest.fixture
def bounded(database_url: str) -> Iterator[Engine]:
    """A platform engine whose statements may run for ``_BOUND_MS`` at most."""
    engine = create_db_engine(
        database_url, bounds=DbConnectionBounds(statement_timeout_ms=_BOUND_MS)
    )
    yield engine
    engine.dispose()


def _sqlstate(exc: OperationalError) -> str | None:
    return getattr(exc.orig, "sqlstate", None)


def test_a_statement_past_the_bound_is_cancelled(bounded: Engine) -> None:
    began = time.monotonic()
    with pytest.raises(OperationalError) as caught, bounded.connect() as conn:
        conn.execute(text("SELECT pg_sleep(10)"))
    assert _sqlstate(caught.value) == "57014"  # query_canceled
    assert time.monotonic() - began < 5


@pytest.mark.usefixtures("migrated_engine")
def test_the_rls_engine_is_bounded_by_default(database_url: str) -> None:
    engine = make_rls_engine(database_url, pool_size=1)
    try:
        with engine.connect() as conn:
            shown = conn.execute(text("SHOW statement_timeout")).scalar_one()
    finally:
        engine.dispose()
    assert shown == f"{DEFAULT_STATEMENT_TIMEOUT_MS // 60_000}min"


def test_lifting_the_bound_lasts_one_transaction(bounded: Engine) -> None:
    with bounded.begin() as conn:
        lift_statement_timeout(conn)
        conn.execute(text("SELECT pg_sleep(0.6)"))  # twice the bound, and it finishes
    with bounded.connect() as conn:
        assert conn.execute(text("SHOW statement_timeout")).scalar_one() == f"{_BOUND_MS}ms"


def _seed_wallet(engine: Engine, balance: int) -> None:
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
            {"i": _USER, "e": f"{_USER}@x.test"},
        )
        conn.execute(
            pg_insert(credits_t)
            .values(user_id=_USER, balance=balance)
            .on_conflict_do_update(index_elements=[credits_t.c.user_id], set_={"balance": balance})
        )


def test_a_cancelled_deduct_leaves_no_trace(migrated_engine: Engine, bounded: Engine) -> None:
    """The wallet row is locked elsewhere, so the deduct waits past the bound and is cancelled.

    The deduct books the day cap first, in the same transaction; the cancel rolls that back
    too. Balance, ledger and day spend are exactly as before.
    """
    _seed_wallet(migrated_engine, 100)
    with migrated_engine.connect() as locker:
        locker.execute(
            select(credits_t.c.balance).where(credits_t.c.user_id == _USER).with_for_update()
        )
        with pytest.raises(OperationalError) as caught:
            credits_service.deduct(
                rls_engine=bounded, user_id=_USER, amount=7, reason="chat_turn", daily_cap=1000
            )
        locker.rollback()
    assert _sqlstate(caught.value) == "57014"
    with migrated_engine.connect() as conn:
        balance = conn.execute(
            select(credits_t.c.balance).where(credits_t.c.user_id == _USER)
        ).scalar_one()
        ledger = conn.execute(
            select(func.count()).select_from(credit_tx_t).where(credit_tx_t.c.user_id == _USER)
        ).scalar_one()
        spent = conn.execute(
            text("SELECT coalesce(sum(spent), 0) FROM day_spend WHERE user_id = :u"), {"u": _USER}
        ).scalar_one()
    assert (balance, ledger, spent) == (100, 0, 0)


def test_the_erasure_graph_step_outwaits_the_bound(
    migrated_engine: Engine, bounded: Engine
) -> None:
    """A consolidation holds the owner's graph lock for longer than the bound; the step waits."""
    owner = "u_stmt_timeout_graph"
    held, release = threading.Event(), threading.Event()

    def _hold() -> None:
        with migrated_engine.begin() as conn:
            take_graph_maintenance_lock(conn, owner)
            held.set()
            release.wait(timeout=10)

    holder = threading.Thread(target=_hold, daemon=True)
    holder.start()
    assert held.wait(timeout=10)
    timer = threading.Timer(1.0, release.set)  # about three times the bound
    timer.start()
    try:
        counts = erase_persona_graph(bounded, owner_id=owner, persona_id="p_gone")
    finally:
        release.set()
        timer.cancel()
        holder.join(timeout=10)
    assert all(n == 0 for n in counts.values())

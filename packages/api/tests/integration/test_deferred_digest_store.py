"""The deferred-digest sink — atomic consume, no double-deliver (Spec A6, A6-D-10 binding).

The risk the 040 DDL gate named: consume-and-mark must be ONE atomic statement so a concurrent
morning build never double-delivers a chatter line and never silently drops one. Real Postgres —
the atomic ``UPDATE … WHERE delivered_at IS NULL RETURNING`` is the whole point (a fake wouldn't
exercise it).
"""

# ruff: noqa: ARG001 — ``migrated_engine`` is a dependency-ordering fixture param.
from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from persona_api.digest import DeferredDigestStore
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 7, 7, 6, 0, tzinfo=UTC)


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (non-superuser role) not set; skipping deferred-digest test")
    engine = create_engine(app_url.replace("+asyncpg", "+psycopg"))
    yield engine
    engine.dispose()


def _seed(su: Engine, owner: str, persona: str) -> None:
    with su.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:u, :e)"), {"u": owner, "e": f"{owner}@x"}
        )
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :o, 'name: x')"),
            {"p": persona, "o": owner},
        )


def test_defer_then_consume_returns_and_marks_delivered(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed(migrated_engine, "u", "p")
    store = DeferredDigestStore(app_engine)
    store.defer("u", "p", "filed 3 receipts", now=_NOW)
    store.defer("u", "p", "checked fares", now=_NOW)

    first = store.consume_undelivered("u", now=_NOW)
    assert [i.content for i in first] == ["filed 3 receipts", "checked fares"]  # oldest-first
    # a second consume returns nothing — the rows are delivered (no re-read, no double-deliver).
    assert store.consume_undelivered("u", now=_NOW) == []


def test_consume_is_owner_scoped(migrated_engine: Engine, app_engine: Engine) -> None:
    _seed(migrated_engine, "u_a", "p_a")
    _seed(migrated_engine, "u_b", "p_b")
    store = DeferredDigestStore(app_engine)
    store.defer("u_a", "p_a", "a's chatter", now=_NOW)
    store.defer("u_b", "p_b", "b's chatter", now=_NOW)
    assert [i.content for i in store.consume_undelivered("u_a", now=_NOW)] == ["a's chatter"]


@pytest.mark.asyncio
async def test_concurrent_consume_never_double_delivers(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    """Two morning builds racing the same owner's chatter → each line delivered EXACTLY once."""
    _seed(migrated_engine, "u", "p")
    store = DeferredDigestStore(app_engine)
    for i in range(20):
        store.defer("u", "p", f"line {i}", now=_NOW)

    async def _consume() -> list[str]:
        return [
            i.content for i in await asyncio.to_thread(store.consume_undelivered, "u", now=_NOW)
        ]

    a, b = await asyncio.gather(_consume(), _consume())

    assert set(a).isdisjoint(set(b))  # no line claimed by both — the atomic claim held
    assert sorted(a + b) == sorted(f"line {i}" for i in range(20))  # none dropped, none duplicated

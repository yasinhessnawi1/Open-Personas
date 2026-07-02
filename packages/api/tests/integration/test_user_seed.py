"""Integration: the K6-D-1 Clerk name seed in ``ensure_user`` (seed-once-when-null).

Drives ``ensure_user`` directly against real Postgres. Proves — non-vacuously —
all three branches of the claims-presence-gated seed: claims present + columns
null → seeded; claims present + columns already set → untouched (our DB stays
authoritative); claims absent → untouched, zero change. Plus: seeded values get
the same normalisation (K6-D-8) as a PATCH, and a partial seed fills only the
null column.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from persona_api.middleware.rls_context import make_rls_engine
from persona_api.services.user_service import ensure_user
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine

pytestmark = pytest.mark.integration


@pytest.fixture
def su_engine(migrated_engine: Engine) -> Iterator[Engine]:  # noqa: ARG001 — ensures schema at head
    engine = make_rls_engine(os.environ["DATABASE_URL"])  # superuser (provisioning bypasses RLS)
    yield engine
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id LIKE 'seed_%'"))
    engine.dispose()


def _names(engine: Engine, uid: str) -> tuple[str | None, str | None]:
    with engine.connect() as conn:
        row = (
            conn.execute(text("SELECT first_name, last_name FROM users WHERE id = :i"), {"i": uid})
            .mappings()
            .first()
        )
    assert row is not None
    return row["first_name"], row["last_name"]


def test_claims_seed_when_columns_are_null(su_engine: Engine) -> None:
    ensure_user(su_engine, user_id="seed_a", email="a@x", first_name="Ada", last_name="Lovelace")
    assert _names(su_engine, "seed_a") == ("Ada", "Lovelace")


def test_claims_do_not_overwrite_a_name_already_set(su_engine: Engine) -> None:
    # Our DB is authoritative: a name set here is never clobbered by a later claim.
    ensure_user(su_engine, user_id="seed_b", email="b@x", first_name="Ada", last_name="Lovelace")
    ensure_user(su_engine, user_id="seed_b", email="b@x", first_name="Grace", last_name="Hopper")
    assert _names(su_engine, "seed_b") == ("Ada", "Lovelace")


def test_absent_claims_are_a_graceful_noop(su_engine: Engine) -> None:
    ensure_user(su_engine, user_id="seed_c", email="c@x")  # no claims (the default path)
    assert _names(su_engine, "seed_c") == (None, None)
    ensure_user(su_engine, user_id="seed_c", email="c@x")  # re-provision, still no claims
    assert _names(su_engine, "seed_c") == (None, None)


def test_seeded_values_are_normalised(su_engine: Engine) -> None:
    # Same K6-D-8 hygiene as a PATCH — control chars stripped, length capped.
    ensure_user(
        su_engine, user_id="seed_d", email="d@x", first_name="  Ada\n ", last_name="x" * 200
    )
    first, last = _names(su_engine, "seed_d")
    assert first == "Ada"
    assert last is not None
    assert len(last) == 100


def test_partial_seed_fills_only_the_null_column(su_engine: Engine) -> None:
    # first_name set, last_name null → a later claim seeds ONLY last_name.
    ensure_user(su_engine, user_id="seed_e", email="e@x", first_name="Ada")
    ensure_user(su_engine, user_id="seed_e", email="e@x", first_name="Grace", last_name="Hopper")
    assert _names(su_engine, "seed_e") == ("Ada", "Hopper")

"""A9-T4 — the voice delegation writer lands a REAL durable ``delegated_turn`` job (A9-D-5).

The enqueue half of the crossing, against the REAL ``jobs`` table (not a fake connection): the
**voice** raw-INSERT writer (``persona_voice`` — a peer process to api) writes a durable job that is
queued, carries the verbatim ask + provenance, and dedups on a re-enqueue (draft-hash-primary over
the verbatim ask + conversation). This is the real-DB proof for T4; the **worker-drain→create** half
of the transition (the api-side ``delegated_turn`` handler running the chat pipeline) is proven end
to end at T5, where the handler exists — the full enqueue→worker→create chain, no forced end-state
([[feedback_synthetic_harness_real_transition]]).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest
from persona_voice.session.delegation_enqueue import enqueue_delegated_turn
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

_OWNER = "voice_deleg_user"
_PERSONA = "voice_deleg_persona"
_CONVO = "voice_deleg_call"
_ASK = "check the news every morning and brief me"


@pytest.fixture
def seeded(migrated_engine: Engine) -> Engine:
    """Seed the owner + persona + a voice conversation (the jobs.owner_id FK parent)."""
    with migrated_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:o, 'vdeleg@example.com')"), {"o": _OWNER}
        )
        conn.execute(
            text(
                "INSERT INTO personas (id, owner_id, yaml) "
                "VALUES (:p, :o, 'schema_version: \"1.0\"')"
            ),
            {"p": _PERSONA, "o": _OWNER},
        )
        conn.execute(
            text(
                "INSERT INTO conversations (id, owner_id, persona_id, compacted_summary) "
                "VALUES (:c, :o, :p, '')"
            ),
            {"c": _CONVO, "o": _OWNER, "p": _PERSONA},
        )
    return migrated_engine


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:  # noqa: ARG001 — migrations first
    """The non-superuser ``persona_app`` engine (RLS-enforced; scopes reads by the GUC)."""
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (non-superuser role) not set")
    from persona_api.middleware.rls_context import make_rls_engine

    engine = make_rls_engine(app_url.replace("+asyncpg", "+psycopg"))
    yield engine
    engine.dispose()


def test_voice_writer_lands_a_real_queued_delegated_turn_job(seeded: Engine) -> None:
    job_id = enqueue_delegated_turn(
        seeded, owner_id=_OWNER, conversation_id=_CONVO, verbatim_ask=_ASK, persona_id=_PERSONA
    )
    assert job_id is not None
    with seeded.begin() as conn:
        row = (
            conn.execute(
                text("SELECT type, state, owner_id, payload FROM jobs WHERE id = :i"), {"i": job_id}
            )
            .mappings()
            .one()
        )
    assert row["type"] == "delegated_turn"
    assert row["state"] == "queued"  # a real durable job, claimable by the A0 worker (T5)
    assert row["owner_id"] == _OWNER
    payload = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])
    # The VERBATIM ask crossed — never a parsed draft (A9-D-5).
    assert payload["verbatim_ask"] == _ASK
    assert payload["conversation_id"] == _CONVO
    assert payload["persona_id"] == _PERSONA
    assert payload["provenance"] == "voice"


def test_reenqueue_of_the_same_ask_is_a_conflict_noop(seeded: Engine) -> None:
    first = enqueue_delegated_turn(
        seeded, owner_id=_OWNER, conversation_id=_CONVO, verbatim_ask=_ASK, persona_id=_PERSONA
    )
    second = enqueue_delegated_turn(
        seeded, owner_id=_OWNER, conversation_id=_CONVO, verbatim_ask=_ASK, persona_id=_PERSONA
    )
    assert first is not None
    assert second is None  # a double-confirm / redelivery dedups (over-dedup bias, A9-D-5)
    with seeded.begin() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM jobs WHERE owner_id = :o AND type = 'delegated_turn'"),
            {"o": _OWNER},
        ).scalar_one()
    assert count == 1  # exactly one durable delegation


def test_dedup_token_makes_a_deliberate_repeat_distinct(seeded: Engine) -> None:
    # The escape hatch: a gate-minted token lets the same spoken phrase be TWO delegations (A9-D-5).
    first = enqueue_delegated_turn(
        seeded,
        owner_id=_OWNER,
        conversation_id=_CONVO,
        verbatim_ask=_ASK,
        persona_id=_PERSONA,
        dedup_token="proposal-1",
    )
    second = enqueue_delegated_turn(
        seeded,
        owner_id=_OWNER,
        conversation_id=_CONVO,
        verbatim_ask=_ASK,
        persona_id=_PERSONA,
        dedup_token="proposal-2",
    )
    assert first is not None
    assert second is not None
    assert first != second
    with seeded.begin() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM jobs WHERE owner_id = :o AND type = 'delegated_turn'"),
            {"o": _OWNER},
        ).scalar_one()
    assert count == 2


def test_cross_tenant_rls_isolation_on_the_delegated_job(
    seeded: Engine, app_engine: Engine
) -> None:
    # Owner A's delegation is invisible to owner B (non-vacuous RLS on the crossing). The read runs
    # on the RLS-enforced ``persona_app`` engine scoped by the ``current_user_id`` GUC (a superuser
    # engine would bypass RLS and prove nothing).
    from persona_api.middleware.rls_context import current_user_id

    other = "voice_deleg_user_b"
    with seeded.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:o, 'vdelegb@example.com')"), {"o": other}
        )
    enqueue_delegated_turn(
        seeded, owner_id=_OWNER, conversation_id=_CONVO, verbatim_ask=_ASK, persona_id=_PERSONA
    )
    # Scoped to owner A: the delegation is visible.
    token = current_user_id.set(_OWNER)
    try:
        with app_engine.begin() as conn:
            mine = conn.execute(
                text("SELECT count(*) FROM jobs WHERE type = 'delegated_turn'")
            ).scalar_one()
    finally:
        current_user_id.reset(token)
    assert mine == 1
    # Scoped to owner B: owner A's delegation is invisible.
    token = current_user_id.set(other)
    try:
        with app_engine.begin() as conn:
            visible = conn.execute(
                text("SELECT count(*) FROM jobs WHERE type = 'delegated_turn'")
            ).scalar_one()
    finally:
        current_user_id.reset(token)
    assert visible == 0

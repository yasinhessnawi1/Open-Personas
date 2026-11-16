"""``PgSynthesisRepository.exists`` on Postgres, the deleted case included (review LOW-5).

The synthesis handler asks it after the model call: a persona deleted while the model ran takes
its conversations with it, and nothing may then be written to the graph for them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from persona_api.jobs.handlers.synthesis import PgSynthesisRepository, SynthesisJobPayload
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

_OWNER = "user_exists"
_PERSONA = "persona_0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e"
_CONVO = "conv_exists"
_PAYLOAD = SynthesisJobPayload(
    interaction_kind="conversation",
    interaction_id=_CONVO,
    persona_id=_PERSONA,
    high_water_mark=1,
)


def _exists(engine: Engine, payload: SynthesisJobPayload = _PAYLOAD) -> bool:
    with engine.connect() as conn:
        return PgSynthesisRepository().exists(conn, owner_id=_OWNER, payload=payload)


def test_exists_until_the_persona_and_its_conversation_are_deleted(
    migrated_engine: Engine,
) -> None:
    with migrated_engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES (:o, 'e@x.test')"), {"o": _OWNER})
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :o, 'x: 1')"),
            {"p": _PERSONA, "o": _OWNER},
        )
        conn.execute(
            text("INSERT INTO conversations (id, owner_id, persona_id) VALUES (:c, :o, :p)"),
            {"c": _CONVO, "o": _OWNER, "p": _PERSONA},
        )

    assert _exists(migrated_engine) is True
    assert _exists(migrated_engine, _PAYLOAD.model_copy(update={"interaction_id": "conv_x"})) is (
        False
    )

    with migrated_engine.begin() as conn:
        conn.execute(text("DELETE FROM personas WHERE id = :p"), {"p": _PERSONA})

    assert _exists(migrated_engine) is False, "the conversation went with its persona"

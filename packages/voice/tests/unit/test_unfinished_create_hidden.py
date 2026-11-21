"""A persona create still in progress (or abandoned) is invisible to voice (R9-293 LOW-5).

The api inserts the row with ``create_complete`` false and sets it once the create finished.
Voice reads ``personas`` with raw SQL (the layering line), so each of its two reads carries
the same clause: the token route's ownership check answers 404 and the session's persona load
answers not found, exactly as for a persona that does not exist.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from fastapi import HTTPException
from persona.errors import PersonaNotFoundError
from persona_voice.agent.runner import _load_persona
from persona_voice.config import VoiceConfig
from persona_voice.http.app import _check_persona_ownership
from sqlalchemy import create_engine, text

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy import Engine

OWNER = "user_voice_owner"
DONE = "persona_done"
HALF = "persona_half"
_YAML = """\
schema_version: "1.0"
identity:
  name: Astrid
  role: assistant
  background: A helper for the unfinished create test.
"""


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = create_engine(f"sqlite:///{tmp_path / 'voice.db'}")
    with eng.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE personas (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, "
                "yaml TEXT NOT NULL, create_complete BOOLEAN NOT NULL DEFAULT 1)"
            )
        )
        for pid, complete in ((DONE, True), (HALF, False)):
            conn.execute(
                text(
                    "INSERT INTO personas (id, owner_id, yaml, create_complete) "
                    "VALUES (:id, :o, :y, :c)"
                ),
                {"id": pid, "o": OWNER, "y": _YAML, "c": complete},
            )
    yield eng
    eng.dispose()


def _request(engine: Engine) -> SimpleNamespace:
    state = SimpleNamespace(voice_config=VoiceConfig(edition="cloud"), ownership_engine=engine)
    return SimpleNamespace(app=SimpleNamespace(state=state))


def test_the_token_route_does_not_own_an_unfinished_create(engine: Engine) -> None:
    request = _request(engine)
    _check_persona_ownership(request, persona_id=DONE, user_id=OWNER)  # type: ignore[arg-type]
    with pytest.raises(HTTPException) as refused:
        _check_persona_ownership(request, persona_id=HALF, user_id=OWNER)  # type: ignore[arg-type]
    assert refused.value.status_code == 404


def test_a_session_cannot_load_an_unfinished_create(engine: Engine) -> None:
    assert _load_persona(engine, DONE).identity.name == "Astrid"
    with pytest.raises(PersonaNotFoundError):
        _load_persona(engine, HALF)

"""``delete_persona`` on Postgres, as the request path runs it: persona_app under RLS.

Two things the unit tests pin on SQLite, held here on the real database and role:

- a persona that is some channel's active persona deletes, the channel keeps its owner and
  forgets its pointer (the composite foreign key's plain ``SET NULL`` used to null the NOT
  NULL ``owner_id`` and refuse the delete, after the files were already gone);
- a database refusal leaves every file in place, because the files go inside the row's
  transaction, after the database has accepted the delete and before it commits.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from persona_api.errors import PersonaNotFoundError
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from persona_api.services import persona_service
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_ALICE = "user_alice_delete"
_BOB = "user_bob_delete"
_MINE = "persona_d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1"
_THEIRS = "persona_d2d2d2d2d2d2d2d2d2d2d2d2d2d2d2d2"


@pytest.fixture
def seeded(migrated_engine: Engine, tmp_path: Path) -> Iterator[tuple[Engine, Path]]:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    with migrated_engine.begin() as conn:
        for owner, persona in ((_ALICE, _MINE), (_BOB, _THEIRS)):
            conn.execute(
                text("INSERT INTO users (id, email) VALUES (:o, :e)"),
                {"o": owner, "e": f"{owner}@x.test"},
            )
            conn.execute(
                text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :o, 'x: 1')"),
                {"p": persona, "o": owner},
            )
        conn.execute(
            text(
                "INSERT INTO connector_channels "
                "(id, owner_id, platform, channel_key, active_persona_id) "
                "VALUES ('ch_alice', :o, 'telegram', '42', :p)"
            ),
            {"o": _ALICE, "p": _MINE},
        )
    workspace = tmp_path / "workspace"
    for owner, persona in ((_ALICE, _MINE), (_BOB, _THEIRS)):
        folder = workspace / owner / persona / "uploads"
        folder.mkdir(parents=True)
        (folder / "x.png").write_bytes(b"x")
    rls = make_rls_engine(app_url)
    token = current_user_id.set(_ALICE)
    try:
        yield rls, workspace
    finally:
        current_user_id.reset(token)
        rls.dispose()


def _personas(engine: Engine) -> set[str]:
    with engine.begin() as conn:
        return set(conn.execute(text("SELECT id FROM personas")).scalars())


def test_a_channels_active_persona_deletes_and_the_channel_keeps_its_owner(
    seeded: tuple[Engine, Path], migrated_engine: Engine
) -> None:
    rls, workspace = seeded
    persona_service.delete_persona(
        rls_engine=rls, persona_id=_MINE, workspace_root=workspace, owner_id=_ALICE
    )
    assert _personas(migrated_engine) == {_THEIRS}
    with migrated_engine.begin() as conn:
        row = conn.execute(
            text("SELECT owner_id, active_persona_id FROM connector_channels WHERE id = 'ch_alice'")
        ).one()
    assert (row.owner_id, row.active_persona_id) == (_ALICE, None)
    assert not (workspace / _ALICE / _MINE).exists()
    assert (workspace / _BOB / _THEIRS / "uploads" / "x.png").exists()


def test_another_tenants_persona_is_a_404_and_nothing_moves(
    seeded: tuple[Engine, Path], migrated_engine: Engine
) -> None:
    rls, workspace = seeded
    with pytest.raises(PersonaNotFoundError):
        persona_service.delete_persona(
            rls_engine=rls, persona_id=_THEIRS, workspace_root=workspace, owner_id=_ALICE
        )
    assert _personas(migrated_engine) == {_MINE, _THEIRS}
    assert (workspace / _BOB / _THEIRS / "uploads" / "x.png").exists()


def test_a_database_refusal_leaves_the_files(
    seeded: tuple[Engine, Path], migrated_engine: Engine
) -> None:
    rls, workspace = seeded
    with migrated_engine.begin() as conn:
        conn.execute(
            text(
                "CREATE FUNCTION refuse_delete() RETURNS trigger LANGUAGE plpgsql AS "
                "$$ BEGIN RAISE EXCEPTION 'refused'; END $$"
            )
        )
        conn.execute(
            text(
                "CREATE TRIGGER refuse_persona_delete BEFORE DELETE ON personas "
                "FOR EACH ROW EXECUTE FUNCTION refuse_delete()"
            )
        )
    try:
        with pytest.raises(Exception, match="refused"):
            persona_service.delete_persona(
                rls_engine=rls, persona_id=_MINE, workspace_root=workspace, owner_id=_ALICE
            )
        assert (workspace / _ALICE / _MINE / "uploads" / "x.png").exists()
        assert _personas(migrated_engine) == {_MINE, _THEIRS}
    finally:
        with migrated_engine.begin() as conn:
            conn.execute(text("DROP TRIGGER IF EXISTS refuse_persona_delete ON personas"))
            conn.execute(text("DROP FUNCTION IF EXISTS refuse_delete()"))

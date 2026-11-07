"""``delete_persona`` removes one persona's folder, and only after it knows the persona is yours.

The defect: the folder was removed first, from ``workspace_root / owner_id / persona_id`` joined
raw, with errors ignored, and only then did the database say whether the persona existed. So
an id of ``..`` removed the whole workspace and the caller got a 404 for it.

The route now refuses such ids before the service is reached (test_persona_id_path.py). This
is the second door: the service holds the same line for any caller, on the community engine
where no row level security narrows the lookup to the caller.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from persona_api.db.community import create_community_schema, ensure_owner, make_community_engine
from persona_api.db.models import personas as personas_t
from persona_api.errors import PersonaNotFoundError
from persona_api.services import persona_service
from sqlalchemy import insert, select
from sqlalchemy.engine import Engine

_ALICE = "user_alice"
_BOB = "user_bob"
_MINE = "persona_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_THEIRS = "persona_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    eng = make_community_engine(tmp_path / "community.db")
    create_community_schema(eng)
    ensure_owner(eng, owner_id=_ALICE, email="alice@x.test")
    ensure_owner(eng, owner_id=_BOB, email="bob@x.test")
    with eng.begin() as conn:
        conn.execute(insert(personas_t).values(id=_MINE, owner_id=_ALICE, yaml="x: 1"))
        conn.execute(insert(personas_t).values(id=_THEIRS, owner_id=_BOB, yaml="x: 1"))
    return eng


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    for owner, persona in ((_ALICE, _MINE), (_BOB, _THEIRS)):
        folder = root / owner / persona / "uploads"
        folder.mkdir(parents=True)
        (folder / "x.png").write_bytes(b"x")
    (root / _ALICE / "loose.txt").write_bytes(b"owner level")
    return root


def _files(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def _persona_ids(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        return set(conn.execute(select(personas_t.c.id)).scalars())


@pytest.mark.parametrize("bad", ["..", ".", "", "a/../..", "..\\..", "astrid", f"{_MINE}/.."])
def test_an_id_that_is_not_a_persona_touches_nothing(
    engine: Engine, workspace: Path, bad: str
) -> None:
    before = _files(workspace)
    with pytest.raises(PersonaNotFoundError):
        persona_service.delete_persona(
            rls_engine=engine, persona_id=bad, workspace_root=workspace, owner_id=_ALICE
        )
    assert _files(workspace) == before
    assert _persona_ids(engine) == {_MINE, _THEIRS}


@pytest.mark.parametrize("bad", ["..", ".", "astrid"])
def test_an_id_that_is_not_a_persona_is_refused_before_any_lookup(
    workspace: Path, bad: str
) -> None:
    """The format check stands on its own: no database is consulted for such an id."""

    class _NoDatabase:
        def begin(self) -> object:
            raise AssertionError("a malformed id must not reach the database")

    with pytest.raises(PersonaNotFoundError):
        persona_service.delete_persona(
            rls_engine=_NoDatabase(),  # type: ignore[arg-type]
            persona_id=bad,
            workspace_root=workspace,
            owner_id=_ALICE,
        )


def test_another_owners_persona_is_neither_deleted_nor_emptied(
    engine: Engine, workspace: Path
) -> None:
    before = _files(workspace)
    with pytest.raises(PersonaNotFoundError):
        persona_service.delete_persona(
            rls_engine=engine, persona_id=_THEIRS, workspace_root=workspace, owner_id=_ALICE
        )
    assert _files(workspace) == before
    assert _persona_ids(engine) == {_MINE, _THEIRS}


def test_nothing_on_disk_is_touched_before_the_persona_is_known_to_be_yours(
    engine: Engine, workspace: Path
) -> None:
    """Ownership comes before any file work, even when a folder by that name is yours."""
    stray = workspace / _ALICE / _THEIRS / "uploads"
    stray.mkdir(parents=True)
    (stray / "kept.png").write_bytes(b"x")
    with pytest.raises(PersonaNotFoundError):
        persona_service.delete_persona(
            rls_engine=engine, persona_id=_THEIRS, workspace_root=workspace, owner_id=_ALICE
        )
    assert (stray / "kept.png").exists()


def test_your_own_persona_goes_with_exactly_its_folder(engine: Engine, workspace: Path) -> None:
    persona_service.delete_persona(
        rls_engine=engine, persona_id=_MINE, workspace_root=workspace, owner_id=_ALICE
    )
    assert _persona_ids(engine) == {_THEIRS}
    assert _files(workspace) == {f"{_ALICE}/loose.txt", f"{_BOB}/{_THEIRS}/uploads/x.png"}


def test_a_folder_that_cannot_be_removed_stops_the_delete_and_keeps_the_row(
    engine: Engine, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Files go before the row (D-13-4), so a failure leaves a row the user can delete again."""

    def _refuse(*_args: object, ignore_errors: bool = False, **_kwargs: object) -> None:
        # Behaves like the real call: asked to ignore errors, it swallows the failure.
        if ignore_errors:
            return
        raise PermissionError("in use")

    monkeypatch.setattr(persona_service.shutil, "rmtree", _refuse)
    with pytest.raises(PermissionError):
        persona_service.delete_persona(
            rls_engine=engine, persona_id=_MINE, workspace_root=workspace, owner_id=_ALICE
        )
    assert _persona_ids(engine) == {_MINE, _THEIRS}

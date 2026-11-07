"""A persona id in a URL names one persona, and a ref under it never leaves that persona.

The defect (found in the R9-284 audit, 2026-11-07): ``DELETE /v1/personas/%2E%2E`` reached the
handler as ``..``, and the handler removed ``workspace_root/<owner>/..`` (the whole workspace,
every tenant's files) before it ever looked the persona up. ``%2e`` did the same to all of
the caller's own personas. Separately, an uploads or artifacts ref such as
``../../<other owner>/<their persona>/uploads/x.png`` stayed inside the workspace root, which
is all the storage resolver checks, so it read or deleted another tenant's file.

These run against the real application routes with no database: the persona lookup is
stubbed to the ownership answer a database would give.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig
from persona_api.errors import PersonaNotFoundError
from persona_api.services import persona_service
from persona_api.storage import LocalFileStorage
from persona_api.workspace_paths import PERSONA_ID_PATTERN

_OWNER = "user_alice"
_OTHER_OWNER = "user_bob"
_MINE = "persona_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_MY_OTHER = "persona_cccccccccccccccccccccccccccccccc"
_THEIRS = "persona_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

#: Each of these reaches a ``{persona_id}`` route as something other than one persona id.
_BAD_IDS = [
    "%2E%2E",  # ..
    "%2e%2e",  # ..
    "%2E",  # .
    "%2e",  # .
    ".%2E",  # ..
    "..%5C..",  # ..\..
    "astrid",  # not the minted format
    "persona_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",  # upper case is never minted
    "persona_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",  # one short
    "%00",
]


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    for owner, persona, name in (
        (_OWNER, _MINE, "mine.png"),
        (_OWNER, _MY_OTHER, "my_other.png"),
        (_OTHER_OWNER, _THEIRS, "theirs.png"),
    ):
        folder = root / owner / persona / "uploads"
        folder.mkdir(parents=True)
        (folder / name).write_bytes(b"\x89PNG\r\n\x1a\n" + name.encode())
    return root


@pytest.fixture
def deletes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every persona id the delete service was asked to delete."""
    seen: list[str] = []

    def _delete(**kwargs: Any) -> None:  # noqa: ANN401
        seen.append(str(kwargs["persona_id"]))

    monkeypatch.setattr(persona_service, "delete_persona", _delete)
    return seen


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, workspace_root: Path) -> TestClient:
    app = create_app(
        APIConfig(
            database_url="postgresql+psycopg://super@localhost/persona_shell",
            app_database_url="postgresql+psycopg://persona_app@localhost/persona_shell",
        )
    )

    async def _verify(token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id=token, email=None)

    from persona_api.middleware.rate_limit import InMemoryRateLimitStore, RateLimiter

    app.state.verify_token = _verify
    app.state.rls_engine = None
    app.state.workspace_root = workspace_root
    app.state.file_storage = LocalFileStorage(workspace_root)
    app.state.rate_limiter = RateLimiter(
        InMemoryRateLimitStore(), default_limit=10_000, per_endpoint={}
    )

    def _get_persona(*, rls_engine: Any, persona_id: str) -> dict[str, Any]:  # noqa: ANN401, ARG001
        # What RLS answers for user_alice: her two personas are visible, nothing else is.
        if persona_id in (_MINE, _MY_OTHER):
            return {"id": persona_id, "owner_id": _OWNER, "yaml": ""}
        raise PersonaNotFoundError("persona not found", context={"persona_id": persona_id})

    monkeypatch.setattr(persona_service, "get_persona", _get_persona)
    return TestClient(app)


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {_OWNER}"}


def _every_file(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


# --- the id itself -------------------------------------------------------------------------


@pytest.mark.parametrize("bad", _BAD_IDS)
def test_a_delete_with_anything_but_a_persona_id_never_reaches_the_service(
    client: TestClient, deletes: list[str], workspace_root: Path, bad: str
) -> None:
    before = _every_file(workspace_root)
    resp = client.delete(f"/v1/personas/{bad}", headers=_auth())
    assert resp.status_code in (404, 422), resp.text
    assert deletes == []
    assert _every_file(workspace_root) == before


def test_a_delete_of_a_real_id_still_reaches_the_service(
    client: TestClient, deletes: list[str]
) -> None:
    resp = client.delete(f"/v1/personas/{_MINE}", headers=_auth())
    assert resp.status_code == 204, resp.text
    assert deletes == [_MINE]


def test_an_encoded_slash_in_the_id_is_not_a_persona_route(
    client: TestClient, deletes: list[str], workspace_root: Path
) -> None:
    before = _every_file(workspace_root)
    resp = client.delete(f"/v1/personas/x%2F..%2F..%2F{_OTHER_OWNER}", headers=_auth())
    assert resp.status_code in (404, 405, 422), resp.text
    assert deletes == []
    assert _every_file(workspace_root) == before


def test_every_persona_id_path_parameter_carries_the_one_pattern() -> None:
    """A route added later with a bare ``persona_id: str`` fails here, not in production."""
    app = create_app(
        APIConfig(
            database_url="postgresql+psycopg://super@localhost/persona_shell",
            app_database_url="postgresql+psycopg://persona_app@localhost/persona_shell",
        )
    )
    checked = 0
    for route in app.routes:
        if not isinstance(route, APIRoute) or "{persona_id" not in route.path:
            continue
        params = {p.name: p for p in route.dependant.path_params}
        field = params["persona_id"].field_info
        patterns = [getattr(m, "pattern", None) for m in field.metadata]
        assert PERSONA_ID_PATTERN in patterns, f"{sorted(route.methods)} {route.path}"
        checked += 1
    assert checked >= 20  # the persona routes exist; an empty sweep proves nothing


# --- refs under a persona ------------------------------------------------------------------


_ESCAPING_REFS = [
    f"..%2F..%2F{_OTHER_OWNER}%2F{_THEIRS}%2Fuploads%2Ftheirs.png",
    f"uploads%2F..%2F..%2F..%2F{_OTHER_OWNER}%2F{_THEIRS}%2Fuploads%2Ftheirs.png",
    f"uploads/..%2F..%2F..%2F{_OTHER_OWNER}%2F{_THEIRS}%2Fuploads%2Ftheirs.png",
    f"..%2F{_MY_OTHER}%2Fuploads%2Fmy_other.png",
    f"uploads%2F..%2F..%2F{_MY_OTHER}%2Fuploads%2Fmy_other.png",
    f"%2E%2E%2F{_MY_OTHER}%2Fuploads%2Fmy_other.png",
    f"uploads%5C..%5C..%5C{_MY_OTHER}%5Cuploads%5Cmy_other.png",
]


@pytest.mark.parametrize("ref", _ESCAPING_REFS)
def test_an_upload_ref_never_serves_another_personas_file(client: TestClient, ref: str) -> None:
    resp = client.get(f"/v1/personas/{_MINE}/uploads/{ref}", headers=_auth())
    assert resp.status_code == 404, resp.text
    # The 404 body may echo the caller's own ref back; it must never carry a file's bytes.
    assert b"\x89PNG" not in resp.content


def test_an_upload_ref_inside_the_persona_is_still_served(client: TestClient) -> None:
    resp = client.get(f"/v1/personas/{_MINE}/uploads/uploads/mine.png", headers=_auth())
    assert resp.status_code == 200, resp.text
    assert resp.content.endswith(b"mine.png")


@pytest.mark.parametrize("ref", _ESCAPING_REFS)
def test_an_artifact_delete_never_removes_another_personas_file(
    client: TestClient, workspace_root: Path, ref: str
) -> None:
    before = _every_file(workspace_root)
    resp = client.delete(f"/v1/personas/{_MINE}/artifacts/{ref}", headers=_auth())
    assert resp.status_code == 404, resp.text
    assert _every_file(workspace_root) == before


def test_an_artifact_delete_inside_the_persona_still_works(
    client: TestClient, workspace_root: Path
) -> None:
    resp = client.delete(f"/v1/personas/{_MINE}/artifacts/uploads/mine.png", headers=_auth())
    assert resp.status_code == 204, resp.text
    assert not (workspace_root / _OWNER / _MINE / "uploads" / "mine.png").exists()
    assert (workspace_root / _OTHER_OWNER / _THEIRS / "uploads" / "theirs.png").exists()


def test_the_artifact_list_never_reaches_past_the_persona(client: TestClient) -> None:
    # A persona whose id starts with mine is impossible in the minted format, but the list
    # prefix must still end at the persona folder rather than match by string prefix.
    resp = client.get(f"/v1/personas/{_MINE}/artifacts", headers=_auth())
    assert resp.status_code == 200, resp.text
    assert [i["ref"] for i in resp.json()["items"]] == ["uploads/mine.png"]

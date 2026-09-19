"""Issue #16 - attaching a file to a task or routine hand-off.

A hand-off dialog has no conversation, so the conversation-scoped document branch cannot
take its files. ``scope=task`` stores them persona-scoped in the same ``uploads/``
directory a chat image lands in, and returns the same workspace-relative ref shape the
task contract carries. The chat path is untouched: a document upload that forgets its
conversation still gets the 422 it always got.

No DB: the route's RLS pre-flight and the audit write are stood in for, exactly as the
Spec 14 route sweep does.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig
from persona_api.errors import PersonaNotFoundError
from persona_api.middleware.rate_limit import InMemoryRateLimitStore, RateLimiter
from persona_api.services import audit_service, persona_service
from persona_api.storage import LocalFileStorage

_PERSONA = "astrid"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


@pytest.fixture
def client(
    monkeypatch: pytest.MonkeyPatch, workspace: Path
) -> tuple[TestClient, list[dict[str, Any]]]:
    app = create_app(
        APIConfig(
            database_url="postgresql+psycopg://super@localhost/persona_shell",
            app_database_url="postgresql+psycopg://persona_app@localhost/persona_shell",
        )
    )

    async def _verify(token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id=token, email=None)

    app.state.verify_token = _verify
    app.state.rls_engine = None
    app.state.workspace_root = workspace
    app.state.file_storage = LocalFileStorage(workspace)
    app.state.rate_limiter = RateLimiter(
        InMemoryRateLimitStore(), default_limit=1000, per_endpoint={}
    )

    def _fake_get_persona(*, rls_engine: Any, persona_id: str) -> dict[str, Any]:  # noqa: ANN401, ARG001
        if persona_id == _PERSONA:
            return {"id": _PERSONA, "owner_id": "u1", "yaml": ""}
        raise PersonaNotFoundError("persona not found", context={"persona_id": persona_id})

    recorded: list[dict[str, Any]] = []

    def _record(**kwargs: Any) -> None:  # noqa: ANN401
        recorded.append(kwargs)

    monkeypatch.setattr(persona_service, "get_persona", _fake_get_persona)
    monkeypatch.setattr(audit_service, "record", _record)

    return TestClient(app), recorded


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer u1"}


def test_task_scoped_document_lands_in_the_persona_workspace(
    client: tuple[TestClient, list[dict[str, Any]]], workspace: Path
) -> None:
    c, recorded = client
    resp = c.post(
        f"/v1/personas/{_PERSONA}/uploads",
        headers=_auth(),
        files={"file": ("brief.md", b"# the quarter\nrows here\n", "text/markdown")},
        data={"scope": "task"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # The same ref shape a chat image gets, so the leg's file_read opens it unchanged.
    assert body["workspace_path"].startswith("uploads/")
    assert body["workspace_path"].endswith(".md")
    assert body["filename"] == "brief.md"
    stored = workspace / "u1" / _PERSONA / body["workspace_path"]
    assert stored.read_bytes() == b"# the quarter\nrows here\n"
    assert [r["action"] for r in recorded] == ["upload.create"]


def test_chat_document_upload_still_needs_its_conversation(
    client: tuple[TestClient, list[dict[str, Any]]],
) -> None:
    # The new branch is opt-in. Without scope=task the conversation-scoped guard stands,
    # so a chat upload that lost its conversation_id fails loudly rather than quietly
    # landing somewhere the conversation will never read.
    c, _ = client
    resp = c.post(
        f"/v1/personas/{_PERSONA}/uploads",
        headers=_auth(),
        files={"file": ("brief.md", b"hi", "text/markdown")},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "conversation_id_required"


def test_a_format_the_parsers_do_not_know_is_refused(
    client: tuple[TestClient, list[dict[str, Any]]],
) -> None:
    c, _ = client
    resp = c.post(
        f"/v1/personas/{_PERSONA}/uploads",
        headers=_auth(),
        files={"file": ("thing.bin", b"\x00\x01", "application/octet-stream")},
        data={"scope": "task"},
    )
    assert resp.status_code == 415


def test_cross_tenant_persona_is_404_before_any_bytes_are_written(
    client: tuple[TestClient, list[dict[str, Any]]], workspace: Path
) -> None:
    c, _ = client
    resp = c.post(
        "/v1/personas/someone_elses/uploads",
        headers=_auth(),
        files={"file": ("brief.md", b"hi", "text/markdown")},
        data={"scope": "task"},
    )
    assert resp.status_code == 404
    assert not (workspace / "u1" / "someone_elses").exists()

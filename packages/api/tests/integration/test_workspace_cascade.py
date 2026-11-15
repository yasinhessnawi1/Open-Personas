"""Workspace cascade-delete integration tests (spec 13 T12, D-13-4).

Verifies D-13-4 + D-13-4-v0.1-coarse-cascade: persona delete rmtree's the
``{workspace_root}/{owner_id}/{persona_id}`` subtree (no orphan files);
conversation delete leaves workspace files intact in v0.1 (no per-row image
tracking until ``messages.images`` column ships — per-conversation cleanup
is the v0.2 follow-up, persona delete catches everything at the larger
boundary).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig
from persona_api.middleware.rls_context import make_rls_engine
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine
    from tests.conftest import HashEmbedder384

pytestmark = pytest.mark.integration


_VALID_YAML = """\
schema_version: "1.0"
identity:
  name: Astrid
  role: Norwegian tenancy law assistant
  background: |
    Helps tenants understand husleieloven.
  language_default: en
  constraints: []
"""

# Minimum-valid 1x1 RGB PNG (mirrors test_uploads.py's _TINY_PNG; deterministic
# 69-byte payload that survives Pillow's downscale path).
_TINY_PNG: bytes = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "0000000c49444154789c63f8cfc0000003010100c9fe92ef0000000049454e44ae"
    "426082"
)
# Second image with one byte different so the blake2b content-hash differs
# (otherwise the idempotent content-addressed write collapses to one file).
_TINY_PNG_2: bytes = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "0000000c49444154789c63f8cfc0000003010100c9fe92f00000000049454e44ae"
    "426082"
)


@pytest.fixture
def client(
    migrated_engine: Engine,  # noqa: ARG001 — ensures schema + grants
    embedder: HashEmbedder384,
    tmp_path: Path,
) -> Iterator[tuple[TestClient, str, Path]]:
    """Real FastAPI client wired to Docker Postgres + a tmp_path workspace."""
    import os

    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")

    workspace_root = tmp_path / "workspace"
    cfg = APIConfig(
        app_database_url=app_url,
        audit_root=str(tmp_path / "audit"),
        workspace_root=workspace_root,
    )
    app = create_app(cfg)

    async def _fake_verify(token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id=token, email=None)

    user_id = "user_t12_cascade"
    with TestClient(app) as c:
        app.state.verify_token = _fake_verify
        app.state.embedder = embedder
        # Drop the lifespan-installed TierRegistry so the persona-detail
        # capabilities surface doesn't lazily instantiate a real chat backend
        # (AuthenticationError("missing API key") on CI without ANTHROPIC_API_KEY).
        if hasattr(app.state, "tier_registry"):
            app.state.tier_registry = None
        su = make_rls_engine(os.environ["DATABASE_URL"])
        with su.begin() as conn:
            conn.execute(
                text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
                {"i": user_id, "e": f"{user_id}@x.test"},
            )
        su.dispose()
        yield c, user_id, workspace_root
        su = make_rls_engine(os.environ["DATABASE_URL"])
        with su.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE id = :i"), {"i": user_id})
        su.dispose()


def _auth(user_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {user_id}"}


def _create_persona(c: TestClient, user_id: str) -> str:
    resp = c.post("/v1/personas", json={"yaml": _VALID_YAML}, headers=_auth(user_id))
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def _upload(c: TestClient, user_id: str, persona_id: str, payload: bytes) -> str:
    resp = c.post(
        f"/v1/personas/{persona_id}/uploads",
        files={"file": ("tiny.png", payload, "image/png")},
        headers=_auth(user_id),
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["workspace_path"])


def test_delete_conversation_leaves_workspace_intact_v01(
    client: tuple[TestClient, str, Path],
) -> None:
    """D-13-4-v0.1-coarse-cascade: conversation delete does NOT touch workspace files."""
    c, user_id, workspace_root = client
    pid = _create_persona(c, user_id)
    ref_a = _upload(c, user_id, pid, _TINY_PNG)
    ref_b = _upload(c, user_id, pid, _TINY_PNG_2)
    resp = c.post(f"/v1/personas/{pid}/conversations", json={}, headers=_auth(user_id))
    assert resp.status_code == 201, resp.text
    conv_id = resp.json()["id"]
    file_a = workspace_root / user_id / pid / ref_a
    file_b = workspace_root / user_id / pid / ref_b
    assert file_a.is_file()
    assert file_b.is_file()
    resp = c.delete(f"/v1/conversations/{conv_id}", headers=_auth(user_id))
    assert resp.status_code == 204
    # v0.1 trade-off: workspace files survive a conversation delete.
    assert file_a.is_file()
    assert file_b.is_file()


def test_delete_persona_cleans_workspace_images(
    client: tuple[TestClient, str, Path],
) -> None:
    """Persona delete removes every workspace file under the persona (R9-284: by the erasure,
    which runs inline here because this app starts no worker)."""
    c, user_id, workspace_root = client
    pid = _create_persona(c, user_id)
    for _ in range(2):
        resp = c.post(f"/v1/personas/{pid}/conversations", json={}, headers=_auth(user_id))
        assert resp.status_code == 201
    ref_a = _upload(c, user_id, pid, _TINY_PNG)
    ref_b = _upload(c, user_id, pid, _TINY_PNG_2)
    persona_root = workspace_root / user_id / pid
    file_a = workspace_root / user_id / pid / ref_a
    file_b = workspace_root / user_id / pid / ref_b
    assert file_a.is_file()
    assert file_b.is_file()
    resp = c.delete(f"/v1/personas/{pid}", headers=_auth(user_id))
    assert resp.status_code == 202
    assert not file_a.exists()
    assert not file_b.exists()
    assert not persona_root.exists()

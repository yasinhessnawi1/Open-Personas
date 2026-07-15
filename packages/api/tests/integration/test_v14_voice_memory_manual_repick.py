"""V14-T5b — manual re-pick also records per-provider voice memory (review finding I1).

The editor's ``VoiceSelector`` never calls ``persona_service.set_voice`` — it
PATCHes the persona's full YAML (Spec V14 phase-notes' verified call chain:
``VoiceSelector.onChange`` -> ``PersonaForm`` -> ``PersonaEditor`` -> the
autosave PATCH). This is the choke point's second half
(``_remember_voice_by_provider``, wired into both ``create_persona`` and
``update_persona``): a REAL create + PATCH through the actual routes must
persist ``identity.voice_by_provider`` exactly like the programmatic
auto-pick/auto-remap path does through ``set_voice``.

Real FastAPI ``TestClient`` + real Postgres (skips when ``APP_DATABASE_URL`` is
unset) — mirrors ``test_avatar_provenance.py``'s client fixture.
"""

# ruff: noqa: ARG001 — ``migrated_engine`` fixture param drives schema-migration ordering only.
from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
import yaml as pyyaml
from fastapi.testclient import TestClient
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig
from persona_api.middleware.rls_context import make_rls_engine
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy.engine import Engine
    from tests.conftest import HashEmbedder384

pytestmark = pytest.mark.integration

_USER = "u_voice_memory_repick"

_YAML_WITH_CARTESIA_VOICE = """\
schema_version: "1.0"
identity:
  name: Astrid
  role: Norwegian tenancy law assistant
  background: |
    Helps tenants understand husleieloven.
  language_default: en
  voice: cartesia:builder-voice
"""


@pytest.fixture
def client(
    migrated_engine: Engine, embedder: HashEmbedder384, tmp_path: Path
) -> Iterator[tuple[TestClient, Engine]]:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")

    cfg = APIConfig(
        app_database_url=app_url,
        audit_root=str(tmp_path / "audit"),
        workspace_root=tmp_path / "workspace",
    )
    app = create_app(cfg)

    async def _fake_verify(token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id=token, email=None)

    with TestClient(app) as c:
        app.state.verify_token = _fake_verify
        app.state.embedder = embedder
        # Disable the async voice auto-pick/avatar background hooks entirely
        # (test_avatar_provenance.py's precedent) — this test is about the
        # SYNCHRONOUS manual re-pick write path, not the background hooks.
        if hasattr(app.state, "tier_registry"):
            app.state.tier_registry = None

        su = make_rls_engine(os.environ["DATABASE_URL"])
        with su.begin() as conn:
            conn.execute(
                text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
                {"i": _USER, "e": f"{_USER}@x.test"},
            )
        yield c, su
        with su.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE id = :i"), {"i": _USER})
        su.dispose()


def _auth(user_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {user_id}"}


def _read_identity(su: Engine, persona_id: str) -> dict[str, object]:
    with su.begin() as conn:
        row = conn.execute(
            text("SELECT yaml FROM personas WHERE id = :i"), {"i": persona_id}
        ).scalar_one()
    parsed = pyyaml.safe_load(row)
    identity = parsed["identity"]
    assert isinstance(identity, dict)
    return identity


def test_create_with_a_builder_authored_voice_records_the_memory(
    client: tuple[TestClient, Engine],
) -> None:
    c, su = client
    resp = c.post("/v1/personas", json={"yaml": _YAML_WITH_CARTESIA_VOICE}, headers=_auth(_USER))
    assert resp.status_code == 201, resp.text
    pid = resp.json()["id"]

    identity = _read_identity(su, pid)
    assert identity["voice"] == "cartesia:builder-voice"
    assert identity["voice_by_provider"] == {"cartesia": "builder-voice"}


def test_manual_repick_via_patch_records_the_new_providers_memory_and_keeps_the_old(
    client: tuple[TestClient, Engine],
) -> None:
    """The headline case: a manual re-pick in the editor (a full-YAML PATCH,
    never touching ``set_voice``) still ends up remembered — AND the persona's
    PRIOR provider's voice survives the merge (not clobbered)."""
    c, su = client
    created = c.post("/v1/personas", json={"yaml": _YAML_WITH_CARTESIA_VOICE}, headers=_auth(_USER))
    assert created.status_code == 201, created.text
    pid = created.json()["id"]

    repicked_yaml = _YAML_WITH_CARTESIA_VOICE.replace(
        "voice: cartesia:builder-voice", "voice: elevenlabs:manually-repicked"
    )
    patched = c.patch(f"/v1/personas/{pid}", json={"yaml": repicked_yaml}, headers=_auth(_USER))
    assert patched.status_code == 200, patched.text

    identity = _read_identity(su, pid)
    assert identity["voice"] == "elevenlabs:manually-repicked"
    assert identity["voice_by_provider"] == {
        "cartesia": "builder-voice",  # the ORIGINAL, still remembered
        "elevenlabs": "manually-repicked",  # the NEW pick, recorded
    }

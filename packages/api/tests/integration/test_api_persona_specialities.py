"""Spec S3 T2 (S3-D-2/D-3): the persona-scoped specialities + consent HTTP surface.

Against real Postgres (``migrated_engine``) + a TestClient, proves the endpoint
boundary — especially the **forge-prevention** invariant: the client sends only
``{granted}``; the ``content_hash`` consent binds to and the trust tier are
server-derived (a request that tries to supply either is rejected, and the stored
hash is the server's, not the client's). Plus consent_state rendering, the
non-gated/unknown/unowned guards, and the end-to-end re-gate on a body change.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from persona.schema.skills import SkillProvenance, SkillSpec, SkillTrust
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from persona_api.services import catalog_service, persona_service
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.engine import Engine
    from tests.conftest import HashEmbedder384

pytestmark = pytest.mark.integration

_H = "hash_v1_deadbeef"
_H_PRIME = "hash_v2_cafef00d"

_GATED = SkillSpec(
    name="legal_research",
    description="External legal research helper.",
    path=Path("/x/legal_research"),
    when_to_use="Use for case-law lookups.",
    trust=SkillTrust.THIRD_PARTY,
    provenance=SkillProvenance(
        source="github:acme/skills",
        source_uri="https://github.com/acme/skills",
        source_ref="abc123",
        content_hash=_H,
    ),
)
_FREE = SkillSpec(
    name="code_review",
    description="Builtin code review.",
    path=Path("/x/code_review"),
    trust=SkillTrust.BUILTIN,
    provenance=SkillProvenance(source="builtin", content_hash="builtin_hash"),
)

_YAML = """\
schema_version: "1.0"
identity:
  name: Astrid
  role: assistant
  background: A helper for the S3 specialities endpoint test.
"""


@pytest.fixture
def client(
    migrated_engine: Engine,  # noqa: ARG001 — ensures schema + persona_app grants
    embedder: HashEmbedder384,
    tmp_path: Path,
) -> Iterator[tuple[TestClient, str]]:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    cfg = APIConfig(app_database_url=app_url, audit_root=str(tmp_path / "audit"))
    app = create_app(cfg)

    async def _fake_verify(token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id=token, email=None)

    user_id = "user_s3_ep"
    with TestClient(app) as c:
        app.state.verify_token = _fake_verify
        app.state.embedder = embedder
        su = make_rls_engine(os.environ["DATABASE_URL"])
        with su.begin() as conn:
            conn.execute(
                text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
                {"i": user_id, "e": f"{user_id}@x.test"},
            )
        su.dispose()
        yield c, user_id
        su = make_rls_engine(os.environ["DATABASE_URL"])
        with su.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE id = :i"), {"i": user_id})
        su.dispose()


@pytest.fixture
def _catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject a gated (third_party) + a free (builtin) speciality into the catalog."""
    monkeypatch.setattr(catalog_service, "list_specialities", lambda **_kw: [_GATED, _FREE])


def _auth(user_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {user_id}"}


def _make_persona(user_id: str, embedder: HashEmbedder384, tmp_path: Path) -> str:
    engine = make_rls_engine(os.environ["APP_DATABASE_URL"])
    tok = current_user_id.set(user_id)
    try:
        return persona_service.create_persona(
            rls_engine=engine,
            embedder=embedder,
            audit_root=tmp_path / "seed_audit",
            owner_id=user_id,
            yaml_str=_YAML,
        )
    finally:
        current_user_id.reset(tok)
        engine.dispose()


@pytest.mark.usefixtures("_catalog")
def test_get_specialities_returns_consent_state(
    client: tuple[TestClient, str], embedder: HashEmbedder384, tmp_path: Path
) -> None:
    c, uid = client
    pid = _make_persona(uid, embedder, tmp_path)
    resp = c.get(f"/v1/personas/{pid}/specialities", headers=_auth(uid))
    assert resp.status_code == 200, resp.text
    rows = {s["name"]: s for s in resp.json()}
    # gated, never consented → "none"; tier + hash carried from the catalog.
    assert rows["legal_research"]["requires_consent"] is True
    assert rows["legal_research"]["trust"] == "third_party"
    assert rows["legal_research"]["content_hash"] == _H
    assert rows["legal_research"]["consent_state"] == "none"
    # builtin → never gated.
    assert rows["code_review"]["consent_state"] == "not_required"


@pytest.mark.usefixtures("_catalog")
def test_post_consent_grants_and_stores_server_derived_hash(
    client: tuple[TestClient, str], embedder: HashEmbedder384, tmp_path: Path
) -> None:
    c, uid = client
    pid = _make_persona(uid, embedder, tmp_path)
    resp = c.post(
        f"/v1/personas/{pid}/skills/legal_research/consent",
        json={"granted": True},
        headers=_auth(uid),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["consent_state"] == "granted"

    # The stored hash is the SERVER's current catalog hash — not anything the client sent.
    engine = make_rls_engine(os.environ["APP_DATABASE_URL"])
    tok = current_user_id.set(uid)
    try:
        with engine.begin() as conn:
            stored = conn.execute(
                text(
                    "SELECT content_hash, granted FROM skill_consents "
                    "WHERE persona_id = :p AND skill_name = :s"
                ),
                {"p": pid, "s": "legal_research"},
            ).one()
    finally:
        current_user_id.reset(tok)
        engine.dispose()
    assert stored[0] == _H  # server-derived
    assert stored[1] is True


@pytest.mark.usefixtures("_catalog")
def test_client_cannot_supply_content_hash_or_tier(
    client: tuple[TestClient, str], embedder: HashEmbedder384, tmp_path: Path
) -> None:
    """Forge-prevention: the request model forbids extras → a client-supplied hash/tier is 422."""
    c, uid = client
    pid = _make_persona(uid, embedder, tmp_path)
    forged_hash = c.post(
        f"/v1/personas/{pid}/skills/legal_research/consent",
        json={"granted": True, "content_hash": "forged_by_client"},
        headers=_auth(uid),
    )
    assert forged_hash.status_code == 422
    forged_tier = c.post(
        f"/v1/personas/{pid}/skills/legal_research/consent",
        json={"granted": True, "trust": "vetted"},
        headers=_auth(uid),
    )
    assert forged_tier.status_code == 422


@pytest.mark.usefixtures("_catalog")
def test_post_consent_on_non_gated_skill_is_rejected(
    client: tuple[TestClient, str], embedder: HashEmbedder384, tmp_path: Path
) -> None:
    c, uid = client
    pid = _make_persona(uid, embedder, tmp_path)
    resp = c.post(
        f"/v1/personas/{pid}/skills/code_review/consent",
        json={"granted": True},
        headers=_auth(uid),
    )
    assert resp.status_code == 400  # builtin/vetted does not take consent


@pytest.mark.usefixtures("_catalog")
def test_post_consent_unknown_skill_is_404(
    client: tuple[TestClient, str], embedder: HashEmbedder384, tmp_path: Path
) -> None:
    c, uid = client
    pid = _make_persona(uid, embedder, tmp_path)
    resp = c.post(
        f"/v1/personas/{pid}/skills/does_not_exist/consent",
        json={"granted": True},
        headers=_auth(uid),
    )
    assert resp.status_code == 404


@pytest.mark.usefixtures("_catalog")
def test_specialities_on_unowned_persona_is_404(client: tuple[TestClient, str]) -> None:
    c, uid = client
    resp = c.get("/v1/personas/persona_not_mine/specialities", headers=_auth(uid))
    assert resp.status_code == 404
    resp2 = c.post(
        "/v1/personas/persona_not_mine/skills/legal_research/consent",
        json={"granted": True},
        headers=_auth(uid),
    )
    assert resp2.status_code == 404


@pytest.mark.usefixtures("_catalog")
def test_body_change_regates_granted_to_stale_end_to_end(
    client: tuple[TestClient, str],
    embedder: HashEmbedder384,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Grant at H → GET shows granted; the body changes to H' → GET shows stale (S1-D-5)."""
    c, uid = client
    pid = _make_persona(uid, embedder, tmp_path)
    grant = c.post(
        f"/v1/personas/{pid}/skills/legal_research/consent",
        json={"granted": True},
        headers=_auth(uid),
    )
    assert grant.status_code == 200
    assert grant.json()["consent_state"] == "granted"

    # The upstream body changed → the catalog now reports a new hash for the same skill.
    regated = SkillSpec(
        name="legal_research",
        description=_GATED.description,
        path=_GATED.path,
        trust=SkillTrust.THIRD_PARTY,
        provenance=SkillProvenance(source="github:acme/skills", content_hash=_H_PRIME),
    )
    monkeypatch.setattr(catalog_service, "list_specialities", lambda **_kw: [regated, _FREE])
    resp = c.get(f"/v1/personas/{pid}/specialities", headers=_auth(uid))
    rows = {s["name"]: s for s in resp.json()}
    assert rows["legal_research"]["content_hash"] == _H_PRIME
    assert rows["legal_research"]["consent_state"] == "stale"  # prior consent no longer valid

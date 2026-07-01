"""Spec S3 T2 (S3-D-1/D-2): the real skill-consent store — the security gate.

Against real Postgres (migration ``026_skill_consents`` via ``migrated_engine``) and
the **non-superuser ``persona_app`` role** (``make_rls_engine`` over ``APP_DATABASE_URL``
— RLS bypasses superusers, so a superuser test would false-green), this proves the
load-bearing invariants the whole consent model rests on:

- **empty store ≡ default-deny, non-vacuously** — the SAME ``(persona, skill, hash)``
  that denies with no row is ENABLED once a grant is recorded, so the deny is not an
  empty-table false pass; and the real store over an empty table returns exactly what
  the S1 ``DenyUnvettedConsent`` stub returns (swapping the stub in never opens access);
- **content-hash re-gate (S1-D-5)** — a grant at hash ``H`` enables at ``H`` but is
  denied at a changed hash ``H'``, and ``consent_state`` flips ``granted → stale``;
- **cross-tenant RLS, non-vacuously** — owner B's consent is visible to B (non-vacuity)
  and invisible to A (isolation), under the non-superuser role.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from persona.skills import DenyUnvettedConsent
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from persona_api.services import persona_service, skill_consent_service
from persona_api.services.skill_consent_service import PostgresSkillConsentStore
from sqlalchemy import text

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine
    from tests.conftest import HashEmbedder384

pytestmark = pytest.mark.integration

_SKILL = "legal_research"
_H = "hash_v1_deadbeef"
_H_PRIME = "hash_v2_cafef00d"  # a body change → a new content hash
_T0 = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)

_YAML = """\
schema_version: "1.0"
identity:
  name: Astrid
  role: assistant
  background: A helper for the S3 consent-store test.
"""


def _app_engine() -> Engine:
    """The non-superuser ``persona_app`` engine — the one RLS actually constrains."""
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set (non-superuser role required for RLS)")
    return make_rls_engine(app_url)


def _seed_user_superuser(owner: str) -> None:
    su = make_rls_engine(os.environ["DATABASE_URL"])
    with su.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
            {"i": owner, "e": f"{owner}@x.test"},
        )
    su.dispose()


def _make_persona(engine: Engine, embedder: HashEmbedder384, audit: Path, owner: str) -> str:
    return persona_service.create_persona(
        rls_engine=engine,
        embedder=embedder,
        audit_root=audit,
        owner_id=owner,
        yaml_str=_YAML,
    )


@pytest.mark.usefixtures("migrated_engine")
def test_empty_store_denies_non_vacuously_and_matches_deny_stub(
    embedder: HashEmbedder384, tmp_path: Path
) -> None:
    """No consent row ≡ DenyUnvettedConsent; a recorded grant then ENABLES the same triple."""
    engine = _app_engine()
    owner = "user_s3_empty"
    _seed_user_superuser(owner)
    tok = current_user_id.set(owner)
    try:
        persona_id = _make_persona(engine, embedder, tmp_path / "audit", owner)
        store = PostgresSkillConsentStore(engine)
        stub = DenyUnvettedConsent()

        # Empty table: the real store denies exactly like the S1 stub (default-deny).
        assert store.is_enabled(persona_id, _SKILL, _H) is False
        assert stub.is_enabled(persona_id, _SKILL, _H) is False

        # Non-vacuity: a recorded grant OPENS the same triple for the real store only —
        # so the deny above was a real gate, not an empty-table false pass. The stub
        # never opens (that is the whole point of swapping it for the real store).
        skill_consent_service.record_consent(
            rls_engine=engine,
            persona_id=persona_id,
            skill_name=_SKILL,
            content_hash=_H,
            granted=True,
            now=_T0,
        )
        assert store.is_enabled(persona_id, _SKILL, _H) is True
        assert stub.is_enabled(persona_id, _SKILL, _H) is False
    finally:
        current_user_id.reset(tok)


@pytest.mark.usefixtures("migrated_engine")
def test_content_hash_change_regates_granted_to_stale(
    embedder: HashEmbedder384, tmp_path: Path
) -> None:
    """A grant at H is enabled at H but denied at H'; consent_state flips granted→stale (S1-D-5)."""
    engine = _app_engine()
    owner = "user_s3_regate"
    _seed_user_superuser(owner)
    tok = current_user_id.set(owner)
    try:
        persona_id = _make_persona(engine, embedder, tmp_path / "audit", owner)
        store = PostgresSkillConsentStore(engine)
        skill_consent_service.record_consent(
            rls_engine=engine,
            persona_id=persona_id,
            skill_name=_SKILL,
            content_hash=_H,
            granted=True,
            now=_T0,
        )

        # At the consented hash: enabled + "granted".
        assert store.is_enabled(persona_id, _SKILL, _H) is True
        assert (
            skill_consent_service.consent_state_for(
                rls_engine=engine,
                persona_id=persona_id,
                skill_name=_SKILL,
                current_hash=_H,
                requires_consent=True,
            )
            == skill_consent_service.CONSENT_GRANTED
        )

        # The body changed → the runtime now asks with H': denied, and the surface
        # shows "stale" (re-gate), NEVER still-consented.
        assert store.is_enabled(persona_id, _SKILL, _H_PRIME) is False
        assert (
            skill_consent_service.consent_state_for(
                rls_engine=engine,
                persona_id=persona_id,
                skill_name=_SKILL,
                current_hash=_H_PRIME,
                requires_consent=True,
            )
            == skill_consent_service.CONSENT_STALE
        )

        # A revoke event (append-only) → "none" (default-deny), latest event wins.
        skill_consent_service.record_consent(
            rls_engine=engine,
            persona_id=persona_id,
            skill_name=_SKILL,
            content_hash=_H,
            granted=False,
            now=_T0 + timedelta(minutes=1),
        )
        assert store.is_enabled(persona_id, _SKILL, _H) is False
        assert (
            skill_consent_service.consent_state_for(
                rls_engine=engine,
                persona_id=persona_id,
                skill_name=_SKILL,
                current_hash=_H,
                requires_consent=True,
            )
            == skill_consent_service.CONSENT_NONE
        )
    finally:
        current_user_id.reset(tok)


@pytest.mark.usefixtures("migrated_engine")
def test_not_required_tier_is_never_gated(embedder: HashEmbedder384, tmp_path: Path) -> None:
    """A builtin/vetted skill (requires_consent=False) is always ``not_required``."""
    engine = _app_engine()
    owner = "user_s3_free"
    _seed_user_superuser(owner)
    tok = current_user_id.set(owner)
    try:
        persona_id = _make_persona(engine, embedder, tmp_path / "audit", owner)
        assert (
            skill_consent_service.consent_state_for(
                rls_engine=engine,
                persona_id=persona_id,
                skill_name="code_review",
                current_hash=_H,
                requires_consent=False,
            )
            == skill_consent_service.CONSENT_NOT_REQUIRED
        )
    finally:
        current_user_id.reset(tok)


@pytest.mark.usefixtures("migrated_engine")
def test_consent_is_rls_isolated_across_tenants_non_vacuously(
    embedder: HashEmbedder384, tmp_path: Path
) -> None:
    """B's consent is visible to B (non-vacuity) and invisible to A (isolation), non-superuser."""
    engine = _app_engine()
    _seed_user_superuser("user_s3_b")
    _seed_user_superuser("user_s3_a")

    # Owner B grants consent for its own persona.
    tok_b = current_user_id.set("user_s3_b")
    try:
        persona_b = _make_persona(engine, embedder, tmp_path / "audit_b", "user_s3_b")
        skill_consent_service.record_consent(
            rls_engine=engine,
            persona_id=persona_b,
            skill_name=_SKILL,
            content_hash=_H,
            granted=True,
            now=_T0,
        )
        store = PostgresSkillConsentStore(engine)
        # Non-vacuity: under B's scope the grant IS visible.
        assert store.is_enabled(persona_b, _SKILL, _H) is True
    finally:
        current_user_id.reset(tok_b)

    # Owner A cannot see B's consent row (RLS through the persona FK-chain).
    tok_a = current_user_id.set("user_s3_a")
    try:
        _make_persona(engine, embedder, tmp_path / "audit_a", "user_s3_a")
        store = PostgresSkillConsentStore(engine)
        # Isolation: B's persona/consent is invisible under A → denied.
        assert store.is_enabled(persona_b, _SKILL, _H) is False
        assert (
            skill_consent_service.consent_state_for(
                rls_engine=engine,
                persona_id=persona_b,
                skill_name=_SKILL,
                current_hash=_H,
                requires_consent=True,
            )
            == skill_consent_service.CONSENT_NONE
        )
    finally:
        current_user_id.reset(tok_a)

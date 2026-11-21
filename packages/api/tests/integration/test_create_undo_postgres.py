"""A failed persona create is undone on the hosted path, on Postgres (R9-293 review MEDIUM-3).

The create runs as production does: the personas row and the identity chunks through the
``persona_app`` row-level-security engine under the owner's GUC, and the undo
(``erase_persona_now``) captures through that same engine and erases on ``persona_platform``
(BYPASSRLS, not a superuser): fence, row, memory, graph, files, rows. The identity index writes
its chunks and then fails; afterwards no row, no chunk and no open erasure is left, and the
graph step ran. Runs in CI.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from persona.registry import PersonaRegistry
from persona.stores.postgres import PostgresBackend
from persona_api.config import APIConfig
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from persona_api.services import erasure_start, persona_service
from persona_api.services.erasure_ledger import ErasureLedger, ErasureState, SubjectKind
from sqlalchemy import create_engine, event, text

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

_OWNER = "user_create_undo"
_YAML = """\
schema_version: "1.0"
identity:
  name: Astrid
  role: Norwegian tenancy law assistant
  background: |
    Helps tenants understand husleieloven.
  language_default: en
self_facts:
  - fact: Specialised in Norwegian residential tenancy.
    confidence: 1.0
worldview: []
"""
_PLATFORM_GRANTS = (
    "GRANT USAGE ON SCHEMA public TO persona_platform",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO persona_platform",
    "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO persona_platform",
)


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:  # noqa: ARG001 - migrations first
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (non-superuser role) not set")
    engine = make_rls_engine(app_url)
    yield engine
    engine.dispose()


@pytest.fixture
def platform(migrated_engine: Engine, database_url: str) -> Iterator[Engine]:
    """``persona_platform`` (BYPASSRLS, not a superuser), taken with SET ROLE, as in production."""
    with migrated_engine.begin() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_roles WHERE rolname = 'persona_platform'")
        ).first()
        if exists is None:
            conn.execute(text("CREATE ROLE persona_platform NOLOGIN BYPASSRLS"))
        conn.execute(text("GRANT persona_platform TO CURRENT_USER"))
        for grant in _PLATFORM_GRANTS:
            conn.execute(text(grant))
    engine = create_engine(database_url)

    @event.listens_for(engine, "connect")
    def _become_platform(dbapi_connection: Any, _record: Any) -> None:  # noqa: ANN401
        with dbapi_connection.cursor() as cursor:
            cursor.execute("SET ROLE persona_platform")

    yield engine
    engine.dispose()


def test_a_failed_create_is_erased_through_the_hosted_path(
    migrated_engine: Engine,
    app_engine: Engine,
    platform: Engine,
    embedder: Any,  # noqa: ANN401
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with migrated_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:o, 'undo@x.test') ON CONFLICT DO NOTHING"),
            {"o": _OWNER},
        )
    real_load = PersonaRegistry.load_persona
    created: list[str] = []

    def _writes_then_fails(self: PersonaRegistry, persona: Any) -> None:  # noqa: ANN401
        created.append(str(persona.persona_id))
        real_load(self, persona)  # the identity chunks are written, on the RLS engine
        msg = "the identity index went away"
        raise RuntimeError(msg)

    monkeypatch.setattr(PersonaRegistry, "load_persona", _writes_then_fails)
    audit_root = tmp_path / "audit"
    audit_root.mkdir()
    state = SimpleNamespace(
        platform_engine=platform,
        embedder=embedder,
        config=APIConfig(audit_root=str(audit_root)),
        file_storage=None,
        workspace_root=tmp_path / "workspace",
    )
    token = current_user_id.set(_OWNER)
    try:
        with pytest.raises(RuntimeError, match="identity index"):
            persona_service.create_persona(
                rls_engine=app_engine,
                embedder=embedder,
                audit_root=audit_root,
                owner_id=_OWNER,
                yaml_str=_YAML,
                memory_backend=PostgresBackend(engine=app_engine, embedder=embedder),
                compensate=lambda pid: erasure_start.erase_persona_now(
                    state, rls_engine=app_engine, owner_id=_OWNER, persona_id=pid
                ),
            )
    finally:
        current_user_id.reset(token)

    (pid,) = created
    with migrated_engine.connect() as conn:
        rows = conn.execute(text("SELECT count(*) FROM personas WHERE id = :p"), {"p": pid})
        chunks = conn.execute(
            text("SELECT count(*) FROM memory_chunks WHERE persona_id = :p"), {"p": pid}
        )
        assert rows.scalar_one() == 0, "no persona row"
        assert chunks.scalar_one() == 0, "no memory chunk"
    erasure = ErasureLedger(migrated_engine).get(SubjectKind.PERSONA, pid)
    assert erasure is not None
    assert erasure.state is ErasureState.COMPLETE
    assert "graph" in erasure.counts, "the graph step ran"
    assert sum(erasure.counts["memory"].values()) > 0, "the identity chunks were deleted"

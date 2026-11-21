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
import threading
import time
import uuid
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from persona.registry import PersonaRegistry
from persona.stores.postgres import PostgresBackend
from persona_api.config import APIConfig
from persona_api.jobs.handlers.persona_erase import PersonaEraseSweep
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


def _owner(migrated_engine: Engine) -> None:
    with migrated_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:o, 'undo@x.test') ON CONFLICT DO NOTHING"),
            {"o": _OWNER},
        )


def test_the_sweep_claims_an_abandoned_create_as_persona_platform(
    migrated_engine: Engine, platform: Engine
) -> None:
    """Round 3, Finding 7: the claim on Postgres, under the platform role's grants."""
    _owner(migrated_engine)
    pid = f"persona_{uuid.uuid4().hex}"
    with migrated_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO personas (id, owner_id, yaml, create_complete, created_at) "
                "VALUES (:p, :o, 'x: 1', false, now() - interval '1 hour')"
            ),
            {"p": pid, "o": _OWNER},
        )

    PersonaEraseSweep(
        engine=platform, run=lambda _erasure, _token: None, find_leftovers=lambda *_a, **_k: []
    )()

    with migrated_engine.connect() as conn:
        rows = conn.execute(text("SELECT count(*) FROM personas WHERE id = :p"), {"p": pid})
        assert rows.scalar_one() == 0, "the abandoned row is gone"
    erasure = ErasureLedger(migrated_engine).get(SubjectKind.PERSONA, pid)
    assert erasure is not None, "its erasure is claimed"
    assert erasure.counts.get("row") == {"personas": 1}


def test_a_create_finishing_under_the_sweeps_lock_matches_nothing_and_is_undone(
    migrated_engine: Engine,
    app_engine: Engine,
    platform: Engine,
    embedder: Any,  # noqa: ANN401
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 3, Finding 7: two connections. The sweep holds the row lock; the final UPDATE waits.

    The create's identity is indexed, then the sweep locks the still incomplete row. The create's
    final UPDATE blocks on that lock; the sweep, seeing it wait, deletes the row and claims the
    erasure. The UPDATE then matches no row, so the create fails and its undo runs.
    """
    _owner(migrated_engine)
    committed, locked = threading.Event(), threading.Event()
    real_load = PersonaRegistry.load_persona
    created: list[str] = []

    def _indexed_then_wait(self: PersonaRegistry, persona: Any) -> None:  # noqa: ANN401
        real_load(self, persona)  # the chunks' FK check runs before the sweep takes the lock
        created.append(str(persona.persona_id))
        committed.set()
        assert locked.wait(20), "the sweep never took the lock"

    def _update_waiting() -> bool:
        with migrated_engine.connect() as conn:
            waiting = conn.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                    "AND query LIKE 'UPDATE personas%'"
                )
            )
            return int(waiting.scalar_one()) > 0

    capture = erasure_start.capture_persona

    def _capture_once_the_update_waits(conn: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        locked.set()  # the row is locked FOR UPDATE by now
        deadline = time.monotonic() + 20
        while not _update_waiting():
            assert time.monotonic() < deadline, "the final UPDATE never waited on the lock"
            time.sleep(0.05)
        return capture(conn, **kwargs)

    monkeypatch.setattr(PersonaRegistry, "load_persona", _indexed_then_wait)
    monkeypatch.setattr(erasure_start, "capture_persona", _capture_once_the_update_waits)
    undone: list[str] = []
    swept: list[object] = []

    def _sweep() -> None:
        assert committed.wait(20)
        swept.append(
            erasure_start.claim_abandoned_create(platform, owner_id=_OWNER, persona_id=created[0])
        )

    sweeper = threading.Thread(target=_sweep)
    sweeper.start()
    audit_root = tmp_path / "audit"
    audit_root.mkdir()
    token = current_user_id.set(_OWNER)
    try:
        with pytest.raises(RuntimeError, match="swept before it finished"):
            persona_service.create_persona(
                rls_engine=app_engine,
                embedder=embedder,
                audit_root=audit_root,
                owner_id=_OWNER,
                yaml_str=_YAML,
                memory_backend=PostgresBackend(engine=app_engine, embedder=embedder),
                compensate=undone.append,
            )
    finally:
        current_user_id.reset(token)
        sweeper.join(30)

    (pid,) = created
    assert undone == [pid], "the create's undo ran"
    assert len(swept) == 1
    assert swept[0] is not None, "the sweep claimed it"
    with migrated_engine.connect() as conn:
        rows = conn.execute(text("SELECT count(*) FROM personas WHERE id = :p"), {"p": pid})
        audits = conn.execute(
            text("SELECT count(*) FROM audit_log WHERE target = :p AND action = 'persona.create'"),
            {"p": pid},
        )
        assert rows.scalar_one() == 0
        assert audits.scalar_one() == 0, "the audit row rolled back with the UPDATE"

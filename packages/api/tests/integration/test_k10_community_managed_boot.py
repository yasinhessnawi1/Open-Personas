"""Community managed-Postgres boot — real embedded provision (Spec K10, T1+T2).

Boots a REAL bundled Postgres (pixeltable-pgserver) in a short temp datadir and
proves the foundation end-to-end:

- **T1** — :class:`CommunityDbManager` provisions/reattaches, the SAME Alembic
  chain migrates to head, ``ensure_owner`` seeds, ``pg_ctl stop -m fast`` actually
  stops the postmaster (D-K10-9 — ``cleanup_mode=None`` won't), and a cold restart
  re-attaches to a durable datadir (the seeded owner survives).
- **T2** — the FULL app boots under ``PERSONA_EDITION=community`` +
  ``PERSONA_COMMUNITY_DB_MODE=embedded``: a Postgres ``rls_engine`` (not SQLite),
  no admin engine, ``PostgresBackend`` typed memory, and a real route works — all
  invisible, zero external services.

Marked ``integration`` (provisions its own DB; needs no shared :5436). The datadir
sits under a short ``/tmp`` path so the unix-socket path stays under the AF_UNIX cap.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.integration

pytest.importorskip("pixeltable_pgserver")

if TYPE_CHECKING:
    from collections.abc import Iterator

    from tests.conftest import HashEmbedder384


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, owned by another user
        return True
    return True


@pytest.fixture
def short_base_dir() -> Iterator[Path]:
    # A SHORT base so ``<base>/pg16/.s.PGSQL.5432`` stays under the AF_UNIX cap.
    base = Path(tempfile.mkdtemp(prefix="k10", dir="/tmp"))
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_embedded_manager_provisions_migrates_seeds_stops_and_restarts(
    short_base_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from persona_api.db.community import ensure_owner
    from persona_api.db.community_managed import CommunityDbManager, run_migrations

    # run_migrations mutates os.environ["DATABASE_URL"] (D-K10-8); neutralise the leak.
    monkeypatch.delenv("DATABASE_URL", raising=False)

    mgr = CommunityDbManager(external_url=None, base_dir=short_base_dir)
    try:
        url = mgr.start()
        assert mgr.is_embedded is True
        assert url.startswith("postgresql+psycopg://")

        run_migrations(url)

        engine = create_engine(url)
        with engine.connect() as conn:
            n_tables = conn.execute(
                text("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
            ).scalar()
            head = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            vector_ext = conn.execute(
                text("SELECT extversion FROM pg_extension WHERE extname='vector'")
            ).scalar()
            is_super = conn.execute(text("SELECT current_setting('is_superuser')")).scalar()
        # 45 canonical tables (incl. the graph_* tables) + alembic_version.
        assert n_tables is not None
        assert n_tables >= 45
        assert head is not None
        assert vector_ext is not None  # CREATE EXTENSION vector succeeded (D-K10-2)
        assert str(is_super).lower() == "on"  # superuser posture (D-K10-2)

        ensure_owner(engine, owner_id="local-owner", email="local@localhost")
        engine.dispose()

        # The postmaster is really running; pg_ctl stop really stops it (D-K10-9).
        pid = mgr._server.get_pid()  # noqa: SLF001 — lifecycle assertion
        assert pid is not None
        assert _pid_alive(pid)
        mgr.stop()
        assert not _pid_alive(pid), "pg_ctl stop -m fast must actually stop the postmaster"

        # Cold restart reattaches a DURABLE datadir — the seeded owner survives.
        url2 = mgr.start()
        engine2 = create_engine(url2)
        with engine2.connect() as conn:
            owner_ids = conn.execute(text("SELECT id FROM users")).scalars().all()
        engine2.dispose()
        assert "local-owner" in owner_ids
    finally:
        mgr.stop()


def test_community_app_boots_on_managed_embedded_postgres(
    short_base_dir: Path, monkeypatch: pytest.MonkeyPatch, embedder: HashEmbedder384
) -> None:
    from fastapi.testclient import TestClient
    from persona.stores.postgres import PostgresBackend
    from persona_api.app import create_app
    from persona_api.config import APIConfig, Edition
    from persona_api.services import persona_service

    # Embedded mode ignores DATABASE_URL; clear it so config.database_url is empty
    # AND to neutralise run_migrations' env mutation (D-K10-8) at teardown.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)
    # Torch-free deterministic embedder (PostgresBackend needs real 384-dim vectors).
    monkeypatch.setattr(persona_service, "default_embedder", lambda *_a, **_k: embedder)

    work = short_base_dir / "app"
    config = APIConfig(
        edition=Edition.community,
        community_db_mode="embedded",
        community_managed_db_dir=short_base_dir,
        # Isolate the legacy-import source: this is a FRESH-boot test, so point the
        # legacy SQLite/Chroma paths at nonexistent locations (never the host's real
        # cwd/.persona_community.db default) → has_legacy_data() is False, no import.
        community_db_path=short_base_dir / "no-legacy.db",
        community_memory_path=short_base_dir / "no-legacy-chroma",
        workspace_root=work / "work",
        audit_root=str(work / "audit"),
    )
    app = create_app(config)
    with TestClient(app) as client:  # lifespan: provision → migrate → ensure_owner → serve
        state = client.app.state
        # Postgres rls_engine (NOT sqlite), no admin engine, managed manager present.
        assert str(state.rls_engine.url).startswith("postgresql")
        assert state.admin_engine is None
        assert state.community_db_manager is not None
        assert state.community_db_manager.is_embedded is True
        # Typed memory is PostgresBackend/memory_chunks on the managed path (D-K10-3).
        assert isinstance(state.memory_backend, PostgresBackend)
        # A real route runs a SELECT under the seeded local owner (no auth wall).
        resp = client.get("/v1/personas")
        assert resp.status_code == 200, resp.text
        assert resp.json() == []

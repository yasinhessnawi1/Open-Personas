"""Unit tests for the community managed-Postgres substrate helpers (Spec K10, T1).

Pure-logic coverage — no Postgres boots here (the real embedded provision/migrate/
stop lives in the marked integration test). Proves the D-K10-1 resolution order,
the D-K10-10 datadir contract (version-stamp, socket-length, refuse-don't-corrupt),
the DSN coercion, and the manager's not-started / external / lifecycle guards.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from persona_api.config import APIConfig, Edition
from persona_api.db.community_managed import (
    EMBEDDED_PG_MAJOR,
    CommunityDbManager,
    CommunityDbMode,
    guard_datadir_major,
    guard_socket_path_length,
    resolve_managed_external_url,
    to_sync_psycopg_url,
    versioned_datadir,
)
from persona_api.errors import CommunityDbError


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("postgresql://u:p@/db?host=/x", "postgresql+psycopg://u:p@/db?host=/x"),
        ("postgres://u@/db", "postgresql+psycopg://u@/db"),
        ("postgresql+asyncpg://u@h/db", "postgresql+psycopg://u@h/db"),
        ("postgresql+psycopg://u@h/db", "postgresql+psycopg://u@h/db"),
    ],
)
def test_to_sync_psycopg_url_coerces_every_flavor(raw: str, expected: str) -> None:
    assert to_sync_psycopg_url(raw) == expected


def test_versioned_datadir_stamps_the_pg_major() -> None:
    expected = Path(f"/home/u/.persona/pg{EMBEDDED_PG_MAJOR}")
    assert versioned_datadir(Path("/home/u/.persona")) == expected


def test_resolve_external_mode_requires_a_url() -> None:
    with pytest.raises(CommunityDbError) as exc:
        resolve_managed_external_url(CommunityDbMode.external, "")
    assert exc.value.context["reason"] == "external_url_unset"


def test_resolve_external_mode_coerces_the_url() -> None:
    out = resolve_managed_external_url(CommunityDbMode.external, "postgresql://u@h/db")
    assert out == "postgresql+psycopg://u@h/db"


def test_resolve_embedded_mode_is_always_embedded() -> None:
    assert resolve_managed_external_url(CommunityDbMode.embedded, "") is None
    # a stray DATABASE_URL is ignored in embedded mode
    assert resolve_managed_external_url(CommunityDbMode.embedded, "postgresql://u@h/db") is None


def test_resolve_auto_prefers_external_then_embedded() -> None:
    assert resolve_managed_external_url(CommunityDbMode.auto, "postgresql://u@h/db") == (
        "postgresql+psycopg://u@h/db"
    )
    assert resolve_managed_external_url(CommunityDbMode.auto, "") is None


def test_resolve_rejects_legacy_sqlite() -> None:
    with pytest.raises(CommunityDbError) as exc:
        resolve_managed_external_url(CommunityDbMode.legacy_sqlite, "")
    assert exc.value.context["reason"] == "not_managed"


def test_socket_path_guard_accepts_a_short_datadir() -> None:
    guard_socket_path_length(Path("/home/u/.persona/pg16"))  # no raise


def test_socket_path_guard_refuses_an_overlong_datadir() -> None:
    long_dir = Path("/tmp/" + "x" * 100 + "/pg16")
    with pytest.raises(CommunityDbError) as exc:
        guard_socket_path_length(long_dir)
    assert exc.value.context["reason"] == "socket_path_too_long"


def test_major_guard_passes_a_fresh_datadir(tmp_path: Path) -> None:
    guard_datadir_major(tmp_path / "does-not-exist", expected_major=EMBEDDED_PG_MAJOR)  # no raise


def test_major_guard_passes_a_matching_datadir(tmp_path: Path) -> None:
    (tmp_path / "PG_VERSION").write_text(f"{EMBEDDED_PG_MAJOR}\n", encoding="utf-8")
    guard_datadir_major(tmp_path, expected_major=EMBEDDED_PG_MAJOR)  # no raise


def test_major_guard_refuses_a_mismatched_datadir(tmp_path: Path) -> None:
    (tmp_path / "PG_VERSION").write_text("15\n", encoding="utf-8")
    with pytest.raises(CommunityDbError) as exc:
        guard_datadir_major(tmp_path, expected_major=EMBEDDED_PG_MAJOR)
    assert exc.value.context["reason"] == "pg_major_mismatch"
    assert exc.value.context["found_major"] == "15"


def test_manager_external_start_returns_the_dsn_and_stop_is_a_noop(tmp_path: Path) -> None:
    mgr = CommunityDbManager(external_url="postgresql+psycopg://u@h/db", base_dir=tmp_path)
    assert mgr.is_embedded is False
    assert mgr.start() == "postgresql+psycopg://u@h/db"
    assert mgr.database_url == "postgresql+psycopg://u@h/db"
    mgr.stop()  # external mode: no postmaster to stop, must not raise


def test_manager_database_url_before_start_raises(tmp_path: Path) -> None:
    mgr = CommunityDbManager(external_url=None, base_dir=tmp_path)
    assert mgr.is_embedded is True
    with pytest.raises(CommunityDbError) as exc:
        _ = mgr.database_url
    assert exc.value.context["reason"] == "not_started"


def test_config_defaults_keep_the_legacy_sqlite_path(monkeypatch: pytest.MonkeyPatch) -> None:
    # No env overrides → default mode is the deprecated-but-retained legacy path,
    # so existing installs are byte-unchanged through the T1/T2 foundation.
    monkeypatch.delenv("PERSONA_COMMUNITY_DB_MODE", raising=False)
    config = APIConfig(edition=Edition.community)
    assert config.community_db_mode == "legacy-sqlite"
    assert CommunityDbMode(config.community_db_mode) is CommunityDbMode.legacy_sqlite
    assert config.community_managed_db_dir.name == ".persona"


# ---- T3: the worker default-on policy (D-K10-7) ---------------------------


def test_worker_defaults_on_for_community_managed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PERSONA_API_IN_PROCESS_WORKER", raising=False)
    config = APIConfig(edition=Edition.community)
    # managed path → default ON (makes K2/K7/K8 parity real)
    assert config.effective_in_process_worker(community_managed=True) is True
    # legacy-sqlite path (no manager) → default OFF
    assert config.effective_in_process_worker(community_managed=False) is False


def test_worker_defaults_off_for_cloud(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PERSONA_API_IN_PROCESS_WORKER", raising=False)
    config = APIConfig(edition=Edition.cloud)
    # cloud keeps the pre-activation posture regardless of substrate.
    assert config.effective_in_process_worker(community_managed=False) is False
    assert config.effective_in_process_worker(community_managed=True) is False


def test_explicit_worker_flag_always_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSONA_API_IN_PROCESS_WORKER", "false")
    off = APIConfig(edition=Edition.community)
    # explicit OFF beats the community-managed default-on (overridable, D-K10-7)
    assert off.effective_in_process_worker(community_managed=True) is False

    monkeypatch.setenv("PERSONA_API_IN_PROCESS_WORKER", "true")
    on = APIConfig(edition=Edition.cloud)
    # explicit ON beats the cloud default-off (unchanged behavior for the flag)
    assert on.effective_in_process_worker(community_managed=False) is True


def test_worker_refuses_a_sqlite_engine() -> None:
    # T3: the worker's graph store + durable queue are Postgres-only; force-enabling
    # it on the deprecated legacy-SQLite path must refuse loudly, not compose a
    # Postgres-typed backend over SQLite that breaks on first use.
    from persona_api.background.worker_root import start_in_process_worker
    from sqlalchemy import create_engine

    engine = create_engine("sqlite+pysqlite:///:memory:")
    with pytest.raises(CommunityDbError) as exc:
        start_in_process_worker(
            config=APIConfig(edition=Edition.community),
            rls_engine=engine,
            embedder=object(),  # type: ignore[arg-type]  # unused — guard raises first
            tier_registry=object(),  # type: ignore[arg-type]
            free_tier_registry=None,  # R9-096: no plans here — gating off, stated
        )
    assert exc.value.context["reason"] == "worker_requires_postgres"
    assert exc.value.context["dialect"] == "sqlite"

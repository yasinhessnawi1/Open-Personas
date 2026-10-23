"""release.py keeps the api's non-superuser platform role's grants complete (security H1).

On the production box the api's DATABASE_URL is ``persona_platform`` (BYPASSRLS, not a
superuser, DML only) and only the one-shot migration holds the superuser DSN. After every
migration the release step must grant that role DML on every table and set the default
privileges for tables future migrations create. Where the role does not exist (the
Fly-era layout, CI, community) the step must do nothing at all.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

_RELEASE = Path(__file__).resolve().parents[2] / "release.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("persona_api_release_under_test", _RELEASE)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Result:
    def __init__(self, row: tuple[int] | None) -> None:
        self._row = row

    def first(self) -> tuple[int] | None:
        return self._row


class _Conn:
    def __init__(self, role_exists: bool, log: list[str]) -> None:
        self._role_exists = role_exists
        self._log = log

    def __enter__(self) -> _Conn:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, statement: object) -> _Result:
        sql = str(statement)
        self._log.append(sql)
        if "FROM pg_roles" in sql:
            return _Result((1,) if self._role_exists else None)
        return _Result(None)


class _Engine:
    def __init__(self, role_exists: bool, log: list[str]) -> None:
        self._conn = _Conn(role_exists, log)
        self.disposed = False

    def connect(self) -> _Conn:
        return self._conn

    def dispose(self) -> None:
        self.disposed = True


def _run(monkeypatch: pytest.MonkeyPatch, *, role_exists: bool) -> tuple[list[str], _Engine]:
    release = _load()
    log: list[str] = []
    engine = _Engine(role_exists, log)
    monkeypatch.setattr(release, "create_engine", lambda *_a, **_k: engine)
    release._grant_persona_platform("postgresql+psycopg://persona:x@db/persona")
    return log, engine


def test_absent_role_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    log, engine = _run(monkeypatch, role_exists=False)
    assert len(log) == 1
    assert "pg_roles" in log[0]
    assert engine.disposed


def test_present_role_gets_dml_and_default_privileges_only(monkeypatch: pytest.MonkeyPatch) -> None:
    log, engine = _run(monkeypatch, role_exists=True)
    grants = log[1:]
    joined = "\n".join(grants)
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public" in joined
    assert "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public" in joined
    assert joined.count("ALTER DEFAULT PRIVILEGES IN SCHEMA public") == 2
    assert all("persona_platform" in g for g in grants)
    # DML only: never DDL, ownership, role attributes or TRUNCATE.
    for forbidden in ("CREATE", "ALTER ROLE", "OWNER", "TRUNCATE", "ALL PRIVILEGES", "SUPERUSER"):
        assert forbidden not in joined
    assert engine.disposed


def test_main_runs_the_platform_grants_after_the_app_grants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = _load()
    calls: list[str] = []
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://persona:x@db/persona")
    monkeypatch.setattr(release, "_upgrade_to_head", lambda _dsn: calls.append("upgrade"))
    monkeypatch.setattr(release, "_grant_persona_app", lambda _dsn: calls.append("app"))
    monkeypatch.setattr(release, "_grant_persona_platform", lambda _dsn: calls.append("platform"))
    release.main()
    assert calls == ["upgrade", "app", "platform"]

"""Readiness probes the erasure ledger again until it passes (R9-284 review L3).

A transient error at boot (the database not up yet) must not pin /healthz at 503 for the life
of the process; and only a refusal that will not change by itself (a missing grant, a row level
security refusal, no ledger table) counts as "unwritable".
"""

from __future__ import annotations

import time
from pathlib import Path  # noqa: TC003 - a fixture annotation
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from persona_api.config import Edition
from persona_api.db.community import create_community_schema, make_community_engine
from persona_api.routes import health
from persona_api.routes.health import router
from persona_api.services.erasure_ledger import ErasureLedger
from sqlalchemy.exc import OperationalError


class _OkEngine:
    def connect(self) -> _OkEngine:
        return self

    def __enter__(self) -> _OkEngine:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def execute(self, *_args: object, **_kwargs: object) -> None:
        return None


def _app(platform: object, *, writable: bool | None, probed_ago: float) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.rls_engine = _OkEngine()
    app.state.in_process_worker = None
    app.state.config = SimpleNamespace(edition=Edition.cloud)
    app.state.platform_engine = platform
    app.state.erasure_ledger_writable = writable
    app.state.erasure_ledger_probed_at = time.monotonic() - probed_ago
    return app


@pytest.fixture
def ready(tmp_path: Path) -> object:
    engine = make_community_engine(tmp_path / "ready.db")
    create_community_schema(engine)
    return engine


@pytest.mark.parametrize("was", [False, None])
def test_a_ledger_that_works_now_makes_the_api_ready_again(ready: object, was: bool | None) -> None:
    app = _app(ready, writable=was, probed_ago=3600)

    resp = TestClient(app).get("/healthz")

    assert resp.status_code == 200
    assert app.state.erasure_ledger_writable is True


def test_the_probe_is_not_repeated_on_every_request(ready: object) -> None:
    app = _app(ready, writable=False, probed_ago=1)

    resp = TestClient(app).get("/healthz")

    assert resp.status_code == 503
    assert app.state.erasure_ledger_writable is False


class _Unreachable:
    """An engine whose every connection fails the way a database that is not up yet does."""

    dialect = SimpleNamespace(name="postgresql")

    def connect(self) -> object:
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))


def test_an_unreachable_database_is_not_unwritable_for_good() -> None:
    assert ErasureLedger(_Unreachable()).can_write() is None  # type: ignore[arg-type]

    app = _app(_Unreachable(), writable=None, probed_ago=3600)
    resp = TestClient(app).get("/healthz")

    assert resp.status_code == 200
    assert app.state.erasure_ledger_writable is None


def test_a_missing_ledger_table_is_unwritable_for_good(tmp_path: Path) -> None:
    bare = make_community_engine(tmp_path / "bare.db")

    assert ErasureLedger(bare).can_write() is False


def test_a_permission_refusal_is_unwritable_for_good() -> None:
    class _RefusedError(Exception):
        sqlstate = "42501"

    class _ReadOnly(_Unreachable):
        def connect(self) -> object:
            raise OperationalError("INSERT", {}, _RefusedError("permission denied"))

    assert ErasureLedger(_ReadOnly()).can_write() is False  # type: ignore[arg-type]
    assert health._LEDGER_REPROBE_SECONDS > 0  # noqa: SLF001

"""Readiness fails on hosted when the erasure ledger cannot be written (R9-284 review L7).

Such an api would accept every persona delete, delete the row, and fail every erasure after
it. The boot probe (ErasureLedger.can_write) finds that once; /healthz is where it shows.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path  # noqa: TC003 - a fixture annotation
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from persona_api.config import Edition
from persona_api.db.community import create_community_schema, make_community_engine
from persona_api.routes.health import router
from persona_api.services.erasure_ledger import ErasureLedger


class _OkEngine:
    def connect(self) -> _OkEngine:
        return self

    def __enter__(self) -> _OkEngine:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def execute(self, *_args: object, **_kwargs: object) -> None:
        return None


@contextmanager
def _client(edition: Edition, writable: bool | None) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(router)
    app.state.rls_engine = _OkEngine()
    app.state.in_process_worker = None
    app.state.config = SimpleNamespace(edition=edition)
    app.state.erasure_ledger_writable = writable
    with TestClient(app) as client:
        yield client


def test_hosted_with_an_unwritable_ledger_is_not_ready() -> None:
    with _client(Edition.cloud, writable=False) as client:
        resp = client.get("/healthz")

    assert resp.status_code == 503
    assert resp.json()["erasure_ledger"] == "unwritable"


@pytest.mark.parametrize(
    ("edition", "writable"),
    [(Edition.cloud, True), (Edition.cloud, None), (Edition.community, False)],
)
def test_otherwise_the_ledger_does_not_decide_readiness(
    edition: Edition, writable: bool | None
) -> None:
    with _client(edition, writable) as client:
        resp = client.get("/healthz")

    assert resp.status_code == 200
    assert "erasure_ledger" not in resp.json()


def test_the_probe_writes_nothing_and_says_whether_it_could(tmp_path: Path) -> None:
    ready = make_community_engine(tmp_path / "ready.db")
    create_community_schema(ready)
    bare = make_community_engine(tmp_path / "bare.db")

    assert ErasureLedger(ready).can_write() is True
    assert ErasureLedger(ready).unfinished() == []
    assert ErasureLedger(bare).can_write() is False
    ready.dispose()
    bare.dispose()

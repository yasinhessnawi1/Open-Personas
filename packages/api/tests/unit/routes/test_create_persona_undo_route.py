"""A persona create that fails after its row committed leaves nothing behind (R9-293).

Runs the real create on the legacy community edition (SQLite and Chroma). The identity index
writes its chunks and then fails, as it did in the R9-292 smoke test. The request answers 500,
and by then the row is gone: the owner's list never shows a half-made persona. The rest of the
persona erasure (every chunk and every file) runs in the background (R9-320); each test waits
for that run before it looks.
"""

from __future__ import annotations

from collections.abc import Iterator  # noqa: TC003 - a fixture annotation
from concurrent.futures import Future
from pathlib import Path  # noqa: TC003 - a fixture annotation
from typing import Any

import pytest
from _erase_seed import OWNER, PERSONA_KINDS, Seeded, legacy_edition
from fastapi.testclient import TestClient
from persona.registry import PersonaRegistry
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig
from persona_api.db.models import personas as personas_t
from persona_api.middleware.rate_limit import InMemoryRateLimitStore, RateLimiter
from persona_api.services import erasure_start
from persona_api.services.erasure_ledger import ErasureLedger, ErasureState, SubjectKind
from sqlalchemy import select

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


@pytest.fixture
def seeded(tmp_path: Path, embedder: object) -> Iterator[Seeded]:
    with legacy_edition(tmp_path, embedder) as legacy:  # type: ignore[arg-type]
        yield legacy


@pytest.fixture
def undo_runs(monkeypatch: pytest.MonkeyPatch) -> list[Future[None]]:
    """The background runs the route's undos hand off; a test waits on them before it looks."""
    runs: list[Future[None]] = []
    real = erasure_start.undo_failed_create

    def _undo(*args: Any, **kwargs: Any) -> Future[None] | None:  # noqa: ANN401
        run = real(*args, **kwargs)
        assert run is not None, "no worker here: the erasure runs in the background"
        runs.append(run)
        return run

    monkeypatch.setattr(erasure_start, "undo_failed_create", _undo)
    return runs


def _client(seeded: Seeded, embedder: object) -> TestClient:
    app = create_app(
        APIConfig(
            database_url="postgresql+psycopg://super@localhost/persona_shell",
            app_database_url="postgresql+psycopg://persona_app@localhost/persona_shell",
        )
    )

    async def _verify(token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id=token, email=None)

    app.state.verify_token = _verify
    app.state.rate_limiter = RateLimiter(InMemoryRateLimitStore(), default_limit=1000)
    app.state.embedder = embedder
    app.state.rls_engine = seeded.engine
    app.state.platform_engine = seeded.engine
    app.state.memory_backend = seeded.memory
    app.state.audit_logger = seeded.audit
    app.state.file_storage = seeded.storage
    app.state.workspace_root = seeded.workspace_root
    app.state.audit_root = seeded.audit_root
    app.state.job_queue = None
    app.state.in_process_worker = None
    return TestClient(app, raise_server_exceptions=False)


def _owned(seeded: Seeded) -> set[str]:
    with seeded.engine.connect() as conn:
        return set(
            conn.execute(select(personas_t.c.id).where(personas_t.c.owner_id == OWNER)).scalars()
        )


def test_a_failing_identity_index_leaves_no_persona(
    seeded: Seeded,
    embedder: object,
    monkeypatch: pytest.MonkeyPatch,
    undo_runs: list[Future[None]],
) -> None:
    real_load = PersonaRegistry.load_persona
    created: list[str] = []

    def _writes_then_fails(self: PersonaRegistry, persona: Any) -> None:  # noqa: ANN401
        created.append(str(persona.persona_id))
        real_load(self, persona)  # every chunk is written, as far as the index got
        msg = "the identity index went away"
        raise RuntimeError(msg)

    monkeypatch.setattr(PersonaRegistry, "load_persona", _writes_then_fails)
    before = _owned(seeded)

    resp = _client(seeded, embedder).post(
        "/v1/personas", json={"yaml": _YAML}, headers={"Authorization": f"Bearer {OWNER}"}
    )

    assert resp.status_code == 500
    assert len(created) == 1
    pid = created[0]
    assert _owned(seeded) == before, "no persona row"
    (run,) = undo_runs
    run.result(timeout=60)
    chunks = sum(len(seeded.memory.get_all(persona_id=pid, store_kind=k)) for k in PERSONA_KINDS)
    assert chunks == 0, "no memory chunk"
    assert not (seeded.workspace_root / OWNER / pid).exists(), "no workspace folder"
    assert not list(seeded.audit_root.glob(f"{pid}.tools.jsonl")), "no tool log"
    erasure = ErasureLedger(seeded.engine).get(SubjectKind.PERSONA, pid)
    assert erasure is not None
    assert erasure.state is ErasureState.COMPLETE


def test_a_create_that_succeeds_is_not_erased(seeded: Seeded, embedder: object) -> None:
    before = _owned(seeded)

    resp = _client(seeded, embedder).post(
        "/v1/personas", json={"yaml": _YAML}, headers={"Authorization": f"Bearer {OWNER}"}
    )

    assert resp.status_code == 201
    pid = resp.json()["id"]
    assert _owned(seeded) == {*before, pid}
    assert ErasureLedger(seeded.engine).get(SubjectKind.PERSONA, pid) is None


def test_an_undo_whose_erasure_fails_still_answers_500_and_leaves_it_to_the_sweep(
    seeded: Seeded,
    embedder: object,
    monkeypatch: pytest.MonkeyPatch,
    undo_runs: list[Future[None]],
) -> None:
    """Review LOW-5: a later erasure step raises. The create still answers 500; the row is
    gone (the route's half of the erasure ran), and the ledger is left failed for the sweep."""
    from persona_api.services import erasure_service  # noqa: PLC0415

    real_load = PersonaRegistry.load_persona
    created: list[str] = []

    def _writes_then_fails(self: PersonaRegistry, persona: Any) -> None:  # noqa: ANN401
        created.append(str(persona.persona_id))
        real_load(self, persona)
        msg = "the identity index went away"
        raise RuntimeError(msg)

    def _files_fail(*_a: Any) -> dict[str, int]:  # noqa: ANN401
        msg = "the file store is down"
        raise OSError(msg)

    monkeypatch.setattr(PersonaRegistry, "load_persona", _writes_then_fails)
    monkeypatch.setattr(
        erasure_service,
        "STEPS",
        tuple(
            (name, _files_fail if name == "files" else step) for name, step in erasure_service.STEPS
        ),
    )
    before = _owned(seeded)

    resp = _client(seeded, embedder).post(
        "/v1/personas", json={"yaml": _YAML}, headers={"Authorization": f"Bearer {OWNER}"}
    )

    assert resp.status_code == 500
    pid = created[0]
    assert _owned(seeded) == before, "the row is gone"
    (run,) = undo_runs
    run.result(timeout=60)
    erasure = ErasureLedger(seeded.engine).get(SubjectKind.PERSONA, pid)
    assert erasure is not None
    assert erasure.state is ErasureState.FAILED, "left for the sweep to finish"

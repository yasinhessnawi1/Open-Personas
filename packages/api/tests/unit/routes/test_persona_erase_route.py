"""``DELETE /v1/personas/{id}`` erases the persona (R9-284, D-R9-284-erase).

The real route over the community engine with Chroma and local files (the legacy edition): the
answer is 202, the persona is gone at once, and with no worker in this process the erasure runs
in the background and leaves nothing (the full inventory is in test_erase_persona.py). With a
worker that consumes it, the job is enqueued instead. Another owner's persona is a 404 and is
never claimed, and a process that could not erase the memory refuses before touching the row.
"""

from __future__ import annotations

import asyncio
from pathlib import Path  # noqa: TC003 - a fixture annotation
from types import SimpleNamespace
from typing import Any

import pytest
from _erase_seed import (
    OTHER_OWNER,
    OWNER,
    PERSONA,
    PERSONA_KINDS,
    STRANGER,
    Seeded,
    conversation_of,
    ids_naming,
    seed,
)
from fastapi.testclient import TestClient
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig
from persona_api.middleware.rate_limit import InMemoryRateLimitStore, RateLimiter
from persona_api.services import audit_service, erasure_start
from persona_api.services.erasure_ledger import ErasureLedger, ErasureState, SubjectKind
from persona_api.services.erasure_service import PERSONA_ERASE_JOB_TYPE, capture_persona


@pytest.fixture
def seeded(tmp_path: Path, embedder: object) -> Seeded:
    return seed(tmp_path, embedder)  # type: ignore[arg-type]


def _client(seeded: Seeded, **state: Any) -> TestClient:  # noqa: ANN401
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
    app.state.rls_engine = seeded.engine
    app.state.platform_engine = seeded.engine
    app.state.memory_backend = seeded.memory
    app.state.audit_logger = seeded.audit
    app.state.file_storage = seeded.storage
    app.state.workspace_root = seeded.workspace_root
    app.state.audit_root = seeded.audit_root
    app.state.job_queue = None
    app.state.in_process_worker = None
    for name, value in state.items():
        setattr(app.state, name, value)
    return TestClient(app)


def _auth(owner: str = OWNER) -> dict[str, str]:
    return {"Authorization": f"Bearer {owner}"}


def _state(seeded: Seeded, persona: str = PERSONA) -> ErasureState | None:
    found = ErasureLedger(seeded.engine).get(SubjectKind.PERSONA, persona)
    return None if found is None else found.state


def _memory(seeded: Seeded, pid: str) -> int:
    kinds = sum(len(seeded.memory.get_all(persona_id=pid, store_kind=k)) for k in PERSONA_KINDS)
    docs = seeded.memory.get_all(persona_id=conversation_of(pid), store_kind="document")
    return kinds + len(docs)


@pytest.fixture(autouse=True)
def _no_audit_noise(monkeypatch: pytest.MonkeyPatch) -> None:
    """The route's own ``persona.delete`` row is not what these tests are about."""
    real = audit_service.record

    def _record(**kwargs: Any) -> None:  # noqa: ANN401
        if kwargs.get("action") != "persona.delete":
            real(**kwargs)

    monkeypatch.setattr(audit_service, "record", _record)


def test_a_delete_answers_202_and_nothing_of_the_persona_is_left(seeded: Seeded) -> None:
    resp = _client(seeded).delete(f"/v1/personas/{PERSONA}", headers=_auth())

    assert resp.status_code == 202, resp.text
    erasure = ErasureLedger(seeded.engine).get(SubjectKind.PERSONA, PERSONA)
    assert erasure is not None
    assert resp.json() == {"erasure_id": erasure.id}
    assert erasure.state is ErasureState.COMPLETE
    assert ids_naming(seeded.engine, "personas", PERSONA) == []
    assert _memory(seeded, PERSONA) == 0
    assert not (seeded.workspace_root / OWNER / PERSONA).exists()


def test_another_owners_persona_is_a_404_and_is_never_claimed(seeded: Seeded) -> None:
    resp = _client(seeded).delete(f"/v1/personas/{STRANGER}", headers=_auth())

    assert resp.status_code == 404, resp.text
    assert _state(seeded, STRANGER) is None
    assert ids_naming(seeded.engine, "personas", STRANGER) == [STRANGER]
    assert _memory(seeded, STRANGER) == 7
    assert (seeded.workspace_root / OTHER_OWNER / STRANGER).exists()


def test_a_process_that_cannot_erase_memory_refuses_before_the_row(seeded: Seeded) -> None:
    resp = _client(seeded, platform_engine=None).delete(f"/v1/personas/{PERSONA}", headers=_auth())

    assert resp.status_code == 503, resp.text
    assert _state(seeded) is None
    assert ids_naming(seeded.engine, "personas", PERSONA) == [PERSONA]


class _Queue:
    def __init__(self) -> None:
        self.keys: list[str] = []

    def enqueue(self, **kwargs: Any) -> None:  # noqa: ANN401
        self.keys.append(str(kwargs["idempotency_key"]))


class _Worker:
    job_types = frozenset({PERSONA_ERASE_JOB_TYPE})


def test_with_a_worker_the_job_is_enqueued_and_the_row_is_already_gone(seeded: Seeded) -> None:
    queue = _Queue()
    client = _client(seeded, job_queue=queue, in_process_worker=_Worker())

    resp = client.delete(f"/v1/personas/{PERSONA}", headers=_auth())

    assert resp.status_code == 202, resp.text
    erasure = ErasureLedger(seeded.engine).get(SubjectKind.PERSONA, PERSONA)
    assert erasure is not None
    assert queue.keys == [f"erase:{erasure.id}"]
    assert erasure.state is ErasureState.RUNNING
    assert erasure.counts["row"] == {"personas": 1}
    assert ids_naming(seeded.engine, "personas", PERSONA) == []
    assert ids_naming(seeded.engine, "schedules", PERSONA) == []
    assert _memory(seeded, PERSONA) == 7, "the memory is the job's, not the route's"


class _Turns:
    def __init__(self) -> None:
        self.cancelled: list[str] = []

    def request_cancel(self, conversation_id: str) -> bool:
        self.cancelled.append(conversation_id)
        return True


class _Task:
    def __init__(self) -> None:
        self.cancelled = False

    def done(self) -> bool:
        return False

    def cancel(self) -> None:
        self.cancelled = True


class _Token:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class _Run:
    def __init__(self) -> None:
        self.task = _Task()
        self.cancel_token = _Token()


class _Runs:
    def __init__(self, run_id: str) -> None:
        self.handles = {run_id: _Run()}

    def get(self, run_id: str) -> _Run | None:
        return self.handles.get(run_id)


def test_the_personas_live_turns_and_runs_are_cancelled(seeded: Seeded) -> None:
    turns, runs = _Turns(), _Runs("run_aaaaaa")
    client = _client(seeded, chat_turn_registry=turns, run_registry=runs)

    resp = client.delete(f"/v1/personas/{PERSONA}", headers=_auth())

    assert resp.status_code == 202, resp.text
    assert turns.cancelled == [conversation_of(PERSONA)]
    handle = runs.handles["run_aaaaaa"]
    assert handle.task.cancelled
    assert handle.cancel_token.cancelled


def test_an_unfinished_erasure_is_finished_at_the_next_start(seeded: Seeded) -> None:
    """A process that stopped mid-erasure (or whose inline run failed) finishes it on boot."""
    with seeded.engine.connect() as conn:
        captured = capture_persona(conn, owner_id=OWNER, persona_id=PERSONA)
    ErasureLedger(seeded.engine).claim(
        owner_id=OWNER, subject_kind=SubjectKind.PERSONA, subject_id=PERSONA, captured=captured
    )
    state = SimpleNamespace(
        platform_engine=seeded.engine,
        memory_backend=seeded.memory,
        audit_logger=seeded.audit,
        file_storage=seeded.storage,
        workspace_root=seeded.workspace_root,
        audit_root=seeded.audit_root,
        job_queue=None,
        in_process_worker=None,
    )

    async def _boot() -> None:
        task = erasure_start.resume_persona_erasures_on_startup(state)
        assert task is not None
        await task

    asyncio.run(_boot())

    assert _state(seeded) is ErasureState.COMPLETE
    assert ids_naming(seeded.engine, "personas", PERSONA) == []
    assert _memory(seeded, PERSONA) == 0

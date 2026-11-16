"""``DELETE /v1/personas/{id}`` erases the persona (R9-284, D-R9-284-erase).

The real route over the community engine with Chroma and local files (the legacy edition): the
answer is 202, the persona is gone at once, and with no worker in this process the erasure runs
in the background and leaves nothing (the full inventory is in test_erase_persona.py). With a
worker that consumes it, the job is enqueued instead. Another owner's persona is a 404 and is
never claimed, and a process that could not erase the memory refuses before touching the row.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator  # noqa: TC003 - a fixture annotation
from datetime import timedelta
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
    legacy_edition,
)
from fastapi.testclient import TestClient
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig
from persona_api.jobs.handlers.persona_erase import PersonaEraseSweep
from persona_api.middleware.rate_limit import InMemoryRateLimitStore, RateLimiter
from persona_api.services import audit_service, erasure_start
from persona_api.services.erasure_ledger import ErasureLedger, ErasureState, SubjectKind
from persona_api.services.erasure_service import PERSONA_ERASE_JOB_TYPE, capture_persona


@pytest.fixture
def seeded(tmp_path: Path, embedder: object) -> Iterator[Seeded]:
    with legacy_edition(tmp_path, embedder) as legacy:  # type: ignore[arg-type]
        yield legacy


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
        task = erasure_start.resume_persona_erasures_on_startup(
            state, interval_seconds=0.05, grace=timedelta(0)
        )
        assert task is not None
        for _ in range(600):
            if _state(seeded) is ErasureState.COMPLETE:
                break
            await asyncio.sleep(0.05)
        task.cancel()

    asyncio.run(_boot())

    assert _state(seeded) is ErasureState.COMPLETE
    assert ids_naming(seeded.engine, "personas", PERSONA) == []
    assert _memory(seeded, PERSONA) == 0


def test_a_failure_after_the_row_delete_is_picked_up_by_the_sweep(
    seeded: Seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review M2: a 500 between the row delete and the hand-off leaves an erasure no job holds.

    The persona is already gone from the owner's view; the next sweep finishes the rest.
    """

    def _broken(*_args: Any, **_kwargs: Any) -> bool:  # noqa: ANN401
        raise RuntimeError("the queue is unavailable")

    monkeypatch.setattr(erasure_start, "dispatch_persona_erasure", _broken)
    app_client = _client(seeded)
    client = TestClient(app_client.app, raise_server_exceptions=False)

    resp = client.delete(f"/v1/personas/{PERSONA}", headers=_auth())

    assert resp.status_code == 500
    assert _state(seeded) is ErasureState.RUNNING
    assert ids_naming(seeded.engine, "personas", PERSONA) == []
    assert _memory(seeded, PERSONA) == 7

    deps = erasure_start.erase_deps_from_state(app_client.app.state)
    assert deps is not None
    sweep = PersonaEraseSweep(
        engine=seeded.engine,
        run=lambda erasure, _token: erasure_start.run_persona_erasure_inline(deps, erasure),
        grace=timedelta(0),
    )
    assert sweep() == 1

    assert _state(seeded) is ErasureState.COMPLETE
    assert _memory(seeded, PERSONA) == 0


def test_a_process_with_no_worker_keeps_sweeping(seeded: Seeded) -> None:
    """Review M2: an erasure stranded after start is finished by a later sweep, not a restart."""
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

    async def _run() -> None:
        task = erasure_start.resume_persona_erasures_on_startup(
            state, interval_seconds=0.05, grace=timedelta(0)
        )
        assert task is not None
        await asyncio.sleep(0.3)  # the first sweeps find nothing
        with seeded.engine.connect() as conn:
            captured = capture_persona(conn, owner_id=OWNER, persona_id=PERSONA)
        ErasureLedger(seeded.engine).claim(
            owner_id=OWNER, subject_kind=SubjectKind.PERSONA, subject_id=PERSONA, captured=captured
        )
        for _ in range(600):
            if _state(seeded) is ErasureState.COMPLETE:
                break
            await asyncio.sleep(0.05)
        task.cancel()

    asyncio.run(_run())

    assert _state(seeded) is ErasureState.COMPLETE


def test_a_row_delete_that_keeps_racing_is_accepted_and_left_to_the_job(
    seeded: Seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review LOW: conversations appearing under the row lock are a retry, not a 500."""
    from persona_api.services.erasure_service import ErasureRaceError  # noqa: PLC0415

    def _racing(*_args: Any, **_kwargs: Any) -> int:  # noqa: ANN401
        raise ErasureRaceError("conversations kept appearing")

    monkeypatch.setattr(erasure_start, "delete_row_capturing", _racing)

    resp = _client(seeded).delete(f"/v1/personas/{PERSONA}", headers=_auth())

    assert resp.status_code == 202, resp.text
    assert _state(seeded) is ErasureState.COMPLETE, "the erasure's own row step finished it"
    assert ids_naming(seeded.engine, "personas", PERSONA) == []


def test_a_persona_deleted_before_the_ledger_is_erased_at_the_next_start(seeded: Seeded) -> None:
    """R9-290: its audit files outlived it with no row anywhere; the boot claims and erases it."""
    import json  # noqa: PLC0415
    import os  # noqa: PLC0415
    import time  # noqa: PLC0415

    legacy = "persona_dddddddddddddddddddddddddddddddd"
    store = seeded.audit_root / f"{legacy}.jsonl"
    event = {
        "timestamp": "2026-01-01T00:00:00+00:00",
        "persona_id": legacy,
        "action": "write",
        "store": "self_facts",
        "source": "user",
        "written_by": OWNER,
        "reason": "initial persona load",
        "chunk_ids": ["c1"],
        "logical_ids": [],
        "metadata": {},
    }
    store.write_text(json.dumps(event) + "\n", encoding="utf-8")
    tools = seeded.audit_root / f"{legacy}.tools.jsonl"
    tools.write_text("{}\n", encoding="utf-8")
    for path in (store, tools):
        then = time.time() - 7200
        os.utime(path, (then, then))
    state = SimpleNamespace(
        platform_engine=seeded.engine,
        memory_backend=seeded.memory,
        audit_logger=seeded.audit,
        file_storage=seeded.storage,
        workspace_root=seeded.workspace_root,
        audit_root=seeded.audit_root,
        job_queue=None,
        in_process_worker=None,
        erasure_ledger_writable=True,  # the boot's probe passed (review H1 a)
    )
    ledger = ErasureLedger(seeded.engine)

    def _done() -> bool:
        erasure = ledger.get(SubjectKind.PERSONA, legacy)
        return erasure is not None and erasure.state is ErasureState.COMPLETE

    async def _boot() -> None:
        task = erasure_start.resume_persona_erasures_on_startup(
            state, interval_seconds=0.05, grace=timedelta(0)
        )
        assert task is not None
        for _ in range(600):
            if _done():
                break
            await asyncio.sleep(0.05)
        task.cancel()

    asyncio.run(_boot())

    assert _done()
    assert not tools.exists()

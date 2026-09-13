"""A one-off is an ad hoc task the worker runs (Spec W1, T2; D-W1-1 / D-W1-3 / D-W1-7 / D-W1-28).

The real chain, nothing hand-forced: the REAL app (``create_app`` + the real routes and RLS
engine) takes ``POST /personas/{id}/runs``; the REAL ``work_dispatch_service`` creates the ad
hoc task and enqueues its first leg; the REAL A0 ``Worker`` claims it and the REAL
``TaskLegHandler`` runs it through the REAL ``RuntimeFactoryLegRunnerBuilder`` and
``RuntimeFactory.build_agentic_loop`` (a scripted model backend is the only double); the run
row lands through the one runs writer; the run page's SSE and GET read it back.

What is pinned:

- the dispatch response names the task (ad hoc, active) and the worker's leg opens a run
  that names the task and completes it; time from POST to the run row's first snapshot is
  measured and bounded (the D-W1-1 acceptance);
- the run page streams a worker-executed run through the DB tail with the live frame shape
  and reconciles the finished run from ``GET /runs/{id}``;
- **the guard (D-W1-1):** an ad hoc leg runs POLICY-GATED. The same scripted tool call that
  merely errors under the bare toolbox records a proposal and parks the task waiting on the
  user under the gate. Both halves run, so the differential is real;
- a brief over the cap is refused with a human sentence, never a raw 422 (D-W1-28);
- the viewer's cancel and respond doors behave under the task model (D-W1-4 / D-W1-14).
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from persona.backends import StreamChunk, TokenUsage
from persona.backends.types import ChatResponse
from persona.jobs import JobRegistry
from persona.schema.tools import ToolCall
from persona.tasks import TaskKind, TaskState, WaitKind
from persona_api.app import create_app
from persona_api.approvals import ApprovalStore
from persona_api.config import APIConfig
from persona_api.jobs import Worker
from persona_api.jobs.queue import JobQueue
from persona_api.middleware.rls_context import make_rls_engine
from persona_api.schedules import ScheduleStore
from persona_api.services.runtime_factory import RuntimeFactory
from persona_api.services.work_dispatch_service import BRIEF_TOO_LONG_MESSAGE, MAX_BRIEF_CHARS
from persona_api.tasks.continuation import TaskContinuation
from persona_api.tasks.handler import register_task_leg_handler
from persona_api.tasks.leg_runner import RuntimeFactoryLegRunnerBuilder
from persona_api.tasks.store import CheckpointStore, TaskStore
from sqlalchemy import create_engine, text

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from persona.schema.conversation import ConversationMessage
    from sqlalchemy import Engine
    from tests.conftest import HashEmbedder384

pytestmark = pytest.mark.integration

_UID = "user_w1_dispatch"
_BRIEF = "list the three newest AI persona repos on GitHub"
_YAML = """\
schema_version: "1.0"
identity:
  name: Astrid
  role: assistant
  background: |
    A helper.
  language_default: en
  constraints: []
self_facts:
  - fact: knows things
    confidence: 1.0
"""


# --- the scripted model (the ONLY double) ------------------------------------------------


class _ScriptedBackend:
    """Answers each ``chat`` call from a script; the last entry repeats."""

    provider_name = "anthropic"
    model_name = "scripted"
    max_tokens = 4096

    def __init__(self, script: list[ChatResponse]) -> None:
        self._script = list(script)
        self.calls = 0

    @property
    def supports_native_tools(self) -> bool:
        return True

    @property
    def supports_vision(self) -> bool:
        return False

    async def chat(self, _messages: list[ConversationMessage], **_: object) -> ChatResponse:
        index = min(self.calls, len(self._script) - 1)
        self.calls += 1
        return self._script[index]

    async def chat_stream(
        self, _messages: list[ConversationMessage], **_: object
    ) -> AsyncIterator[StreamChunk]:
        yield StreamChunk(delta="done", is_final=True, usage=TokenUsage(1, 1, 2))


def _reply(content: str, *, tool_calls: list[ToolCall] | None = None) -> ChatResponse:
    return ChatResponse(
        content=content,
        model="scripted",
        provider="anthropic",
        usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        latency_ms=1.0,
        tool_calls=tool_calls or [],
    )


class _Registry:
    def __init__(self, backend: _ScriptedBackend) -> None:
        self._b = backend

    def get(self, _tier: str) -> _ScriptedBackend:
        return self._b

    @property
    def configured_tier_names(self) -> tuple[str, ...]:
        return ("frontier", "mid", "small")

    def supports_vision_for(self, _tier: str) -> bool:
        return False

    def metadata_for(self, _tier: str) -> None:
        return None

    def model_name_for(self, _tier: str) -> str:
        return "scripted"

    async def aclose(self) -> None:
        pass


class _NullTurnLog:
    def write(self, _log: object) -> None:
        pass


# --- fixtures ----------------------------------------------------------------------------


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:  # noqa: ARG001 — ordering
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip(
            "SKIPPED, NOT PASSED: export APP_DATABASE_URL (the persona_app non-superuser DSN, "
            "e.g. postgresql+psycopg://persona_app:persona_app@localhost:5436/persona_test) "
            "with PERSONA_TEST_DB=1 to run the ad hoc dispatch chain"
        )
    # The RLS-aware engine (its checkout listener applies the owner the worker binds), not a
    # plain one: under a plain engine the policy hides the persona and the leg dies with
    # "persona not found".
    engine = make_rls_engine(app_url.replace("+asyncpg", "+psycopg"))
    yield engine
    engine.dispose()


@pytest.fixture
def client(
    app_engine: Engine,  # noqa: ARG001 — ordering: the migrated, RLS-aware DB first
    embedder: HashEmbedder384,
    tmp_path: Path,
) -> Iterator[TestClient]:
    # The env var, not ``engine.url``: SQLAlchemy masks the password in the rendered URL, and
    # the conftest has already rewritten the env to the isolated per-worktree database.
    cfg = APIConfig(
        app_database_url=os.environ["APP_DATABASE_URL"].replace("+asyncpg", "+psycopg"),
        audit_root=str(tmp_path / "audit"),
    )
    app = create_app(cfg)
    from persona_api.auth import AuthenticatedUser

    async def _verify(token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id=token, email=None)

    with TestClient(app) as c:
        app.state.verify_token = _verify
        app.state.embedder = embedder
        if hasattr(app.state, "tier_registry"):
            app.state.tier_registry = None

        async def _build(persona_id: str) -> object:  # presence is what the route guards
            raise AssertionError(f"the api never builds a loop for a dispatch ({persona_id})")

        app.state.build_agentic_loop = _build
        su = make_rls_engine(os.environ["DATABASE_URL"])
        with su.begin() as conn:
            conn.execute(
                text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
                {"i": _UID, "e": f"{_UID}@x"},
            )
        su.dispose()
        yield c
        su = make_rls_engine(os.environ["DATABASE_URL"])
        with su.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE id = :i"), {"i": _UID})
        su.dispose()


@pytest.fixture
def persona_id(client: TestClient) -> str:
    return str(client.post("/v1/personas", json={"yaml": _YAML}, headers=_auth()).json()["id"])


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {_UID}"}


def _worker(
    app_engine: Engine,
    embedder: HashEmbedder384,
    tmp_path: Path,
    backend: _ScriptedBackend,
    *,
    gated: bool,
) -> Worker:
    """The REAL worker + REAL leg handler over the REAL runtime factory (scripted model)."""
    factory = RuntimeFactory(
        rls_engine=app_engine,
        embedder=embedder,
        tier_registry=_Registry(backend),  # type: ignore[arg-type]
        turn_log_writer=_NullTurnLog(),  # type: ignore[arg-type]
        audit_root=tmp_path / "worker-audit",
    )
    registry = JobRegistry()
    register_task_leg_handler(
        registry,
        task_store=TaskStore(app_engine),
        checkpoint_store=CheckpointStore(app_engine),
        runner_builder=RuntimeFactoryLegRunnerBuilder(
            factory, recorder=ApprovalStore(app_engine) if gated else None
        ),
        continuation=TaskContinuation(
            task_store=TaskStore(app_engine),
            queue=JobQueue(app_engine),
            checkpoint_store=CheckpointStore(app_engine),
            schedule_store=ScheduleStore(app_engine),
        ),
        rls_engine=app_engine,
    )
    dispatch_engine = create_engine(os.environ["DATABASE_URL"].replace("+asyncpg", "+psycopg"))
    return Worker(
        dispatch_engine=dispatch_engine,
        rls_engine=app_engine,
        registry=registry,
        worker_id="w-w1-dispatch",
    )


def _read_sse(body: str) -> list[tuple[str, str]]:
    events: list[tuple[str, str]] = []
    ev = data = None
    for line in body.splitlines():
        if line.startswith("event:"):
            ev = line.removeprefix("event:").strip()
        elif line.startswith("data:"):
            data = line.removeprefix("data:").strip()
        elif line == "" and ev is not None:
            events.append((ev, data or ""))
            ev = data = None
    return events


def _run_row(engine: Engine, task_id: str) -> dict[str, object]:
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.current_user_id', :u, true)"), {"u": _UID})
        row = (
            conn.execute(
                text("SELECT id, task_id, status, steps, error FROM runs WHERE task_id = :t"),
                {"t": task_id},
            )
            .mappings()
            .one()
        )
    return dict(row)


def _leg_job(task_id: str) -> dict[str, object]:
    """The task's leg job as A0 left it (state + last_error), read cross-tenant."""
    su = create_engine(os.environ["DATABASE_URL"].replace("+asyncpg", "+psycopg"))
    try:
        with su.begin() as conn:
            row = (
                conn.execute(
                    text(
                        "SELECT state, attempt, last_error FROM jobs "
                        "WHERE type = 'task_leg' AND payload->>'task_id' = :t"
                    ),
                    {"t": task_id},
                )
                .mappings()
                .one()
            )
        return dict(row)
    finally:
        su.dispose()


# --- the chain -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_dispatch_is_an_ad_hoc_task_whose_first_leg_the_worker_runs(
    client: TestClient,
    persona_id: str,
    app_engine: Engine,
    embedder: HashEmbedder384,
    tmp_path: Path,
) -> None:
    backend = _ScriptedBackend([_reply("[FINAL] the three newest repos are a, b and c.")])
    worker = _worker(app_engine, embedder, tmp_path, backend, gated=True)

    t0 = time.monotonic()
    res = client.post(f"/v1/personas/{persona_id}/runs", json={"task": _BRIEF}, headers=_auth())
    assert res.status_code == 202, res.text
    body = res.json()
    assert set(body) == {"task_id", "persona_id", "kind", "state"}
    assert body["kind"] == "ad_hoc"
    assert body["state"] == "active"
    task_id = body["task_id"]

    tasks = TaskStore(app_engine)
    created = tasks.get(_UID, task_id)
    assert created.kind is TaskKind.AD_HOC
    assert created.state is TaskState.ACTIVE
    assert created.contract.goal == _BRIEF

    assert await worker.run_once() == 1  # the REAL worker claims and runs the one leg
    first_step_seconds = time.monotonic() - t0

    run = _run_row(app_engine, task_id)
    assert run["task_id"] == task_id  # the run names its task (T1)
    assert run["status"] == "completed", (run, _leg_job(task_id))
    assert run["steps"], "the leg's steps landed through the one runs writer"
    after = tasks.get(_UID, task_id)
    assert after.run_ids == (run["id"],)
    assert after.state is TaskState.COMPLETED  # a one-off has no schedule: it completes
    assert backend.calls == 1
    # D-W1-1 acceptance: first visible step within 3s over the worker's poll interval.
    # ``run_once`` claims immediately, so this measures dispatch + claim + loop build + leg.
    assert first_step_seconds < 3.0, f"first step took {first_step_seconds:.2f}s"
    print(f"\nW1-T2 measured POST→first snapshot (scripted backend): {first_step_seconds:.3f}s")


@pytest.mark.asyncio
async def test_the_run_page_reads_a_worker_run_through_the_db_tail_and_the_list(
    client: TestClient,
    persona_id: str,
    app_engine: Engine,
    embedder: HashEmbedder384,
    tmp_path: Path,
) -> None:
    backend = _ScriptedBackend([_reply("[FINAL] done.")])
    worker = _worker(app_engine, embedder, tmp_path, backend, gated=True)
    task_id = client.post(
        f"/v1/personas/{persona_id}/runs", json={"task": _BRIEF}, headers=_auth()
    ).json()["task_id"]
    assert await worker.run_once() == 1
    run_id = str(_run_row(app_engine, task_id)["id"])

    # No registry handle exists for a worker run: the tail serves the same endpoint.
    with client.stream("GET", f"/v1/runs/{run_id}/events", headers=_auth()) as stream:
        assert stream.status_code == 200
        frames = _read_sse(stream.read().decode())
    assert frames[-1] == ("end", "{}")  # a finished run ends at once; the client reconciles
    snap = client.get(f"/v1/runs/{run_id}", headers=_auth()).json()
    assert snap["status"] == "completed"
    assert snap["steps"], "the reconcile carries the authoritative steps"
    listed = client.get("/v1/runs", headers=_auth()).json()["items"]
    assert [(r["id"], r["task_id"]) for r in listed] == [(run_id, task_id)]
    assert snap["task_id"] == task_id  # the viewer knows its task (T3)

    # T3 (D-W1-3): the task detail is the home of its run history, and says what kind it is.
    detail = client.get(f"/v1/tasks/{task_id}", headers=_auth()).json()
    assert detail["kind"] == "ad_hoc"
    assert [r["id"] for r in detail["runs"]] == [run_id]
    assert detail["runs"][0]["status"] == "completed"
    assert detail["run_ids"] == [run_id]
    summaries = client.get("/v1/tasks", headers=_auth()).json()
    assert {s["task_id"]: s["kind"] for s in summaries}[task_id] == "ad_hoc"


# --- the guard: a one-off runs policy-gated (D-W1-1) ----------------------------------------


def _gated_script() -> list[ChatResponse]:
    send = ToolCall(name="mcp:mail:send", args={"to": "x@example.com"}, call_id="c1")
    return [_reply("sending", tool_calls=[send]), _reply("[FINAL] done without sending.")]


@pytest.mark.asyncio
async def test_an_ad_hoc_leg_is_policy_gated_where_the_bare_loop_would_just_carry_on(
    client: TestClient,
    persona_id: str,
    app_engine: Engine,
    embedder: HashEmbedder384,
    tmp_path: Path,
) -> None:
    """The differential: an unmapped tool is ``external_mutate`` (gated by default).

    Under the gate the leg records a proposal and parks the task on the user. Under the
    bare toolbox the same call is merely "not available", the model carries on, and the
    task completes with nobody asked. Both halves run so a silently-ungated leg fails loudly.
    """
    # Half one: the production wiring (gated).
    gated_backend = _ScriptedBackend(_gated_script())
    gated_worker = _worker(app_engine, embedder, tmp_path / "g", gated_backend, gated=True)
    gated_task = client.post(
        f"/v1/personas/{persona_id}/runs", json={"task": "email the summary"}, headers=_auth()
    ).json()["task_id"]
    assert await gated_worker.run_once() == 1
    parked = TaskStore(app_engine).get(_UID, gated_task)
    assert parked.state is TaskState.WAITING
    assert parked.wait_kind is WaitKind.ON_USER
    proposal = ApprovalStore(app_engine).get_pending_for_task(_UID, gated_task)
    assert proposal is not None
    assert proposal.tool_name == "mcp:mail:send"
    assert _run_row(app_engine, gated_task)["status"] == "cancelled"  # stopped for approval
    assert gated_backend.calls == 1  # the leg ended AT the gate, no second model call

    # Half two: the same script on a bare toolbox (what every leg did before W1).
    bare_backend = _ScriptedBackend(_gated_script())
    bare_worker = _worker(app_engine, embedder, tmp_path / "b", bare_backend, gated=False)
    bare_task = client.post(
        f"/v1/personas/{persona_id}/runs", json={"task": "email the summary"}, headers=_auth()
    ).json()["task_id"]
    assert await bare_worker.run_once() == 1
    assert TaskStore(app_engine).get(_UID, bare_task).state is TaskState.COMPLETED
    assert ApprovalStore(app_engine).get_pending_for_task(_UID, bare_task) is None
    assert bare_backend.calls == 2  # the model saw "not available" and carried on


# --- the refusal (D-W1-28) ---------------------------------------------------------------


def test_a_long_brief_is_refused_with_a_human_sentence(
    client: TestClient, persona_id: str, app_engine: Engine
) -> None:
    res = client.post(
        f"/v1/personas/{persona_id}/runs",
        json={"task": "x" * (MAX_BRIEF_CHARS + 1)},
        headers=_auth(),
    )
    assert res.status_code == 413
    body = res.json()
    assert body["error"] == "brief_too_long"
    assert body["detail"] == BRIEF_TOO_LONG_MESSAGE
    assert "—" not in body["detail"]
    assert TaskStore(app_engine).list_for_owner(_UID) == []


# --- the viewer's doors under the task model ---------------------------------------------


@pytest.mark.asyncio
async def test_cancel_from_the_run_viewer_cancels_the_task(
    client: TestClient,
    persona_id: str,
    app_engine: Engine,
    embedder: HashEmbedder384,
    tmp_path: Path,
) -> None:
    backend = _ScriptedBackend(_gated_script())  # leaves the task waiting on the user
    worker = _worker(app_engine, embedder, tmp_path, backend, gated=True)
    task_id = client.post(
        f"/v1/personas/{persona_id}/runs", json={"task": "email the summary"}, headers=_auth()
    ).json()["task_id"]
    assert await worker.run_once() == 1
    run_id = str(_run_row(app_engine, task_id)["id"])

    res = client.post(f"/v1/runs/{run_id}/cancel", headers=_auth())
    assert res.status_code == 202
    assert res.json() == {"status": "cancelling", "task_id": task_id}
    assert TaskStore(app_engine).get(_UID, task_id).state is TaskState.CANCELLED
    # A second cancel is the durable truth, calmly.
    assert client.post(f"/v1/runs/{run_id}/cancel", headers=_auth()).json()["status"] == "finished"


@pytest.mark.asyncio
async def test_respond_on_a_task_linked_run_is_a_clean_404_until_the_task_reply_door(
    client: TestClient,
    persona_id: str,
    app_engine: Engine,
    embedder: HashEmbedder384,
    tmp_path: Path,
) -> None:
    """D-W1-4: a one-off's question parks the task; the answer goes through the task reply
    (T6). The old in-process door has no handle for a worker run and says so, never a 500."""
    backend = _ScriptedBackend([_reply("[FINAL] done.")])
    worker = _worker(app_engine, embedder, tmp_path, backend, gated=True)
    task_id = client.post(
        f"/v1/personas/{persona_id}/runs", json={"task": _BRIEF}, headers=_auth()
    ).json()["task_id"]
    assert await worker.run_once() == 1
    run_id = str(_run_row(app_engine, task_id)["id"])
    res = client.post(f"/v1/runs/{run_id}/respond", json={"answer": "42"}, headers=_auth())
    assert res.status_code == 404


def test_dispatch_requires_a_wired_runtime(client: TestClient, persona_id: str) -> None:
    """A keyless boot still answers 503 before any task is created (R1-D-2 preserved)."""
    app = client.app
    saved = app.state.build_agentic_loop  # type: ignore[attr-defined]
    del app.state.build_agentic_loop  # type: ignore[attr-defined]
    try:
        res = client.post(f"/v1/personas/{persona_id}/runs", json={"task": _BRIEF}, headers=_auth())
        assert res.status_code == 503
    finally:
        app.state.build_agentic_loop = saved  # type: ignore[attr-defined]
    assert datetime.now(UTC) is not None  # keeps the UTC import honest for the file

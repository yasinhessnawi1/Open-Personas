"""A pause pressed while a leg runs, through the real chain (R9-158).

The REAL app takes the dispatch; the REAL A0 ``Worker`` claims the leg and the REAL leg
handler runs it through the REAL runtime factory and ``AgenticLoop``; the REAL
continuation settles it on the REAL stores and job queue. A scripted model is the only
double, and it presses Pause through ``task_control_service.pause_task`` (the one pause
every door uses) from inside the model call of a chosen step, so where the pause lands is
exact rather than raced.

Each case asserts what the task page reads, as one tuple: the task row's state and
``paused``, the detail route's derived ``status``, and the run row statuses; and the
``jobs`` rows for the task, exactly. The owner ruled on 2026-09-26 that a pause pressed
while a leg finishes the work completes the task.

- the pause trips the leg and the step it opened finishes the work: COMPLETED, not paused;
- the pause trips a leg with work left: held ACTIVE and paused, no next leg; Resume puts it
  back from the salvaged head and it completes;
- the pause lands during the final call (nothing left to trip): COMPLETED, not paused;
- the pause trips the leg and the step it opened asks the user: parked on the question and
  still paused; Resume clears the overlay and queues nothing, the reply moves it;
- the pause lands after the leg's last boundary and the box ends it: held, no next leg.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from persona.backends import StreamChunk, TokenUsage
from persona.backends.types import ChatResponse
from persona.jobs import JobRegistry
from persona.tasks import LegBox, WaitKind
from persona_api.app import create_app
from persona_api.approvals import ApprovalStore
from persona_api.config import APIConfig
from persona_api.jobs import Worker
from persona_api.jobs.queue import JobQueue
from persona_api.middleware.rls_context import make_rls_engine
from persona_api.schedules import ScheduleStore
from persona_api.services import task_control_service
from persona_api.services.runtime_factory import RuntimeFactory
from persona_api.tasks.continuation import TaskContinuation
from persona_api.tasks.handler import register_task_leg_handler
from persona_api.tasks.leg_runner import RuntimeFactoryLegRunnerBuilder
from persona_api.tasks.store import CheckpointStore, TaskStore
from sqlalchemy import create_engine, text

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator

    from persona.schema.conversation import ConversationMessage
    from sqlalchemy import Engine
    from tests.conftest import HashEmbedder384

pytestmark = pytest.mark.integration

_UID = "user_r9_158"
_QUESTION = "Which account should I use?"
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


def _reply(content: str) -> ChatResponse:
    return ChatResponse(
        content=content,
        model="scripted",
        provider="anthropic",
        usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        latency_ms=1.0,
        tool_calls=[],
    )


class _ScriptedBackend:
    """Answers each ``chat`` call from a script; on call ``press_on`` it presses Pause first."""

    provider_name = "anthropic"
    model_name = "scripted"
    max_tokens = 4096

    def __init__(
        self,
        script: list[ChatResponse],
        *,
        press_on: int | None = None,
        press: Callable[[], None] | None = None,
    ) -> None:
        self._script = list(script)
        self._press_on = press_on
        self._press = press
        self.calls = 0

    @property
    def supports_native_tools(self) -> bool:
        return True

    @property
    def supports_vision(self) -> bool:
        return False

    async def chat(self, _messages: list[ConversationMessage], **_: object) -> ChatResponse:
        self.calls += 1
        if self.calls == self._press_on and self._press is not None:
            self._press()  # the user presses Pause while this step's model call is in flight
        return self._script[min(self.calls, len(self._script)) - 1]

    async def chat_stream(
        self, _messages: list[ConversationMessage], **_: object
    ) -> AsyncIterator[StreamChunk]:
        yield StreamChunk(delta="done", is_final=True, usage=TokenUsage(1, 1, 2))


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


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:  # noqa: ARG001, the fixture only orders setup
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip(
            "SKIPPED, NOT PASSED: export APP_DATABASE_URL (the persona_app non-superuser DSN) "
            "with PERSONA_TEST_DB=1 to run the pause-settles chain"
        )
    engine = make_rls_engine(app_url.replace("+asyncpg", "+psycopg"))
    yield engine
    engine.dispose()


@pytest.fixture
def su_engine() -> Iterator[Engine]:
    su = create_engine(os.environ["DATABASE_URL"].replace("+asyncpg", "+psycopg"))
    yield su
    su.dispose()


@pytest.fixture
def client(
    app_engine: Engine,  # noqa: ARG001, the fixture only orders setup
    embedder: HashEmbedder384,
    tmp_path: Path,
) -> Iterator[TestClient]:
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

        async def _build(persona_id: str) -> object:
            raise AssertionError(f"the api never builds a loop here ({persona_id})")

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
            conn.execute(text("DELETE FROM users WHERE id = :a"), {"a": _UID})
        su.dispose()


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {_UID}"}


@pytest.fixture
def persona_id(client: TestClient) -> str:
    return str(client.post("/v1/personas", json={"yaml": _YAML}, headers=_auth()).json()["id"])


def _worker(
    app_engine: Engine,
    su_engine: Engine,
    embedder: HashEmbedder384,
    tmp: Path,
    backend: _ScriptedBackend,
    *,
    box: LegBox | None = None,
) -> Worker:
    factory = RuntimeFactory(
        rls_engine=app_engine,
        embedder=embedder,
        tier_registry=_Registry(backend),  # type: ignore[arg-type]
        turn_log_writer=_NullTurnLog(),  # type: ignore[arg-type]
        audit_root=tmp / "worker-audit",
    )
    registry = JobRegistry()
    register_task_leg_handler(
        registry,
        task_store=TaskStore(app_engine),
        checkpoint_store=CheckpointStore(app_engine),
        runner_builder=RuntimeFactoryLegRunnerBuilder(factory, recorder=ApprovalStore(app_engine)),
        continuation=TaskContinuation(
            task_store=TaskStore(app_engine),
            queue=JobQueue(app_engine),
            checkpoint_store=CheckpointStore(app_engine),
            schedule_store=ScheduleStore(app_engine),
        ),
        rls_engine=app_engine,
        box=box,
    )
    return Worker(
        dispatch_engine=su_engine,
        rls_engine=app_engine,
        registry=registry,
        worker_id="w-r9-158",
    )


def _dispatch(client: TestClient, persona_id: str, brief: str) -> str:
    res = client.post(f"/v1/personas/{persona_id}/runs", json={"task": brief}, headers=_auth())
    assert res.status_code == 202, res.text
    return str(res.json()["task_id"])


def _press_pause(app_engine: Engine, task_id: str) -> Callable[[], None]:
    def press() -> None:
        task = TaskStore(app_engine).get(_UID, task_id)
        outcome = task_control_service.pause_task(app_engine, _UID, task, now=datetime.now(UTC))
        assert outcome.changed, outcome.note

    return press


def _resume(app_engine: Engine, task_id: str) -> None:
    task = TaskStore(app_engine).get(_UID, task_id)
    outcome = task_control_service.resume_task(
        app_engine, _UID, task, now=datetime.now(UTC), queue=JobQueue(app_engine)
    )
    assert outcome.changed, outcome.note


def _page(client: TestClient, app_engine: Engine, task_id: str) -> tuple[str, bool, str, list[str]]:
    """(task state, paused, derived status, run statuses oldest first), as the page reads it."""
    task = TaskStore(app_engine).get(_UID, task_id)
    detail = client.get(f"/v1/tasks/{task_id}", headers=_auth())
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["paused"] is task.paused  # the route and the row agree
    runs = sorted(body["runs"], key=lambda r: str(r["started_at"]))
    return (task.state.value, task.paused, body["status"], [str(r["status"]) for r in runs])


def _stop_reasons(client: TestClient, task_id: str) -> list[str | None]:
    """Why each run stopped early (R9-158), oldest first, as the detail route reports it."""
    body = client.get(f"/v1/tasks/{task_id}", headers=_auth()).json()
    runs = sorted(body["runs"], key=lambda r: str(r["started_at"]))
    return [r["stop_reason"] for r in runs]


def _leg_queued(client: TestClient, task_id: str) -> bool:
    """Whether the detail route says a leg is still to run (R9-158)."""
    return bool(client.get(f"/v1/tasks/{task_id}", headers=_auth()).json()["leg_queued"])


def _jobs(su: Engine, task_id: str) -> list[tuple[str, str]]:
    with su.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT state, idempotency_key FROM jobs "
                "WHERE type = 'task_leg' AND payload->>'task_id' = :t ORDER BY created_at"
            ),
            {"t": task_id},
        ).all()
    return [(str(r.state), str(r.idempotency_key)) for r in rows]


@pytest.mark.asyncio
async def test_a_pause_that_trips_a_leg_which_then_finishes_the_work_completes_the_task(
    client: TestClient,
    persona_id: str,
    app_engine: Engine,
    su_engine: Engine,
    embedder: HashEmbedder384,
    tmp_path: Path,
) -> None:
    task_id = _dispatch(client, persona_id, "summarise the newsletters")
    backend = _ScriptedBackend(
        [_reply("Reading the newsletters."), _reply("[FINAL] Three themes this week.")],
        press_on=1,  # pressed in step 0; the watcher reads it at step 1's boundary and trips
        press=_press_pause(app_engine, task_id),
    )
    assert await _worker(app_engine, su_engine, embedder, tmp_path, backend).run_once() == 1
    assert backend.calls == 2  # step 1 ran to its FINAL: no salvage call
    assert _page(client, app_engine, task_id) == ("completed", False, "completed", ["completed"])
    assert _stop_reasons(client, task_id) == [None]  # it ended on its own
    assert _jobs(su_engine, task_id) == [("succeeded", f"task:{task_id}:after:init")]


@pytest.mark.asyncio
async def test_a_pause_that_trips_a_leg_with_work_left_holds_it_until_resume(
    client: TestClient,
    persona_id: str,
    app_engine: Engine,
    su_engine: Engine,
    embedder: HashEmbedder384,
    tmp_path: Path,
) -> None:
    task_id = _dispatch(client, persona_id, "summarise the newsletters")
    backend = _ScriptedBackend(
        [
            _reply("Reading the first newsletter."),
            _reply("Reading the second newsletter."),
            _reply("Two newsletters read so far."),  # the salvage summary (R9-109)
        ],
        press_on=1,
        press=_press_pause(app_engine, task_id),
    )
    assert await _worker(app_engine, su_engine, embedder, tmp_path, backend).run_once() == 1
    assert backend.calls == 3
    assert _page(client, app_engine, task_id) == ("active", True, "paused", ["cancelled"])
    assert _stop_reasons(client, task_id) == ["paused"]
    assert _jobs(su_engine, task_id) == [("succeeded", f"task:{task_id}:after:init")]

    _resume(app_engine, task_id)
    assert _jobs(su_engine, task_id) == [
        ("succeeded", f"task:{task_id}:after:init"),
        ("queued", f"task:{task_id}:after:0"),  # from the salvaged head
    ]
    # Resume's first refresh: the old run is still the only row, and the route says the
    # next one is on its way, which is what the page shows as "Starting".
    assert (_stop_reasons(client, task_id), _leg_queued(client, task_id)) == (["paused"], True)
    finisher = _ScriptedBackend([_reply("[FINAL] Three themes this week.")])
    assert await _worker(app_engine, su_engine, embedder, tmp_path, finisher).run_once() == 1
    assert _page(client, app_engine, task_id) == (
        "completed",
        False,
        "completed",
        ["cancelled", "completed"],
    )
    assert _stop_reasons(client, task_id) == ["paused", None]
    assert _leg_queued(client, task_id) is False
    assert [state for state, _ in _jobs(su_engine, task_id)] == ["succeeded", "succeeded"]


@pytest.mark.asyncio
async def test_a_pause_during_the_final_call_completes_the_task_unpaused(
    client: TestClient,
    persona_id: str,
    app_engine: Engine,
    su_engine: Engine,
    embedder: HashEmbedder384,
    tmp_path: Path,
) -> None:
    task_id = _dispatch(client, persona_id, "summarise the newsletters")
    backend = _ScriptedBackend(
        [_reply("Reading the newsletters."), _reply("[FINAL] Three themes this week.")],
        press_on=2,  # pressed after the last boundary: nothing is left to trip
        press=_press_pause(app_engine, task_id),
    )
    assert await _worker(app_engine, su_engine, embedder, tmp_path, backend).run_once() == 1
    assert backend.calls == 2
    assert _page(client, app_engine, task_id) == ("completed", False, "completed", ["completed"])
    assert _stop_reasons(client, task_id) == [None]
    assert _jobs(su_engine, task_id) == [("succeeded", f"task:{task_id}:after:init")]


@pytest.mark.asyncio
async def test_a_pause_that_trips_a_leg_which_then_asks_parks_it_on_the_question(
    client: TestClient,
    persona_id: str,
    app_engine: Engine,
    su_engine: Engine,
    embedder: HashEmbedder384,
    tmp_path: Path,
) -> None:
    task_id = _dispatch(client, persona_id, "pay the electricity bill")
    backend = _ScriptedBackend(
        [_reply("Finding the bill."), _reply(f"[ASK_USER] {_QUESTION}")],
        press_on=1,
        press=_press_pause(app_engine, task_id),
    )
    assert await _worker(app_engine, su_engine, embedder, tmp_path, backend).run_once() == 1
    assert _page(client, app_engine, task_id) == ("waiting", True, "paused", ["awaiting_user"])
    assert _stop_reasons(client, task_id) == [None]  # it stopped ON a question, not early
    assert TaskStore(app_engine).get(_UID, task_id).wait_kind is WaitKind.ON_USER
    checkpoint = CheckpointStore(app_engine).get_latest(_UID, task_id)
    assert checkpoint is not None
    assert checkpoint.open_questions == (_QUESTION,)
    assert _jobs(su_engine, task_id) == [("succeeded", f"task:{task_id}:after:init")]

    # Resume clears a waiting task's overlay and queues nothing: the reply moves it on.
    _resume(app_engine, task_id)
    assert _page(client, app_engine, task_id) == (
        "waiting",
        False,
        "waiting_on_user",
        ["awaiting_user"],
    )
    assert _jobs(su_engine, task_id) == [("succeeded", f"task:{task_id}:after:init")]


@pytest.mark.asyncio
async def test_a_pause_after_the_last_boundary_of_a_leg_the_box_ends_holds_its_next_leg(
    client: TestClient,
    persona_id: str,
    app_engine: Engine,
    su_engine: Engine,
    embedder: HashEmbedder384,
    tmp_path: Path,
) -> None:
    # The box trips at step 1's boundary (max_steps=2), before the control watcher reads the
    # row, so the pause pressed in step 1 trips nothing. The leg continues; its next leg would
    # only be a job the claim consumes, so it is held.
    task_id = _dispatch(client, persona_id, "summarise the newsletters")
    backend = _ScriptedBackend(
        [
            _reply("Reading the first newsletter."),
            _reply("Reading the second newsletter."),
            _reply("Two newsletters read so far."),
        ],
        press_on=2,
        press=_press_pause(app_engine, task_id),
    )
    worker = _worker(app_engine, su_engine, embedder, tmp_path, backend, box=LegBox(max_steps=2))
    assert await worker.run_once() == 1
    assert backend.calls == 3
    assert _page(client, app_engine, task_id) == ("active", True, "paused", ["cancelled"])
    assert _stop_reasons(client, task_id) == ["steps"]  # the box stopped it, not the pause
    assert _jobs(su_engine, task_id) == [("succeeded", f"task:{task_id}:after:init")]

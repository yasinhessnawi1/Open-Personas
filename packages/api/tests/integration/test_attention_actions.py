"""The attention actions act, through the real chain (Spec W1, T6; D-W1-10 / D-W1-20 / D-W1-21).

The REAL app takes the routes; the REAL sweep parks a task on a REAL dead-lettered job (A0's
own enqueue → claim → run → dead); the REAL continuation resumes with the retry-suffixed key;
the REAL A0 ``Worker`` claims it and the REAL leg handler runs it through the REAL runtime
factory (a scripted model is the only double). What is pinned, each as a chain and never a
hand-forced end state:

- **pickup after a dead-letter** enqueues a NEW claimable job (the key carries the retry
  suffix, so the dead row no longer absorbs it) and the worker runs it to completion; a second
  pickup dedups to that one job (A0's invariant);
- **R9-130**: an approval answered after the park (the REAL resolver's decline path) enqueues
  a claimable job the worker runs;
- **reply** rides the text into the next leg's trigger;
- **cancel during a running leg** stops it at the next step boundary, the checkpoint lands,
  and nothing further is enqueued (D-W1-21, the seam R9-129 named);
- **a leg that asks** stops ON the question (Spec W1, T8): the run ends `awaiting_user`, the
  checkpoint carries the question, the task parks `waiting(on_user)` with `reply` offered, and
  the reply rides the next leg's trigger verbatim (D-W1-34);
- **retry** of a FAILED task runs it again as a NEW task;
- cross-tenant pickup is not found; a paused owner's pickup enqueues nothing;
- **resume after a pause** (R9-146): a task paused MID-LEG (the boundary stop, the checkpoint
  landed) and a task paused BEFORE its leg was claimed (the leg consumed at claim) both come
  back through the one resume seam, the leg runs again from the salvaged head, and the task
  completes; the chat verb rides the same seam.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from persona.approvals import ActionProposal
from persona.approvals.interpret import LexiconReplyInterpreter
from persona.backends import StreamChunk, TokenUsage
from persona.backends.types import ChatResponse
from persona.jobs import JobRegistry
from persona.tasks import ScheduledFire, TaskState, WaitKind
from persona.tools import ActionCategory
from persona_api.app import create_app
from persona_api.approvals import ApprovalStore
from persona_api.approvals.resolver import ApprovalResolver, ExecutedAction
from persona_api.config import APIConfig
from persona_api.jobs import Worker
from persona_api.jobs.queue import JobQueue
from persona_api.middleware.rls_context import make_rls_engine
from persona_api.schedules import ScheduleStore
from persona_api.services import task_control_service
from persona_api.services.runtime_factory import RuntimeFactory
from persona_api.tasks.continuation import TaskContinuation
from persona_api.tasks.handler import enqueue_task_leg, register_task_leg_handler
from persona_api.tasks.leg_runner import RuntimeFactoryLegRunnerBuilder
from persona_api.tasks.store import CheckpointStore, TaskStore
from sqlalchemy import create_engine, text

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator, Mapping

    from persona.schema.conversation import ConversationMessage
    from sqlalchemy import Engine
    from tests.conftest import HashEmbedder384

pytestmark = pytest.mark.integration

_UID = "user_w1_actions"
_OTHER = "user_w1_other"
_NOW = datetime(2026, 9, 6, 9, 0, tzinfo=UTC)
_DEAD_CAUSE = "every backend in MultiModelChatBackend exhausted"
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
    """Answers each ``chat`` call from a script (the last entry repeats); can pause mid-leg."""

    provider_name = "anthropic"
    model_name = "scripted"
    max_tokens = 4096

    def __init__(self, script: list[ChatResponse], *, gate: asyncio.Event | None = None) -> None:
        self._script = list(script)
        self._gate = gate
        self.calls = 0
        self.prompts: list[str] = []

    @property
    def supports_native_tools(self) -> bool:
        return True

    @property
    def supports_vision(self) -> bool:
        return False

    async def chat(self, messages: list[ConversationMessage], **_: object) -> ChatResponse:
        index = min(self.calls, len(self._script) - 1)
        self.calls += 1
        self.prompts.append("\n".join(m.content for m in messages))
        if self._gate is not None and self.calls == 2:
            await self._gate.wait()  # hold the SECOND call until the test says go
        return self._script[index]

    async def chat_stream(
        self, _messages: list[ConversationMessage], **_: object
    ) -> AsyncIterator[StreamChunk]:
        yield StreamChunk(delta="done", is_final=True, usage=TokenUsage(1, 1, 2))


def _reply(content: str) -> ChatResponse:
    return ChatResponse(
        content=content,
        model="scripted",
        provider="anthropic",
        usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        latency_ms=1.0,
        tool_calls=[],
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
            "SKIPPED, NOT PASSED: export APP_DATABASE_URL (the persona_app non-superuser DSN) "
            "with PERSONA_TEST_DB=1 to run the attention-actions chain"
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
    app_engine: Engine,  # noqa: ARG001 — ordering
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
            for uid in (_UID, _OTHER):
                conn.execute(
                    text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
                    {"i": uid, "e": f"{uid}@x"},
                )
        su.dispose()
        yield c
        su = make_rls_engine(os.environ["DATABASE_URL"])
        with su.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE id IN (:a, :b)"), {"a": _UID, "b": _OTHER})
        su.dispose()


def _auth(uid: str = _UID) -> dict[str, str]:
    return {"Authorization": f"Bearer {uid}"}


@pytest.fixture
def persona_id(client: TestClient) -> str:
    return str(client.post("/v1/personas", json={"yaml": _YAML}, headers=_auth()).json()["id"])


def _worker(
    app_engine: Engine,
    su_engine: Engine,
    embedder: HashEmbedder384,
    tmp: Path,
    backend: _ScriptedBackend,
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
    )
    return Worker(
        dispatch_engine=su_engine,
        rls_engine=app_engine,
        registry=registry,
        worker_id="w-w1-actions",
    )


def _dispatch(client: TestClient, persona_id: str, brief: str) -> str:
    res = client.post(f"/v1/personas/{persona_id}/runs", json={"task": brief}, headers=_auth())
    assert res.status_code == 202, res.text
    return str(res.json()["task_id"])


def _dead_letter_the_queued_leg(su: Engine, task_id: str) -> None:
    """Turn the task's queued first leg into a REAL dead-letter the way A0 does."""
    queue = JobQueue(su)
    claimed = queue.claim(worker_id="w-dead", lease_seconds=60, limit=10)
    job = next(j for j in claimed if j.payload.get("task_id") == task_id)
    for other in claimed:
        if other.id != job.id:  # put anything else back untouched
            queue.retry(job_id=other.id, worker_id="w-dead", error="not mine", scheduled_at=_NOW)
    assert queue.mark_running(job_id=job.id, worker_id="w-dead")
    assert queue.mark_dead(job_id=job.id, worker_id="w-dead", error=_DEAD_CAUSE)


def _park_via_the_sweep(app_engine: Engine, su: Engine, task_id: str) -> None:
    continuation = TaskContinuation(
        task_store=TaskStore(app_engine),
        queue=JobQueue(app_engine),
        checkpoint_store=CheckpointStore(app_engine),
    )
    assert continuation.sweep_dead_legs(JobQueue(su), now=_NOW) >= 1
    parked = TaskStore(app_engine).get(_UID, task_id)
    assert parked.state is TaskState.WAITING
    assert parked.wait_kind is WaitKind.ON_USER


def _jobs_for(su: Engine, task_id: str) -> list[Mapping[str, object]]:
    with su.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT state, idempotency_key, attempt, last_error FROM jobs "
                "WHERE type = 'task_leg' AND payload->>'task_id' = :t ORDER BY created_at"
            ),
            {"t": task_id},
        ).mappings()
        return [dict(r) for r in rows]


def _run_status(su: Engine, task_id: str) -> str:
    with su.begin() as conn:
        return str(
            conn.execute(
                text("SELECT status FROM runs WHERE task_id = :t ORDER BY started_at DESC"),
                {"t": task_id},
            ).scalar_one()
        )


# --- pickup after a dead-letter (D-W1-20) ---------------------------------------------------


@pytest.mark.asyncio
async def test_pickup_after_a_dead_letter_enqueues_a_claimable_leg_the_worker_runs(
    client: TestClient,
    persona_id: str,
    app_engine: Engine,
    su_engine: Engine,
    embedder: HashEmbedder384,
    tmp_path: Path,
) -> None:
    task_id = _dispatch(client, persona_id, "brief hacker news")
    _dead_letter_the_queued_leg(su_engine, task_id)
    _park_via_the_sweep(app_engine, su_engine, task_id)
    assert [j["state"] for j in _jobs_for(su_engine, task_id)] == ["dead"]

    # The attention item for it offers a pickup with the real cause.
    review = client.get("/v1/autonomy/review", headers=_auth()).json()
    waiting = next(s for s in review["sections"] if s["kind"] == "waiting")
    item = next(i for i in waiting["items"] if i["ref"]["id"] == task_id)
    assert item["detail"] == _DEAD_CAUSE
    assert "pickup" in item["actions"]

    res = client.post(f"/v1/tasks/{task_id}/pickup", headers=_auth())
    assert res.status_code == 200, res.text
    assert res.json()["changed"] is True
    jobs = _jobs_for(su_engine, task_id)
    assert [j["state"] for j in jobs] == ["dead", "queued"]
    # ``after:init`` because the park left the head where the dead job was enqueued; had the park
    # appended a checkpoint, the pickup would key ``after:0`` and the revival sweep would no
    # longer find the dead row at all (R9-173).
    assert jobs[1]["idempotency_key"] == f"task:{task_id}:after:init:retry:1"  # not absorbed

    # A second pickup dedups to that one queued job (A0's invariant, kept).
    again = client.post(f"/v1/tasks/{task_id}/pickup", headers=_auth())
    assert again.status_code == 200
    assert [j["state"] for j in _jobs_for(su_engine, task_id)] == ["dead", "queued"]

    backend = _ScriptedBackend([_reply("[FINAL] picked up and done.")])
    worker = _worker(app_engine, su_engine, embedder, tmp_path, backend)
    assert await worker.run_once() == 1
    assert TaskStore(app_engine).get(_UID, task_id).state is TaskState.COMPLETED
    assert "Pick this up where you left off." in backend.prompts[0]  # the trigger reached the leg
    assert [j["state"] for j in _jobs_for(su_engine, task_id)] == ["dead", "succeeded"]


# --- R9-130: an approval answered after the park -------------------------------------------


class _NoExecutor:
    async def execute(self, tool_name: str, arguments: Mapping[str, object]) -> ExecutedAction:  # noqa: ARG002
        raise AssertionError("a decline never executes")


class _QuietNotifier:
    async def ask(self, proposal: ActionProposal) -> None: ...
    async def reconfirm(self, proposal: ActionProposal) -> None: ...
    async def clarify(self, proposal: ActionProposal) -> None: ...
    async def remind(self, proposal: ActionProposal) -> None: ...
    async def expired(self, proposal: ActionProposal) -> None: ...


@pytest.mark.asyncio
async def test_an_approval_answered_after_a_dead_letter_park_resumes_the_task(
    client: TestClient,
    persona_id: str,
    app_engine: Engine,
    su_engine: Engine,
    embedder: HashEmbedder384,
    tmp_path: Path,
) -> None:
    """R9-130: the resolver resumes through the same seam; before W1 its enqueue collided
    with the dead row's key and the answer was lost until the archive sweep, a day later."""
    task_id = _dispatch(client, persona_id, "email the landlord about the deposit")
    _dead_letter_the_queued_leg(su_engine, task_id)
    _park_via_the_sweep(app_engine, su_engine, task_id)
    approvals = ApprovalStore(app_engine)
    approvals.create_proposal(
        ActionProposal(
            proposal_id="p-w1-130",
            owner_id=_UID,
            task_id=task_id,
            persona_id=persona_id,
            categories=frozenset({ActionCategory.COMMUNICATE_AS_USER}),
            tool_name="send_email",
            arguments={"to": "landlord@example.com"},
            description="Email the landlord about the deposit",
            created_at=_NOW,
        )
    )
    # A free-text reply is refused with a pointer at the approval (D-W1-4 / T6).
    refused = client.post(f"/v1/tasks/{task_id}/reply", json={"reply": "yes"}, headers=_auth())
    assert refused.status_code == 409
    assert refused.json()["error"] == "approval_pending"

    resolver = ApprovalResolver(
        approvals=approvals,
        tasks=TaskStore(app_engine),
        checkpoints=CheckpointStore(app_engine),
        continuation=TaskContinuation(
            task_store=TaskStore(app_engine),
            queue=JobQueue(app_engine),
            checkpoint_store=CheckpointStore(app_engine),
        ),
        interpreter=LexiconReplyInterpreter(),
        executor=_NoExecutor(),
        notifier=_QuietNotifier(),
    )
    outcome = await resolver.resolve(_UID, "p-w1-130", "no", "web", now=_NOW)
    assert outcome.resumed is True
    jobs = _jobs_for(su_engine, task_id)
    assert [j["state"] for j in jobs] == ["dead", "queued"]  # claimable, NOT absorbed
    # What this chain taught: the resolver writes its resolution checkpoint BEFORE it
    # resumes, so the head moves (init → 0) and the resume key never meets the dead row's
    # key at all. The approval half of R9-130 is safe by construction; the pickup and reply
    # half (no checkpoint write) was the real loss, and carries the retry suffix above.
    assert jobs[1]["idempotency_key"] == f"task:{task_id}:after:0"

    backend = _ScriptedBackend([_reply("[FINAL] understood, not sending.")])
    worker = _worker(app_engine, su_engine, embedder, tmp_path, backend)
    assert await worker.run_once() == 1
    assert TaskStore(app_engine).get(_UID, task_id).state is TaskState.COMPLETED


# --- reply rides into the next leg ----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_reply_reaches_the_next_leg_verbatim(
    client: TestClient,
    persona_id: str,
    app_engine: Engine,
    su_engine: Engine,
    embedder: HashEmbedder384,
    tmp_path: Path,
) -> None:
    task_id = _dispatch(client, persona_id, "book the dentist")
    _dead_letter_the_queued_leg(su_engine, task_id)
    _park_via_the_sweep(app_engine, su_engine, task_id)
    res = client.post(
        f"/v1/tasks/{task_id}/reply",
        json={"reply": "The one on Storgata, any morning."},
        headers=_auth(),
    )
    assert res.status_code == 200, res.text
    assert res.json()["changed"] is True
    backend = _ScriptedBackend([_reply("[FINAL] booked.")])
    worker = _worker(app_engine, su_engine, embedder, tmp_path, backend)
    assert await worker.run_once() == 1
    assert "the user replied: The one on Storgata, any morning." in backend.prompts[0]


# --- cancel during a running leg (D-W1-21, R9-129) -------------------------------------------


@pytest.mark.asyncio
async def test_a_cancel_during_a_running_leg_stops_it_at_the_next_boundary(
    client: TestClient,
    persona_id: str,
    app_engine: Engine,
    su_engine: Engine,
    embedder: HashEmbedder384,
    tmp_path: Path,
) -> None:
    """The control is read at every ``thinking`` event and takes effect at the top of the step
    after it (the A2 box semantics: never mid-step). The cancel lands while call 2 is held; the
    boundary before call 3 reads it and trips; the loop breaks at the top of step 3 with the
    salvage summary (R9-109), so the FINAL the script holds in reserve is never reached."""
    task_id = _dispatch(client, persona_id, "summarise the newsletters")
    gate = asyncio.Event()
    backend = _ScriptedBackend(
        [
            _reply("Reading the first newsletter."),
            _reply("Reading the second newsletter."),
            _reply("Reading the third newsletter."),
            _reply("[FINAL] summarised."),
        ],
        gate=gate,
    )
    worker = _worker(app_engine, su_engine, embedder, tmp_path, backend)
    running = asyncio.create_task(worker.run_once())
    for _ in range(200):
        if backend.calls >= 2:
            break
        await asyncio.sleep(0.05)
    assert backend.calls == 2  # the leg is mid-flight, holding on its second call

    res = client.post(f"/v1/tasks/{task_id}/cancel", headers=_auth())
    assert res.status_code == 200, res.text
    gate.set()
    assert await running == 1

    task = TaskStore(app_engine).get(_UID, task_id)
    assert task.state is TaskState.CANCELLED
    assert task.head_checkpoint_seq == 0  # the salvaged work landed as the checkpoint
    checkpoint = CheckpointStore(app_engine).get_latest(_UID, task_id)
    assert checkpoint is not None
    assert checkpoint.progress_conclusions  # the summary of what was read so far
    assert backend.calls == 4  # calls 2 and 3 finish, then the salvage summary; no FINAL
    jobs = _jobs_for(su_engine, task_id)
    assert [j["state"] for j in jobs] == ["succeeded"], jobs  # one execution, nothing further
    assert _run_status(su_engine, task_id) == "cancelled"


@pytest.mark.asyncio
async def test_a_leg_that_completes_after_the_cancel_settles_without_a_retry_storm(
    client: TestClient,
    persona_id: str,
    app_engine: Engine,
    su_engine: Engine,
    embedder: HashEmbedder384,
    tmp_path: Path,
) -> None:
    """The race the first chain exposed: the held call returns FINAL, so no boundary is left
    to trip. The handler must settle from the durable row (cancelled) instead of driving
    cancelled → completed, which raised, made A0 re-run the leg, and dead-lettered it."""
    task_id = _dispatch(client, persona_id, "summarise the newsletters")
    gate = asyncio.Event()
    backend = _ScriptedBackend(
        [_reply("Reading the first newsletter."), _reply("[FINAL] summarised.")], gate=gate
    )
    worker = _worker(app_engine, su_engine, embedder, tmp_path, backend)
    running = asyncio.create_task(worker.run_once())
    for _ in range(200):
        if backend.calls >= 2:
            break
        await asyncio.sleep(0.05)
    assert client.post(f"/v1/tasks/{task_id}/cancel", headers=_auth()).status_code == 200
    gate.set()
    assert await running == 1

    assert TaskStore(app_engine).get(_UID, task_id).state is TaskState.CANCELLED
    assert CheckpointStore(app_engine).get_latest(_UID, task_id) is not None  # landed
    jobs = _jobs_for(su_engine, task_id)
    assert [j["state"] for j in jobs] == ["succeeded"], jobs  # never retried, never dead
    assert backend.calls == 2


# --- retry of a FAILED task ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_runs_a_failed_task_again_as_a_new_task(
    client: TestClient,
    persona_id: str,
    app_engine: Engine,
    su_engine: Engine,
    embedder: HashEmbedder384,
    tmp_path: Path,
) -> None:
    task_id = _dispatch(client, persona_id, "renew the parking permit")
    _dead_letter_the_queued_leg(su_engine, task_id)
    store = TaskStore(app_engine)
    store.fail(_UID, task_id, now=_NOW)
    review = client.get("/v1/autonomy/review", headers=_auth()).json()
    stuck = next(s for s in review["sections"] if s["kind"] == "stuck")
    assert next(i for i in stuck["items"] if i["ref"]["id"] == task_id)["actions"] == ["retry"]

    res = client.post(f"/v1/tasks/{task_id}/retry", headers=_auth())
    assert res.status_code == 200, res.text
    successor = res.json()["successor_task_id"]
    assert successor
    assert successor != task_id
    fresh = store.get(_UID, successor)
    assert fresh.contract == store.get(_UID, task_id).contract
    assert fresh.kind is store.get(_UID, task_id).kind
    backend = _ScriptedBackend([_reply("[FINAL] renewed.")])
    worker = _worker(app_engine, su_engine, embedder, tmp_path, backend)
    assert await worker.run_once() == 1
    assert store.get(_UID, successor).state is TaskState.COMPLETED
    assert store.get(_UID, task_id).state is TaskState.FAILED  # the record of what failed stays


# --- fail closed (D-W1-10) -------------------------------------------------------------------


def test_cross_tenant_pickup_is_not_found(
    client: TestClient, persona_id: str, app_engine: Engine, su_engine: Engine
) -> None:
    task_id = _dispatch(client, persona_id, "their thing")
    _dead_letter_the_queued_leg(su_engine, task_id)
    _park_via_the_sweep(app_engine, su_engine, task_id)
    assert client.post(f"/v1/tasks/{task_id}/pickup", headers=_auth(_OTHER)).status_code == 404
    assert (
        client.post(
            f"/v1/tasks/{task_id}/reply", json={"reply": "x"}, headers=_auth(_OTHER)
        ).status_code
        == 404
    )
    assert [j["state"] for j in _jobs_for(su_engine, task_id)] == ["dead"]


def test_a_paused_owner_cannot_pick_up_and_nothing_is_enqueued(
    client: TestClient, persona_id: str, app_engine: Engine, su_engine: Engine
) -> None:
    task_id = _dispatch(client, persona_id, "brief hacker news")
    _dead_letter_the_queued_leg(su_engine, task_id)
    _park_via_the_sweep(app_engine, su_engine, task_id)
    assert client.post("/v1/autonomy/pause", headers=_auth()).status_code == 200
    res = client.post(f"/v1/tasks/{task_id}/pickup", headers=_auth())
    assert res.status_code == 200
    body = res.json()
    assert body["changed"] is False
    assert body["owner_autonomy_paused"] is True
    assert [j["state"] for j in _jobs_for(su_engine, task_id)] == ["dead"]
    assert client.post("/v1/autonomy/resume", headers=_auth()).status_code == 200


def test_a_long_reply_is_refused_with_a_human_sentence(client: TestClient, persona_id: str) -> None:
    task_id = _dispatch(client, persona_id, "x")
    res = client.post(f"/v1/tasks/{task_id}/reply", json={"reply": "y" * 8_001}, headers=_auth())
    assert res.status_code == 413
    assert "8,000" in res.json()["detail"]


# --- resume after a pause (R9-146; D-W1-29 / D-W1-30) -----------------------------------------


async def _wait_for_calls(backend: _ScriptedBackend, n: int) -> None:
    for _ in range(200):
        if backend.calls >= n:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"backend never reached {n} calls (got {backend.calls})")


@pytest.mark.asyncio
async def test_a_task_paused_mid_leg_resumes_from_the_salvaged_head_and_completes(
    client: TestClient,
    persona_id: str,
    app_engine: Engine,
    su_engine: Engine,
    embedder: HashEmbedder384,
    tmp_path: Path,
) -> None:
    """The pause reaches the running leg: it stops at the next boundary, the salvaged
    checkpoint lands (head 0), the task stays ACTIVE + paused and NO continuation is
    enqueued. Before the fix, resume cleared the overlay and nothing else: no job, no
    schedule, stalled forever (R9-146). Now resume rides the continuation from head 0 and
    the worker runs the next leg to completion."""
    task_id = _dispatch(client, persona_id, "summarise the newsletters")
    gate = asyncio.Event()
    backend = _ScriptedBackend(
        [
            _reply("Reading the first newsletter."),
            _reply("Reading the second newsletter."),
            _reply("Reading the third newsletter."),
            _reply("[FINAL] summarised."),
        ],
        gate=gate,
    )
    worker = _worker(app_engine, su_engine, embedder, tmp_path, backend)
    running = asyncio.create_task(worker.run_once())
    await _wait_for_calls(backend, 2)  # mid-flight, holding on its second call

    res = client.post(f"/v1/tasks/{task_id}/pause", headers=_auth())
    assert res.status_code == 200, res.text
    assert res.json()["changed"] is True
    gate.set()
    assert await running == 1

    store = TaskStore(app_engine)
    paused = store.get(_UID, task_id)
    assert paused.state is TaskState.ACTIVE
    assert paused.paused is True
    assert paused.head_checkpoint_seq == 0  # the boundary stop landed the salvaged work
    salvaged = CheckpointStore(app_engine).get_latest(_UID, task_id)
    assert salvaged is not None
    assert salvaged.progress_conclusions
    assert backend.calls == 4  # calls 2 and 3 finish, then the salvage summary; no FINAL
    jobs = _jobs_for(su_engine, task_id)
    assert [j["state"] for j in jobs] == ["succeeded"], jobs  # nothing further was enqueued

    res = client.post(f"/v1/tasks/{task_id}/resume", headers=_auth())
    assert res.status_code == 200, res.text
    assert res.json()["changed"] is True
    resumed = store.get(_UID, task_id)
    assert resumed.paused is False
    assert resumed.state is TaskState.ACTIVE
    jobs = _jobs_for(su_engine, task_id)
    assert [j["state"] for j in jobs] == ["succeeded", "queued"], jobs
    assert jobs[1]["idempotency_key"] == f"task:{task_id}:after:0"  # from the salvaged head

    finisher = _ScriptedBackend([_reply("[FINAL] summarised from where it left off.")])
    assert await _worker(app_engine, su_engine, embedder, tmp_path, finisher).run_once() == 1
    assert finisher.calls == 1
    # The salvaged checkpoint fed the next leg's reconstruction, verbatim.
    assert f"- {salvaged.progress_conclusions[0]}" in finisher.prompts[0]
    done = store.get(_UID, task_id)
    assert done.state is TaskState.COMPLETED
    assert done.head_checkpoint_seq == 1
    assert [j["state"] for j in _jobs_for(su_engine, task_id)] == ["succeeded", "succeeded"]


@pytest.mark.asyncio
async def test_a_task_paused_before_its_leg_was_claimed_resumes_past_the_consumed_row(
    client: TestClient,
    persona_id: str,
    app_engine: Engine,
    su_engine: Engine,
    embedder: HashEmbedder384,
    tmp_path: Path,
) -> None:
    """The claim-side guard CONSUMES a paused task's queued leg (the job succeeds without
    running). A resume at the same head (``after:init``) used to re-key onto that consumed
    row and vanish into the duplicate guard. The spent-attempt count (D-W1-29) suffixes the
    key past it, and the worker runs the leg."""
    task_id = _dispatch(client, persona_id, "brief hacker news")
    assert client.post(f"/v1/tasks/{task_id}/pause", headers=_auth()).status_code == 200
    backend = _ScriptedBackend([_reply("[FINAL] briefed.")])
    worker = _worker(app_engine, su_engine, embedder, tmp_path, backend)
    assert await worker.run_once() == 1  # claimed, skipped: consumed without running
    assert backend.calls == 0
    store = TaskStore(app_engine)
    assert store.get(_UID, task_id).head_checkpoint_seq is None
    jobs = _jobs_for(su_engine, task_id)
    assert [(j["state"], j["idempotency_key"]) for j in jobs] == [
        ("succeeded", f"task:{task_id}:after:init")
    ], jobs

    res = client.post(f"/v1/tasks/{task_id}/resume", headers=_auth())
    assert res.status_code == 200, res.text
    assert res.json()["changed"] is True
    jobs = _jobs_for(su_engine, task_id)
    assert [(j["state"], j["idempotency_key"]) for j in jobs] == [
        ("succeeded", f"task:{task_id}:after:init"),
        ("queued", f"task:{task_id}:after:init:retry:1"),
    ], jobs

    assert await worker.run_once() == 1
    assert backend.calls == 1
    assert store.get(_UID, task_id).state is TaskState.COMPLETED


@pytest.mark.asyncio
async def test_the_chat_resume_verb_puts_the_leg_back_like_the_route(
    client: TestClient,
    persona_id: str,
    app_engine: Engine,
    su_engine: Engine,
    embedder: HashEmbedder384,
    tmp_path: Path,
) -> None:
    """The chat turn's steering verb goes through the SAME seam as the route, built by the
    PRODUCTION composition (``compose_task_origination_services``), so a spoken "resume"
    also puts the leg back; a composition that fell back to the bare store fails here."""
    from persona.stores.postgres import PostgresBackend
    from persona_api.config import Edition
    from persona_api.services.task_origination_composition import (
        compose_task_origination_services,
    )

    task_id = _dispatch(client, persona_id, "brief hacker news")
    assert client.post(f"/v1/tasks/{task_id}/pause", headers=_auth()).status_code == 200
    backend = _ScriptedBackend([_reply("[FINAL] briefed.")])
    worker = _worker(app_engine, su_engine, embedder, tmp_path, backend)
    assert await worker.run_once() == 1  # consumed while paused
    assert backend.calls == 0

    steering = compose_task_origination_services(
        rls_engine=app_engine,
        memory_backend=PostgresBackend(engine=app_engine, embedder=embedder),
        edition=Edition.cloud,
        audit_root=tmp_path / "steer-audit",
    ).steering
    await steering.steer({"owner_id": _UID, "task_id": task_id, "verb": "resume"})
    jobs = _jobs_for(su_engine, task_id)
    assert [j["state"] for j in jobs] == ["succeeded", "queued"], jobs
    assert jobs[1]["idempotency_key"] == f"task:{task_id}:after:init:retry:1"
    assert await worker.run_once() == 1
    assert backend.calls == 1
    assert TaskStore(app_engine).get(_UID, task_id).state is TaskState.COMPLETED


# --- a leg that stops on a question (Spec W1, T8; D-W1-34) ------------------------------------


@pytest.mark.asyncio
async def test_a_leg_that_asks_parks_the_task_and_the_reply_carries_the_answer_back(
    client: TestClient,
    persona_id: str,
    app_engine: Engine,
    su_engine: Engine,
    embedder: HashEmbedder384,
    tmp_path: Path,
) -> None:
    """The chain D-W1-4 always promised and never had.

    A leg's model asks the user something only they know. There is no one to ask
    synchronously, so the leg stops ON the question: the run ends `awaiting_user`, the
    checkpoint carries the question, and the task parks `waiting(on_user)` with the
    question as the attention line's reason and text. The Reply verb then resumes it, the
    answer rides the next leg's trigger verbatim, and the task completes.

    Before this, the loop answered on the user's behalf ("proceed with your best judgment")
    and the task ran on, so the reply verb T6 and T7 built had no producer at all.
    """
    task_id = _dispatch(client, persona_id, "book me a dentist appointment next week")
    backend = _ScriptedBackend(
        [
            _reply("[ASK_USER] Which dentist, and which day suits you?"),
            _reply("[FINAL] Booked with Dr Lie on Tuesday."),
        ]
    )
    worker = _worker(app_engine, su_engine, embedder, tmp_path, backend)
    assert await worker.run_once() == 1
    assert backend.calls == 1  # it stopped ON the question; the reserve is untouched

    store = TaskStore(app_engine)
    parked = store.get(_UID, task_id)
    assert parked.state is TaskState.WAITING
    assert parked.wait_kind is WaitKind.ON_USER
    assert parked.head_checkpoint_seq == 0  # the leg DID work, so its checkpoint landed

    checkpoint = CheckpointStore(app_engine).get_latest(_UID, task_id)
    assert checkpoint is not None
    assert checkpoint.open_questions == ("Which dentist, and which day suits you?",)
    # A question is not an answer: the marker never reaches anything a person reads.
    assert all("[ASK_USER]" not in c for c in checkpoint.progress_conclusions)

    # The run is the record of the leg that asked: stopped, waiting on a person, and its
    # last step carries the unanswered question the run viewer offers to answer.
    with su_engine.begin() as conn:
        status, steps = conn.execute(
            text("SELECT status, steps FROM runs WHERE task_id = :t"), {"t": task_id}
        ).one()
    assert status == "awaiting_user"
    asked = [s for s in steps if s.get("question") and not s.get("user_answer")]
    assert asked, steps
    assert asked[-1]["question"] == "Which dentist, and which day suits you?"

    # The attention surface: ONE line, and it offers the verb that answers a question.
    review = client.get("/v1/autonomy/review", headers=_auth()).json()
    waiting = next(s for s in review["sections"] if s["kind"] == "waiting")
    item = next(i for i in waiting["items"] if i["ref"]["id"] == task_id)
    assert item["reason"] == "question"
    assert item["detail"] == "Which dentist, and which day suits you?"
    assert item["actions"] == ["reply", "cancel"]
    assert client.get("/v1/me/nav-counts", headers=_auth()).json()["attention"] == 1

    # Reply: the answer rides the next leg's trigger, verbatim.
    res = client.post(
        f"/v1/tasks/{task_id}/reply",
        json={"reply": "Dr Lie on Storgata, Tuesday afternoon."},
        headers=_auth(),
    )
    assert res.status_code == 200, res.text
    assert res.json()["changed"] is True
    jobs = _jobs_for(su_engine, task_id)
    assert [j["state"] for j in jobs] == ["succeeded", "queued"], jobs
    assert jobs[1]["idempotency_key"] == f"task:{task_id}:after:0"

    finisher = _ScriptedBackend([_reply("[FINAL] Booked with Dr Lie on Tuesday.")])
    assert await _worker(app_engine, su_engine, embedder, tmp_path, finisher).run_once() == 1
    assert "Dr Lie on Storgata, Tuesday afternoon." in finisher.prompts[0]
    assert "Which dentist, and which day suits you?" in finisher.prompts[0]  # its own question
    done = store.get(_UID, task_id)
    assert done.state is TaskState.COMPLETED
    assert client.get("/v1/me/nav-counts", headers=_auth()).json()["attention"] == 0


@pytest.mark.asyncio
async def test_a_parked_run_is_not_reaped_as_an_orphan_by_a_restart(
    client: TestClient,
    persona_id: str,
    app_engine: Engine,
    su_engine: Engine,
    embedder: HashEmbedder384,
    tmp_path: Path,
) -> None:
    """The restart sweep errors runs left in flight, on the premise that their in-process
    response queue died with the process. A parked TASK run breaks that premise: its answer
    comes back through the durable reply route, which no restart can lose. Erroring it would
    kill a question the user is about to answer."""
    from persona_api.background.restart_sweep import reconcile_in_flight_on_startup

    task_id = _dispatch(client, persona_id, "book me a dentist appointment")
    backend = _ScriptedBackend([_reply("[ASK_USER] Which day?"), _reply("[FINAL] done")])
    assert await _worker(app_engine, su_engine, embedder, tmp_path, backend).run_once() == 1

    reconcile_in_flight_on_startup(engine=su_engine)

    with su_engine.begin() as conn:
        status = conn.execute(
            text("SELECT status FROM runs WHERE task_id = :t"), {"t": task_id}
        ).scalar_one()
    assert status == "awaiting_user"  # spared
    assert TaskStore(app_engine).get(_UID, task_id).state is TaskState.WAITING
    # And the question is still answerable after the "restart".
    assert (
        client.post(
            f"/v1/tasks/{task_id}/reply", json={"reply": "Tuesday"}, headers=_auth()
        ).status_code
        == 200
    )


@pytest.mark.asyncio
async def test_the_line_shows_the_question_the_task_is_parked_on_now(
    client: TestClient,
    persona_id: str,
    app_engine: Engine,
    su_engine: Engine,
    embedder: HashEmbedder384,
    tmp_path: Path,
) -> None:
    """Spec W1 (D-W1-35): an ANSWERED question must not keep asking.

    Every checkpoint writer copies ``prior.open_questions`` forward and nothing removed one,
    so the shape below left the review page showing a question the user had already answered:
    park on Q1, answer it, the persona asks Q2 and parks again, and the line still read Q1.
    The user would answer a resolved question, and every later leg was told in its own
    reconstruction that the answered one was still open, which invites re-asking.
    """
    task_id = _dispatch(client, persona_id, "book me a dentist appointment next week")
    first = "Which dentist, and which day suits you?"
    second = "Morning or afternoon on Tuesday?"
    worker = _worker(
        app_engine,
        su_engine,
        embedder,
        tmp_path,
        _ScriptedBackend([_reply(f"[ASK_USER] {first}")]),
    )
    assert await worker.run_once() == 1

    def waiting_line() -> Mapping[str, object]:
        review = client.get("/v1/autonomy/review", headers=_auth()).json()
        section = next(s for s in review["sections"] if s["kind"] == "waiting")
        return next(i for i in section["items"] if i["ref"]["id"] == task_id)

    assert waiting_line()["detail"] == first

    # The user answers, and the persona comes back with a DIFFERENT question.
    assert (
        client.post(
            f"/v1/tasks/{task_id}/reply",
            json={"reply": "Dr Lie on Storgata, Tuesday."},
            headers=_auth(),
        ).status_code
        == 200
    )
    second_leg = _worker(
        app_engine,
        su_engine,
        embedder,
        tmp_path,
        _ScriptedBackend([_reply(f"[ASK_USER] {second}")]),
    )
    assert await second_leg.run_once() == 1

    # The line asks what is actually open now, and the answered one is gone for good.
    line = waiting_line()
    assert line["detail"] == second
    assert line["reason"] == "question"
    checkpoint = CheckpointStore(app_engine).get_latest(_UID, task_id)
    assert checkpoint is not None
    assert checkpoint.open_questions == (second,)
    # And the next leg's reconstruction cannot re-raise the answered one.
    detail = client.get(f"/v1/tasks/{task_id}", headers=_auth()).json()
    assert detail["open_questions"] == [second]


def test_the_unused_helper_keeps_the_imports_honest() -> None:
    assert task_control_service.PICKUP_REPLY
    assert ScheduledFire(schedule_id="s", fire_time=_NOW).kind == "scheduled_fire"
    assert enqueue_task_leg is not None

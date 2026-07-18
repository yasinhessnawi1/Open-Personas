"""Agentic runs: start / events / respond / cancel (spec 08, T11, KEYSTONE 2).

Drives the real app + Docker Postgres with a fake verifier and a SCRIPTED
AgenticLoop injected via app.state.build_agentic_loop. Covers: start → SSE
events → completed + persisted (#6); ask-user → /respond delivers the answer
(#6); /cancel → status cancelled (#7).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from persona_api.app import create_app
from persona_api.config import APIConfig
from persona_api.middleware.rls_context import make_rls_engine
from persona_runtime.agentic.events import RunEvent
from persona_runtime.agentic.run import Run, RunStatus
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator

    from persona_runtime.agentic.run import CancelToken
    from sqlalchemy import Engine
    from tests.conftest import HashEmbedder384

pytestmark = pytest.mark.integration

_YAML = """\
schema_version: "1.0"
identity:
  name: A
  role: assistant
  background: |
    x
  language_default: en
  constraints: []
"""


class _ScriptedAgenticLoop:
    """Emits a few events, optionally asks the user, respects cancellation.

    ``mode``: "plain" (run to completion), "ask" (ask the user then complete),
    "cancellable" (poll the cancel token at step boundaries → cancelled).
    """

    def __init__(self, persona_id: str, *, mode: str = "plain") -> None:
        self._persona_id = persona_id
        self._mode = mode

    async def run(
        self,
        task: str,
        on_event: Callable[[RunEvent], Awaitable[None]] | None = None,
        user_respond: Callable[[str], Awaitable[str]] | None = None,
        cancel_token: CancelToken | None = None,
        # Spec M3 (T4a): the run worker now passes a per-step billing callback when
        # a credits policy is wired (as create_app does here). This lifecycle double
        # accepts-and-ignores it — the T4a billing hook itself is exercised by
        # test_task_leg_handler.py; wiring it here would let a per-step capture flip
        # the cancel token and perturb the status/event assertions below.
        on_step_usage: Callable[..., Awaitable[None]] | None = None,  # noqa: ARG002
    ) -> Run:
        import asyncio

        started = datetime.now(UTC)

        async def emit(ev: RunEvent) -> None:
            if on_event is not None:
                await on_event(ev)

        def terminal(status: RunStatus, output: str | None = None) -> Run:
            return Run(
                id="",
                persona_id=self._persona_id,
                task=task,
                status=status,
                output=output,
                started_at=started,
                finished_at=datetime.now(UTC),
            )

        await emit(RunEvent.started(task))

        if self._mode == "cancellable":
            # Poll the cancel token at step boundaries (D-06-7 semantics).
            for step in range(100):
                if cancel_token is not None and cancel_token.is_cancelled:
                    await emit(RunEvent.cancelled(step))
                    return terminal(RunStatus.CANCELLED)
                await emit(RunEvent.thinking(step))
                await asyncio.sleep(0.02)
            return terminal(RunStatus.MAX_STEPS_REACHED, "gave up")

        await emit(RunEvent.thinking(0))
        answer = None
        if self._mode == "ask" and user_respond is not None:
            await emit(RunEvent.asking_user(0, "What is your apartment number?"))
            answer = await user_respond("What is your apartment number?")
            await emit(RunEvent.user_responded(1))
        output = f"done: {answer}" if answer else "done"
        await emit(RunEvent.completed(1, output))
        return terminal(RunStatus.COMPLETED, output)


@pytest.fixture
def client(
    migrated_engine: Engine,  # noqa: ARG001
    embedder: HashEmbedder384,
    tmp_path: object,
) -> Iterator[tuple[TestClient, str, str]]:
    import os

    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    cfg = APIConfig(app_database_url=app_url, audit_root=str(tmp_path) + "/audit")
    app = create_app(cfg)

    from persona_api.auth import AuthenticatedUser

    async def _verify(token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id=token, email=None)

    # default: a non-asking loop; individual tests can replace app.state.build_agentic_loop
    uid = "user_t11"
    with TestClient(app) as c:
        app.state.verify_token = _verify
        app.state.embedder = embedder
        # Drop the lifespan-installed TierRegistry so the persona-detail
        # capabilities surface doesn't lazily instantiate a real chat backend
        # (AuthenticationError("missing API key") on CI without ANTHROPIC_API_KEY).
        if hasattr(app.state, "tier_registry"):
            app.state.tier_registry = None

        async def _build(persona_id: str) -> _ScriptedAgenticLoop:
            return _ScriptedAgenticLoop(persona_id, mode="plain")

        app.state.build_agentic_loop = _build
        su = make_rls_engine(os.environ["DATABASE_URL"])
        with su.begin() as conn:
            conn.execute(
                text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
                {"i": uid, "e": f"{uid}@x"},
            )
        su.dispose()
        pid = c.post(
            "/v1/personas", json={"yaml": _YAML}, headers={"Authorization": f"Bearer {uid}"}
        ).json()["id"]
        yield c, uid, pid
        su = make_rls_engine(os.environ["DATABASE_URL"])
        with su.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE id = :i"), {"i": uid})
        su.dispose()


def _auth(uid: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {uid}"}


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


def _poll_run(
    c: TestClient, run_id: str, uid: str, *, terminal: tuple[str, ...]
) -> dict[str, object]:
    """Poll GET /runs/{id} until status is terminal (the background task may not
    have committed the final Run the instant the SSE stream ends). Bounded."""
    import time

    deadline = time.monotonic() + 5.0
    last: dict[str, object] = {}
    while time.monotonic() < deadline:
        last = c.get(f"/v1/runs/{run_id}", headers=_auth(uid)).json()
        if last.get("status") in terminal:
            return last
        time.sleep(0.05)
    return last


def test_list_runs(client: tuple[TestClient, str, str]) -> None:
    """Spec 35: GET /v1/runs lists the caller's runs (newest first, light)."""
    c, uid, pid = client
    r1 = c.post(f"/v1/personas/{pid}/runs", json={"task": "first task"}, headers=_auth(uid))
    r2 = c.post(f"/v1/personas/{pid}/runs", json={"task": "second task"}, headers=_auth(uid))
    assert r1.status_code == 202
    assert r2.status_code == 202

    lst = c.get("/v1/runs", headers=_auth(uid))
    assert lst.status_code == 200, lst.text
    items = lst.json()["items"]
    tasks = [it["task"] for it in items]
    assert "first task" in tasks
    assert "second task" in tasks
    # Newest first (each POST is its own txn → distinct started_at).
    assert tasks.index("second task") < tasks.index("first task")
    # Light projection: persona + status + started_at, NO heavy steps JSON.
    sample = items[tasks.index("second task")]
    assert sample["persona_id"] == pid
    assert "started_at" in sample
    assert "steps" not in sample


def test_start_stream_complete(client: tuple[TestClient, str, str]) -> None:
    c, uid, pid = client
    r = c.post(f"/v1/personas/{pid}/runs", json={"task": "do a thing"}, headers=_auth(uid))
    assert r.status_code == 202, r.text
    run_id = r.json()["id"]
    assert r.json()["status"] == "running"

    # stream events until end
    resp = c.get(f"/v1/runs/{run_id}/events", headers=_auth(uid))
    assert resp.status_code == 200
    kinds = [e for e, _ in _read_sse(resp.text)]
    assert "started" in kinds
    assert "completed" in kinds
    assert kinds[-1] == "end"

    # the run persisted as completed with output (poll: the background task may
    # commit the final Run just after the SSE stream's end sentinel)
    got = _poll_run(c, run_id, uid, terminal=("completed", "error", "cancelled"))
    assert got["status"] == "completed"
    assert got["output"] == "done"


def test_ask_user_respond_delivers_answer(client: tuple[TestClient, str, str]) -> None:
    c, uid, pid = client

    async def _build_asking(persona_id: str) -> _ScriptedAgenticLoop:
        return _ScriptedAgenticLoop(persona_id, mode="ask")

    c.app.state.build_agentic_loop = _build_asking  # type: ignore[attr-defined]

    run_id = c.post(f"/v1/personas/{pid}/runs", json={"task": "ask me"}, headers=_auth(uid)).json()[
        "id"
    ]
    # the run is now blocked awaiting the answer; respond
    rr = c.post(f"/v1/runs/{run_id}/respond", json={"answer": "3B"}, headers=_auth(uid))
    assert rr.status_code == 204
    # drain events → completed with the answer woven in
    resp = c.get(f"/v1/runs/{run_id}/events", headers=_auth(uid))
    kinds = [e for e, _ in _read_sse(resp.text)]
    assert "asking_user" in kinds
    assert "user_responded" in kinds
    got = _poll_run(c, run_id, uid, terminal=("completed", "error", "cancelled"))
    assert got["output"] == "done: 3B"


def test_cancel_sets_status_cancelled(client: tuple[TestClient, str, str]) -> None:
    c, uid, pid = client

    # a loop that polls the cancel token at step boundaries (D-06-7) so /cancel
    # drives it to a terminal `cancelled` status.
    async def _build_cancellable(persona_id: str) -> _ScriptedAgenticLoop:
        return _ScriptedAgenticLoop(persona_id, mode="cancellable")

    c.app.state.build_agentic_loop = _build_cancellable  # type: ignore[attr-defined]
    run_id = c.post(f"/v1/personas/{pid}/runs", json={"task": "long"}, headers=_auth(uid)).json()[
        "id"
    ]
    rc = c.post(f"/v1/runs/{run_id}/cancel", headers=_auth(uid))
    assert rc.status_code == 202
    assert rc.json()["status"] == "cancelling"
    # the loop checks the token at its next step boundary → status `cancelled` (#7)
    got = _poll_run(c, run_id, uid, terminal=("cancelled", "completed", "error"))
    assert got["status"] == "cancelled"

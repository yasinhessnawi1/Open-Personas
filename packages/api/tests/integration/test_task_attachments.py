"""Issue #16 - a file attached to a hand-off reaches the persona that runs the work.

Real Postgres, the real app, the real routes. The chain under test is the one a person
actually walks: upload the file through the same ``/uploads`` endpoint chat uses, hand the
task over with the returned ref, and reopen the task. What is pinned:

- the ref lands on the TASK CONTRACT, which is what every leg re-reads, so this is not a
  form field that evaporates at dispatch;
- the task detail returns the attachments on a FRESH fetch, so reopening the page shows
  what the persona works from;
- a routine created through the calendar door carries them on its backing task, which is
  the same contract every occurrence runs against;
- the leg's reconstructed context names each file and the tool that opens it.
"""

# ruff: noqa: ARG001 - ``migrated_engine`` is a dependency-ordering fixture param.
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from persona.schedules import RecurrenceKind, RecurrencePattern
from persona.tasks import ContractAttachment, UserDispatch, reconstruct_context
from persona_api.app import create_app
from persona_api.config import APIConfig
from persona_api.middleware.rls_context import make_rls_engine
from persona_api.schedules import ScheduleStore
from persona_api.services.schedule_create_service import create_user_schedule
from persona_api.tasks.store import TaskStore
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy import Engine
    from tests.conftest import HashEmbedder384

pytestmark = pytest.mark.integration

_UID = "user_issue16_attachments"
_NOW = datetime(2026, 9, 19, 9, 0, tzinfo=UTC)
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


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip(
            "SKIPPED, NOT PASSED: export APP_DATABASE_URL (the persona_app non-superuser "
            "DSN) with PERSONA_TEST_DB=1 to run the task attachment chain"
        )
    engine = make_rls_engine(app_url.replace("+asyncpg", "+psycopg"))
    yield engine
    engine.dispose()


@pytest.fixture
def client(
    app_engine: Engine,
    embedder: HashEmbedder384,
    tmp_path: Path,
) -> Iterator[TestClient]:
    cfg = APIConfig(
        app_database_url=os.environ["APP_DATABASE_URL"].replace("+asyncpg", "+psycopg"),
        audit_root=str(tmp_path / "audit"),
        workspace_root=str(tmp_path / "workspace"),
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


def _upload(client: TestClient, persona_id: str, name: str, body: bytes) -> dict[str, str]:
    """Attach one file through the same endpoint the chat composer posts to."""
    resp = client.post(
        f"/v1/personas/{persona_id}/uploads",
        headers=_auth(),
        files={"file": (name, body, "text/markdown")},
        data={"scope": "task"},
    )
    assert resp.status_code == 201, resp.text
    payload = resp.json()
    return {
        "ref": payload["workspace_path"],
        "filename": payload["filename"],
        "media_type": payload["media_type"],
    }


def _contract_attachments(*refs: dict[str, str]) -> list[ContractAttachment]:
    """The upload responses as the contract's own type (what the route does internally)."""
    return [
        ContractAttachment(ref=r["ref"], filename=r["filename"], media_type=r["media_type"])
        for r in refs
    ]


def test_two_attached_files_ride_the_contract_and_reopen_on_the_detail(
    client: TestClient, persona_id: str, app_engine: Engine
) -> None:
    first = _upload(client, persona_id, "quarter.md", b"# Q3\nrevenue up\n")
    second = _upload(client, persona_id, "rows.csv", b"a,b\n1,2\n")

    dispatched = client.post(
        f"/v1/personas/{persona_id}/runs",
        headers=_auth(),
        json={"task": "summarise these two files", "attachments": [first, second]},
    )
    assert dispatched.status_code == 202, dispatched.text
    task_id = dispatched.json()["task_id"]

    # On the CONTRACT, which is what every leg re-reads - not a transient form field.
    stored = TaskStore(app_engine).get(_UID, task_id)
    assert [a.ref for a in stored.contract.attachments] == [first["ref"], second["ref"]]
    assert [a.filename for a in stored.contract.attachments] == ["quarter.md", "rows.csv"]

    # A fresh fetch of the detail: reopening the task shows the same files.
    detail = client.get(f"/v1/tasks/{task_id}", headers=_auth())
    assert detail.status_code == 200, detail.text
    assert [a["ref"] for a in detail.json()["attachments"]] == [first["ref"], second["ref"]]
    assert [a["filename"] for a in detail.json()["attachments"]] == ["quarter.md", "rows.csv"]

    # And the leg's own context names each file and the tool that opens it.
    rendered = reconstruct_context(
        contract=stored.contract, trigger=UserDispatch(dispatched_at=_NOW)
    )[0].content
    assert first["ref"] in rendered
    assert second["ref"] in rendered
    assert "file_read" in rendered


def test_a_task_with_nothing_attached_reports_an_empty_list(
    client: TestClient, persona_id: str
) -> None:
    dispatched = client.post(
        f"/v1/personas/{persona_id}/runs",
        headers=_auth(),
        json={"task": "just think about it"},
    )
    assert dispatched.status_code == 202, dispatched.text
    detail = client.get(f"/v1/tasks/{dispatched.json()['task_id']}", headers=_auth())
    assert detail.json()["attachments"] == []


def test_a_routine_carries_its_files_on_the_backing_task(
    client: TestClient, persona_id: str, app_engine: Engine
) -> None:
    # Every occurrence of a routine runs against ONE backing task contract, so attaching a
    # file to the routine attaches it to occurrence one and occurrence forty alike.
    attached = _upload(client, persona_id, "checklist.md", b"- water the plants\n")
    result = create_user_schedule(
        app_engine,
        ScheduleStore(app_engine),
        TaskStore(app_engine),
        owner_id=_UID,
        pattern=RecurrencePattern(kind=RecurrenceKind.DAILY, hour=9, minute=0),
        one_time_at=None,
        timezone="Europe/Oslo",
        persona_id=persona_id,
        subject="run the morning checklist",
        idempotency_key="issue16-routine-key",
        now=_NOW - timedelta(hours=1),
        attachments=_contract_attachments(attached),
    )
    backing = TaskStore(app_engine).get(_UID, result.task_id)
    assert [a.ref for a in backing.contract.attachments] == [attached["ref"]]
    assert (
        "checklist.md"
        in reconstruct_context(contract=backing.contract, trigger=UserDispatch(dispatched_at=_NOW))[
            0
        ].content
    )

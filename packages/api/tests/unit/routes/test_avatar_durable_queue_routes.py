"""Avatar generation picks the durable queue by itself when a worker can run it.

The cutover flag (``PERSONA_API_AVATAR_VIA_QUEUE``) was meant to be flipped once
the worker was deployed and never was, so production kept running avatar
generation inside the request process where a restart lost it silently. The
path selection is now automatic: the create and regenerate routes enqueue a
durable job when THIS process's worker carries the ``avatar_generation`` handler,
and fall back to the in-request ``BackgroundTasks`` path otherwise (community
without a worker, keyless boots). ``PERSONA_API_AVATAR_INLINE_ONLY`` is the only
knob left, an explicit opt-out.

No Postgres: the queue and the worker handle are fakes that record what the route
asked of them; the enrichment hooks are recorders, as in
``test_create_async_enrichment``.
"""

# ruff: noqa: ANN401, ARG001, ARG002 — the fakes mirror real keyword signatures
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import BackgroundTasks, FastAPI
from persona.imagegen import SyntheticPersonaHasNoPortraitError
from persona.jobs import JobState
from persona_api.config import Edition
from persona_api.errors import register_exception_handlers
from persona_api.jobs.handlers.avatar import AVATAR_JOB_TYPE
from persona_api.jobs.queue import JobRecord
from persona_api.routes import personas as personas_routes

_OWNER = "u_queue"
_PERSONA = "persona_queue"
_YAML = "schema_version: '1.0'\nidentity:\n  name: Astrid\n"


class _FakeQueue:
    """Records enqueues; serves a scripted ``latest`` for the read side."""

    def __init__(self, latest: JobRecord | None = None) -> None:
        self.enqueued: list[dict[str, Any]] = []
        self._latest = latest

    def enqueue(self, **kwargs: Any) -> None:
        self.enqueued.append(kwargs)

    def latest(self, *, owner_id: str, job_type: str, idempotency_key_prefix: str) -> Any:
        return self._latest


def _worker(*types: str) -> SimpleNamespace:
    """A stand-in for the started in-process worker handle: only its job types matter."""
    return SimpleNamespace(job_types=frozenset(types))


def _row() -> dict[str, Any]:
    return {
        "id": _PERSONA,
        "yaml": _YAML,
        "schema_version": "1.0",
        "avatar_url": None,
        "consent_to_auto_dispatch": None,
        "consent_updated_at": None,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }


def _job(state: JobState, *, key: str = f"avatar:{_PERSONA}:create") -> JobRecord:
    now = datetime.now(UTC)
    return JobRecord(
        id="job-avatar",
        type=AVATAR_JOB_TYPE,
        owner_id=_OWNER,
        payload={"persona_id": _PERSONA},
        idempotency_key=key,
        state=state,
        priority=0,
        attempt=1,
        max_attempts=3,
        scheduled_at=now,
        created_at=now,
        lease_expires_at=None,
        locked_by=None,
        last_error="provider down" if state in (JobState.DEAD, JobState.FAILED) else None,
    )


@pytest.fixture(autouse=True)
def recorded_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the enrichment hooks with recorders and stub the persistence seams."""
    calls: list[str] = []

    async def _fake_voice(
        request: object, *, owner_id: str, persona_id: str, yaml_str: str
    ) -> None:
        calls.append("voice")

    async def _fake_avatar(
        request: object,
        *,
        owner_id: str,
        persona_id: str,
        yaml_str: str,
        billing_key: str | None = None,
    ) -> None:
        calls.append("avatar")

    monkeypatch.setattr(personas_routes.voice_assignment_service, "maybe_assign_voice", _fake_voice)
    monkeypatch.setattr(personas_routes, "_maybe_generate_avatar", _fake_avatar)
    monkeypatch.setattr(personas_routes.persona_service, "create_persona", lambda **_: _PERSONA)
    monkeypatch.setattr(personas_routes.persona_service, "get_persona", lambda **_: _row())
    monkeypatch.setattr(personas_routes.audit_service, "record", lambda **_: None)
    monkeypatch.setattr(personas_routes, "_tier_registry", lambda _r: None)
    monkeypatch.setattr(
        personas_routes.notifications_service, "publish_sidebar_changed", lambda *_a, **_k: None
    )
    return calls


def _request(
    *,
    job_queue: _FakeQueue | None,
    in_process_worker: SimpleNamespace | None,
    inline_only: bool = False,
    image_backend: object | None = object(),
) -> SimpleNamespace:
    state = SimpleNamespace(
        rls_engine=object(),
        embedder=object(),
        audit_root="/tmp/audit",
        config=SimpleNamespace(edition=Edition.cloud, avatar_inline_only=inline_only),
        job_queue=job_queue,
        in_process_worker=in_process_worker,
        image_backend=image_backend,
        file_storage=object(),
    )
    return SimpleNamespace(app=SimpleNamespace(state=state))


def _create(
    request: SimpleNamespace, *, avatar_url: str | None = None, yaml_str: str = _YAML
) -> tuple[Any, BackgroundTasks]:
    background = BackgroundTasks()
    detail = asyncio.run(
        personas_routes.create_persona(
            SimpleNamespace(yaml=yaml_str, avatar_url=avatar_url),  # type: ignore[arg-type]
            request,  # type: ignore[arg-type]
            background,
            SimpleNamespace(id=_OWNER, email=None),  # type: ignore[arg-type]
        )
    )
    return detail, background


# ---------------------------------------------------------------------------
# Create: the path is chosen by what can consume it, not by a flag.
# ---------------------------------------------------------------------------


def test_create_enqueues_when_the_worker_carries_the_avatar_handler(
    recorded_calls: list[str],
) -> None:
    """Queue present + worker registered the type: one durable job, no inline generation."""
    queue = _FakeQueue()
    detail, background = _create(
        _request(job_queue=queue, in_process_worker=_worker(AVATAR_JOB_TYPE))
    )

    assert [j["type"] for j in queue.enqueued] == [AVATAR_JOB_TYPE]
    job = queue.enqueued[0]
    assert job["owner_id"] == _OWNER
    assert job["idempotency_key"] == f"avatar:{_PERSONA}:create"
    assert job["payload"] == {"persona_id": _PERSONA}
    # The response tells the web something is on its way.
    assert detail.avatar_url is None
    assert detail.avatar_status == "pending"

    # Draining the request's background tasks runs the voice pick only: the
    # avatar is the worker's, so the inline generator must NOT run.
    asyncio.run(background())
    assert recorded_calls == ["voice"]


def test_create_runs_inline_when_no_worker_consumes(recorded_calls: list[str]) -> None:
    """A queue object alone is not enough: with no worker the inline path runs."""
    queue = _FakeQueue()
    detail, background = _create(_request(job_queue=queue, in_process_worker=None))

    assert queue.enqueued == []
    assert detail.avatar_status == "pending"
    asyncio.run(background())
    assert recorded_calls == ["voice", "avatar"]


def test_create_runs_inline_when_the_worker_lacks_the_avatar_handler(
    recorded_calls: list[str],
) -> None:
    """A worker without the tenant (no image backend at boot) never receives a dead job."""
    queue = _FakeQueue()
    _, background = _create(_request(job_queue=queue, in_process_worker=_worker("synthesis")))

    assert queue.enqueued == []
    asyncio.run(background())
    assert recorded_calls == ["voice", "avatar"]


def test_inline_only_opt_out_keeps_the_request_path(recorded_calls: list[str]) -> None:
    """PERSONA_API_AVATAR_INLINE_ONLY is the explicit opt-out; the default is the queue."""
    queue = _FakeQueue()
    _, background = _create(
        _request(job_queue=queue, in_process_worker=_worker(AVATAR_JOB_TYPE), inline_only=True)
    )

    assert queue.enqueued == []
    asyncio.run(background())
    assert recorded_calls == ["voice", "avatar"]


def test_user_supplied_avatar_skips_both_paths(recorded_calls: list[str]) -> None:
    queue = _FakeQueue()
    detail, background = _create(
        _request(job_queue=queue, in_process_worker=_worker(AVATAR_JOB_TYPE)),
        avatar_url="uploads/mine.png",
    )

    assert queue.enqueued == []
    assert detail.avatar_status is None
    asyncio.run(background())
    assert recorded_calls == ["voice"]


def test_create_without_image_backend_is_not_pending(recorded_calls: list[str]) -> None:
    """No image backend anywhere: nothing will draw, so the response must not say pending."""
    detail, _ = _create(_request(job_queue=None, in_process_worker=None, image_backend=None))
    assert detail.avatar_status is None


# ---------------------------------------------------------------------------
# Regenerate: its own idempotency key, so it is never deduped against create.
# ---------------------------------------------------------------------------


def test_regenerate_enqueues_a_distinct_regen_job() -> None:
    queue = _FakeQueue()
    result = asyncio.run(
        personas_routes.regenerate_avatar(
            _PERSONA,
            _request(job_queue=queue, in_process_worker=_worker(AVATAR_JOB_TYPE)),  # type: ignore[arg-type]
            BackgroundTasks(),
            SimpleNamespace(id=_OWNER, email=None),  # type: ignore[arg-type]
        )
    )
    assert result.queued is True
    assert len(queue.enqueued) == 1
    job = queue.enqueued[0]
    assert job["idempotency_key"].startswith(f"avatar:{_PERSONA}:regen:")
    assert job["idempotency_key"] != f"avatar:{_PERSONA}:create"
    assert job["payload"]["regenerate"] is True
    assert job["payload"]["persona_id"] == _PERSONA


def test_regenerate_falls_back_inline_without_a_worker(recorded_calls: list[str]) -> None:
    queue = _FakeQueue()
    background = BackgroundTasks()
    result = asyncio.run(
        personas_routes.regenerate_avatar(
            _PERSONA,
            _request(job_queue=queue, in_process_worker=None),  # type: ignore[arg-type]
            background,
            SimpleNamespace(id=_OWNER, email=None),  # type: ignore[arg-type]
        )
    )
    assert result.queued is False
    assert queue.enqueued == []
    asyncio.run(background())
    assert recorded_calls == ["avatar"]


# ---------------------------------------------------------------------------
# Read side: a failed job is visible on a reopened GET, not only while watching.
# ---------------------------------------------------------------------------


def _get(request: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(personas_routes.persona_service, "conversation_count_for", lambda **_: 0)
    monkeypatch.setattr(personas_routes, "_tasks_run_count", lambda *_a: 0)
    monkeypatch.setattr(personas_routes, "_memory_count", lambda *_a: 0)

    async def _no_remap(request: object, **_: object) -> None:
        return None

    monkeypatch.setattr(personas_routes, "_remap_voice_after_response", _no_remap)
    return asyncio.run(
        personas_routes.get_persona(
            _PERSONA,
            request,  # type: ignore[arg-type]
            BackgroundTasks(),
            SimpleNamespace(id=_OWNER, email=None),  # type: ignore[arg-type]
        )
    )


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (JobState.QUEUED, "pending"),
        (JobState.CLAIMED, "pending"),
        (JobState.RUNNING, "pending"),
        (JobState.DEAD, "failed"),
        (JobState.FAILED, "failed"),
        (JobState.SUCCEEDED, None),
    ],
)
def test_get_exposes_the_latest_avatar_job_state(
    monkeypatch: pytest.MonkeyPatch, state: JobState, expected: str | None
) -> None:
    queue = _FakeQueue(latest=_job(state))
    detail = _get(
        _request(job_queue=queue, in_process_worker=_worker(AVATAR_JOB_TYPE)), monkeypatch
    )
    assert detail.avatar_status == expected


def test_get_has_no_status_when_no_avatar_job_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    queue = _FakeQueue(latest=None)
    detail = _get(
        _request(job_queue=queue, in_process_worker=_worker(AVATAR_JOB_TYPE)), monkeypatch
    )
    assert detail.avatar_status is None


def test_get_does_not_read_the_queue_when_no_worker_consumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a consumer nothing was ever enqueued from here; the read must not touch the queue."""
    queue = _FakeQueue(latest=_job(JobState.DEAD))
    detail = _get(_request(job_queue=queue, in_process_worker=None), monkeypatch)
    assert detail.avatar_status is None


# ---------------------------------------------------------------------------
# R9-155: a persona that is not a person is never sent down either door.
# ---------------------------------------------------------------------------

_SYNTHETIC_YAML = (
    "schema_version: '1.0'\n"
    "identity:\n"
    "  name: JARVIS\n"
    "  role: personal chief of staff\n"
    "  background: Composed, precise, one dry aside per occasion.\n"
    "  presentation:\n"
    "    form: synthetic\n"
    "    presents: masculine\n"
)


def _synthetic_row() -> dict[str, Any]:
    return {**_row(), "yaml": _SYNTHETIC_YAML}


def test_a_synthetic_persona_is_never_queued_for_a_portrait(
    recorded_calls: list[str],
) -> None:
    """A worker is right there and willing; it is still not asked to draw a face."""
    queue = _FakeQueue()
    detail, background = _create(
        _request(job_queue=queue, in_process_worker=_worker(AVATAR_JOB_TYPE)),
        yaml_str=_SYNTHETIC_YAML,
    )

    assert queue.enqueued == []
    # And the response must not tell the web to wait for a portrait that is
    # never coming: a "pending" here is a spinner that spins forever.
    assert detail.avatar_status is None
    asyncio.run(background())
    assert recorded_calls == ["voice"]


def test_a_synthetic_persona_does_not_take_the_inline_path_either(
    recorded_calls: list[str],
) -> None:
    """The other door. Both gates read the one predicate, so neither can drift."""
    queue = _FakeQueue()
    detail, background = _create(
        _request(job_queue=queue, in_process_worker=None), yaml_str=_SYNTHETIC_YAML
    )

    assert queue.enqueued == []
    assert detail.avatar_status is None
    asyncio.run(background())
    assert recorded_calls == ["voice"]


def test_a_synthetic_persona_still_gets_a_voice(recorded_calls: list[str]) -> None:
    """Form and voice are independent: a ship's computer speaks, it just has no face."""
    _, background = _create(
        _request(job_queue=_FakeQueue(), in_process_worker=_worker(AVATAR_JOB_TYPE)),
        yaml_str=_SYNTHETIC_YAML,
    )
    asyncio.run(background())
    assert "voice" in recorded_calls


def test_a_human_persona_is_unaffected(recorded_calls: list[str]) -> None:
    """The gate reads `synthetic`, not "has a presentation": humans still get drawn."""
    human_yaml = _SYNTHETIC_YAML.replace("form: synthetic", "form: human")
    queue = _FakeQueue()
    detail, background = _create(
        _request(job_queue=queue, in_process_worker=_worker(AVATAR_JOB_TYPE)),
        yaml_str=human_yaml,
    )

    assert [j["type"] for j in queue.enqueued] == [AVATAR_JOB_TYPE]
    assert detail.avatar_status == "pending"
    asyncio.run(background())
    assert recorded_calls == ["voice"]


def test_regenerating_a_synthetic_persona_is_refused_not_silently_dropped(
    monkeypatch: pytest.MonkeyPatch, recorded_calls: list[str]
) -> None:
    """The owner pressed a button. A 202 they can poll forever is the silent failure."""
    monkeypatch.setattr(
        personas_routes.persona_service, "get_persona", lambda **_: _synthetic_row()
    )
    queue = _FakeQueue()
    background = BackgroundTasks()

    with pytest.raises(SyntheticPersonaHasNoPortraitError):
        asyncio.run(
            personas_routes.regenerate_avatar(
                _PERSONA,
                _request(job_queue=queue, in_process_worker=_worker(AVATAR_JOB_TYPE)),  # type: ignore[arg-type]
                background,
                SimpleNamespace(id=_OWNER, email=None),  # type: ignore[arg-type]
            )
        )

    assert queue.enqueued == []
    asyncio.run(background())
    assert recorded_calls == []


def test_the_refusal_is_a_422_the_web_can_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wired to a handler, so the owner gets an honest reason and not a 500."""
    app = FastAPI()
    register_exception_handlers(app)
    handler = app.exception_handlers[SyntheticPersonaHasNoPortraitError]
    response = asyncio.run(
        handler(
            SimpleNamespace(),  # type: ignore[arg-type]
            SyntheticPersonaHasNoPortraitError("drawn as its own mark", context={"p": "1"}),
        )
    )
    assert response.status_code == 422
    assert b"no_portrait_for_synthetic_persona" in response.body


@pytest.mark.parametrize(
    "unloadable",
    [
        "identity: [this is not a mapping]",
        "schema_version: '1.0'\nidentity:\n  name: Astrid\n  presentation:\n    form: robot\n",
        ": : not yaml at all",
        "",
    ],
)
def test_a_persona_that_will_not_load_is_treated_as_wanting_a_portrait(
    unloadable: str,
) -> None:
    """Degrade to what every persona did before the field existed, never to "no avatar".

    The gate reads the persona through the real loader, and a YAML that will not
    load has bigger problems than its avatar. Failing closed here would quietly
    strip avatars from personas whose only sin is being unreadable by this one
    helper.
    """
    assert personas_routes._wants_portrait(unloadable, persona_id=_PERSONA, owner_id=_OWNER) is True


def test_the_detail_surfaces_the_authored_presentation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R9-155/T6: the editor reads a typed value, not a second YAML parse."""
    monkeypatch.setattr(
        personas_routes.persona_service, "get_persona", lambda **_: _synthetic_row()
    )
    detail = _get(_request(job_queue=_FakeQueue(latest=None), in_process_worker=None), monkeypatch)
    assert detail.presentation is not None
    assert detail.presentation.form == "synthetic"
    assert detail.presentation.presents == "masculine"


def test_the_detail_reports_no_presentation_for_a_persona_that_never_declared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detail = _get(_request(job_queue=_FakeQueue(latest=None), in_process_worker=None), monkeypatch)
    assert detail.presentation is None


@pytest.mark.parametrize(
    "broken",
    ["  presentation: []\n", "  presentation:\n    form: robot\n", "  presentation: 3\n"],
)
def test_a_malformed_presentation_does_not_break_the_detail(
    monkeypatch: pytest.MonkeyPatch, broken: str
) -> None:
    """The detail must still render; the editor shows the raw YAML beside it anyway."""
    row = {**_row(), "yaml": f"schema_version: '1.0'\nidentity:\n  name: A\n  role: B\n{broken}"}
    monkeypatch.setattr(personas_routes.persona_service, "get_persona", lambda **_: row)
    detail = _get(_request(job_queue=_FakeQueue(latest=None), in_process_worker=None), monkeypatch)
    assert detail.presentation is None

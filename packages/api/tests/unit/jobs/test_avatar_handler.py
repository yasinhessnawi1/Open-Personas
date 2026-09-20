"""The durable avatar handler generates, persists like the request path, and fails honestly.

No Postgres: the handler's only DB affordance is the owner-scoped connection the
context hands it, so an in-memory SQLite ``personas`` table (the four columns the
handler touches) stands in. The failure case is driven through the REAL
``JobExecutor`` with a fake queue that records the lifecycle transition, so the
"dead" the read side maps to ``avatar_status="failed"`` is the executor's own
verdict, not a state the test set by hand.
"""

# ruff: noqa: ANN401, ARG001, ARG002, ARG005 — protocol-required arguments the fakes do not use
from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from persona.imagegen import ImageProviderError
from persona.jobs import JobRegistry, JobState
from persona_api.imagegen.service import ImagegenAvatarGenerator
from persona_api.jobs import context as context_module
from persona_api.jobs.executor import JobExecutor
from persona_api.jobs.handlers import avatar as avatar_module
from persona_api.jobs.handlers.avatar import (
    AVATAR_JOB_TYPE,
    AvatarGenerationHandler,
    AvatarGenerationPayload,
    AvatarResult,
    avatar_billing_key,
    avatar_idempotency_key,
    avatar_status_from_job,
    register_avatar_handler,
)
from persona_api.jobs.queue import JobRecord
from sqlalchemy import Engine, create_engine, text

if TYPE_CHECKING:
    from collections.abc import Iterator

_OWNER = "u_handler"
_PERSONA = "persona_handler"
_YAML = "schema_version: '1.0'\nidentity:\n  name: Astrid\n"


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = create_engine("sqlite+pysqlite:///:memory:")
    with eng.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE personas (id TEXT PRIMARY KEY, yaml TEXT, "
                "avatar_url TEXT, avatar_source TEXT, updated_at TEXT)"
            )
        )
        conn.execute(
            text("INSERT INTO personas (id, yaml) VALUES (:id, :yaml)"),
            {"id": _PERSONA, "yaml": _YAML},
        )
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def ready_notifications(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the persona-ready notification instead of writing the notifications table."""
    written: list[str] = []
    monkeypatch.setattr(
        avatar_module.persona_service,
        "write_persona_ready",
        lambda conn, persona_id: written.append(persona_id),
    )
    return written


def _avatar_row(engine: Engine) -> tuple[str | None, str | None]:
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT avatar_url, avatar_source FROM personas WHERE id = :id"),
            {"id": _PERSONA},
        ).one()
    return row[0], row[1]


def _set_avatar(engine: Engine, url: str, source: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE personas SET avatar_url = :u, avatar_source = :s WHERE id = :id"),
            {"u": url, "s": source, "id": _PERSONA},
        )


class _Context:
    """The handler's whole world: an owner, a job id, a connection, a meter."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self.metered: list[dict[str, Any]] = []

    @property
    def owner_id(self) -> str:
        return _OWNER

    @property
    def job_id(self) -> str:
        return "job-avatar"

    @contextmanager
    def connection(self) -> Iterator[Any]:
        with self._engine.begin() as conn:
            yield conn

    def meter(self, *, amount_micros: int, kind: str, detail: Any = None) -> None:
        self.metered.append(
            {"amount_micros": amount_micros, "kind": kind, "detail": dict(detail or {})}
        )


class _Generator:
    """Records every call; returns a scripted result or raises."""

    def __init__(self, result: AvatarResult | None, *, raises: Exception | None = None) -> None:
        self._result = result
        self._raises = raises
        self.calls: list[dict[str, str]] = []

    async def generate(
        self, *, persona_id: str, owner_id: str, yaml_str: str, billing_key: str
    ) -> AvatarResult | None:
        self.calls.append(
            {
                "persona_id": persona_id,
                "owner_id": owner_id,
                "yaml_str": yaml_str,
                "billing_key": billing_key,
            }
        )
        if self._raises is not None:
            raise self._raises
        return self._result


_RESULT = AvatarResult(avatar_url="uploads/new.png", cost_micros=250, provider="fake")


# ---------------------------------------------------------------------------
# Create-time job.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_job_generates_persists_meters_and_announces(
    engine: Engine, ready_notifications: list[str]
) -> None:
    generator = _Generator(_RESULT)
    ctx = _Context(engine)

    await AvatarGenerationHandler(generator=generator).handle(
        AvatarGenerationPayload(persona_id=_PERSONA), ctx
    )

    assert generator.calls == [
        {
            "persona_id": _PERSONA,
            "owner_id": _OWNER,
            "yaml_str": _YAML,
            "billing_key": avatar_billing_key(_PERSONA),
        }
    ]
    # Persisted exactly as the request path persists: url + provenance together.
    assert _avatar_row(engine) == ("uploads/new.png", "generated")
    assert ctx.metered == [
        {"amount_micros": 250, "kind": "model", "detail": {"provider": "fake", "surface": "avatar"}}
    ]
    assert ready_notifications == [_PERSONA]


@pytest.mark.asyncio
async def test_create_job_is_a_no_op_when_an_avatar_already_exists(engine: Engine) -> None:
    """At-least-once redelivery after the write landed must not regenerate."""
    _set_avatar(engine, "uploads/mine.png", "uploaded")
    generator = _Generator(_RESULT)

    await AvatarGenerationHandler(generator=generator).handle(
        AvatarGenerationPayload(persona_id=_PERSONA), _Context(engine)
    )

    assert generator.calls == []
    assert _avatar_row(engine) == ("uploads/mine.png", "uploaded")


@pytest.mark.asyncio
async def test_declined_generation_leaves_no_avatar_and_no_error(
    engine: Engine, ready_notifications: list[str]
) -> None:
    ctx = _Context(engine)
    await AvatarGenerationHandler(generator=_Generator(None)).handle(
        AvatarGenerationPayload(persona_id=_PERSONA), ctx
    )
    assert _avatar_row(engine) == (None, None)
    assert ctx.metered == []
    assert ready_notifications == []


@pytest.mark.asyncio
async def test_deleted_persona_is_a_no_op(engine: Engine) -> None:
    generator = _Generator(_RESULT)
    await AvatarGenerationHandler(generator=generator).handle(
        AvatarGenerationPayload(persona_id="gone"), _Context(engine)
    )
    assert generator.calls == []


# ---------------------------------------------------------------------------
# Regeneration: replaces the current avatar, under its own keys.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_regenerate_job_replaces_an_existing_avatar(engine: Engine) -> None:
    _set_avatar(engine, "uploads/old.png", "generated")
    generator = _Generator(_RESULT)

    await AvatarGenerationHandler(generator=generator).handle(
        AvatarGenerationPayload(persona_id=_PERSONA, regenerate=True, request_id="abc123"),
        _Context(engine),
    )

    assert len(generator.calls) == 1
    assert generator.calls[0]["billing_key"] == avatar_billing_key(_PERSONA, regen_token="abc123")
    assert _avatar_row(engine) == ("uploads/new.png", "generated")


def test_regen_keys_are_distinct_from_create_keys() -> None:
    assert avatar_idempotency_key(_PERSONA) == f"avatar:{_PERSONA}:create"
    assert avatar_idempotency_key(_PERSONA, regen_token="t1") == f"avatar:{_PERSONA}:regen:t1"
    assert avatar_billing_key(_PERSONA) == f"avatar:{_PERSONA}"
    assert avatar_billing_key(_PERSONA, regen_token="t1") == f"avatar:{_PERSONA}:regen:t1"


def test_registered_recipe_matches_the_producer_keys() -> None:
    """The registry's declared recipe and the enqueue site must agree, or dedup lies."""
    registry = JobRegistry()
    register_avatar_handler(registry, _Generator(None))
    assert registry.idempotency_key_for(
        AVATAR_JOB_TYPE, AvatarGenerationPayload(persona_id=_PERSONA)
    ) == avatar_idempotency_key(_PERSONA)
    assert registry.idempotency_key_for(
        AVATAR_JOB_TYPE,
        AvatarGenerationPayload(persona_id=_PERSONA, regenerate=True, request_id="t1"),
    ) == avatar_idempotency_key(_PERSONA, regen_token="t1")


def test_legacy_payload_still_parses() -> None:
    """Jobs enqueued before the regen fields existed are still valid durable rows."""
    registry = JobRegistry()
    register_avatar_handler(registry, _Generator(None))
    payload = registry.parse_payload(AVATAR_JOB_TYPE, {"persona_id": _PERSONA})
    assert isinstance(payload, AvatarGenerationPayload)
    assert payload.regenerate is False


# ---------------------------------------------------------------------------
# Failure, through the real executor: the job dead-letters and the read side says so.
# ---------------------------------------------------------------------------


class _LifecycleQueue:
    """Holds one record and applies the executor's transitions to it."""

    def __init__(self, record: JobRecord) -> None:
        self.record = record

    def mark_running(self, *, job_id: str, worker_id: str) -> bool:
        self.record = self.record.model_copy(update={"state": JobState.RUNNING})
        return True

    def complete(self, *, job_id: str, worker_id: str) -> bool:
        self.record = self.record.model_copy(update={"state": JobState.SUCCEEDED})
        return True

    def retry(self, *, job_id: str, worker_id: str, error: str, scheduled_at: object) -> bool:
        self.record = self.record.model_copy(update={"state": JobState.QUEUED, "last_error": error})
        return True

    def mark_dead(self, *, job_id: str, worker_id: str, error: str) -> bool:
        self.record = self.record.model_copy(update={"state": JobState.DEAD, "last_error": error})
        return True

    def mark_failed(self, *, job_id: str, worker_id: str, error: str) -> bool:
        self.record = self.record.model_copy(update={"state": JobState.FAILED, "last_error": error})
        return True

    def heartbeat(self, *, job_id: str, worker_id: str, lease_seconds: int) -> bool:
        return True


def _record(*, attempt: int) -> JobRecord:
    now = datetime.now(UTC)
    return JobRecord(
        id="job-avatar",
        type=AVATAR_JOB_TYPE,
        owner_id=_OWNER,
        payload={"persona_id": _PERSONA},
        idempotency_key=avatar_idempotency_key(_PERSONA),
        state=JobState.CLAIMED,
        priority=0,
        attempt=attempt,
        max_attempts=3,
        scheduled_at=now,
        created_at=now,
        lease_expires_at=None,
        locked_by="w1",
        last_error=None,
    )


@pytest.fixture
def sqlite_owner_scope(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """Route the worker context's owner-scoped connection to the SQLite engine."""

    @contextmanager
    def _conn(eng: Engine, user_id: str) -> Iterator[Any]:
        with eng.begin() as conn:
            yield conn

    monkeypatch.setattr(context_module, "rls_connection", _conn)


def _executor(queue: _LifecycleQueue, generator: _Generator, engine: Engine) -> JobExecutor:
    registry = JobRegistry()
    register_avatar_handler(registry, generator)
    return JobExecutor(
        queue=queue,  # type: ignore[arg-type]
        registry=registry,
        rls_engine=engine,
        worker_id="w1",
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("sqlite_owner_scope")
async def test_provider_failure_on_the_last_attempt_dead_letters_with_the_cause(
    engine: Engine,
) -> None:
    boom = ImageProviderError("provider down", context={"provider": "fake"})
    queue = _LifecycleQueue(_record(attempt=3))

    outcome = await _executor(queue, _Generator(None, raises=boom), engine).execute(queue.record)

    assert outcome is JobState.DEAD
    assert queue.record.state is JobState.DEAD
    assert queue.record.last_error is not None
    assert queue.record.last_error.startswith("provider down")
    # The persona is left exactly as the web can show it: no avatar, no spinner.
    assert _avatar_row(engine) == (None, None)
    assert avatar_status_from_job(queue.record) == "failed"


@pytest.mark.asyncio
@pytest.mark.usefixtures("sqlite_owner_scope")
async def test_provider_failure_before_exhaustion_is_retried_and_reads_as_pending(
    engine: Engine,
) -> None:
    boom = ImageProviderError("rate limited", context={"provider": "fake"})
    queue = _LifecycleQueue(_record(attempt=1))

    outcome = await _executor(queue, _Generator(None, raises=boom), engine).execute(queue.record)

    assert outcome is JobState.QUEUED  # re-queued for its backoff, not settled
    assert queue.record.state is JobState.QUEUED
    assert avatar_status_from_job(queue.record) == "pending"


@pytest.mark.asyncio
@pytest.mark.usefixtures("sqlite_owner_scope")
async def test_success_through_the_executor_persists_and_reads_as_settled(
    engine: Engine,
) -> None:
    queue = _LifecycleQueue(_record(attempt=1))

    outcome = await _executor(queue, _Generator(_RESULT), engine).execute(queue.record)

    assert outcome is JobState.SUCCEEDED
    assert _avatar_row(engine) == ("uploads/new.png", "generated")
    assert avatar_status_from_job(queue.record) is None


def test_no_job_reads_as_no_status() -> None:
    assert avatar_status_from_job(None) is None


# ---------------------------------------------------------------------------
# R9-155: a job for a persona that is not a person declines, and is NOT retried.
# ---------------------------------------------------------------------------

_SYNTHETIC_YAML = (
    "schema_version: '1.0'\n"
    "identity:\n"
    "  name: TARS\n"
    "  role: mission support unit\n"
    "  background: Blunt, funny, extremely capable.\n"
    "  presentation:\n"
    "    form: synthetic\n"
    "    presents: masculine\n"
)


class _ForbiddenImageBackend:
    """An image backend that fails the test if anything asks it to draw."""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def provider_name(self) -> str:
        return "forbidden"

    @property
    def model_name(self) -> str:
        return "forbidden-1"

    async def generate(self, prompt: str, *, options: Any = None) -> Any:
        self.calls += 1
        msg = f"the image backend was asked to draw a synthetic persona: {prompt!r}"
        raise AssertionError(msg)

    async def edit(self, *_a: Any, **_k: Any) -> Any:
        raise NotImplementedError


def _make_synthetic(engine: Engine) -> None:
    """Flip the persona to synthetic, as an owner edit after the job was enqueued."""
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE personas SET yaml = :y WHERE id = :id"),
            {"y": _SYNTHETIC_YAML, "id": _PERSONA},
        )


def _real_generator(backend: _ForbiddenImageBackend) -> ImagegenAvatarGenerator:
    """The REAL generator the worker root composes, over a backend that must not be called."""
    return ImagegenAvatarGenerator(backend=backend, file_storage=object())  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.usefixtures("sqlite_owner_scope")
async def test_a_synthetic_persona_declines_and_the_job_is_not_retried(engine: Engine) -> None:
    """The regression this task could introduce: an avatar job that never settles.

    A persona edited to synthetic AFTER its create job was enqueued is the one
    case the route gates cannot catch. The generator must decline, not raise: a
    raise on attempt 1 re-queues (see the provider-failure test above), and a
    persona that will never have a portrait would then burn every attempt and
    dead-letter a job that was correct the first time. R9-013 is the retry loop
    this project has already shipped once.
    """
    _make_synthetic(engine)
    backend = _ForbiddenImageBackend()
    queue = _LifecycleQueue(_record(attempt=1))

    outcome = await _executor(queue, _real_generator(backend), engine).execute(queue.record)

    # Settled on the first delivery, not re-queued for another go.
    assert outcome is JobState.SUCCEEDED
    assert queue.record.state is JobState.SUCCEEDED
    assert queue.record.last_error is None
    # Nothing was drawn, nothing was charged, nothing was stored.
    assert backend.calls == 0
    assert _avatar_row(engine) == (None, None)
    # And the web is told to stop waiting rather than to keep polling.
    assert avatar_status_from_job(queue.record) is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("sqlite_owner_scope")
async def test_a_synthetic_persona_declines_on_its_last_attempt_too(engine: Engine) -> None:
    """Same verdict at the retry budget's edge: succeeded, never dead-lettered.

    The attempt-3 twin of the test above. If the decline ever became a raise,
    this is the delivery that would leave the persona reading "failed" in the
    UI forever, for a portrait it was never supposed to have.
    """
    _make_synthetic(engine)
    backend = _ForbiddenImageBackend()
    queue = _LifecycleQueue(_record(attempt=3))

    outcome = await _executor(queue, _real_generator(backend), engine).execute(queue.record)

    assert outcome is JobState.SUCCEEDED
    assert backend.calls == 0
    assert avatar_status_from_job(queue.record) is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("sqlite_owner_scope")
async def test_a_regeneration_of_a_synthetic_persona_declines_the_same_way(
    engine: Engine,
) -> None:
    """A regen job carries its own key and replaces unconditionally: it must decline too."""
    _set_avatar(engine, "uploads/old.png", "generated")
    _make_synthetic(engine)
    backend = _ForbiddenImageBackend()
    record = _record(attempt=1).model_copy(
        update={"payload": {"persona_id": _PERSONA, "regenerate": True, "request_id": "abc123"}}
    )
    queue = _LifecycleQueue(record)

    outcome = await _executor(queue, _real_generator(backend), engine).execute(queue.record)

    assert outcome is JobState.SUCCEEDED
    assert backend.calls == 0
    # The existing avatar is left alone rather than wiped by a failed redraw.
    assert _avatar_row(engine) == ("uploads/old.png", "generated")

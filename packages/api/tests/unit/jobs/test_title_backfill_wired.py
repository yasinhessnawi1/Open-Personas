"""Issue #8: the untitled-conversation backfill is REACHABLE in production.

A sweep nobody calls is a sweep that never titles anything, and nothing goes red.
Three links are proven here, each against the real object: the worker's loop runs
it, ``build_worker`` accepts and holds it, and the in-process worker root (the
composition root the api lifespan actually starts) hands ``build_worker`` a
builder that makes a real one.
"""

# ruff: noqa: SLF001, exercising private loop internals directly.
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest
from persona.jobs import JobRegistry
from persona_api.config import APIConfig, Edition
from persona_api.errors import CommunityDbError
from persona_api.jobs import Worker
from persona_api.jobs.worker import build_worker
from persona_api.services.title_backfill import UntitledConversationBackfill

if TYPE_CHECKING:
    from sqlalchemy import Engine


class _CountingBackfill:
    """Duck-typed backfill: counts passes, optionally blows up."""

    def __init__(self, *, fail: bool = False) -> None:
        self.passes = 0
        self._fail = fail

    def run_once(self) -> int:
        self.passes += 1
        if self._fail:
            msg = "the database went away mid-scan"
            raise RuntimeError(msg)
        return 0


def _worker(**kw: Any) -> Worker:  # noqa: ANN401, passthrough of Worker's own kwargs
    return Worker(
        dispatch_engine=MagicMock(),
        rls_engine=MagicMock(),
        registry=JobRegistry(),
        worker_id="w-title-backfill",
        **kw,
    )


def _mock_queue() -> MagicMock:
    queue = MagicMock()
    queue.claim.return_value = []
    queue.reclaim_expired.return_value = 0
    queue.archive_finished.return_value = 0
    queue.purge_archived.return_value = 0
    return queue


# ----- link 1: the real loop runs it ------------------------------------------


def test_the_real_worker_loop_runs_the_backfill() -> None:
    backfill = _CountingBackfill()
    worker = _worker(
        poll_interval_seconds=0.01,
        poll_jitter_seconds=0.0,
        title_backfill=backfill,
        title_backfill_interval_seconds=900.0,
    )
    queue = _mock_queue()

    def _claim_then_drain(**_kw: object) -> list[object]:
        worker.request_drain()
        return []

    queue.claim.side_effect = _claim_then_drain
    worker._queue = queue

    asyncio.run(worker.run(install_signal_handlers=False))

    assert backfill.passes == 1, (
        "the loop never swept for untitled conversations; the backlog would sit forever"
    )


def test_a_worker_without_a_backfill_is_unchanged() -> None:
    worker = _worker(poll_interval_seconds=0.01, poll_jitter_seconds=0.0)
    queue = _mock_queue()

    def _claim_then_drain(**_kw: object) -> list[object]:
        worker.request_drain()
        return []

    queue.claim.side_effect = _claim_then_drain
    worker._queue = queue

    asyncio.run(worker.run(install_signal_handlers=False))  # must not raise


def test_a_backfill_failure_never_crashes_the_loop_and_advances_the_clock() -> None:
    backfill = _CountingBackfill(fail=True)
    worker = _worker(title_backfill=backfill)

    asyncio.run(worker._maybe_run_title_backfill())  # must not raise

    assert backfill.passes == 1
    assert worker._last_title_backfill is not None, (
        "a failed sweep must not spin hot retrying every loop iteration"
    )


def test_the_cadence_holds_between_passes() -> None:
    backfill = _CountingBackfill()
    worker = _worker(title_backfill=backfill, title_backfill_interval_seconds=900.0)

    asyncio.run(worker._maybe_run_title_backfill())
    asyncio.run(worker._maybe_run_title_backfill())

    assert backfill.passes == 1, "the second call is inside the interval, so one pass only"


# ----- link 2: build_worker holds it ------------------------------------------


def test_build_worker_wires_the_backfill_builder() -> None:
    made: list[Engine] = []
    backfill = _CountingBackfill()

    def _builder(dispatch_engine: Engine) -> Any:  # noqa: ANN401, duck-typed stand-in
        made.append(dispatch_engine)
        return backfill

    config = APIConfig(
        edition=Edition.community,  # community never probes, so nothing connects here
        database_url="postgresql+psycopg://persona:persona@127.0.0.1:5999/never_connected",
        app_database_url="postgresql+psycopg://persona_app:x@127.0.0.1:5999/never_connected",
    )
    worker = build_worker(config, JobRegistry(), title_backfill_builder=_builder)

    assert len(made) == 1, "the builder ran, on the worker's cross-tenant dispatch engine"
    assert worker._title_backfill is backfill
    assert worker._title_backfill_interval == config.title_backfill_interval_seconds


# ----- link 3: the composition root the lifespan starts passes one -------------


def test_the_in_process_worker_root_hands_build_worker_a_real_backfill() -> None:
    from persona_api.background import worker_root

    engine = MagicMock()
    engine.dialect.name = "postgresql"
    captured: dict[str, Any] = {}

    def _fake_build_worker(_config: object, _registry: object, **kwargs: Any) -> MagicMock:  # noqa: ANN401
        captured.update(kwargs)
        return MagicMock()

    with (
        patch.object(worker_root, "build_worker_registry", return_value=JobRegistry()),
        patch.object(worker_root, "build_worker", side_effect=_fake_build_worker),
        patch.object(worker_root.InProcessWorker, "start", lambda _self: None),
    ):
        worker_root.start_in_process_worker(
            config=APIConfig(edition=Edition.community),
            rls_engine=engine,
            embedder=MagicMock(),
            tier_registry=MagicMock(),
            free_tier_registry=None,
        )

    builder = captured.get("title_backfill_builder")
    assert builder is not None, (
        "the composition root the api lifespan starts wired no backfill; untitled "
        "conversations would stay untitled in the deployment that actually runs"
    )
    # Building one needs only the dispatch engine; nothing connects until a pass runs.
    assert isinstance(builder(engine), UntitledConversationBackfill)


def test_a_non_postgres_worker_is_still_refused() -> None:
    # Unchanged guard: the backfill wiring must not have slipped in front of it.
    from persona_api.background import worker_root

    engine = MagicMock()
    engine.dialect.name = "sqlite"
    with pytest.raises(CommunityDbError):
        worker_root.start_in_process_worker(
            config=APIConfig(edition=Edition.community),
            rls_engine=engine,
            embedder=MagicMock(),
            tier_registry=MagicMock(),
            free_tier_registry=None,
        )

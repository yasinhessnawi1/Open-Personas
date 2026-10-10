"""Which scheduler indexes documents in this process (D-FV0-31): enqueue implies handler."""

# ruff: noqa: PLC0415, SLF001
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from persona_api.services.document_index import (
    DOCUMENT_INDEX_JOB_TYPE,
    DocumentIndexer,
    JobDocumentIndexScheduler,
    LocalDocumentIndexQueue,
)
from persona_api.storage import LocalFileStorage


def _app(tmp_path: Path, **state: Any) -> Any:  # noqa: ANN401
    defaults = {
        "job_queue": SimpleNamespace(_engine=object()),
        "file_storage": LocalFileStorage(tmp_path),
        "memory_backend": object(),
        "document_indexer": DocumentIndexer(),
        "document_parser": object(),
        "event_channel": None,
    }
    return SimpleNamespace(state=SimpleNamespace(**{**defaults, **state}))


def _pick(app: Any, worker: Any) -> Any:  # noqa: ANN401
    from persona_api.app import _build_document_index_scheduler

    async def _go() -> Any:  # noqa: ANN401
        local = _build_document_index_scheduler(app, worker)
        if local is not None:
            await local.aclose()
        return app.state.document_index_scheduler

    return asyncio.run(_go())


def test_the_jobs_system_when_this_processs_worker_carries_the_type(tmp_path: Path) -> None:
    worker = SimpleNamespace(job_types=frozenset({DOCUMENT_INDEX_JOB_TYPE, "synthesis"}))
    assert isinstance(_pick(_app(tmp_path), worker), JobDocumentIndexScheduler)


def test_the_in_process_queue_without_such_a_worker(tmp_path: Path) -> None:
    assert isinstance(_pick(_app(tmp_path), None), LocalDocumentIndexQueue)
    worker = SimpleNamespace(job_types=frozenset({"synthesis"}))
    assert isinstance(_pick(_app(tmp_path), worker), LocalDocumentIndexQueue)


def test_nothing_without_a_document_store(tmp_path: Path) -> None:
    assert _pick(_app(tmp_path, memory_backend=None), None) is None


class _ClaimSpy:
    def __init__(self, on_claim: Any = None) -> None:  # noqa: ANN401
        self.calls: list[dict[str, Any]] = []
        self._on_claim = on_claim

    def claim(self, **kwargs: Any) -> list[Any]:  # noqa: ANN401
        self.calls.append(kwargs)
        if self._on_claim is not None:
            self._on_claim()
        return []


def test_the_worker_claims_with_its_registrys_serial_types() -> None:
    """Both claim paths pass the serial types, or the claim cannot hold one in flight."""
    from unittest import mock

    from persona.jobs import JobPayload, JobRegistry, JobTypeSpec
    from persona_api.jobs.worker import Worker

    class _P(JobPayload):
        pass

    registry = JobRegistry(
        [
            JobTypeSpec(type="plain", payload_model=_P, handler=object(), idempotency_key=str),  # type: ignore[arg-type]
            JobTypeSpec(
                type=DOCUMENT_INDEX_JOB_TYPE,
                payload_model=_P,
                handler=object(),  # type: ignore[arg-type]
                idempotency_key=str,  # type: ignore[arg-type]
                serial=True,
            ),
        ]
    )
    worker = Worker(
        dispatch_engine=mock.MagicMock(),
        rls_engine=mock.MagicMock(),
        registry=registry,
        poll_interval_seconds=0.01,
        poll_jitter_seconds=0.0,
    )
    spy = _ClaimSpy()
    worker._queue = spy  # type: ignore[assignment]
    asyncio.run(worker.run_once())
    spy._on_claim = worker.request_drain  # the loop's first claim, then it drains
    worker._maybe_run_maintenance = lambda: None  # type: ignore[method-assign]
    asyncio.run(worker.run(install_signal_handlers=False))
    assert [call["serial_types"] for call in spy.calls] == [[DOCUMENT_INDEX_JOB_TYPE]] * 2


def test_a_settled_serial_job_wakes_the_loop_at_once() -> None:
    """Fourth review finding 1: the lane's next job is claimed now, not a poll later."""
    import time

    from persona.jobs import JobRegistry
    from persona_api.jobs.worker import Worker

    worker = Worker(
        dispatch_engine=object(),  # type: ignore[arg-type]
        rls_engine=object(),  # type: ignore[arg-type]
        registry=JobRegistry(),
        worker_id="w",
        poll_interval_seconds=30.0,
        poll_jitter_seconds=0.0,
    )

    async def _go() -> float:
        loop = asyncio.get_running_loop()
        loop.call_later(0.05, worker._free_lane, None)
        started = time.monotonic()
        await worker._idle_wait()
        return time.monotonic() - started

    assert asyncio.run(_go()) < 5.0
    assert not worker._lane_free.is_set()  # cleared for the next wait


def test_without_a_freed_lane_the_loop_waits_its_poll_interval() -> None:
    import time

    from persona.jobs import JobRegistry
    from persona_api.jobs.worker import Worker

    worker = Worker(
        dispatch_engine=object(),  # type: ignore[arg-type]
        rls_engine=object(),  # type: ignore[arg-type]
        registry=JobRegistry(),
        worker_id="w",
        poll_interval_seconds=0.3,
        poll_jitter_seconds=0.0,
    )

    async def _go() -> float:
        started = time.monotonic()
        await worker._idle_wait()
        return time.monotonic() - started

    assert asyncio.run(_go()) >= 0.25


def test_the_app_wires_the_embedder_warm_up_into_its_indexer() -> None:
    from persona_api.app import create_app
    from persona_api.config import APIConfig, Edition

    # the edition is pinned: the suite's cloud default would refuse a config without a DSN
    app = create_app(APIConfig(edition=Edition.community))
    assert app.state.document_indexer._warm_up is not None


def test_a_serial_jobs_task_frees_the_lane_when_it_settles() -> None:
    """The run loop registers the wake for serial types only."""
    from persona.jobs import JobRegistry
    from persona_api.jobs.worker import Worker

    worker = Worker(
        dispatch_engine=object(),  # type: ignore[arg-type]
        rls_engine=object(),  # type: ignore[arg-type]
        registry=JobRegistry(),
        worker_id="w",
        poll_interval_seconds=30.0,
        poll_jitter_seconds=0.0,
    )
    worker._serial_types = ["serial_type"]
    records = [
        SimpleNamespace(type="serial_type", id="1"),
        SimpleNamespace(type="other", id="2"),
    ]
    claimed: list[Any] = []

    class _Queue:
        def claim(self, **_: Any) -> list[Any]:  # noqa: ANN401
            if records:
                claimed.append(records.pop(0))
                return [claimed[-1]]
            worker.request_drain()
            return []

    class _Executor:
        async def execute(self, record: Any) -> None:  # noqa: ANN401, ARG002
            return None

    worker._queue = _Queue()  # type: ignore[assignment]
    worker._executor = _Executor()  # type: ignore[assignment]
    woke: list[str] = []
    real_free = worker._free_lane

    def _spy(task: Any) -> None:  # noqa: ANN401
        woke.append("serial")
        real_free(task)

    worker._free_lane = _spy  # type: ignore[method-assign]
    for name in (
        "_maybe_run_maintenance",
        "_maybe_run_scheduler_tick",
    ):
        setattr(worker, name, lambda: None)

    async def _noop() -> None:
        return None

    for name in (
        "_maybe_run_catalog_sync",
        "_maybe_run_skill_catalog_sync",
        "_maybe_run_initiative_provisioner",
        "_maybe_run_ignored_proposal_sweep",
        "_maybe_run_approval_sweep",
        "_maybe_run_dead_leg_sweep",
        "_maybe_run_revival_sweep",
        "_maybe_run_title_backfill",
    ):
        setattr(worker, name, _noop)
    asyncio.run(asyncio.wait_for(worker.run(install_signal_handlers=False), timeout=20))
    assert [r.type for r in claimed] == ["serial_type", "other"]
    assert woke == ["serial"]

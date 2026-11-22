"""A failed create's undo deletes the row, then hands the rest of the erasure off (R9-320).

The undo used to run the whole persona erasure in the request's thread. Its graph step waits on
the owner's graph maintenance lock, which a consolidation run can hold for minutes, so the
failed request hung that long and held a thread the app's other ``to_thread`` calls share.
Now the undo claims, fences and deletes the row (``begin_persona_erasure``) and hands the rest
to the ``persona_erase`` job, or, where no worker here consumes it, to one background thread,
as the DELETE route does. The request answers at once.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from persona_api.jobs.handlers.persona_erase import PERSONA_ERASE_JOB_TYPE
from persona_api.services import erasure_start
from persona_api.services.erasure_ledger import Erasure, ErasureState, SubjectKind

_OWNER = "user_undo"
_PERSONA = "persona_" + "b" * 32
#: Long enough that an undo still waiting on the "graph lock" is plainly not answering.
_PROMPT_S = 5.0


def _erasure() -> Erasure:
    now = datetime.now(UTC)
    return Erasure(
        id="erasure_" + "c" * 32,
        owner_id=_OWNER,
        subject_kind=SubjectKind.PERSONA,
        subject_id=_PERSONA,
        state=ErasureState.RUNNING,
        captured={},
        counts={"row": {"personas": 1}},
        error_code=None,
        requested_at=now,
        updated_at=now,
        completed_at=None,
    )


class _Worker:
    """An in-process worker that consumes the erase job."""

    job_types = frozenset({PERSONA_ERASE_JOB_TYPE})


def _state(**extra: Any) -> SimpleNamespace:  # noqa: ANN401
    fields: dict[str, Any] = {
        "platform_engine": object(),
        "job_queue": None,
        "in_process_worker": None,
    }
    return SimpleNamespace(**{**fields, **extra})


@pytest.fixture
def begun(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """``begin_persona_erasure`` stands in: it records its call and returns the erasure."""
    calls: list[dict[str, Any]] = []

    def _begin(**kwargs: Any) -> Erasure:  # noqa: ANN401
        calls.append(kwargs)
        return _erasure()

    monkeypatch.setattr(erasure_start, "begin_persona_erasure", _begin)
    monkeypatch.setattr(erasure_start, "erase_deps_from_state", lambda _state: object())
    return calls


def _undo(state: SimpleNamespace) -> Any:  # noqa: ANN401
    return erasure_start.undo_failed_create(
        state, rls_engine=object(), owner_id=_OWNER, persona_id=_PERSONA
    )


def test_the_undo_answers_while_the_graph_step_waits_on_its_lock(
    monkeypatch: pytest.MonkeyPatch, begun: list[dict[str, Any]]
) -> None:
    lock_released = threading.Event()
    ran: list[tuple[str, str]] = []

    def _erase_waiting_on_the_graph_lock(_deps: object, erasure: Erasure) -> None:
        ran.append((erasure.id, threading.current_thread().name))
        assert lock_released.wait(30), "the test never released the graph lock"

    monkeypatch.setattr(erasure_start, "erase_persona", _erase_waiting_on_the_graph_lock)
    answered: list[Any] = []
    caller = threading.Thread(target=lambda: answered.append(_undo(_state())))

    started = time.monotonic()
    caller.start()
    caller.join(_PROMPT_S)
    try:
        assert not caller.is_alive(), "the undo waited for the graph step"
        assert time.monotonic() - started < _PROMPT_S
        assert len(begun) == 1, "the row was claimed, fenced and deleted first"
        assert begun[0]["owner_id"] == _OWNER
        assert begun[0]["persona_id"] == _PERSONA
    finally:
        lock_released.set()
    (running,) = answered
    assert running is not None, "no worker here: the erasure runs in the background"
    running.result(timeout=_PROMPT_S)
    assert [eid for eid, _ in ran] == [_erasure().id], "the rest of the erasure still ran"
    assert ran[0][1] != caller.name, "and not in the request's thread"


def test_with_a_worker_here_the_rest_goes_to_the_erase_job(
    monkeypatch: pytest.MonkeyPatch, begun: list[dict[str, Any]]
) -> None:
    enqueued: list[str] = []

    def _never(*_a: Any) -> None:  # noqa: ANN401
        pytest.fail("the erasure ran here; it belongs to the job")

    monkeypatch.setattr(erasure_start, "erase_persona", _never)
    monkeypatch.setattr(
        erasure_start,
        "enqueue_persona_erase",
        lambda _queue, erasure, **_k: enqueued.append(erasure.id),
    )

    answered = _undo(_state(job_queue=object(), in_process_worker=_Worker()))

    assert answered is None, "nothing runs here"
    assert len(begun) == 1
    assert enqueued == [_erasure().id]


def test_undos_run_one_at_a_time_on_one_thread(
    monkeypatch: pytest.MonkeyPatch,
    begun: list[dict[str, Any]],  # noqa: ARG001 - stands in for the row's delete
) -> None:
    """Retries of a create that fails for everyone queue up, and never pile up threads."""
    lock_released = threading.Event()
    threads: list[str] = []
    overlapping: list[int] = []
    running = [0]
    guard = threading.Lock()

    def _erase(_deps: object, _erasure: Erasure) -> None:
        with guard:
            running[0] += 1
            overlapping.append(running[0])
            threads.append(threading.current_thread().name)
        lock_released.wait(30)
        with guard:
            running[0] -= 1

    monkeypatch.setattr(erasure_start, "erase_persona", _erase)

    futures = [_undo(_state()) for _ in range(3)]
    lock_released.set()
    for future in futures:
        future.result(timeout=_PROMPT_S)

    assert max(overlapping) == 1, "one erasure at a time"
    assert len(set(threads)) == 1, "on one thread"


def test_a_row_already_gone_is_raised_and_nothing_is_handed_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sweep or an account erasure took the row first; the create's undo says so (R9-293)."""
    from persona.errors import PersonaNotFoundError  # noqa: PLC0415

    def _gone(**_k: Any) -> Erasure:  # noqa: ANN401
        raise PersonaNotFoundError("persona not found", context={"id": _PERSONA})

    def _never(*_a: Any) -> None:  # noqa: ANN401
        pytest.fail("nothing to erase here")

    monkeypatch.setattr(erasure_start, "begin_persona_erasure", _gone)
    monkeypatch.setattr(erasure_start, "erase_deps_from_state", lambda _state: object())
    monkeypatch.setattr(erasure_start, "erase_persona", _never)

    with pytest.raises(PersonaNotFoundError):
        _undo(_state())


def test_with_no_platform_engine_the_undo_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(erasure_start, "erase_deps_from_state", lambda _state: None)

    with pytest.raises(RuntimeError, match="no platform engine"):
        _undo(_state())

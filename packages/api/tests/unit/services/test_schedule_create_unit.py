"""A10 T1 — unit: the shared builders, subject hygiene, boundary 422s, and the grep-proof.

The pure halves of the create door: deterministic id derivation (A10-D-6), the
schedule-backed-task shape (A10-D-8 — WAITING + fire bridge), subject normalisation
(K6-D-8 posture), the route's cadence XOR / timezone / missing-key 422s, and the
STRUCTURAL one-write-path proof (A10-D-9): no module outside ``schedules/store.py``
INSERTs the ``schedules`` table.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import HTTPException
from persona.schedules import RecurrenceFreq, RecurrenceRule
from persona.tasks import Contract, TaskState, WaitKind
from persona_api.routes.me import _reschedule_body
from persona_api.schemas.requests import ScheduleCreateRequest
from persona_api.services.schedule_create_service import (
    SUBJECT_MAX_LENGTH,
    normalize_subject,
)
from persona_api.tasks.scheduled_fire import TASK_SCHEDULED_FIRE_JOB_TYPE
from persona_api.tasks.scheduled_task_builders import (
    build_backing_schedule,
    build_backing_task,
    derive_task_and_schedule_ids,
)
from pydantic import ValidationError

_NOW = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
_CONTRACT = Contract(goal="Remind and update the user about: hydration")


# --- the shared builders (A10-D-8) ---------------------------------------------------


def test_derive_ids_are_deterministic_and_key_scoped() -> None:
    """One key ⇒ one (task, schedule) pair, always; distinct keys ⇒ distinct pairs."""
    a1 = derive_task_and_schedule_ids("user-create:owner:key-1")
    a2 = derive_task_and_schedule_ids("user-create:owner:key-1")
    b = derive_task_and_schedule_ids("user-create:owner:key-2")
    assert a1 == a2
    assert a1 != b
    assert a1[0].startswith("task-")
    assert a1[1].startswith("sched-")


def test_schedule_backed_task_is_born_waiting_until_time() -> None:
    """The load-bearing shape: a scheduled task is dormant-but-runnable, never inert."""
    task = build_backing_task(
        task_id="task-x",
        owner_id="owner",
        persona_id="p1",
        contract=_CONTRACT,
        conversation_id=None,
        schedule_id="sched-x",
        now=_NOW,
    )
    assert task.state is TaskState.WAITING
    assert task.wait_kind is WaitKind.UNTIL_TIME
    assert task.conversation_id is None  # the calendar door has no originating chat turn


def test_scheduleless_task_stays_defined() -> None:
    task = build_backing_task(
        task_id="task-x",
        owner_id="owner",
        persona_id="p1",
        contract=_CONTRACT,
        conversation_id="conv",
        schedule_id=None,
        now=_NOW,
    )
    assert task.state is TaskState.DEFINED
    assert task.wait_kind is None


def test_backing_schedule_targets_the_fire_bridge_with_the_task_payload() -> None:
    """A10-D-2: the fire bridge, never ``task_leg`` directly; the payload names the task."""
    schedule = build_backing_schedule(
        schedule_id="sched-x",
        owner_id="owner",
        timezone="Europe/Oslo",
        recurrence=RecurrenceRule(freq=RecurrenceFreq.DAILY, byhour=(9,), byminute=(0,)),
        one_time_at=None,
        task_id="task-x",
        now=_NOW,
    )
    assert schedule.target_job_type == TASK_SCHEDULED_FIRE_JOB_TYPE
    # Default (no notify/subject passed): the template is byte-unchanged and notify_on_fire
    # is off — a background schedule stays quiet and snapshots nothing extra.
    assert schedule.payload_template == {"task_id": "task-x"}
    assert schedule.notify_on_fire is False


# --- subject hygiene (A10-D-1, the K6-D-8 posture) ------------------------------------


def test_subject_strips_control_chars_and_trims() -> None:
    assert normalize_subject("  drink\nwater‎\t ") == "drinkwater"


def test_subject_empty_after_normalisation_is_falsy() -> None:
    assert normalize_subject(" \n\t​ ") == ""


def test_subject_is_capped() -> None:
    assert len(normalize_subject("x" * 10_000)) == SUBJECT_MAX_LENGTH


# --- the boundary 422s (route + schema) ------------------------------------------------


def _create_body(**overrides: object) -> ScheduleCreateRequest:
    base: dict[str, object] = {
        "one_time_at": datetime(2026, 8, 1, 9, 0, tzinfo=UTC),
        "timezone": "Europe/Oslo",
        "persona_id": "p1",
        "subject": "hydration",
        "idempotency_key": "dialog-0001",
    }
    base.update(overrides)
    return ScheduleCreateRequest.model_validate(base)


def test_cadence_xor_both_kinds_is_422() -> None:
    from persona.schedules import RecurrenceKind, RecurrencePattern

    body = _create_body(
        pattern=RecurrencePattern(kind=RecurrenceKind.DAILY, hour=9, minute=0),
    )
    with pytest.raises(HTTPException) as err:
        _reschedule_body(body)
    assert err.value.status_code == 422


def test_cadence_xor_neither_kind_is_422() -> None:
    body = _create_body(one_time_at=None)
    with pytest.raises(HTTPException) as err:
        _reschedule_body(body)
    assert err.value.status_code == 422


def test_unknown_timezone_is_422() -> None:
    body = _create_body(timezone="Mars/Olympus_Mons")
    with pytest.raises(HTTPException) as err:
        _reschedule_body(body)
    assert err.value.status_code == 422


def test_missing_idempotency_key_is_rejected_at_the_schema() -> None:
    """Every create is keyed — no unkeyed branch a replay could duplicate (A10-D-6)."""
    with pytest.raises(ValidationError):
        ScheduleCreateRequest.model_validate(
            {
                "one_time_at": "2026-08-01T09:00:00Z",
                "timezone": "Europe/Oslo",
                "persona_id": "p1",
                "subject": "hydration",
            }
        )


def test_short_idempotency_key_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _create_body(idempotency_key="short")


# --- the structural one-write-path proof (A10-D-9, grep-proof) -------------------------


def test_no_schedule_write_path_outside_the_store() -> None:
    """Mechanical D-9: only ``schedules/store.py`` INSERTs/UPDATEs the schedules table.

    A second write path is the two-doors-drift risk (spec §7); this pins it structurally —
    any new module touching ``schedules_t`` with a write verb fails here, not in review.
    """
    src = Path(__file__).resolve().parents[3] / "src" / "persona_api"
    write_re = re.compile(r"(insert|update|delete)\(\s*schedules_t")
    offenders: list[str] = []
    for path in src.rglob("*.py"):
        if path.name == "store.py" and path.parent.name == "schedules":
            continue
        if write_re.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(src)))
    assert offenders == [], f"schedule-write path outside ScheduleStore: {offenders}"

"""Unit tests for the grounded-introspection projection + reader contract (Spec A4, T7)."""

from __future__ import annotations

from datetime import UTC, datetime

from persona.tasks import (
    Contract,
    CostLedger,
    IntrospectionStatus,
    Task,
    TaskCheckpoint,
    TaskState,
    TaskStateView,
    WaitKind,
    project_task_state,
    summarise_task,
)

_NOW = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)


def _task(
    *,
    state: TaskState = TaskState.ACTIVE,
    wait_kind: WaitKind | None = None,
    paused: bool = False,
    head_seq: int | None = None,
    spent: int = 0,
) -> Task:
    return Task(
        id="t1",
        owner_id="user-a",
        persona_id="astrid",
        contract=Contract(goal="track the Oslo→Bergen fares"),
        state=state,
        wait_kind=wait_kind,
        paused=paused,
        head_checkpoint_seq=head_seq,
        ledger=CostLedger(model_micros=spent),
        created_at=_NOW,
        updated_at=_NOW,
    )


def _checkpoint(
    *,
    progress: tuple[str, ...] = (),
    next_step: str = "",
    blocked_on: str | None = None,
    open_questions: tuple[str, ...] = (),
) -> TaskCheckpoint:
    return TaskCheckpoint(
        task_id="t1",
        leg_id="leg-1",
        checkpoint_seq=1,
        progress_conclusions=progress,
        next_step=next_step,
        blocked_on=blocked_on,
        open_questions=open_questions,
        updated_at=_NOW,
    )


# --- the honest state matrix -----------------------------------------------------------


def test_just_created_task_has_no_progress() -> None:
    view = project_task_state(_task(state=TaskState.DEFINED), None)
    assert view.status is IntrospectionStatus.JUST_CREATED
    assert view.progress == ()
    assert view.next_step == ""


def test_active_without_checkpoint_is_still_just_created() -> None:
    view = project_task_state(_task(state=TaskState.ACTIVE, head_seq=None), None)
    assert view.status is IntrospectionStatus.JUST_CREATED


def test_progressing_surfaces_distilled_conclusions() -> None:
    cp = _checkpoint(progress=("found 9 fares", "cheapest is 1450kr"), next_step="re-check at 7am")
    view = project_task_state(_task(state=TaskState.ACTIVE, head_seq=1), cp)
    assert view.status is IntrospectionStatus.PROGRESSING
    assert view.progress == ("found 9 fares", "cheapest is 1450kr")
    assert view.next_step == "re-check at 7am"


def test_waiting_on_user_surfaces_blocked_on() -> None:
    cp = _checkpoint(blocked_on="the portal needs your login", open_questions=("which account?",))
    view = project_task_state(
        _task(state=TaskState.WAITING, wait_kind=WaitKind.ON_USER, head_seq=1), cp
    )
    assert view.status is IntrospectionStatus.WAITING_ON_USER
    assert view.wait_reason == "the portal needs your login"
    assert view.open_questions == ("which account?",)


def test_scheduled_wait_is_distinct_from_waiting_on_user() -> None:
    view = project_task_state(
        _task(state=TaskState.WAITING, wait_kind=WaitKind.UNTIL_TIME, head_seq=1), _checkpoint()
    )
    assert view.status is IntrospectionStatus.SCHEDULED


def test_terminal_states_project_their_status() -> None:
    assert project_task_state(_task(state=TaskState.COMPLETED), None).status is (
        IntrospectionStatus.COMPLETED
    )
    assert project_task_state(_task(state=TaskState.FAILED), None).status is (
        IntrospectionStatus.FAILED
    )
    assert project_task_state(_task(state=TaskState.CANCELLED), None).status is (
        IntrospectionStatus.CANCELLED
    )


def test_paused_overlay_wins() -> None:
    view = project_task_state(_task(state=TaskState.ACTIVE, paused=True, head_seq=1), _checkpoint())
    assert view.status is IntrospectionStatus.PAUSED


def test_spend_is_surfaced() -> None:
    view = project_task_state(_task(spent=15_000_000, head_seq=1), _checkpoint())
    assert view.spent_micros == 15_000_000


def test_summarise_uses_head_pointer_for_status() -> None:
    assert summarise_task(_task(state=TaskState.ACTIVE, head_seq=None)).status is (
        IntrospectionStatus.JUST_CREATED
    )
    assert summarise_task(_task(state=TaskState.ACTIVE, head_seq=3)).status is (
        IntrospectionStatus.PROGRESSING
    )


# --- transcript-free BY CONSTRUCTION ---------------------------------------------------


def test_view_exposes_only_typed_conclusion_fields_no_transcript() -> None:
    # The view's field set is fixed + typed; none of it is a raw transcript / event log. The
    # checkpoint's only event-ish field (``event_log_cursor``) is a CURSOR, and the projection
    # does not surface it — so there is no path to transcript text through introspection.
    fields = set(TaskStateView.model_fields)
    assert fields == {
        "task_id",
        "goal",
        "status",
        "wait_reason",
        "progress",
        "next_step",
        "open_questions",
        "spent_micros",
        "updated_at",
    }
    assert "event_log_cursor" not in fields
    assert "transcript" not in fields


def test_checkpoint_has_no_transcript_field() -> None:
    # D-A2-1: the checkpoint is conclusions-not-transcripts. Its event reference is a cursor
    # (an id/offset), never the event text — so the reader cannot expose a transcript.
    cp_fields = set(TaskCheckpoint.model_fields)
    assert "event_log_cursor" in cp_fields  # the cursor exists...
    # ...and it is the ONLY event-ish field — there is no raw-events / transcript field.
    assert not (cp_fields & {"transcript", "events", "raw_events", "messages", "event_log"})

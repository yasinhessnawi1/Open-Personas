"""Issue #8: unit tests for the untitled-conversation backfill producer.

Fast, no-DB: the leader gate, the enqueue shape (the SAME ``title_refresh``
payload and key the write-time trigger uses), the dedup accounting, and the
content floor the scan statement carries.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from persona_api.services.title_backfill import (
    MIN_TITLE_MESSAGES,
    TITLE_BACKFILL_LOCK_KEY,
    UntitledConversationBackfill,
    untitled_conversation_candidates,
)
from sqlalchemy.dialects import postgresql

if TYPE_CHECKING:
    from collections.abc import Iterator


class _FakeConn:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self._rows = rows

    def execute(self, _stmt: object) -> list[SimpleNamespace]:
        return self._rows


class _FakeEngine:
    """Duck-typed Engine: ``begin()`` hands out one connection over fixed rows."""

    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self._rows = rows

    @contextlib.contextmanager
    def begin(self) -> Iterator[_FakeConn]:
        yield _FakeConn(self._rows)


class _RecordingQueue:
    def __init__(self, *, accept: bool = True) -> None:
        self.calls: list[dict[str, Any]] = []
        self._accept = accept

    def enqueue(self, **kwargs: Any) -> object | None:  # noqa: ANN401, JobQueue's own shape
        self.calls.append(kwargs)
        return object() if self._accept else None


class _Leader:
    def __init__(self, *, leader: bool) -> None:
        self._leader = leader
        self.asked = 0

    def try_become_leader(self) -> bool:
        self.asked += 1
        return self._leader


def _row(conversation_id: str, owner_id: str, message_count: int) -> SimpleNamespace:
    return SimpleNamespace(
        conversation_id=conversation_id, owner_id=owner_id, message_count=message_count
    )


def _backfill(
    rows: list[SimpleNamespace], *, leader: bool = True, accept: bool = True
) -> tuple[UntitledConversationBackfill, _RecordingQueue, _Leader]:
    queue = _RecordingQueue(accept=accept)
    gate = _Leader(leader=leader)
    return (
        UntitledConversationBackfill(
            dispatch_engine=_FakeEngine(rows),  # type: ignore[arg-type]
            queue=queue,  # type: ignore[arg-type]
            leader=gate,  # type: ignore[arg-type]
        ),
        queue,
        gate,
    )


# ----- the leader gate --------------------------------------------------------


def test_a_follower_enqueues_nothing() -> None:
    backfill, queue, gate = _backfill([_row("conv_1", "u1", 4)], leader=False)
    assert backfill.run_once() == 0
    assert queue.calls == []
    assert gate.asked == 1


# ----- the enqueue: the same job the write-time trigger produces ---------------


def test_each_untitled_conversation_gets_the_ordinary_title_refresh_job() -> None:
    backfill, queue, _ = _backfill([_row("conv_tg", "u1", 2), _row("conv_call", "u2", 7)])
    assert backfill.run_once() == 2
    assert [c["type"] for c in queue.calls] == ["title_refresh", "title_refresh"]
    assert [c["owner_id"] for c in queue.calls] == ["u1", "u2"]
    # Keyed on the count that was actually found, exactly as the voice writer keys
    # on the call's final count, so a conversation that grew is a NEW refresh.
    assert [c["idempotency_key"] for c in queue.calls] == ["title:conv_tg:2", "title:conv_call:7"]
    assert queue.calls[0]["payload"] == {"conversation_id": "conv_tg", "threshold": 2}


def test_a_conflict_noop_is_not_counted_as_work() -> None:
    # The queue already holds this exact key: A0's ON CONFLICT returns nothing, so a
    # repeated pass over an undrained backlog reports zero rather than the same rows.
    backfill, queue, _ = _backfill([_row("conv_1", "u1", 2)], accept=False)
    assert backfill.run_once() == 0
    assert len(queue.calls) == 1


def test_no_candidates_is_a_clean_zero() -> None:
    backfill, queue, _ = _backfill([])
    assert backfill.run_once() == 0
    assert queue.calls == []


# ----- the scan: the content floor and what it reads --------------------------


def test_the_scan_only_takes_untitled_conversations_with_a_real_exchange() -> None:
    sql = str(
        untitled_conversation_candidates(limit=25, min_messages=MIN_TITLE_MESSAGES).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    # Untitled only: a conversation that already has a name is never re-titled here.
    assert "conversations.title = ''" in sql
    # Enough content to name, and only the roles the title handler actually reads.
    assert "HAVING count(messages.id) >= 2" in sql
    assert "messages.role IN ('user', 'assistant')" in sql
    assert "messages.content != ''" in sql
    # Bounded work per pass.
    assert "LIMIT 25" in sql


def test_the_floor_matches_the_triggers_first_threshold() -> None:
    from persona_api.services.title_trigger import TITLE_REFRESH_THRESHOLDS

    assert TITLE_REFRESH_THRESHOLDS[0] == MIN_TITLE_MESSAGES


def test_the_leader_key_is_its_own() -> None:
    from persona_api.approvals import APPROVAL_SWEEP_LOCK_KEY
    from persona_api.schedules.leadership import SCHEDULER_LEADER_LOCK_KEY
    from persona_api.tasks.revival_sweep import REVIVAL_SWEEP_LOCK_KEY

    assert TITLE_BACKFILL_LOCK_KEY not in {
        SCHEDULER_LEADER_LOCK_KEY,
        APPROVAL_SWEEP_LOCK_KEY,
        REVIVAL_SWEEP_LOCK_KEY,
    }

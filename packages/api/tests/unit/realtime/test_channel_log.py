"""Spec A11 T1 — the transport envelope (epoch/seq/``id:``) + the per-user event log
+ the reconnect resolver (A11-D-3, restart-safe resume).

The domain events are pure; this layer assigns the monotonic ``seq``, frames the
SSE bytes with a server-authoritative ``epoch:seq`` id, keeps a bounded per-user
ring, and resolves a reconnecting client's ``Last-Event-ID`` to REPLAY / RESYNC /
READY. All of ``record`` (seq read/increment + ring append) is synchronous — the
publish critical section must never await (T1 impl requirement).
"""

from __future__ import annotations

import json

from persona_api.realtime.envelope import (
    HEARTBEAT,
    format_event_id,
    frame_data,
    frame_ready,
    frame_resync,
    parse_event_id,
)
from persona_api.realtime.events import NotificationCreatedEvent, TaskUpdatedEvent
from persona_api.realtime.log import UserEventLog

EPOCH = "9f3ac1"


def _evt(n: int) -> NotificationCreatedEvent:
    return NotificationCreatedEvent(notification_id=f"n{n}", kind="schedule_fire", ref_id=f"s{n}")


# --- envelope framing ------------------------------------------------------


def test_format_and_parse_event_id_round_trip() -> None:
    assert format_event_id(EPOCH, 42) == "9f3ac1:42"
    assert parse_event_id("9f3ac1:42") == (EPOCH, 42)


def test_parse_event_id_rejects_malformed() -> None:
    assert parse_event_id("no-colon") is None
    assert parse_event_id("epoch:notanint") is None
    assert parse_event_id("") is None


def test_frame_data_exact_bytes() -> None:
    e = TaskUpdatedEvent(task_id="t1", state="WAITING")
    frame = frame_data(EPOCH, 7, e)
    expected = (
        f"id: 9f3ac1:7\nevent: task.updated\ndata: {json.dumps(e.model_dump(mode='json'))}\n\n"
    ).encode()
    assert frame == expected


def test_frame_ready_has_no_id_line_and_control_event_name() -> None:
    frame = frame_ready(EPOCH, 42).decode()
    assert "id:" not in frame
    assert frame.startswith("event: ready\n")
    body = json.loads(frame.split("data: ", 1)[1].rstrip("\n"))
    assert body == {"v": 1, "type": "ready", "epoch": EPOCH, "latest_seq": 42}


def test_frame_resync_has_no_id_line_and_reason() -> None:
    frame = frame_resync(EPOCH, 42, "epoch_changed").decode()
    assert "id:" not in frame
    assert frame.startswith("event: resync\n")
    body = json.loads(frame.split("data: ", 1)[1].rstrip("\n"))
    assert body["type"] == "resync"
    assert body["reason"] == "epoch_changed"


def test_heartbeat_is_a_bare_comment() -> None:
    # A ``:`` comment line the SSE client parser skips; keeps the idle stream alive.
    assert HEARTBEAT == b": hb\n\n"


# --- the per-user log: record (synchronous) --------------------------------


def test_record_assigns_monotonic_seq_from_one() -> None:
    log = UserEventLog(epoch=EPOCH)
    r1 = log.record(_evt(1))
    r2 = log.record(_evt(2))
    assert r1.seq == 1
    assert r2.seq == 2
    assert r1.event_id == "9f3ac1:1"
    assert log.latest_seq == 2


def test_record_returns_the_wire_frame() -> None:
    log = UserEventLog(epoch=EPOCH)
    e = _evt(1)
    r = log.record(e)
    assert r.frame == frame_data(EPOCH, 1, e)


# --- resume: fresh connect --------------------------------------------------


def test_resume_no_last_event_id_is_ready_with_baseline() -> None:
    log = UserEventLog(epoch=EPOCH)
    log.record(_evt(1))
    log.record(_evt(2))
    plan = log.resume(None)
    assert plan.kind == "ready"
    assert plan.frames == (frame_ready(EPOCH, 2),)


def test_resume_ready_baseline_when_empty_is_zero() -> None:
    log = UserEventLog(epoch=EPOCH)
    plan = log.resume(None)
    assert plan.kind == "ready"
    assert plan.frames == (frame_ready(EPOCH, 0),)


# --- resume: matched epoch, in-ring -> replay ------------------------------


def test_resume_in_ring_replays_frames_after_the_cursor() -> None:
    log = UserEventLog(epoch=EPOCH)
    frames = [log.record(_evt(i)).frame for i in range(1, 5)]  # seq 1..4
    plan = log.resume("9f3ac1:2")
    assert plan.kind == "replay"
    assert plan.reason is None
    assert plan.frames == (frames[2], frames[3])  # seq 3, 4


def test_resume_caught_up_replays_nothing_and_goes_live() -> None:
    log = UserEventLog(epoch=EPOCH)
    log.record(_evt(1))
    log.record(_evt(2))
    plan = log.resume("9f3ac1:2")  # client has the latest
    assert plan.kind == "replay"
    assert plan.frames == ()


def test_resume_boundary_oldest_minus_one_still_replays() -> None:
    # ring holds seq {2,3} (oldest=2); a client at seq 1 == oldest-1 is fully
    # covered by replaying from 2 — the boundary is honoured, not gapped.
    log = UserEventLog(epoch=EPOCH, ring_size=2)
    log.record(_evt(1))
    f2 = log.record(_evt(2)).frame
    f3 = log.record(_evt(3)).frame  # evicts seq 1; ring = {2,3}
    plan = log.resume("9f3ac1:1")
    assert plan.kind == "replay"
    assert plan.frames == (f2, f3)


# --- resume: RESYNC cases ---------------------------------------------------


def test_resume_epoch_mismatch_is_resync_epoch_changed() -> None:
    log = UserEventLog(epoch=EPOCH)
    log.record(_evt(1))
    plan = log.resume("OLDEPOCH:1")
    assert plan.kind == "resync"
    assert plan.reason == "epoch_changed"
    assert plan.frames == (frame_resync(EPOCH, 1, "epoch_changed"),)


def test_resume_fell_off_ring_is_resync_ring_gap() -> None:
    log = UserEventLog(epoch=EPOCH, ring_size=2)
    for i in range(1, 6):  # seq 1..5; ring keeps {4,5}, oldest=4
        log.record(_evt(i))
    plan = log.resume("9f3ac1:1")  # 1 < oldest-1 (=3) -> gap
    assert plan.kind == "resync"
    assert plan.reason == "ring_gap"
    assert plan.frames == (frame_resync(EPOCH, 5, "ring_gap"),)


def test_resume_malformed_last_event_id_is_resync_ring_gap() -> None:
    log = UserEventLog(epoch=EPOCH)
    log.record(_evt(1))
    plan = log.resume("garbage")
    assert plan.kind == "resync"
    assert plan.reason == "ring_gap"


def test_resume_seq_beyond_latest_is_resync_ring_gap() -> None:
    log = UserEventLog(epoch=EPOCH)
    log.record(_evt(1))
    plan = log.resume("9f3ac1:99")  # claims a seq we never issued
    assert plan.kind == "resync"
    assert plan.reason == "ring_gap"


# --- the ring is bounded ----------------------------------------------------


def test_ring_is_bounded_to_ring_size() -> None:
    log = UserEventLog(epoch=EPOCH, ring_size=3)
    for i in range(1, 11):  # 10 records, ring holds last 3
        log.record(_evt(i))
    # seq 7 still present (oldest=8? ring={8,9,10}); a cursor at 7 == oldest-1 replays 3
    plan = log.resume("9f3ac1:7")
    assert plan.kind == "replay"
    assert len(plan.frames) == 3
    # a cursor at 6 has fallen off -> gap
    assert log.resume("9f3ac1:6").kind == "resync"

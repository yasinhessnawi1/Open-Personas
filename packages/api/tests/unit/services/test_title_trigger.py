"""R9-020 — unit tests for the title-refresh trigger (thresholds + producer).

Fast, no-DB: the crossing function's EXACT firing set (one fire per threshold,
no re-fire between thresholds, parity-robust), the enqueue payload/idempotency
key shape, and the queue-absent no-op.
"""

from __future__ import annotations

from persona_api.jobs.handlers.title_refresh import (
    TITLE_REFRESH_JOB_TYPE,
    TitleRefreshJobPayload,
    title_refresh_idempotency_key,
)
from persona_api.services.title_trigger import (
    TITLE_REFRESH_THRESHOLDS,
    crossed_title_threshold,
    enqueue_conversation_title_refresh,
)


class _RecordingQueue:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def enqueue(self, **kwargs: object) -> None:
        self.calls.append(kwargs)


# ----- the crossing function: exact set, no re-fire ---------------------------


def test_threshold_set_is_the_r9_020_contract() -> None:
    assert TITLE_REFRESH_THRESHOLDS == (4, 10, 24, 50, 100)


def test_chat_turns_fire_exactly_the_threshold_set() -> None:
    # A pure chat conversation grows 0→2→4→…; walking every turn must fire each
    # threshold exactly once, in order, and nothing in between.
    fired: list[int] = []
    for prior in range(0, 120, 2):
        crossed = crossed_title_threshold(previous_count=prior, new_count=prior + 2)
        if crossed is not None:
            fired.append(crossed)
    assert fired == [4, 10, 24, 50, 100]


def test_no_refire_between_thresholds() -> None:
    # Sitting past a threshold: every step until the NEXT threshold is None.
    assert crossed_title_threshold(previous_count=4, new_count=6) is None
    assert crossed_title_threshold(previous_count=6, new_count=8) is None
    # The next one fires exactly at its own crossing.
    assert crossed_title_threshold(previous_count=8, new_count=10) == 10


def test_crossing_is_parity_robust() -> None:
    # A voice-born / nudge-carrying conversation can sit on an odd count; the
    # window (previous, new] still catches the threshold it passes over.
    assert crossed_title_threshold(previous_count=3, new_count=5) == 4
    assert crossed_title_threshold(previous_count=9, new_count=11) == 10


def test_landing_exactly_on_a_threshold_counts_and_never_refires() -> None:
    assert crossed_title_threshold(previous_count=2, new_count=4) == 4
    # The NEXT growth step from exactly-4 must not re-fire 4 (half-open left edge).
    assert crossed_title_threshold(previous_count=4, new_count=5) is None


def test_a_jump_over_several_thresholds_fires_the_highest() -> None:
    # One durable job per crossing EVENT: a bulk jump titles once, at the
    # latest (most-informed) threshold, not once per skipped threshold.
    assert crossed_title_threshold(previous_count=0, new_count=30) == 24


def test_non_growth_never_fires() -> None:
    assert crossed_title_threshold(previous_count=4, new_count=4) is None
    assert crossed_title_threshold(previous_count=10, new_count=4) is None


# ----- the producer: payload + key + no-op paths ------------------------------


def test_enqueue_fires_with_the_threshold_keyed_idempotency() -> None:
    q = _RecordingQueue()
    enqueue_conversation_title_refresh(
        q,  # type: ignore[arg-type]
        owner_id="u1",
        conversation_id="conv_1",
        previous_count=2,
        new_count=4,
    )
    assert len(q.calls) == 1
    call = q.calls[0]
    assert call["type"] == TITLE_REFRESH_JOB_TYPE
    assert call["owner_id"] == "u1"
    assert call["idempotency_key"] == "title:conv_1:4"
    assert call["payload"] == {"conversation_id": "conv_1", "threshold": 4}


def test_enqueue_is_a_noop_between_thresholds() -> None:
    q = _RecordingQueue()
    enqueue_conversation_title_refresh(
        q,  # type: ignore[arg-type]
        owner_id="u1",
        conversation_id="conv_1",
        previous_count=4,
        new_count=6,
    )
    assert q.calls == []


def test_enqueue_is_a_noop_without_a_queue() -> None:
    # Must not raise — the CLI / unit-test path has no durable queue.
    enqueue_conversation_title_refresh(
        None, owner_id="u1", conversation_id="conv_1", previous_count=2, new_count=4
    )


def test_idempotency_key_shape_is_title_conv_threshold() -> None:
    key = title_refresh_idempotency_key(
        TitleRefreshJobPayload(conversation_id="conv_9", threshold=24)
    )
    assert key == "title:conv_9:24"
    other = title_refresh_idempotency_key(
        TitleRefreshJobPayload(conversation_id="conv_9", threshold=50)
    )
    assert key != other  # a later threshold is a NEW refresh, never deduped away

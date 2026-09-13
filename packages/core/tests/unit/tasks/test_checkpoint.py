"""Unit tests for the task checkpoint schema + size bound (Spec A2, T1).

The checkpoint is the architectural lock (D-A2-1): per-leg sequenced working state
(conclusions/decisions/lessons + regenerated plan + pointers), bounded so it stays
*intent, not history*. These tests pin the frozen shape, the content-hash + tz-aware
discipline, and the token-budget gate that raises ``CheckpointTooLargeError`` — the
boundary that forces conclusions-not-transcripts.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta, timezone

import pytest
from persona.errors import CheckpointTooLargeError
from persona.skills import count_tokens
from persona.tasks import (
    CHECKPOINT_SCHEMA_VERSION,
    DEFAULT_CHECKPOINT_TOKEN_BUDGET,
    ArtifactPointer,
    Decision,
    TaskCheckpoint,
    checkpoint_token_count,
    enforce_checkpoint_budget,
)
from pydantic import ValidationError

_NOW = datetime(2026, 6, 24, 12, 0, tzinfo=UTC)


def _checkpoint(**overrides: object) -> TaskCheckpoint:
    """A minimal valid checkpoint; override any field per test."""
    base: dict[str, object] = {
        "task_id": "task-1",
        "leg_id": "leg-1",
        "checkpoint_seq": 0,
        "next_step": "re-check Wed morning",
        "updated_at": _NOW,
    }
    base.update(overrides)
    return TaskCheckpoint(**base)  # type: ignore[arg-type]


# --- shape: frozen + extra-forbid + minimal-valid ----------------------------


def test_minimal_checkpoint_is_valid() -> None:
    cp = _checkpoint()
    assert cp.task_id == "task-1"
    assert cp.checkpoint_seq == 0
    assert cp.next_step == "re-check Wed morning"
    # the accumulating core defaults empty — a fresh task's first checkpoint.
    assert cp.progress_conclusions == ()
    assert cp.decisions == ()
    assert cp.lessons == ()
    assert cp.schema_version == CHECKPOINT_SCHEMA_VERSION


def test_checkpoint_is_frozen() -> None:
    cp = _checkpoint()
    with pytest.raises(ValidationError):
        cp.next_step = "mutated"  # type: ignore[misc]


def test_checkpoint_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        _checkpoint(unexpected="x")


def test_full_checkpoint_round_trips() -> None:
    cp = _checkpoint(
        progress_conclusions=("best fare so far 1620kr (SAS, Tue)",),
        decisions=(
            Decision(
                decision="exclude redeye flights", rationale="user sleeps poorly", leg_id="leg-1"
            ),
        ),
        lessons=("the airline API rate-limits at 10/min",),
        current_plan=("re-check Wed 07:00", "compare against Thu"),
        open_questions=("is the user flexible on dates?",),
        blocked_on=None,
        artifact_pointers=(ArtifactPointer(kind="workspace", ref="prices.csv"),),
        event_log_cursor="run-42:step-7",
    )
    assert cp.decisions[0].rationale == "user sleeps poorly"
    assert cp.artifact_pointers[0].ref == "prices.csv"
    assert cp.event_log_cursor == "run-42:step-7"


# --- sub-models are frozen boundary types ------------------------------------


def test_decision_is_frozen_and_forbids_extra() -> None:
    d = Decision(decision="x", rationale="y", leg_id="leg-1")
    with pytest.raises(ValidationError):
        d.decision = "z"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        Decision(decision="x", rationale="y", leg_id="leg-1", extra="no")  # type: ignore[call-arg]


def test_artifact_pointer_is_frozen_and_forbids_extra() -> None:
    p = ArtifactPointer(kind="workspace", ref="out.txt")
    with pytest.raises(ValidationError):
        p.ref = "other"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        ArtifactPointer(kind="workspace", ref="out.txt", extra="no")  # type: ignore[call-arg]


# --- tz-aware UTC discipline --------------------------------------------------


def test_naive_updated_at_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _checkpoint(updated_at=datetime(2026, 6, 24, 12, 0))  # noqa: DTZ001 — intentionally naive


def test_non_utc_updated_at_is_normalised_to_utc() -> None:
    plus_two = timezone(timedelta(hours=2))
    cp = _checkpoint(updated_at=datetime(2026, 6, 24, 14, 0, tzinfo=plus_two))
    assert cp.updated_at == _NOW  # 14:00+02:00 == 12:00Z
    assert cp.updated_at.tzinfo == UTC


# --- content_hash: computed, stable, tamper-evident --------------------------


def test_content_hash_autocomputed_when_empty() -> None:
    cp = _checkpoint()
    assert cp.content_hash  # populated
    assert len(cp.content_hash) == 64  # sha256 hexdigest


def test_content_hash_is_deterministic_for_same_content() -> None:
    a = _checkpoint(progress_conclusions=("x", "y"))
    b = _checkpoint(progress_conclusions=("x", "y"))
    assert a.content_hash == b.content_hash


def test_content_hash_changes_with_content() -> None:
    a = _checkpoint(progress_conclusions=("x",))
    b = _checkpoint(progress_conclusions=("x", "y"))
    assert a.content_hash != b.content_hash


def test_content_hash_ignores_updated_at() -> None:
    # A re-write at a later wall-clock time with identical content keeps the hash.
    later = datetime(2026, 6, 25, 9, 0, tzinfo=UTC)
    a = _checkpoint(progress_conclusions=("x",))
    b = _checkpoint(progress_conclusions=("x",), updated_at=later)
    assert a.content_hash == b.content_hash


# --- the size bound: counts the accumulating core only -----------------------


def test_token_count_covers_the_accumulating_core() -> None:
    cp = _checkpoint(
        progress_conclusions=("alpha beta gamma",),
        decisions=(Decision(decision="delta", rationale="epsilon", leg_id="leg-1"),),
        lessons=("zeta",),
    )
    expected = count_tokens("alpha beta gamma delta epsilon zeta")
    # the count is the conclusions+decisions(+rationale)+lessons text; spacing/join
    # may differ so allow a small tokenizer slack, but it must be in the ballpark.
    assert abs(checkpoint_token_count(cp) - expected) <= 3


def test_token_count_covers_the_query_and_source_ledgers() -> None:
    """Spec W1 (D-W1-16): the two ledgers joined the accumulating core, so they are inside
    the same gate. A ledger outside the cap would grow without one, which is precisely the
    unbounded accumulation the cap exists to prevent."""
    lean = _checkpoint(progress_conclusions=("one short conclusion",))
    with_ledgers = _checkpoint(
        progress_conclusions=("one short conclusion",),
        queries_run=("husleieloven deposit interest", "husleietvistutvalget statistics"),
        sources_seen=("https://lovdata.no/dokument/NL/lov/1999-03-26-17",),
    )

    assert checkpoint_token_count(with_ledgers) > checkpoint_token_count(lean)


def test_the_ledgers_can_push_a_checkpoint_over_its_budget() -> None:
    """The gate has to be reachable through them, or being "in the core" means nothing."""
    fat = _checkpoint(
        progress_conclusions=("short",),
        queries_run=tuple(f"a reasonably wordy search query number {i}" for i in range(200)),
    )

    with pytest.raises(CheckpointTooLargeError):
        enforce_checkpoint_budget(fat, token_budget=200)


def test_a_checkpoint_written_before_the_ledgers_existed_still_reads_back() -> None:
    """Every checkpoint already in a database was hashed WITHOUT the two W1 fields. Without
    the pre-ledger hash path each of them would read back as tampered and every running
    task would break on its next leg."""
    current = _checkpoint(progress_conclusions=("found it",))
    blob = current.model_dump(mode="json")
    legacy = {k: v for k, v in blob.items() if k not in {"queries_run", "sources_seen"}}
    payload = json.dumps(
        {k: v for k, v in legacy.items() if k not in {"content_hash", "updated_at"}},
        sort_keys=True,
        ensure_ascii=False,
    )
    legacy["content_hash"] = hashlib.sha256(payload.encode("utf-8")).hexdigest()

    restored = TaskCheckpoint.model_validate(legacy)

    assert restored.progress_conclusions == ("found it",)
    assert restored.queries_run == ()


def test_the_pre_ledger_path_does_not_excuse_a_tampered_checkpoint() -> None:
    """It applies only to the exact shape a pre-W1 row deserializes into, so it can never
    become a way to edit a checkpoint's content and keep its old hash."""
    current = _checkpoint(progress_conclusions=("found it",))
    blob = current.model_dump(mode="json")
    legacy = {k: v for k, v in blob.items() if k not in {"queries_run", "sources_seen"}}
    payload = json.dumps(
        {k: v for k, v in legacy.items() if k not in {"content_hash", "updated_at"}},
        sort_keys=True,
        ensure_ascii=False,
    )
    legacy["content_hash"] = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    tampered = {**legacy, "progress_conclusions": ["edited by someone"]}

    with pytest.raises(ValidationError):
        TaskCheckpoint.model_validate(tampered)

    # And a checkpoint that HAS ledgers gets no such grace: the current hash is the only one.
    with_ledger = {**legacy, "queries_run": ["a query"]}
    with pytest.raises(ValidationError):
        TaskCheckpoint.model_validate(with_ledger)


def test_token_count_excludes_plan_and_pointers() -> None:
    """The cap targets the accumulating core — a huge plan must NOT count against it.

    This is the whole point of the bound: it forces *conclusions, not history*, and
    the regenerated plan / pointers are explicitly outside the cap (D-A2-1).
    """
    huge_plan = tuple(f"plan step {i} " * 50 for i in range(50))
    huge_pointers = tuple(ArtifactPointer(kind="workspace", ref="f" * 200) for _ in range(50))
    lean = _checkpoint(progress_conclusions=("one short conclusion",))
    fat_plan = _checkpoint(
        progress_conclusions=("one short conclusion",),
        current_plan=huge_plan,
        next_step="x" * 5000,
        artifact_pointers=huge_pointers,
    )
    assert checkpoint_token_count(lean) == checkpoint_token_count(fat_plan)


# --- enforce_checkpoint_budget: the boundary gate ----------------------------


def test_default_budget_is_2000() -> None:
    assert DEFAULT_CHECKPOINT_TOKEN_BUDGET == 2000


def test_enforce_passes_under_budget() -> None:
    cp = _checkpoint(progress_conclusions=("short",))
    enforce_checkpoint_budget(cp)  # no raise


def test_enforce_passes_with_huge_plan_under_budget() -> None:
    # A fat plan does not trip the cap — only the accumulating core does.
    cp = _checkpoint(current_plan=tuple(f"step {i} " * 100 for i in range(100)))
    enforce_checkpoint_budget(cp)  # no raise


def test_enforce_raises_over_budget() -> None:
    over = _checkpoint(progress_conclusions=tuple("conclusion " * 100 for _ in range(50)))
    with pytest.raises(CheckpointTooLargeError):
        enforce_checkpoint_budget(over, token_budget=100)


def test_enforce_error_carries_structured_context() -> None:
    over = _checkpoint(
        task_id="task-9",
        leg_id="leg-3",
        checkpoint_seq=4,
        progress_conclusions=tuple("word " * 100 for _ in range(20)),
    )
    with pytest.raises(CheckpointTooLargeError) as exc:
        enforce_checkpoint_budget(over, token_budget=50)
    ctx = exc.value.context
    assert ctx["task_id"] == "task-9"
    assert ctx["leg_id"] == "leg-3"
    assert ctx["checkpoint_seq"] == "4"
    assert ctx["token_budget"] == "50"
    assert int(ctx["token_count"]) > 50


def test_enforce_respects_custom_budget() -> None:
    cp = _checkpoint(progress_conclusions=("a b c d e f g h i j k",))
    # under a generous budget: fine; under a tiny budget: raises.
    enforce_checkpoint_budget(cp, token_budget=1000)
    with pytest.raises(CheckpointTooLargeError):
        enforce_checkpoint_budget(cp, token_budget=1)

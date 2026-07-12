"""Unit tests for persona_runtime.logging (T06; D-05-9, D-05-10)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona_runtime.logging import (
    JSONLTurnLogWriter,
    MemoryTurnLogWriter,
    TurnLog,
    TurnLogWriter,
)

if TYPE_CHECKING:
    from pathlib import Path


def _log(**overrides: object) -> TurnLog:
    base: dict[str, object] = {
        "conversation_id": "c1",
        "turn_index": 0,
        "tier_used": "frontier",
        "model_name": "claude-sonnet-4-6",
        "provider": "anthropic",
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "latency_ms": 123.4,
        "cost_cents": 0.105,
        "tool_calls": 0,
        "skill_used": None,
        "history_compacted": False,
        "timestamp": datetime.now(UTC),
    }
    base.update(overrides)
    return TurnLog(**base)  # type: ignore[arg-type]


class TestTurnLog:
    def test_constructs_with_all_fields(self) -> None:
        log = _log(tool_calls=2, skill_used="web_research", history_compacted=True)
        assert log.tool_calls == 2
        assert log.skill_used == "web_research"
        assert log.history_compacted is True

    def test_naive_timestamp_rejected(self) -> None:
        with pytest.raises(ValueError, match="naive datetime"):
            _log(timestamp=datetime(2026, 5, 28, 9, 0, 0))  # noqa: DTZ001 — intentionally naive

    def test_frozen(self) -> None:
        log = _log()
        with pytest.raises(ValueError, match="frozen|Instance is frozen"):
            log.tier_used = "mid"  # type: ignore[misc]

    def test_extra_field_forbidden(self) -> None:
        with pytest.raises(ValueError, match="Extra inputs|extra"):
            _log(bogus=True)

    def test_round_trips_via_json(self) -> None:
        log = _log(skill_used="document_drafting")
        restored = TurnLog.model_validate_json(log.model_dump_json())
        assert restored == log


class TestTurnLogWriterProtocol:
    def test_memory_writer_satisfies_protocol(self) -> None:
        assert isinstance(MemoryTurnLogWriter(), TurnLogWriter)

    def test_jsonl_writer_satisfies_protocol(self, tmp_path: Path) -> None:
        assert isinstance(JSONLTurnLogWriter(tmp_path), TurnLogWriter)


class TestMemoryTurnLogWriter:
    def test_accumulates_in_order(self) -> None:
        writer = MemoryTurnLogWriter()
        writer.write(_log(turn_index=0))
        writer.write(_log(turn_index=1))
        assert [log.turn_index for log in writer.logs] == [0, 1]


class TestJSONLTurnLogWriter:
    def test_appends_one_line_per_turn(self, tmp_path: Path) -> None:
        writer = JSONLTurnLogWriter(tmp_path / "turnlogs")
        writer.write(_log(conversation_id="conv", turn_index=0))
        writer.write(_log(conversation_id="conv", turn_index=1))

        path = tmp_path / "turnlogs" / "conv.jsonl"
        lines = path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2
        parsed = [json.loads(line) for line in lines]
        assert [p["turn_index"] for p in parsed] == [0, 1]
        assert parsed[0]["provider"] == "anthropic"

    def test_separate_files_per_conversation(self, tmp_path: Path) -> None:
        writer = JSONLTurnLogWriter(tmp_path / "tl")
        writer.write(_log(conversation_id="a"))
        writer.write(_log(conversation_id="b"))
        assert (tmp_path / "tl" / "a.jsonl").exists()
        assert (tmp_path / "tl" / "b.jsonl").exists()


class TestCostBasisField:
    """Spec M2 (M2-T1): the repurposed ``cost_basis`` field on TurnLog.

    The ``TestCostEstimation`` class that lived here tested the deleted
    ``_PRICE_TABLE`` estimator; its replacement coverage is ``test_cost.py``
    (resolver-backed ``compute_turn_cost``). This class pins the FIELD:
    honest-unknown default + legacy-vocabulary rows still validate.
    """

    def test_default_is_unpriced(self) -> None:
        # An unset basis must not claim a price existed (D-M2-1).
        assert _log().cost_basis == "unpriced"

    def test_legacy_jsonl_vocabulary_still_validates(self) -> None:
        # Pre-M2 JSONL rows carry "published" / "verify-at-deploy"; the field
        # stays ``str`` so history remains readable.
        for legacy in ("published", "verify-at-deploy"):
            restored = TurnLog.model_validate_json(_log(cost_basis=legacy).model_dump_json())
            assert restored.cost_basis == legacy

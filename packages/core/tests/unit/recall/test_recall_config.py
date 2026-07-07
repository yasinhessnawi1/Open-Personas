"""RecallSettings — env-tunable defaults + validation (Spec K9, T1)."""

from __future__ import annotations

import pytest
from persona.recall.config import RecallSettings
from pydantic import ValidationError


def test_defaults_are_the_phase3_starting_points() -> None:
    s = RecallSettings()
    assert s.rrf_k == 60
    assert s.pool_size == 20
    assert s.result_budget == 10
    # The measured chat/voice rerank split (K9-D-3): top-20 chat, top-12 voice.
    assert s.rerank_top_k_chat == 20
    assert s.rerank_top_k_voice == 12


def test_traversal_weight_defaults_below_the_dense_weight() -> None:
    s = RecallSettings()
    assert s.weight_graph_traversal < s.weight_graph_dense  # augment, never displace


def test_env_overrides_are_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSONA_RECALL_RRF_K", "30")
    monkeypatch.setenv("PERSONA_RECALL_RERANK_TOP_K_VOICE", "8")
    s = RecallSettings()
    assert s.rrf_k == 30
    assert s.rerank_top_k_voice == 8


def test_rrf_k_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        RecallSettings(rrf_k=0)


def test_a_weight_may_be_zero_but_not_negative() -> None:
    assert RecallSettings(weight_graph_traversal=0.0).weight_graph_traversal == 0.0
    with pytest.raises(ValidationError):
        RecallSettings(weight_graph_dense=-1.0)

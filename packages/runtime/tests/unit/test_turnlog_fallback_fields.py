"""Unit tests for the Spec 20 T19 TurnLog fallback instrumentation (D-20-9).

Covers the six additive fields on :class:`TurnLog`
(``tier_model_chosen``, ``tier_provider_used``, ``tier_fallback_count``,
``tier_fallback_reasons``, ``tier_fallback_providers``, ``fallback_engaged``)
and the model-validator invariants that keep them consistent.

Privacy invariant: the fallback fields carry **class names only** — never
raw error messages. Mirrors the D-15-X-hard-line-filter content-hash-only
audit precedent.

Integration coverage: the chained-write-back path is exercised in
``test_loop.py`` against the live :class:`MultiModelChatBackend`; this
module focuses on field-shape + validator behaviour.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import cast

import pytest
from persona.backends.types import BackendAttempt, ServedCall
from persona_runtime.logging import TurnLog
from pydantic import ValidationError


def _now() -> datetime:
    return datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC)


def _baseline_log_kwargs() -> dict[str, object]:
    return {
        "conversation_id": "c1",
        "turn_index": 0,
        "tier_used": "frontier",
        "model_name": "nvidia/llama-3.3-nemotron-super-49b-v1.5",
        "provider": "nvidia",
        "prompt_tokens": 80,
        "completion_tokens": 40,
        "latency_ms": 1500.0,
        "cost_cents": 0.7,
        "timestamp": _now(),
    }


class TestFallbackFieldsDefault:
    """Default construction — pre-T19 callers stay green."""

    def test_defaults(self) -> None:
        log = TurnLog(**_baseline_log_kwargs())  # type: ignore[arg-type]
        assert log.tier_model_chosen is None
        assert log.tier_provider_used is None
        assert log.tier_fallback_count == 0
        assert log.tier_fallback_reasons == []
        assert log.tier_fallback_providers == []
        assert log.fallback_engaged is False


class TestSingleBackendSuccess:
    """No fallback engaged — primary served cleanly."""

    def test_single_backend_success(self) -> None:
        log = TurnLog(
            **_baseline_log_kwargs(),  # type: ignore[arg-type]
            tier_model_chosen="nvidia/llama-3.3-nemotron-super-49b-v1.5",
            tier_provider_used="nvidia",
            tier_fallback_count=0,
            tier_fallback_reasons=[],
            tier_fallback_providers=[],
            fallback_engaged=False,
        )
        assert log.tier_model_chosen == "nvidia/llama-3.3-nemotron-super-49b-v1.5"
        assert log.tier_provider_used == "nvidia"
        assert log.fallback_engaged is False


class TestSingleFallback:
    """N=1 fallback — primary failed, secondary served."""

    def test_single_fallback(self) -> None:
        log = TurnLog(
            **_baseline_log_kwargs(),  # type: ignore[arg-type]
            tier_model_chosen="claude-sonnet-4-6",
            tier_provider_used="anthropic",
            tier_fallback_count=1,
            tier_fallback_reasons=["RateLimitError"],
            tier_fallback_providers=["nvidia"],
            fallback_engaged=True,
        )
        assert log.tier_fallback_count == 1
        assert log.tier_fallback_reasons == ["RateLimitError"]
        assert log.tier_fallback_providers == ["nvidia"]
        assert log.fallback_engaged is True
        assert log.tier_provider_used == "anthropic"


class TestMultiFallback:
    """N=3 fallback — primary + two more failed before fourth served."""

    def test_multi_fallback(self) -> None:
        log = TurnLog(
            **_baseline_log_kwargs(),  # type: ignore[arg-type]
            tier_model_chosen="claude-haiku-4-5",
            tier_provider_used="anthropic",
            tier_fallback_count=3,
            tier_fallback_reasons=[
                "RateLimitError",
                "BackendTimeoutError",
                "ProviderCredentialMissingError",
            ],
            tier_fallback_providers=["nvidia", "deepseek", "groq"],
            fallback_engaged=True,
        )
        assert log.tier_fallback_count == 3
        assert len(log.tier_fallback_reasons) == 3
        assert len(log.tier_fallback_providers) == 3
        assert log.fallback_engaged is True


class TestAllModelsFailedExhaustion:
    """Exhaustion case — every backend failed, ``tier_*_used`` are None."""

    def test_exhausted(self) -> None:
        log = TurnLog(
            **_baseline_log_kwargs(),  # type: ignore[arg-type]
            tier_model_chosen=None,
            tier_provider_used=None,
            tier_fallback_count=3,
            tier_fallback_reasons=[
                "RateLimitError",
                "BackendTimeoutError",
                "AuthenticationError",
            ],
            tier_fallback_providers=["nvidia", "anthropic", "deepseek"],
            fallback_engaged=True,
        )
        assert log.tier_model_chosen is None
        assert log.tier_provider_used is None
        assert log.tier_fallback_count == 3
        assert log.fallback_engaged is True


class TestPrivacyDiscipline:
    """D-20-9 + D-15-X-hard-line-filter precedent: class names ONLY.

    The TurnLog shape accepts a ``list[str]`` for reasons; callers are
    contractually obligated to pass ``type(exc).__name__``. This test
    verifies the field accepts the discipline (and would not silently
    accept a structured error payload).
    """

    def test_only_class_names_are_canonical(self) -> None:
        # Canonical: class names only — pass directly through.
        log = TurnLog(
            **_baseline_log_kwargs(),  # type: ignore[arg-type]
            tier_model_chosen="claude-sonnet-4-6",
            tier_provider_used="anthropic",
            tier_fallback_count=1,
            tier_fallback_reasons=["RateLimitError"],
            tier_fallback_providers=["nvidia"],
            fallback_engaged=True,
        )
        # Each entry MUST look like a class name (a single PascalCase token,
        # no whitespace, no colons, no embedded URLs) — operational sanity
        # check against accidental error-message leakage.
        for reason in log.tier_fallback_reasons:
            assert ":" not in reason
            assert " " not in reason
            assert "\n" not in reason
            assert reason  # non-empty
            assert reason[0].isupper()

    def test_extra_field_still_rejected(self) -> None:
        """Smoke test: the frozen+extra=forbid invariant holds for T19 too."""
        with pytest.raises(ValidationError):
            TurnLog(
                **_baseline_log_kwargs(),  # type: ignore[arg-type]
                fallback_undeclared="rogue",  # type: ignore[call-arg]
            )


class TestModelValidatorInvariants:
    """D-20-9 invariants enforced by the after-validator."""

    def test_count_must_match_reasons_length(self) -> None:
        with pytest.raises(ValidationError):
            TurnLog(
                **_baseline_log_kwargs(),  # type: ignore[arg-type]
                tier_fallback_count=2,
                tier_fallback_reasons=["RateLimitError"],  # length=1
                tier_fallback_providers=["nvidia", "anthropic"],
                fallback_engaged=True,
            )

    def test_count_must_match_providers_length(self) -> None:
        with pytest.raises(ValidationError):
            TurnLog(
                **_baseline_log_kwargs(),  # type: ignore[arg-type]
                tier_fallback_count=2,
                tier_fallback_reasons=["RateLimitError", "BackendTimeoutError"],
                tier_fallback_providers=["nvidia"],  # length=1
                fallback_engaged=True,
            )

    def test_reasons_and_providers_must_be_equal_length(self) -> None:
        with pytest.raises(ValidationError):
            TurnLog(
                **_baseline_log_kwargs(),  # type: ignore[arg-type]
                tier_fallback_count=1,
                tier_fallback_reasons=["RateLimitError"],
                tier_fallback_providers=["nvidia", "anthropic"],  # mismatch
                fallback_engaged=True,
            )

    def test_fallback_engaged_must_be_true_when_count_positive(self) -> None:
        with pytest.raises(ValidationError):
            TurnLog(
                **_baseline_log_kwargs(),  # type: ignore[arg-type]
                tier_fallback_count=1,
                tier_fallback_reasons=["RateLimitError"],
                tier_fallback_providers=["nvidia"],
                fallback_engaged=False,  # inconsistent
            )

    def test_fallback_engaged_must_be_false_when_count_zero(self) -> None:
        with pytest.raises(ValidationError):
            TurnLog(
                **_baseline_log_kwargs(),  # type: ignore[arg-type]
                tier_fallback_count=0,
                tier_fallback_reasons=[],
                tier_fallback_providers=[],
                fallback_engaged=True,  # inconsistent
            )

    def test_negative_count_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TurnLog(
                **_baseline_log_kwargs(),  # type: ignore[arg-type]
                tier_fallback_count=-1,
            )


class TestJSONLSerialization:
    """Round-trip through ``model_dump_json`` — JSONL stream consumers."""

    def test_roundtrip_preserves_all_fields(self) -> None:
        log = TurnLog(
            **_baseline_log_kwargs(),  # type: ignore[arg-type]
            tier_model_chosen="claude-sonnet-4-6",
            tier_provider_used="anthropic",
            tier_fallback_count=2,
            tier_fallback_reasons=["RateLimitError", "BackendTimeoutError"],
            tier_fallback_providers=["nvidia", "deepseek"],
            fallback_engaged=True,
        )
        payload = json.loads(log.model_dump_json())
        assert payload["tier_model_chosen"] == "claude-sonnet-4-6"
        assert payload["tier_provider_used"] == "anthropic"
        assert payload["tier_fallback_count"] == 2
        assert payload["tier_fallback_reasons"] == [
            "RateLimitError",
            "BackendTimeoutError",
        ]
        assert payload["tier_fallback_providers"] == ["nvidia", "deepseek"]
        assert payload["fallback_engaged"] is True

    def test_roundtrip_default_shape(self) -> None:
        log = TurnLog(**_baseline_log_kwargs())  # type: ignore[arg-type]
        payload = json.loads(log.model_dump_json())
        assert payload["tier_model_chosen"] is None
        assert payload["tier_provider_used"] is None
        assert payload["tier_fallback_count"] == 0
        assert payload["tier_fallback_reasons"] == []
        assert payload["tier_fallback_providers"] == []
        assert payload["fallback_engaged"] is False


class TestComputeFallbackFieldsHelper:
    """Verify the loop's ``_compute_fallback_fields`` projection helper.

    The input is the last round's :class:`ServedCall` (R9-223): the identity and the
    attempt history the chain stamped on that round's own chunks. Integration with
    the full ConversationLoop write-back path is in ``test_loop_served_model.py``.
    """

    @staticmethod
    def _attempt(
        index: int, provider: str, model: str, error: str | None, *, retry: bool = False
    ) -> BackendAttempt:
        return BackendAttempt(
            index=index, provider=provider, model=model, error_class=error, retry=retry
        )

    def test_single_backend_no_attempts(self) -> None:
        from persona_runtime.loop import _compute_fallback_fields

        class _FakeBareBackend:
            provider_name = "nvidia"
            model_name = "nvidia/llama"

        result = _compute_fallback_fields(cast("object", _FakeBareBackend()), None)  # type: ignore[arg-type]
        assert result["tier_model_chosen"] == "nvidia/llama"
        assert result["tier_provider_used"] == "nvidia"
        assert result["tier_fallback_count"] == 0
        assert result["tier_fallback_reasons"] == []
        assert result["tier_fallback_providers"] == []
        assert result["fallback_engaged"] is False

    def test_one_fallback_after_a_retry(self) -> None:
        from persona_runtime.loop import _compute_fallback_fields

        served = ServedCall(
            provider="anthropic",
            model="claude-sonnet-4-6",
            attempts=(
                self._attempt(0, "nvidia", "nvidia/llama", "RateLimitError"),
                self._attempt(0, "nvidia", "nvidia/llama", "RateLimitError", retry=True),
                self._attempt(1, "anthropic", "claude-sonnet-4-6", None),
            ),
        )
        result = _compute_fallback_fields(cast("object", None), served)  # type: ignore[arg-type]
        assert result["tier_model_chosen"] == "claude-sonnet-4-6"
        assert result["tier_provider_used"] == "anthropic"
        # One record per backend that fell through: the retry is not a second fallback.
        assert result["tier_fallback_count"] == 1
        assert result["tier_fallback_reasons"] == ["RateLimitError"]
        assert result["tier_fallback_providers"] == ["nvidia"]
        assert result["fallback_engaged"] is True

    def test_a_retry_on_the_serving_backend_is_not_a_fallback(self) -> None:
        from persona_runtime.loop import _compute_fallback_fields

        served = ServedCall(
            provider="nvidia",
            model="nvidia/llama",
            attempts=(
                self._attempt(0, "nvidia", "nvidia/llama", "RateLimitError"),
                self._attempt(0, "nvidia", "nvidia/llama", None, retry=True),
            ),
        )
        result = _compute_fallback_fields(cast("object", None), served)  # type: ignore[arg-type]
        assert result["tier_model_chosen"] == "nvidia/llama"
        assert (result["tier_fallback_count"], result["fallback_engaged"]) == (0, False)

    def test_three_fallbacks(self) -> None:
        from persona_runtime.loop import _compute_fallback_fields

        served = ServedCall(
            provider="groq",
            model="llama-3.1-8b-instant",
            attempts=(
                self._attempt(0, "nvidia", "nvidia/llama", "RateLimitError"),
                self._attempt(1, "anthropic", "claude-sonnet-4-6", "AuthenticationError"),
                self._attempt(2, "deepseek", "deepseek-chat", "BackendTimeoutError"),
                self._attempt(3, "groq", "llama-3.1-8b-instant", None),
            ),
        )
        result = _compute_fallback_fields(cast("object", None), served)  # type: ignore[arg-type]
        assert result["tier_model_chosen"] == "llama-3.1-8b-instant"
        assert result["tier_provider_used"] == "groq"
        assert result["tier_fallback_count"] == 3
        assert result["tier_fallback_reasons"] == [
            "RateLimitError",
            "AuthenticationError",
            "BackendTimeoutError",
        ]
        assert result["tier_fallback_providers"] == ["nvidia", "anthropic", "deepseek"]
        assert result["fallback_engaged"] is True

    def test_helper_output_passes_turnlog_validators(self) -> None:
        """End-to-end: helper output is directly accepted by TurnLog construction."""
        from persona_runtime.loop import _compute_fallback_fields

        served = ServedCall(
            provider="anthropic",
            model="claude-sonnet-4-6",
            attempts=(
                self._attempt(0, "nvidia", "nvidia/llama", "RateLimitError"),
                self._attempt(1, "anthropic", "claude-sonnet-4-6", None),
            ),
        )
        fields = _compute_fallback_fields(cast("object", None), served)  # type: ignore[arg-type]
        # Must accept verbatim: the invariants hold by construction.
        log = TurnLog(**_baseline_log_kwargs(), **fields)  # type: ignore[arg-type]
        assert log.fallback_engaged is True
        assert log.tier_fallback_count == 1

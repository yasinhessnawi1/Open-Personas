"""Composition-root proof for P9-D-2 — recognition off small (T3; criterion 2).

The A4/A8 interpreters (standing-intent / amendment / steering / reschedule)
are composed on the RECOGNITION tier — mid by default, env-overridable via
``PERSONA_API_RECOGNITION_TIER`` — never small. Proven at the composition
root: the backend each interpreter actually HOLDS is identity-checked against
the tier the registry served, and the registry records which tier was asked
for (so "never small" is a recorded fact, not an inference). Fail-soft
composition (keyless / unconfigured tier ⇒ all ``None`` ⇒ the loop's A4/A8
gates stay inert) is unchanged.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from persona.backends.errors import ProviderError
from persona_api.services.runtime_factory import RuntimeFactory
from persona_runtime.tier import TierNotConfiguredError


class _SentinelBackend:
    """One distinct instance per tier — identity is the assertion."""

    def __init__(self, tier: str) -> None:
        self.tier = tier


class _RecordingRegistry:
    """Serves per-tier sentinels and records every tier asked for."""

    def __init__(self, *tiers: str) -> None:
        self.backends = {t: _SentinelBackend(t) for t in tiers}
        self.requested: list[str] = []

    def get(self, name: str) -> _SentinelBackend:
        self.requested.append(name)
        try:
            return self.backends[name]
        except KeyError:
            raise TierNotConfiguredError(
                f"tier {name!r} not configured", context={"tier": name}
            ) from None


class _KeylessRegistry:
    """Every tier lookup fails like a keyless environment (ProviderError)."""

    def __init__(self) -> None:
        self.requested: list[str] = []

    def get(self, name: str) -> object:
        self.requested.append(name)
        raise ProviderError("no API key configured", context={"tier": name})


class _FakeEmbedder:
    model_name = "fake"
    dimension = 384


def _factory(registry: object, *, api_config: object | None = None) -> RuntimeFactory:
    return RuntimeFactory(
        rls_engine=object(),  # type: ignore[arg-type] — never touched at composition time
        embedder=_FakeEmbedder(),  # type: ignore[arg-type]
        tier_registry=registry,  # type: ignore[arg-type]
        turn_log_writer=object(),  # type: ignore[arg-type]
        audit_root=Path("/tmp/persona-audit-test"),
        api_config=api_config,  # type: ignore[arg-type]
    )


class TestRecognitionTierComposition:
    def test_all_four_interpreters_hold_the_mid_backend_by_default(self) -> None:
        registry = _RecordingRegistry("frontier", "mid", "small")
        recognizer, amendment, steering, reschedule = _factory(registry)._build_task_origination()

        mid = registry.backends["mid"]
        assert recognizer is not None
        assert amendment is not None
        assert steering is not None
        assert reschedule is not None
        assert recognizer._judge._backend is mid  # noqa: SLF001
        assert amendment._backend is mid  # noqa: SLF001
        assert steering._backend is mid  # noqa: SLF001
        assert reschedule._backend is mid  # noqa: SLF001

    def test_small_is_never_requested(self) -> None:
        # The recorded fact behind criterion 2: the composition root no longer
        # asks the registry for the small tier at all.
        registry = _RecordingRegistry("frontier", "mid", "small")
        _factory(registry)._build_task_origination()
        assert registry.requested == ["mid"]

    def test_env_override_composes_the_frontier_backend(self) -> None:
        registry = _RecordingRegistry("frontier", "mid", "small")
        api_config = SimpleNamespace(recognition_tier="frontier")
        recognizer, *_ = _factory(registry, api_config=api_config)._build_task_origination()

        assert registry.requested == ["frontier"]
        assert recognizer is not None
        assert recognizer._judge._backend is registry.backends["frontier"]  # noqa: SLF001


class TestFailSoftPreserved:
    def test_unconfigured_recognition_tier_returns_all_none(self) -> None:
        # Registry without a mid tier (e.g. minimal single-tier deploy) —
        # the loop's A4/A8 gates stay inert, never a construction failure.
        registry = _RecordingRegistry("small")
        assert _factory(registry)._build_task_origination() == (None, None, None, None)

    def test_keyless_environment_returns_all_none(self) -> None:
        registry = _KeylessRegistry()
        assert _factory(registry)._build_task_origination() == (None, None, None, None)
        assert registry.requested == ["mid"]

"""Voice builds its tier registries with the OpenRouter subscription mode (R9-224).

Voice composes its own registries (D-V5-6) and used to build them with no mode, so a
chain the api filtered to its ``:free`` models ran unfiltered on a call. Driven
through the REAL :meth:`InProcessAgentLauncher.warm` (the voice boot path) and a
REAL launch, asserting on the registries a call actually receives; and through the
runner's own fallback build. The mode is resolved by the shared runtime resolver
from the environment; the override skips the network probe, so no test here touches
the network.

Log assertions use a real loguru sink rendering the message AND every extra field,
because the project's log format prints every keyword argument: a value passed as
any kwarg reaches the log line, not only one interpolated into the message.
"""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, Any

import pytest
from loguru import logger as _loguru_logger
from persona_runtime.openrouter_subscription import resolve_openrouter_subscription_mode
from persona_voice.agent import openrouter_mode, runner
from persona_voice.agent.launcher import InProcessAgentLauncher

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from persona.backends.openrouter_catalog import OpenRouterSubscriptionMode
    from persona_runtime.tier import TierRegistry

pytestmark = pytest.mark.asyncio

_MODE_ENV = "PERSONA_OPENROUTER_SUBSCRIPTION_MODE"
#: One paid OpenRouter model and two ``:free`` ones, so the filtered chain is still a
#: multi-model chain whose candidates the registry can enumerate.
_CHAIN = "openrouter/acme/paid-model,openrouter/acme/first:free,openrouter/acme/second:free"
_ALL = (
    ("openrouter", "acme/paid-model"),
    ("openrouter", "acme/first:free"),
    ("openrouter", "acme/second:free"),
)
_FREE_ONLY = (("openrouter", "acme/first:free"), ("openrouter", "acme/second:free"))


class _CloudConfig:
    """The launcher reads only ``is_cloud``; cloud builds the free registry too."""

    is_cloud = True


class _BuildStoppedError(Exception):
    """Raised by a stand-in for the step after the registry build, to end the build there."""


@pytest.fixture
def chain_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """A clean model environment: the mid chain, its free twin, and an OpenRouter key."""
    for tier in ("FRONTIER", "MID", "SMALL"):
        for suffix in ("MODELS", "PROVIDER", "MODEL", "API_KEY", "BASE_URL"):
            monkeypatch.delenv(f"PERSONA_{tier}_{suffix}", raising=False)
        monkeypatch.delenv(f"PERSONA_FREE_{tier}_MODELS", raising=False)
    monkeypatch.delenv(_MODE_ENV, raising=False)
    monkeypatch.setenv("PERSONA_OPENROUTER_API_KEY", "sk-or-v1-test")
    monkeypatch.setenv("PERSONA_MID_MODELS", _CHAIN)
    monkeypatch.setenv("PERSONA_FREE_MID_MODELS", _CHAIN)
    # The crisis encoder is not under test; off, warm() starts no model load.
    monkeypatch.setenv("PERSONA_SAFETY_ENCODER_ENABLED", "false")
    return monkeypatch


@pytest.fixture
def voice_mode_errors() -> Iterator[list[str]]:
    """Every ERROR line the voice mode module logs, rendered with all its extra fields."""
    captured: list[str] = []
    sink_id = _loguru_logger.add(
        lambda message: captured.append(str(message)),
        level="ERROR",
        format="{message} | {extra}",
        filter=lambda record: record["extra"].get("component") == "agent.openrouter_mode",
    )
    yield captured
    _loguru_logger.remove(sink_id)


def _recording_resolver(
    resolved_on: list[int], result: OpenRouterSubscriptionMode | None
) -> Callable[[], OpenRouterSubscriptionMode | None]:
    def _resolve() -> OpenRouterSubscriptionMode | None:
        resolved_on.append(threading.get_ident())
        return result

    return _resolve


async def _registries_a_call_receives(
    launcher: InProcessAgentLauncher,
) -> tuple[TierRegistry, TierRegistry]:
    """Boot the launcher as the voice app does, launch one call, return its registries."""
    received: dict[str, Any] = {}

    async def _runner(**kwargs: object) -> None:
        received.update(kwargs)

    launcher._runner = _runner  # noqa: SLF001 - the call itself is not under test
    # The bge model is not under test; a stand-in makes the warm-up encode fail, which
    # the warm-up swallows, so no model is loaded.
    launcher._embedder = object()  # type: ignore[assignment]  # noqa: SLF001
    await launcher.warm()
    launcher.launch(session_id="s1", user_id="u1", persona_id="p1", conversation_id="c1")
    await asyncio.gather(*launcher._tasks)  # noqa: SLF001
    return received["tier_registry"], received["free_tier_registry"]


async def _boot(launcher: InProcessAgentLauncher) -> tuple[TierRegistry, TierRegistry]:
    try:
        return await _registries_a_call_receives(launcher)
    finally:
        await launcher.aclose()


async def test_a_free_mode_filters_both_registries_a_call_receives(
    chain_env: pytest.MonkeyPatch,
) -> None:
    chain_env.setenv(_MODE_ENV, "free")

    paid, free = await _boot(InProcessAgentLauncher(_CloudConfig()))  # type: ignore[arg-type]

    # Exactly what the api builds from the same settings: the shared resolver, the
    # same builders, the same mode.
    assert resolve_openrouter_subscription_mode() == "free"
    assert paid.candidate_models_for("mid") == _FREE_ONLY
    assert free.candidate_models_for("mid") == _FREE_ONLY


async def test_an_invalid_override_logs_the_setting_not_its_value_and_keeps_every_model(
    chain_env: pytest.MonkeyPatch, voice_mode_errors: list[str]
) -> None:
    # The api refuses to boot on this. Voice ignored the setting until R9-224, so a
    # latent bad value must not become an outage: it logs and keeps today's behaviour.
    chain_env.setenv(_MODE_ENV, "premium-typo")

    paid, free = await _boot(InProcessAgentLauncher(_CloudConfig()))  # type: ignore[arg-type]

    assert paid.candidate_models_for("mid") == _ALL
    assert free.candidate_models_for("mid") == _ALL
    assert len(voice_mode_errors) == 1
    assert _MODE_ENV in voice_mode_errors[0]
    assert "InvalidSubscriptionModeError" in voice_mode_errors[0]
    assert "premium" not in voice_mode_errors[0]


async def test_an_unexpected_resolution_failure_never_stops_voice(
    chain_env: pytest.MonkeyPatch, voice_mode_errors: list[str]
) -> None:
    # Voice has never probed OpenRouter at boot before R9-224. Whatever a first probe
    # does (a decode error, a bug), voice boots unfiltered and says so, naming only
    # the setting and the exception class, never the exception's message.
    def _broken() -> OpenRouterSubscriptionMode | None:
        msg = "upstream said sk-or-v1-leaked-detail"
        raise RuntimeError(msg)

    chain_env.setattr(openrouter_mode, "resolve_openrouter_subscription_mode", _broken)

    paid, free = await _boot(InProcessAgentLauncher(_CloudConfig()))  # type: ignore[arg-type]

    assert paid.candidate_models_for("mid") == _ALL
    assert free.candidate_models_for("mid") == _ALL
    assert len(voice_mode_errors) == 1
    assert _MODE_ENV in voice_mode_errors[0]
    assert "RuntimeError" in voice_mode_errors[0]
    assert "leaked-detail" not in voice_mode_errors[0]


async def test_the_mode_is_resolved_once_and_off_the_event_loop(
    chain_env: pytest.MonkeyPatch,
) -> None:
    # The resolver may probe OpenRouter for up to 30 s. It must never run on the loop
    # that carries a call's audio, and one boot needs it once for both registries.
    resolved_on: list[int] = []
    chain_env.setattr(
        openrouter_mode,
        "resolve_openrouter_subscription_mode",
        _recording_resolver(resolved_on, "free"),
    )

    paid, free = await _boot(InProcessAgentLauncher(_CloudConfig()))  # type: ignore[arg-type]

    assert len(resolved_on) == 1
    assert resolved_on[0] != threading.get_ident()
    assert paid.candidate_models_for("mid") == _FREE_ONLY
    assert free.candidate_models_for("mid") == _FREE_ONLY


async def test_the_runners_own_build_uses_the_mode_resolved_off_the_loop(
    chain_env: pytest.MonkeyPatch,
) -> None:
    # A direct caller of build_agent_session that injects no registry reaches the
    # runner's fallback build. It builds only the paid registry (the free one is only
    # ever injected), so that is the one build to check.
    resolved_on: list[int] = []
    chain_env.setattr(
        openrouter_mode,
        "resolve_openrouter_subscription_mode",
        _recording_resolver(resolved_on, "free"),
    )
    real_builder = runner.tier_registry_from_env
    builds: list[tuple[dict[str, Any], TierRegistry]] = []

    def _spy_builder(**kwargs: Any) -> TierRegistry:  # noqa: ANN401 - forwards verbatim
        registry = real_builder(**kwargs)
        builds.append((kwargs, registry))
        return registry

    def _stop_after_the_registry(_embedder: object) -> asyncio.Task[None]:
        raise _BuildStoppedError

    chain_env.setattr(runner, "tier_registry_from_env", _spy_builder)
    chain_env.setattr(runner, "start_embedder_warmup", _stop_after_the_registry)

    with pytest.raises(_BuildStoppedError):
        await runner.build_agent_session(
            session_id="s1",
            user_id="u1",
            persona_id="p1",
            conversation_id="c1",
            config=_CloudConfig(),  # type: ignore[arg-type]
            embedder=object(),  # type: ignore[arg-type]
            core_config=object(),  # type: ignore[arg-type]
            stt_config=object(),  # type: ignore[arg-type]
            tts_config=object(),  # type: ignore[arg-type]
        )

    assert len(resolved_on) == 1
    assert resolved_on[0] != threading.get_ident()
    assert len(builds) == 1
    kwargs, registry = builds[0]
    assert kwargs == {"openrouter_subscription_mode": "free"}
    assert registry.candidate_models_for("mid") == _FREE_ONLY
    await registry.aclose()

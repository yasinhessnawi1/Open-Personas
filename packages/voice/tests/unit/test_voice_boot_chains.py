"""Voice names the model chains it was handed, once, at boot (R9-213).

Voice composes its own router and tier registry (D-V5-6), so it reads its OWN copy of every
model list. On 2026-09-20 the api's chains were updated, chat moved, calls did not, and
nothing said so. This pins the voice half of the pair of boot lines that makes such a
partial update visible: the real lifespan runs, and the line names all six settings.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from loguru import logger as _loguru_logger
from persona_runtime.chain_report import chain_fingerprint
from persona_runtime.tier import CHAIN_ENV_NAMES
from persona_voice.config import VoiceConfig
from persona_voice.http import app as voice_http_app
from pydantic import SecretStr


def test_the_voice_lifespan_logs_the_model_chains_once(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in CHAIN_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PERSONA_OPENROUTER_SUBSCRIPTION_MODE", "paid")
    monkeypatch.setenv("PERSONA_MID_MODELS", "openrouter/z-ai/glm-x,openrouter/anthropic/c-y")
    monkeypatch.setenv("PERSONA_FREE_FRONTIER_MODELS", "  ")
    # The catalogue pre-warm is fail-soft and unrelated; keep it off the network.
    monkeypatch.setattr(voice_http_app, "_prewarm_catalogue", lambda _app: None)
    cfg = VoiceConfig(
        edition="community",
        livekit_url="ws://localhost:7880",
        livekit_api_key=SecretStr("lk_key_test"),
        livekit_api_secret=SecretStr("very-very-long-test-secret-for-hs256-signing"),
    )
    messages: list[str] = []
    sink_id = _loguru_logger.add(lambda m: messages.append(m.record["message"]), level="INFO")
    try:
        with TestClient(voice_http_app.build_app(cfg)):
            pass  # the real lifespan ran
    finally:
        _loguru_logger.remove(sink_id)

    fingerprint = chain_fingerprint(("openrouter/z-ai/glm-x", "openrouter/anthropic/c-y"))
    assert [m for m in messages if m.startswith("model chains at boot")] == [
        "model chains at boot: 1 of 6 set | PERSONA_FRONTIER_MODELS=unset | "
        "PERSONA_MID_MODELS=[openrouter/z-ai/glm-x,openrouter/anthropic/c-y] "
        f"fp={fingerprint} | PERSONA_SMALL_MODELS=unset | "
        "PERSONA_FREE_FRONTIER_MODELS=empty | PERSONA_FREE_MID_MODELS=unset | "
        "PERSONA_FREE_SMALL_MODELS=unset | PERSONA_OPENROUTER_SUBSCRIPTION_MODE=paid"
    ]

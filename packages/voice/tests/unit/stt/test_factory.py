"""Unit tests for :func:`load_streaming_stt` — the V2 backend dispatcher.

Mirrors Spec 02's :func:`load_backend` test discipline. T04 wires the
``deepgram`` branch through to the concrete
:class:`persona_voice.stt.deepgram_backend.DeepgramStreamingSTT` class
(D-V2-1 LOCK launch). Tests pin three contracts:

1. ``deepgram`` with a valid ``PERSONA_STT_API_KEY`` constructs a
   :class:`DeepgramStreamingSTT` instance.
2. ``deepgram`` without an API key fails fast at construction with
   :class:`STTAuthenticationError` (Spec 02 D-02-10 + V2 D-V2-X-cost-discipline).
3. Unknown / un-wired providers raise :class:`STTError` with the
   structured ``context`` dict the Spec 02 contract requires.
"""

from __future__ import annotations

import os

import pytest
from persona_voice.stt import (
    StreamingSTTConfig,
    STTAuthenticationError,
    STTError,
    load_streaming_stt,
)
from persona_voice.stt.deepgram_backend import DeepgramStreamingSTT


@pytest.fixture(autouse=True)
def _strip_persona_stt_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("PERSONA_STT_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("PERSONA_GLADIA_API_KEY", raising=False)


def test_load_streaming_stt_deepgram_branch_returns_concrete_backend() -> None:
    """T04 swap-in: ``deepgram`` provider yields a
    :class:`DeepgramStreamingSTT` instance when a valid API key is set."""
    config = StreamingSTTConfig(provider="deepgram", api_key="dg-test-key")
    backend = load_streaming_stt(config)
    assert isinstance(backend, DeepgramStreamingSTT)
    assert backend.provider_name == "deepgram"
    assert backend.model_name == "nova-3"


def test_load_streaming_stt_deepgram_branch_fails_fast_without_api_key() -> None:
    """Spec 02 D-02-10 fail-fast: missing ``PERSONA_STT_API_KEY``
    surfaces as :class:`STTAuthenticationError` at construction (the
    concrete Deepgram backend validates the secret in ``__init__``)."""
    config = StreamingSTTConfig(provider="deepgram")
    with pytest.raises(STTAuthenticationError) as exc_info:
        load_streaming_stt(config)
    assert exc_info.value.context["provider"] == "deepgram"


def test_load_streaming_stt_unknown_provider_raises_stt_error() -> None:
    """Construct a config with provider set via Pydantic ``model_construct``
    so we can bypass the Literal validation and exercise the factory's
    unknown-provider branch (Pydantic Settings rejects unknown Literals
    at construction, but the factory must defensively cover the case
    when a future Literal value is added but not wired into the
    factory's dispatch table)."""
    config = StreamingSTTConfig.model_construct(provider="cloud-fancy-9000")
    with pytest.raises(STTError) as exc_info:
        load_streaming_stt(config)
    assert "unknown STT provider" in str(exc_info.value)
    assert exc_info.value.context["provider"] == "cloud-fancy-9000"


def test_load_streaming_stt_speechmatics_not_yet_wired() -> None:
    """``speechmatics`` is documented as the alternative-provider story
    behind the same Protocol seam per D-V2-1 paragraph 2, but T04 ships
    only the launch Deepgram backend. Until a future task lands the
    Speechmatics backend, the factory must raise :class:`STTError` for
    the ``speechmatics`` provider — the factory's dispatch table is the
    contract."""
    config = StreamingSTTConfig(provider="speechmatics")
    with pytest.raises(STTError) as exc_info:
        load_streaming_stt(config)
    assert exc_info.value.context["provider"] == "speechmatics"


def test_load_streaming_stt_whisper_streaming_not_yet_wired() -> None:
    """``whisper-streaming`` is the v0.2 self-hosted candidate per
    D-V2-X-cost-discipline — Literal is pinned so config validates,
    but the factory must raise :class:`STTError` until the v0.2 backend
    lands."""
    config = StreamingSTTConfig(provider="whisper-streaming")
    with pytest.raises(STTError) as exc_info:
        load_streaming_stt(config)
    assert exc_info.value.context["provider"] == "whisper-streaming"


def test_load_streaming_stt_unknown_provider_message_lists_alternatives() -> None:
    """Operator-clarity contract: the unknown-provider message must
    enumerate the wired Literal values (incl. the V14 ``gladia`` addition)."""
    config = StreamingSTTConfig.model_construct(provider="nonsense")
    with pytest.raises(STTError) as exc_info:
        load_streaming_stt(config)
    msg = str(exc_info.value)
    assert "deepgram" in msg
    assert "gladia" in msg
    assert "speechmatics" in msg
    assert "whisper-streaming" in msg


# ---------- Gladia branch (Spec V14 D-V14-9) -------------------------------


def test_load_streaming_stt_gladia_branch_returns_concrete_backend() -> None:
    """``gladia`` with a valid ``PERSONA_GLADIA_API_KEY`` yields a
    :class:`GladiaStreamingSTT` instance (the V14 code-switching backend)."""
    from persona_voice.stt.gladia_backend import GladiaStreamingSTT

    config = StreamingSTTConfig(provider="gladia", gladia_api_key="gl-test-key")
    backend = load_streaming_stt(config)
    assert isinstance(backend, GladiaStreamingSTT)
    assert backend.provider_name == "gladia"
    assert backend.model_name == "solaria-1"


def test_load_streaming_stt_gladia_branch_fails_fast_without_api_key() -> None:
    """Spec 02 D-02-10 fail-fast: missing ``PERSONA_GLADIA_API_KEY`` surfaces as
    :class:`STTAuthenticationError` at construction."""
    config = StreamingSTTConfig(provider="gladia")
    with pytest.raises(STTAuthenticationError) as exc_info:
        load_streaming_stt(config)
    assert exc_info.value.context["provider"] == "gladia"

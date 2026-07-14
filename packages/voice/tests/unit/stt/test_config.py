"""Unit tests for :class:`StreamingSTTConfig` — env-driven configuration
mirroring Spec 02's :class:`BackendConfig` shape.

Covers:

* ``env_prefix="PERSONA_STT_"`` env reads (PERSONA_STT_PROVIDER /
  PERSONA_STT_MODEL / PERSONA_STT_API_KEY).
* :class:`SecretStr` ``api_key`` does NOT leak in ``repr(config)``.
* Defaults: ``provider="deepgram"`` + ``model="nova-3"`` +
  ``language_hint=None`` + ``vad_library="silero"``.
* ``Field`` constraints on the Deepgram endpointing / utterance-end
  windows + Silero tuning knobs validate at construction (fail-fast
  per Spec 02 D-02-10).
* ``extra="ignore"`` tolerates extra env vars (Pydantic Settings
  convention — operators may export ``PERSONA_STT_FOO`` without it
  breaking config construction).

Mirrors :mod:`packages.core.tests.unit.imagegen.test_config` discipline:
``monkeypatch`` is the per-test injection point; the autouse
:func:`_strip_persona_stt_env` fixture below clears every ``PERSONA_STT_*``
env var so tests start from defaults regardless of operator-shell state.
"""

from __future__ import annotations

import os

import pytest
from persona_voice.stt.config import StreamingSTTConfig
from pydantic import SecretStr, ValidationError


@pytest.fixture(autouse=True)
def _strip_persona_stt_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every ``PERSONA_STT_*`` env var (+ the un-prefixed spec-named
    ``PERSONA_GLADIA_API_KEY``, Spec V14) so tests start from defaults."""
    for key in list(os.environ):
        if key.startswith("PERSONA_STT_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("PERSONA_GLADIA_API_KEY", raising=False)


# ---------- env_prefix="PERSONA_STT_" reads ------------------------------


def test_env_prefix_reads_provider_and_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSONA_STT_PROVIDER", "speechmatics")
    monkeypatch.setenv("PERSONA_STT_MODEL", "ursa-2")
    config = StreamingSTTConfig()
    assert config.provider == "speechmatics"
    assert config.model == "ursa-2"


def test_env_prefix_reads_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSONA_STT_API_KEY", "sk-test-123")
    config = StreamingSTTConfig()
    assert config.api_key is not None
    assert config.api_key.get_secret_value() == "sk-test-123"


def test_env_prefix_reads_language_hint_and_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PERSONA_STT_LANGUAGE_HINT", "no")
    monkeypatch.setenv("PERSONA_STT_BASE_URL", "https://proxy.example/v1/")
    config = StreamingSTTConfig()
    assert config.language_hint == "no"
    assert config.base_url == "https://proxy.example/v1/"


# ---------- SecretStr api_key never leaks in repr --------------------------


def test_api_key_does_not_leak_in_repr() -> None:
    config = StreamingSTTConfig(api_key=SecretStr("sk-very-secret-xyz"))
    rendered = repr(config)
    assert "sk-very-secret-xyz" not in rendered


# ---------- defaults -------------------------------------------------------


def test_default_provider_is_deepgram() -> None:
    config = StreamingSTTConfig()
    assert config.provider == "deepgram"


def test_default_model_is_nova_3() -> None:
    config = StreamingSTTConfig()
    assert config.model == "nova-3"


def test_default_language_hint_is_none() -> None:
    config = StreamingSTTConfig()
    assert config.language_hint is None


def test_default_vad_library_is_silero() -> None:
    config = StreamingSTTConfig()
    assert config.vad_library == "silero"


def test_default_request_timeout_is_60s() -> None:
    config = StreamingSTTConfig()
    assert config.request_timeout_s == 60.0


def test_default_deepgram_endpointing_and_utterance_end() -> None:
    config = StreamingSTTConfig()
    assert config.deepgram_endpointing_ms == 300
    assert config.deepgram_utterance_end_ms == 1000


def test_default_silero_tuning_knobs() -> None:
    config = StreamingSTTConfig()
    assert config.silero_min_speech_duration_ms == 50
    assert config.silero_min_silence_duration_ms == 200
    assert config.silero_activation_threshold == 0.5


# ---------- Field constraints (fail-fast at construction) ------------------


def test_request_timeout_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        StreamingSTTConfig(request_timeout_s=0.0)
    with pytest.raises(ValidationError):
        StreamingSTTConfig(request_timeout_s=-5.0)


@pytest.mark.parametrize("value", [9, -10, 2001, 9999])
def test_deepgram_endpointing_ms_out_of_range(value: int) -> None:
    with pytest.raises(ValidationError):
        StreamingSTTConfig(deepgram_endpointing_ms=value)


@pytest.mark.parametrize("value", [99, 5001, -1])
def test_deepgram_utterance_end_ms_out_of_range(value: int) -> None:
    with pytest.raises(ValidationError):
        StreamingSTTConfig(deepgram_utterance_end_ms=value)


@pytest.mark.parametrize("value", [9, 501])
def test_silero_min_speech_duration_out_of_range(value: int) -> None:
    with pytest.raises(ValidationError):
        StreamingSTTConfig(silero_min_speech_duration_ms=value)


@pytest.mark.parametrize("value", [49, 2001])
def test_silero_min_silence_duration_out_of_range(value: int) -> None:
    with pytest.raises(ValidationError):
        StreamingSTTConfig(silero_min_silence_duration_ms=value)


@pytest.mark.parametrize("value", [-0.01, 1.01, 2.0])
def test_silero_activation_threshold_out_of_range(value: float) -> None:
    with pytest.raises(ValidationError):
        StreamingSTTConfig(silero_activation_threshold=value)


# ---------- extra="ignore" tolerates unknown env vars ----------------------


def test_extra_env_vars_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pydantic Settings ``extra="ignore"`` convention — extra
    ``PERSONA_STT_*`` env vars must NOT cause construction to fail."""
    monkeypatch.setenv("PERSONA_STT_UNKNOWN_FUTURE_KNOB", "x")
    monkeypatch.setenv("PERSONA_STT_ANOTHER_EXTRA", "y")
    config = StreamingSTTConfig()
    assert config.provider == "deepgram"


# ---------- Provider Literal -----------------------------------------------


def test_provider_literal_rejects_unknown_value() -> None:
    with pytest.raises(ValidationError):
        StreamingSTTConfig(provider="cloud-fancy-stt-9000")  # type: ignore[arg-type]


def test_vad_library_literal_rejects_unknown_value() -> None:
    with pytest.raises(ValidationError):
        StreamingSTTConfig(vad_library="custom-vad")  # type: ignore[arg-type]


def test_echo_mute_while_speaking_defaults_off() -> None:
    """D-V8-X-bargein-during-speech-fix: the TTS-mute-window is opt-in (default
    OFF) so a real barge-in onset reaches the orchestrator while the persona speaks."""
    assert StreamingSTTConfig().silero_echo_mute_while_speaking is False


# ---------- Gladia (Spec V14 D-V14-9) --------------------------------------


def test_provider_literal_accepts_gladia() -> None:
    """``gladia`` is a wired Provider value (V14 code-switching backend)."""
    assert StreamingSTTConfig(provider="gladia").provider == "gladia"


def test_gladia_api_key_reads_unprefixed_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """D-V14-9: ``gladia_api_key`` escapes the ``PERSONA_STT_`` prefix and reads
    the spec-named ``PERSONA_GLADIA_API_KEY`` the owner provisions."""
    monkeypatch.setenv("PERSONA_GLADIA_API_KEY", "gl-live-key")
    config = StreamingSTTConfig()
    assert config.gladia_api_key is not None
    assert config.gladia_api_key.get_secret_value() == "gl-live-key"


def test_gladia_api_key_does_not_leak_in_repr() -> None:
    config = StreamingSTTConfig(gladia_api_key=SecretStr("gl-very-secret"))
    assert "gl-very-secret" not in repr(config)


def test_gladia_api_key_defaults_none() -> None:
    assert StreamingSTTConfig().gladia_api_key is None


def test_gladia_model_default_is_solaria_1() -> None:
    assert StreamingSTTConfig().gladia_model == "solaria-1"


def test_gladia_model_reads_prefixed_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """``gladia_model`` is a provider-scoped knob on the ``PERSONA_STT_`` prefix
    (like the ``deepgram_*`` knobs)."""
    monkeypatch.setenv("PERSONA_STT_GLADIA_MODEL", "solaria-2")
    assert StreamingSTTConfig().gladia_model == "solaria-2"


def test_gladia_api_key_constructible_by_name() -> None:
    """``populate_by_name`` lets the aliased field be set by its Python name
    (e.g. in tests) even though env reads go through the alias."""
    config = StreamingSTTConfig(gladia_api_key="by-name")
    assert config.gladia_api_key is not None
    assert config.gladia_api_key.get_secret_value() == "by-name"


# ---------- Spec V14 D-V14-5: PERSONA_STT_LANGUAGE_HINT deprecation ---------


def test_language_hint_env_warns_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setting the deprecated global ``PERSONA_STT_LANGUAGE_HINT`` warns once
    (the module flag flips) — the field itself still works (backward compat)."""
    import persona_voice.stt.config as cfg_mod

    monkeypatch.setattr(cfg_mod, "_LANGUAGE_HINT_DEPRECATION_WARNED", False)
    monkeypatch.setenv("PERSONA_STT_LANGUAGE_HINT", "no")
    config = StreamingSTTConfig()
    assert config.language_hint == "no"  # still honored (not a hard removal)
    assert cfg_mod._LANGUAGE_HINT_DEPRECATION_WARNED is True


def test_no_deprecation_warning_when_hint_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    import persona_voice.stt.config as cfg_mod

    monkeypatch.setattr(cfg_mod, "_LANGUAGE_HINT_DEPRECATION_WARNED", False)
    StreamingSTTConfig()  # the autouse fixture stripped PERSONA_STT_*
    assert cfg_mod._LANGUAGE_HINT_DEPRECATION_WARNED is False


def test_apply_stt_route_language_pin_does_not_trip_deprecation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-call language pin (model_copy setting language_hint) must NOT be
    read as the deprecated env stopgap — only the raw env var trips the warning."""
    import persona_voice.stt.config as cfg_mod

    monkeypatch.setattr(cfg_mod, "_LANGUAGE_HINT_DEPRECATION_WARNED", False)
    base = StreamingSTTConfig()  # no env hint
    base.model_copy(update={"language_hint": "no"})  # what apply_stt_route does
    assert cfg_mod._LANGUAGE_HINT_DEPRECATION_WARNED is False

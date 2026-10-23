"""The voice service refuses to start on Windows, with a message that says why (Spec WIN T5).

D-VOICE-vendor-only: voice stays on Linux and macOS. Starting it on Windows fails at
once with a human message instead of partway through a call.
"""

from __future__ import annotations

import sys

import pytest
from persona_voice.http import app as voice_app
from persona_voice.platform import (
    VOICE_UNSUPPORTED_ON_WINDOWS,
    VoicePlatformUnsupportedError,
    require_voice_platform,
)


def test_the_message_reads_exactly() -> None:
    assert VOICE_UNSUPPORTED_ON_WINDOWS == (
        "Voice is not available on Windows. The voice service runs on Linux and macOS; "
        "on Windows, chat, memory, files and tools work without it. To use voice, run the "
        "voice service on a Linux or macOS machine and set PERSONA_VOICE_SERVICE_URL to it."
    )


def test_windows_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(VoicePlatformUnsupportedError) as caught:
        require_voice_platform()
    assert caught.value.context["reason"] == "unsupported_platform"
    assert str(caught.value).startswith(VOICE_UNSUPPORTED_ON_WINDOWS)


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_linux_and_macos_start(monkeypatch: pytest.MonkeyPatch, platform: str) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    require_voice_platform()


def test_the_app_factory_refuses_before_building_anything(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[object] = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(voice_app, "build_app", built.append)
    with pytest.raises(VoicePlatformUnsupportedError):
        voice_app.create_app()
    assert built == []

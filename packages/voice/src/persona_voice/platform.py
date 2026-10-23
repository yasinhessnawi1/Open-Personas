"""Voice runs on Linux and macOS only; on Windows it refuses at start (Spec WIN T5).

The owner ruled voice vendor only (D-VOICE-vendor-only): the voice service and its
LiveKit agent stay POSIX, and Windows is outside voice support. Rather than start
and fail somewhere inside a call, the service refuses to start on Windows with a
message that says so and what still works.
"""

from __future__ import annotations

import sys

from persona.errors import PersonaError

__all__ = [
    "VOICE_UNSUPPORTED_ON_WINDOWS",
    "VoicePlatformUnsupportedError",
    "require_voice_platform",
]

VOICE_UNSUPPORTED_ON_WINDOWS = (
    "Voice is not available on Windows. The voice service runs on Linux and macOS; "
    "on Windows, chat, memory, files and tools work without it. To use voice, run the "
    "voice service on a Linux or macOS machine and set PERSONA_VOICE_SERVICE_URL to it."
)


class VoicePlatformUnsupportedError(PersonaError):
    """The voice service was started on a platform it does not support (Windows)."""


def require_voice_platform() -> None:
    """Refuse to start the voice service on Windows.

    Raises:
        VoicePlatformUnsupportedError: the process runs on Windows.
    """
    if sys.platform == "win32":
        raise VoicePlatformUnsupportedError(
            VOICE_UNSUPPORTED_ON_WINDOWS,
            context={"reason": "unsupported_platform", "platform": sys.platform},
        )

"""Per-user voice-call concurrency cap — re-export shim (Spec R7, R7-D-4).

The advisory-lock primitive was consolidated into ONE shared persona-core helper
(:func:`persona.concurrency.acquire_user_concurrency`) at R7 T7, so persona-voice and
persona-api's imagegen path stop carrying copy-pasted twins (D-15-X-concurrency-cap /
D-V1-X-d15x-precedent-binding; mirrors the credits relocation). This module now
re-exports it as ``acquire_voice_call_concurrency`` (the historical voice name) —
every prior import site keeps working byte-for-byte, and the default ``slots=1``
preserves the shipped one-call-in-flight cap exactly.

:class:`VoiceConcurrencyCappedError` stays here: it is the voice surface's own
fail-loud error (the persona-voice HTTP endpoint maps it to 429 + ``Retry-After``,
mirroring persona-api's ``ConcurrencyCappedError``), distinct from the shared,
error-free acquisition primitive.
"""

from __future__ import annotations

from persona.concurrency import acquire_user_concurrency as acquire_voice_call_concurrency
from persona.errors import PersonaError

__all__ = [
    "VoiceConcurrencyCappedError",
    "acquire_voice_call_concurrency",
]


class VoiceConcurrencyCappedError(PersonaError):
    """Raised when a user already has a voice call in flight.

    The persona-voice HTTP endpoint translates this to a 429 + ``Retry-After``
    header, mirroring persona-api's :class:`ConcurrencyCappedError`
    (D-V1-X-d15x-precedent-binding).
    """

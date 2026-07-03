"""Per-user image-generation concurrency cap — re-export shim (Spec R7, R7-D-4).

The advisory-lock primitive was consolidated into ONE shared persona-core helper
(:func:`persona.concurrency.acquire_user_concurrency`) at R7 T7 so persona-api's
imagegen path and persona-voice stop carrying copy-pasted twins (mirrors the credits
relocation). This module now re-exports it unchanged — every prior import site
(``from persona_api.imagegen.concurrency import acquire_user_concurrency``) keeps
working byte-for-byte, and the default ``slots=1`` preserves the shipped cap-1
behaviour exactly (spec 15 T14, D-15-X-concurrency-cap). The generalized ``slots=N``
form + the durable-count long-op form live in the shared helper's docstring.
"""

from __future__ import annotations

from persona.concurrency import acquire_user_concurrency

__all__ = ["acquire_user_concurrency"]

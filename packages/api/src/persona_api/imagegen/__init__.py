"""``persona_api.imagegen`` — hosted image-generation composition (spec 15).

The core ``persona.imagegen`` Protocol + per-provider backends live in
``packages/core/src/persona/imagegen/``; this package houses the API-side
composition (pre-deduct credits + advisory-lock concurrency cap + bytes
to workspace) — same core/api split as :mod:`persona_api.sandbox`.

Re-exports the public service entry point so callers (route layer in
T16; integration tests in T15/T17/T18) import via
:mod:`persona_api.imagegen` rather than reaching for the submodule.
"""

from __future__ import annotations

from persona_api.imagegen.concurrency import acquire_user_concurrency
from persona_api.imagegen.service import generate

__all__ = [
    "acquire_user_concurrency",
    "generate",
]

"""JWT verification surface shared by persona-api and persona-voice.

Extracted from ``persona_api.auth.deps`` at spec V1 T03
(D-V1-X-jwt-verifier-extraction; additive Spec 08 amendment per the
D-12-X / D-16-X precedent chain). persona-api re-exports for back-compat;
persona-voice imports from here directly so it does not take a persona-api
dependency.

Only the **provider-agnostic** verification surface lives here:

* :class:`AuthenticatedUser` — the structured principal extracted from a verified token.
* :class:`JwtVerifierConfig` — the structural settings shape ``make_jwt_verifier``
  needs (both :class:`persona_api.config.APIConfig` and persona-voice's future
  ``VoiceConfig`` satisfy it).
* :func:`make_jwt_verifier` — the algorithm-confusion-hardened builder for one
  statically pinned key.
* :func:`make_jwks_verifier` (R9-062, additive) — the JWKS-based builder that
  resolves the verification key per-token by ``kid``, for callers that must
  verify tokens from any instance of a provider (or survive key rotation)
  instead of pinning to one static key.

The FastAPI-specific glue (``get_verify_token``, ``get_current_user``,
``_bearer_token``) stays in persona-api — it depends on framework objects
(``Request``, ``Depends``, the RLS contextvar) and has no use outside the API.
"""

from __future__ import annotations

from persona.auth.jwt_verifier import (
    AuthenticatedUser,
    JwtVerifierConfig,
    make_jwks_verifier,
    make_jwt_verifier,
)

__all__ = [
    "AuthenticatedUser",
    "JwtVerifierConfig",
    "make_jwks_verifier",
    "make_jwt_verifier",
]

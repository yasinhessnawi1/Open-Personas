"""Algorithm-confusion-hardened JWT verifier (spec V1 T03 — extracted from
``persona_api.auth.deps``).

The verification is provider-agnostic (Clerk / Supabase / a hand-rolled IdP all
issue JWTs) and lives in persona-core so persona-api and persona-voice both
consume it without persona-voice taking a persona-api dependency
(D-V1-X-jwt-verifier-extraction). The FastAPI-specific glue (``Depends``,
request-scoped contextvar binding) stays in ``persona_api.auth.deps``.

The implementation is verbatim from ``persona_api.auth.deps:82-150`` with one
additive change: ``make_jwt_verifier`` consumes a :class:`JwtVerifierConfig`
:class:`typing.Protocol` (structural subtype) rather than the concrete
``APIConfig``, so persona-voice's future settings class can satisfy it without
needing a shared base. ``APIConfig`` satisfies the Protocol implicitly
(matching field names + types — verified at the call site by mypy ``--strict``).

The algorithm-confusion guard (the spec-08 T05 security-reviewer finding):
the verifier MUST bind the verification key to the token's *own* ``alg`` header
family, never select the key independently. Otherwise an attacker who has the
(public) RSA/EC key can forge an HS256 token by HMAC-signing the signing input
with the public-key bytes as the secret. This module enforces the binding at
two layers: (a) construction-time fail-fast if a key family is configured
without its key (or vice versa), and (b) runtime rejection of any token whose
``alg`` header does not match a configured family.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from jose import JWTError, jwt
from pydantic import BaseModel, ConfigDict, SecretStr

from persona.errors import AuthenticationError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

__all__ = [
    "AuthenticatedUser",
    "JwtVerifierConfig",
    "make_jwt_verifier",
]


class AuthenticatedUser(BaseModel):
    """The authenticated principal extracted from a verified token."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    email: str | None = None
    # Optional name claims (Spec K6, K6-D-1 seed). Present only when the identity
    # provider's session token is configured to emit them (e.g. Clerk custom claims
    # ``given_name``/``family_name``); ``None`` otherwise. Used ONLY to seed our
    # ``users`` name columns once at provisioning when they are still null — our DB
    # stays the source of truth (never read per-request for the prompt). Additive +
    # defaulted, so every existing constructor (``AuthenticatedUser(id=…, email=…)``)
    # is unaffected.
    first_name: str | None = None
    last_name: str | None = None


class JwtVerifierConfig(Protocol):
    """Minimal structural config shape :func:`make_jwt_verifier` requires.

    Both :class:`persona_api.config.APIConfig` (the persona-api settings) and
    persona-voice's ``VoiceConfig`` satisfy this Protocol implicitly — no
    shared concrete base class needed (D-V1-X-jwt-verifier-extraction).
    ``jwt_algorithms_list`` is declared as a property because both concrete
    settings classes compute it from a comma-separated env-var string; a
    plain attribute declaration would reject the ``@property`` implementations.
    """

    jwt_secret: SecretStr | None
    jwt_public_key: SecretStr | None
    jwt_audience: str | None

    @property
    def jwt_algorithms_list(self) -> list[str]: ...


# Algorithm families. The key MUST be bound to the family per the token's own
# `alg` header, NEVER chosen independently of it — otherwise an attacker who has
# the (public) RSA/EC key can forge an HS256 token signed with that key as the
# HMAC secret (the classic JWT algorithm-confusion attack). Security-reviewer
# HIGH finding (spec 08 T05): bind key↔alg, and reject a public key paired with
# an HMAC alg (and vice versa) at construction (fail-fast).
_SYMMETRIC_ALGS = frozenset({"HS256", "HS384", "HS512"})
_ASYMMETRIC_ALGS = frozenset(
    {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256", "PS384", "PS512"}
)


def make_jwt_verifier(
    config: JwtVerifierConfig,
) -> Callable[[str], Awaitable[AuthenticatedUser]]:
    """Build the default ``python-jose`` JWT verifier from config (D-08-4).

    HMAC algorithms verify against the symmetric ``jwt_secret``; RSA/EC
    algorithms verify against the asymmetric ``jwt_public_key``. The key is
    selected **per the verified token's own algorithm family** — never
    independently — so a public key can never be used as an HMAC secret
    (algorithm-confusion attack). A configured algorithm whose key is missing is
    rejected at construction (fail-fast). Fails closed on any
    signature/expiry/audience failure. The ``sub`` claim is the user id.
    """
    secret = config.jwt_secret.get_secret_value() if config.jwt_secret else None
    public_key = config.jwt_public_key.get_secret_value() if config.jwt_public_key else None
    algorithms = config.jwt_algorithms_list
    audience = config.jwt_audience or None

    # Partition configured algorithms by family and pair each with its key.
    sym_algs = [a for a in algorithms if a in _SYMMETRIC_ALGS]
    asym_algs = [a for a in algorithms if a in _ASYMMETRIC_ALGS]
    unknown = [a for a in algorithms if a not in _SYMMETRIC_ALGS and a not in _ASYMMETRIC_ALGS]
    if unknown:
        msg = f"unsupported JWT algorithm(s): {unknown}"
        raise ValueError(msg)
    if sym_algs and not secret:
        msg = f"HMAC algorithm(s) {sym_algs} configured but PERSONA_API_JWT_SECRET is unset"
        raise ValueError(msg)
    if asym_algs and not public_key:
        msg = (
            f"asymmetric algorithm(s) {asym_algs} configured but "
            "PERSONA_API_JWT_PUBLIC_KEY is unset"
        )
        raise ValueError(msg)
    if not sym_algs and not asym_algs:
        msg = "no usable JWT algorithm/key pair configured"
        raise ValueError(msg)

    async def _verify(token: str) -> AuthenticatedUser:
        # Read the token's claimed alg from the (unverified) header, pick the
        # matching family's key, and verify ONLY against that family's algs.
        try:
            header_alg = jwt.get_unverified_header(token).get("alg")
        except JWTError as exc:
            raise AuthenticationError(
                "malformed token header", context={"reason": str(exc)}
            ) from exc
        if header_alg in _SYMMETRIC_ALGS and header_alg in sym_algs:
            key, allowed = secret, sym_algs
        elif header_alg in _ASYMMETRIC_ALGS and header_alg in asym_algs:
            key, allowed = public_key, asym_algs
        else:
            raise AuthenticationError(
                "token algorithm not allowed", context={"alg": str(header_alg)}
            )
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=allowed,
                audience=audience,
                options={"verify_aud": audience is not None},
            )
        except JWTError as exc:
            raise AuthenticationError("invalid token", context={"reason": str(exc)}) from exc
        sub = claims.get("sub")
        if not sub:
            raise AuthenticationError("token missing 'sub' claim")
        # K6 seed (K6-D-1): carry the optional name claims when the provider emits
        # them (Clerk custom claims ``given_name``/``family_name``). Absent ⇒ ``None``
        # ⇒ the seed is a graceful no-op — the token contract is otherwise unchanged.
        return AuthenticatedUser(
            id=str(sub),
            email=claims.get("email"),
            first_name=claims.get("given_name"),
            last_name=claims.get("family_name"),
        )

    return _verify

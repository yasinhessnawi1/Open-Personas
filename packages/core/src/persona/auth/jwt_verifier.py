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

:func:`make_jwks_verifier` (R9-062, additive) is a second, JWKS-based builder
for the same :class:`AuthenticatedUser` contract: instead of one static pinned
key, it resolves the verification key per-token from a JWKS document (matched
by the token's ``kid``), so a connector process can verify tokens issued by
ANY Clerk instance (dev + prod) and survive key rotation without a redeploy.
It does NOT replace or alter :func:`make_jwt_verifier` — that verifier's
behavior, signature, and callers (persona-api, persona-voice) are unchanged.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Protocol

from jose import JWTError, jwt
from pydantic import BaseModel, ConfigDict, SecretStr

from persona.errors import AuthenticationError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

__all__ = [
    "AuthenticatedUser",
    "JwtVerifierConfig",
    "make_jwks_verifier",
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


def _select_jwk(jwks: Mapping[str, Any], kid: str) -> dict[str, Any] | None:
    """Pick the JWK whose ``kid`` matches, from a ``{"keys": [...]}`` JWKS document."""
    keys = jwks.get("keys")
    if not isinstance(keys, list):
        return None
    for candidate in keys:
        if isinstance(candidate, dict) and candidate.get("kid") == kid:
            return candidate
    return None


def make_jwks_verifier(
    *,
    jwks_url: str,
    audience: str | None,
    algorithms: Sequence[str],
    fetch_jwks: Callable[[], Awaitable[dict[str, Any]]],
    cache_ttl_seconds: int = 600,
    min_refetch_interval_seconds: float = 60.0,
) -> Callable[[str], Awaitable[AuthenticatedUser]]:
    """Build a JWKS-based JWT verifier (R9-062) — key-rotation- and multi-instance-safe.

    Unlike :func:`make_jwt_verifier` (one static pinned key), this verifier
    resolves the verification key **per token** from a JWKS document, matched by
    the token's ``kid`` header. This lets one connector process verify tokens
    issued by any Clerk instance (dev + prod) that publishes to ``jwks_url``, and
    survive that instance rotating its signing key without a redeploy.

    Security posture (provider-agnostic — Clerk today, any OIDC-shaped IdP later):

    * ``jwks_url`` is a **pinned** construction-time parameter. It is NEVER
      derived from the token's own ``iss`` claim — that claim is
      attacker-controlled before verification, and resolving the trust anchor
      from unverified input would let an attacker point the verifier at a JWKS
      document of their choosing.
    * JWKS keys are asymmetric (RSA/EC) by construction — a JWKS verifier must
      NEVER accept an HMAC (``HS*``) token, even if ``algorithms`` were
      misconfigured to include one (the same algorithm-confusion guard as
      :func:`make_jwt_verifier`, applied to a fixed asymmetric-only allowlist).
    * Fails closed: any JWKS fetch failure with no usable cached JWKS, any
      unresolvable ``kid``, and any signature/claims failure all raise
      :class:`~persona.errors.AuthenticationError` — never a silently-accepted
      token.
    * **The unknown-``kid`` refetch is rate-limited** (``min_refetch_interval_seconds``).
      The refetch exists so a rotated signing key is picked up without a redeploy,
      but it is reachable by an UNAUTHENTICATED caller: the ``kid`` is read from
      the token header before any signature check, so an attacker minting tokens
      with random ``kid``s would otherwise turn every request into an outbound
      JWKS fetch — request amplification against both this process (blocking
      network I/O per request) and the IdP (rate-limit/ban). The cooldown caps
      forced refetches at one per interval, process-wide; requests arriving
      inside the cooldown are answered from cache and fail closed on a genuinely
      unknown ``kid``. A real rotation is picked up on the next request after the
      interval (or by the ordinary TTL refresh), so the window is bounded and
      self-healing.

    Args:
        jwks_url: The pinned JWKS document URL (e.g. a Clerk instance's
            ``https://<instance>.clerk.accounts.dev/.well-known/jwks.json``).
            Passed through to ``fetch_jwks`` by the caller's own fetcher; kept
            here only for error context and documentation of the pin.
        audience: The expected ``aud`` claim, or ``None`` to skip audience
            verification.
        algorithms: The configured algorithm allowlist. Filtered to asymmetric
            algorithms only (RS*/ES*/PS*); a configured symmetric algorithm is
            a construction-time error (a JWKS verifier has no symmetric key).
        fetch_jwks: An injectable ``async () -> {"keys": [...]}`` fetcher — real
            wiring hits ``jwks_url`` over HTTP; tests inject a fake so
            verification is exercised with no network access.
        cache_ttl_seconds: How long a fetched JWKS is reused before the next
            request triggers a fresh fetch. The refetch-on-unknown-``kid`` path
            (key rotation) is separate from this TTL — it bypasses cache
            freshness, but is itself capped by ``min_refetch_interval_seconds``.
        min_refetch_interval_seconds: Minimum wall-clock gap between two
            unknown-``kid`` forced refetches (default 60s). Bounds the outbound
            fetch rate an unauthenticated caller can drive, since ``kid`` is read
            pre-verification (see the security posture above). Ordinary
            TTL-driven refreshes are unaffected.

    Returns:
        An async ``verify(token) -> AuthenticatedUser`` callable, the same
        contract as :func:`make_jwt_verifier`.

    Raises:
        ValueError: At construction, if ``algorithms`` contains no usable
            asymmetric algorithm (fail-fast, mirroring ``make_jwt_verifier``).
    """
    asym_algs = [a for a in algorithms if a in _ASYMMETRIC_ALGS]
    unknown = [a for a in algorithms if a not in _ASYMMETRIC_ALGS]
    if unknown:
        msg = f"JWKS verifier only supports asymmetric algorithms; got {unknown}"
        raise ValueError(msg)
    if not asym_algs:
        msg = "no usable asymmetric JWT algorithm configured for the JWKS verifier"
        raise ValueError(msg)

    _cached_jwks: dict[str, Any] | None = None
    _cached_at: float = 0.0
    # Monotonic stamp of the last unknown-kid FORCED refetch (the rate-limit anchor).
    # Starts at -inf so the first genuine rotation is picked up immediately.
    _last_forced_at: float = float("-inf")

    async def _get_jwks(*, force: bool) -> dict[str, Any]:
        nonlocal _cached_jwks, _cached_at
        now = time.monotonic()
        if not force and _cached_jwks is not None and (now - _cached_at) < cache_ttl_seconds:
            return _cached_jwks
        try:
            fetched = await fetch_jwks()
        except Exception as exc:
            # Adapter-boundary translation (network/parse/anything the injected
            # fetcher raises) into the domain exception. Fail CLOSED only when
            # there is no usable cache to fall back on — a transient fetch
            # failure with a still-fresh-enough cache must not lock users out.
            if _cached_jwks is not None:
                return _cached_jwks
            raise AuthenticationError(
                "failed to fetch JWKS", context={"jwks_url": jwks_url, "reason": str(exc)}
            ) from exc
        _cached_jwks = fetched
        _cached_at = now
        return fetched

    async def _resolve_key(kid: str) -> dict[str, Any] | None:
        nonlocal _last_forced_at
        jwks = await _get_jwks(force=False)
        key = _select_jwk(jwks, kid)
        if key is not None:
            return key
        # Unknown kid: refetch ONCE (handles key rotation) before failing closed —
        # but rate-limited. `kid` is attacker-supplied (read pre-verification), so an
        # unthrottled forced refetch here is an unauthenticated outbound-fetch
        # amplifier against this process and the IdP. Inside the cooldown we answer
        # from cache, which fails closed for a genuinely unknown kid; a real rotation
        # is picked up on the first request after the window.
        now = time.monotonic()
        if (now - _last_forced_at) < min_refetch_interval_seconds:
            return None
        _last_forced_at = now
        jwks = await _get_jwks(force=True)
        return _select_jwk(jwks, kid)

    async def _verify(token: str) -> AuthenticatedUser:
        try:
            header = jwt.get_unverified_header(token)
        except JWTError as exc:
            raise AuthenticationError(
                "malformed token header", context={"reason": str(exc)}
            ) from exc
        header_alg = header.get("alg")
        if header_alg not in asym_algs:
            # Algorithm-confusion guard: JWKS keys are asymmetric ONLY — never
            # accept an HS* token here, regardless of what the header claims.
            raise AuthenticationError(
                "token algorithm not allowed", context={"alg": str(header_alg)}
            )
        kid = header.get("kid")
        if not kid or not isinstance(kid, str):
            raise AuthenticationError("malformed token header", context={"reason": "missing kid"})
        jwk = await _resolve_key(kid)
        if jwk is None:
            raise AuthenticationError("unknown signing key", context={"kid": kid})
        try:
            claims = jwt.decode(
                token,
                jwk,
                algorithms=asym_algs,
                audience=audience,
                options={"verify_aud": audience is not None},
            )
        except JWTError as exc:
            raise AuthenticationError("invalid token", context={"reason": str(exc)}) from exc
        sub = claims.get("sub")
        if not sub:
            raise AuthenticationError("token missing 'sub' claim")
        return AuthenticatedUser(
            id=str(sub),
            email=claims.get("email"),
            first_name=claims.get("given_name"),
            last_name=claims.get("family_name"),
        )

    return _verify

"""API-domain exceptions and FastAPI exception handlers (spec 08, T02).

Every API-domain failure is a subclass of ``persona.errors.PersonaError`` so it
carries the project's structured ``context: dict[str, str]`` (D-01-12) and the
existing log idioms apply. The handlers translate each exception — ours, the
re-used core/runtime ones, and Pydantic ``ValidationError`` — into the right HTTP
status with a structured JSON body. Stack traces and internal messages are never
leaked to the client (security-first, ENGINEERING_STANDARDS §1.1); a generic 500
hides unexpected errors while the real detail goes to the logs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from persona.errors import (
    CreditsExhaustedError,
    DailySpendCapExceededError,
    PersonaError,
    PersonaNotFoundError,
    RuntimeWriteForbiddenError,
    SchemaVersionMismatchError,
    ToolNotAllowedError,
)
from persona.imagegen.errors import ImageGenUnavailableError
from persona.logging import get_logger
from persona.sandbox.errors import (
    SandboxQuotaExceededError,
    SandboxUnavailableError,
)
from pydantic import ValidationError

if TYPE_CHECKING:
    from fastapi import FastAPI

_log = get_logger("api.errors")

__all__ = [
    "AuthenticationError",
    "CloudConfigRefusedError",
    "CommunityDbError",
    "ConcurrencyCappedError",
    "ConnectorServiceUnavailableError",
    "ConversationNotFoundError",
    "CreditsExhaustedError",
    "DailySpendCapExceededError",
    "MCPAppAlreadyAdoptedError",
    "MCPAppNotAdoptableError",
    "MCPCredentialError",
    "MCPOAuthError",
    "MCPOAuthProviderError",
    "MCPOAuthStateError",
    "MCPServerNotFoundError",
    "MemoryNodeNotFoundError",
    "MCPServerValidationError",
    "ModelBackendUnavailableError",
    "PublicNoAuthRefusedError",
    "RateLimitExceededError",
    "RefinementLimitError",
    "RunNotFoundError",
    "TurnAlreadyActiveError",
    "TurnNotActiveError",
    "register_exception_handlers",
]


# AuthenticationError was relocated to ``persona.errors`` at spec V1 T03
# (D-V1-X-jwt-verifier-extraction) so persona-voice can raise it from the
# extracted ``persona.auth.jwt_verifier.make_jwt_verifier`` without taking a
# persona-api dependency. Re-exported here for back-compat with the existing
# persona-api import sites (``from persona_api.errors import AuthenticationError``).
# Spec R2 F-07 (R2-D-8): the PROVIDER-key auth error — a subclass of
# ``ProviderError(PersonaError)``, NOT of the ``persona.errors.AuthenticationError``
# above. A rotated/invalid model key surfaces this mid-request; without a dedicated
# handler it falls through the catch-all ``_domain_500`` → 500. Aliased to avoid
# shadowing the auth class; mapped to 401 below.
from persona.backends.errors import AuthenticationError as BackendAuthenticationError  # noqa: E402
from persona.errors import AuthenticationError  # noqa: E402, F401

# CreditsExhaustedError was relocated to ``persona.errors`` at Spec 19 L6c
# (D-19-X-credits-service-domain-relocation) alongside the credits_service
# move so persona-voice can raise it from
# :func:`persona.credits.service.require_credits` without taking a persona-api
# dependency. Imported above (``from persona.errors import ...``) and listed
# in ``__all__`` for back-compat with the existing persona-api import sites
# (``from persona_api.errors import CreditsExhaustedError``).


class PublicNoAuthRefusedError(PersonaError):
    """Raised at startup when community/no-auth detects a public bind (Spec 33, D-33-4).

    The fail-safe against an accidentally-exposed open, unauthenticated instance
    that would burn the operator's model keys. ``context`` carries the offending
    ``host``. Set ``PERSONA_ALLOW_PUBLIC_NOAUTH=1`` to override, or run the
    ``cloud`` edition for a public/shared deploy.
    """


class CloudConfigRefusedError(PersonaError):
    """Raised at startup when a ``cloud`` deploy is misconfigured (Spec R2, R2-D-1).

    The fail-fast for the highest-severity audit findings — a cloud edition must
    not silently ship authless (F-01) or run the request path as an
    RLS-bypassing superuser (F-06) or skip the JWT audience check (F-05). The
    guard refuses to start unless, for ``PERSONA_EDITION=cloud``:

    - ``APP_DATABASE_URL`` is set AND distinct from ``DATABASE_URL`` (the request
      path must use the non-superuser ``persona_app`` role, not the superuser DSN);
    - ``PERSONA_API_JWT_AUDIENCE`` is non-empty (cloud forces the ``aud`` check);
    - a defense-in-depth probe finds the rls_engine's role is NOT a superuser.

    ``context`` carries the offending ``edition`` (+ ``reason`` for which check
    tripped). There is intentionally NO request-time exception handler: like its
    sibling guards (:class:`PublicNoAuthRefusedError`,
    :class:`CloudGatewayNotVettedError`) this must crash the boot, never degrade.
    """


class CommunityDbError(PersonaError):
    """Raised when the community managed-Postgres substrate cannot be provisioned (Spec K10, T1).

    The fail-fast for the invisible product-managed database (D-K10-1/-9/-10): a
    misconfigured mode (``PERSONA_COMMUNITY_DB_MODE=external`` with no
    ``DATABASE_URL``), a datadir whose ``PG_VERSION`` disagrees with the bundled
    Postgres major (refuse-don't-corrupt — D-K10-10), a unix-socket datadir path
    that would exceed the AF_UNIX ``sun_path`` cap (D-K10-10), or the embedded
    Postgres package being unavailable. ``context`` carries the offending
    ``reason`` (+ the ``mode`` / ``path`` / ``major`` at fault). Like the sibling
    startup guards this crashes the boot rather than degrading to an unusable
    store.
    """


class CloudGatewayNotVettedError(PersonaError):
    """Raised at startup when cloud has a Docker MCP Gateway URL but no vetting ack (N1, D-N1-7).

    A cloud (multi-tenant) gateway is connect-only to an operator-run gateway whose
    aggregated tools are SHARED across tenants — so it must be operator-vetted. Mirroring
    the D-33-4 public-noauth guard, the operator must acknowledge the vetted-shared
    posture via ``PERSONA_ALLOW_CLOUD_GATEWAY=1``; per-tenant gateways, per-tenant
    container-running, and per-user secret injection are deferred (D-N1-7). Unset the
    gateway URL or set the ack flag.
    """


class ConversationNotFoundError(PersonaError):
    """Raised when a conversation is not visible to the current user (→ 404)."""


class RunNotFoundError(PersonaError):
    """Raised when a run is not visible to the current user (→ 404)."""


class MemoryNodeNotFoundError(PersonaError):
    """Raised when a Memory (graph) node is not the current user's (→ 404; Spec K5).

    The Memory read/edit/delete surface (``/v1/memory/nodes/{id}``) 404s when the
    node id is unknown or belongs to another user — RLS already scopes the read, so
    a miss is indistinguishable from "not yours," which is the correct, leak-free
    signal. ``context`` carries the ``node_id``.
    """


class TurnAlreadyActiveError(PersonaError):
    """Raised when a chat turn is already running for the conversation (→ 409).

    Spec P1 (D-P1-one-active-turn): exactly one active chat turn per
    conversation. A second
    ``POST /messages`` while a turn is in flight is **blocked** (not queued) so
    the standard chat UX (composer disabled while the persona answers) is
    honest. ``context`` carries ``conversation_id``. Backstopped at the DB by the
    partial-unique index on ``messages(conversation_id) WHERE
    streaming_status='running'``.
    """


class TurnNotActiveError(PersonaError):
    """Raised when a conversation has no live chat turn to reattach to (→ 404; spec P1).

    The reattach surface (``GET …/active-turn``, ``…/active-turn/events``,
    ``…/active-turn/cancel``) returns 404 when no turn is in flight — the turn
    finished, was interrupted, or never started. The web client treats this as
    "reconcile via the conversation history" rather than tailing. ``context``
    carries ``conversation_id``. Mirrors the runs ``RunNotFoundError("run is not
    active")`` signal.
    """


class RateLimitExceededError(PersonaError):
    """Raised when a user exceeds an endpoint's per-minute limit (→ 429).

    ``context`` carries the rate-limit metadata the handler turns into
    ``X-RateLimit-*`` / ``Retry-After`` headers: ``limit``, ``remaining`` (0),
    ``reset`` (epoch seconds when the window resets).
    """


class RefinementLimitError(PersonaError):
    """Raised when an authoring-refinement request exceeds the 3-round cap (→ 422; D-10-5)."""


class ConcurrencyCappedError(PersonaError):
    """Raised when a per-user in-flight cap is exhausted (→ 429; D-15-X-concurrency-cap).

    Distinct from :class:`RateLimitExceededError` (per-minute count window):
    a concurrency cap is the *in-flight* bound — the user already has one
    image-generation request running and a second arrived before the first
    completed. The cap is enforced via Postgres advisory transactional
    locks (``pg_try_advisory_xact_lock`` keyed by ``hash(user_id)``) so
    the bound holds across multiple API workers (D-15-X-concurrency-cap;
    async-semaphore rejected). ``context`` always carries ``user_id`` and
    may carry ``retry_after_s`` to feed the HTTP ``Retry-After`` header.
    """


class MCPServerNotFoundError(PersonaError):
    """Raised when a BYO MCP server is not visible to the current user (→ 404; spec 30)."""


class MCPServerValidationError(PersonaError):
    """Raised when a user-supplied MCP server URL is rejected (→ 422; spec 30, D-30-4).

    Covers the SSRF guard (private/loopback/link-local/metadata target, non-https
    scheme, unresolvable host) and basic field validation. ``context`` carries a
    redacted ``reason`` — never the resolved internal IP in a way that aids
    reconnaissance beyond the category.
    """


class MCPCredentialError(PersonaError):
    """Raised when BYO-MCP credential encryption is unavailable/misconfigured (→ 503; D-30-4).

    The operator did not configure ``MCP_CREDENTIAL_KEY`` but a server carrying
    credentials was submitted. Fail loud (never store a secret in plaintext);
    the message never leaks the key or the credential.
    """


class MCPAppNotAdoptableError(PersonaError):
    """Raised when a persona may not self-adopt a catalog app (→ 403; Spec N4, N4-D-6).

    The vetted-set gate refused the entry BEFORE any write (fail-closed): it is not a
    ``type: remote`` app with a ``remote_url`` (local-container / deferred, N4-D-2), or —
    in cloud — it is not in the operator allowlist (``PERSONA_MCP_ADOPT_VETTED``). Nothing
    was written or assigned. ``context`` names the app; never a secret.
    """


class MCPAppAlreadyAdoptedError(PersonaError):
    """Raised when the caller already has a server by this app's name (→ 409; Spec N4).

    A clear conflict (not a 500): re-adopting an app the user already set up. Update or
    remove the existing server first. ``context`` names the app; never a secret.
    """


class MCPOAuthError(PersonaError):
    """Base for a fail-closed MCP OAuth failure (→ 400; Spec R8, R8-D-6).

    Every OAuth failure — bad ``state``, redirect mismatch, code-exchange error,
    refresh failure, an unconfigured provider — surfaces here (or a subclass) and
    leaves the server **not connected**, never half-authenticated. Handlers emit a
    GENERIC body; ``message``/``context`` are author-controlled and carry a redacted
    category only, NEVER a token, code, verifier, or client secret.
    """


class MCPOAuthProviderError(MCPOAuthError):
    """The requested OAuth provider is unknown or not configured (→ 400; R8-D-1/7).

    GitHub is offered only when the operator set its client_id/secret (T9.5 runbook);
    an unconfigured / misspelled provider fails closed here before any redirect.
    """


class MCPOAuthStateError(MCPOAuthError):
    """The callback ``state`` is missing / expired / not the caller's (→ 400; R8-D-6).

    The CSRF gate. Any ``state`` that does not map to a live, un-consumed, in-TTL row
    owned by the current user is rejected — no token is written (fail-closed). The
    body never distinguishes the sub-reason to callers (no oracle).
    """


class ModelBackendUnavailableError(PersonaError):
    """No usable model backend for a model-required write path (→ 503; R1-D-2).

    Raised by the route-local runtime guard at the authoring / chat / run paths
    when the deployment has no usable model backend — either the runtime was
    never wired (no model configured at all) or a configured tier cannot
    construct a backend because no API key is set (community keyless boot). A
    deliberate, documented unavailability (mirrors :class:`ImageGenUnavailableError`),
    NOT a leaked 500. Route-local by design: a cloud bad-key still surfaces
    through the normal provider path, so the global ``AuthenticationError``
    (401) mapping is untouched (R1-D-5 stays a tracked follow-up).
    """


class ConnectorServiceUnavailableError(PersonaError):
    """The connector service could not be reached to issue a link (→ 503; Spec C6, C6-D-0).

    The web front-door (``POST /v1/me/connectors/{platform}/link``) proxies link
    initiation to the separate connector service. When that service is unreachable
    (unset ``PERSONA_CONNECTOR_SERVICE_URL``, connection refused, timeout, or a non-2xx
    upstream), the front-door **fails soft** with this first-class 503 so the surface can
    show the platform "temporarily unavailable" — never a dead spinner or a leaked 500.
    ``context`` carries the ``platform`` only; never the caller's bearer or any secret.
    """


def _body(error: str, detail: str, context: dict[str, str]) -> dict[str, Any]:
    """The structured error body shape every handler returns."""
    payload: dict[str, Any] = {"error": error, "detail": detail}
    if context:
        payload["context"] = context
    return payload


def register_exception_handlers(app: FastAPI) -> None:
    """Attach the exception → HTTP-response handlers to ``app``."""

    @app.exception_handler(AuthenticationError)
    async def _auth(_: Request, exc: AuthenticationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content=_body(
                "authentication_error", exc.message or "authentication required", exc.context
            ),
            headers={"WWW-Authenticate": "Bearer"},
        )

    @app.exception_handler(BackendAuthenticationError)
    async def _provider_auth(_: Request, exc: BackendAuthenticationError) -> JSONResponse:
        # Spec R2 F-07 (R2-D-8): a provider/model-key auth failure (rotated or invalid
        # key) is a 401, not a leaked 500. GENERIC body — never echo the provider
        # message or context (it can carry the key); the detail is logged server-side
        # only. FastAPI dispatches by the exception's MRO and picks this specific
        # handler over the ``PersonaError`` ``_domain_500``, regardless of order.
        # Log the type only — the exception message/context can carry the key.
        _log.warning("provider authentication failed ({kind})", kind=type(exc).__name__)
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content=_body("provider_auth_failed", "upstream authentication failed", {}),
            headers={"WWW-Authenticate": "Bearer"},
        )

    @app.exception_handler(CreditsExhaustedError)
    async def _credits(_: Request, exc: CreditsExhaustedError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            content=_body("credits_exhausted", exc.message or "insufficient credits", exc.context),
        )

    @app.exception_handler(DailySpendCapExceededError)
    async def _daily_cap_429(_: Request, exc: DailySpendCapExceededError) -> JSONResponse:
        """Per-UTC-day spend cap reached (Spec R7, R7-D-5).

        Distinct from :class:`CreditsExhaustedError` (→ 402, "out of credits
        forever"): this is "capped for *today*, back tomorrow", so it maps to
        **429 + ``Retry-After`` = seconds until the next UTC midnight** (the honest
        "come back later" — 402 would wrongly imply a permanent state). The body
        carries ``cap`` / ``spent`` / ``reset_epoch`` for client-side messaging;
        no internal detail leaks. The refusal is audited durably at the policy seam
        (``MeteredCreditsPolicy._audit_daily_cap_refusal``), not here.
        """
        import time

        reset_epoch = int(exc.context.get("reset_epoch", "0") or "0")
        retry_after = max(1, reset_epoch - int(time.time())) if reset_epoch else 60
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content=_body(
                "daily_spend_cap_exceeded",
                exc.message or "daily spend cap reached",
                exc.context,
            ),
            headers={"Retry-After": str(retry_after)},
        )

    @app.exception_handler(ToolNotAllowedError)
    async def _forbidden_tool(_: Request, exc: ToolNotAllowedError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content=_body("tool_not_allowed", exc.message or "tool not permitted", exc.context),
        )

    @app.exception_handler(PersonaNotFoundError)
    async def _persona_404(_: Request, exc: PersonaNotFoundError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content=_body("persona_not_found", exc.message or "persona not found", exc.context),
        )

    @app.exception_handler(ConversationNotFoundError)
    async def _conv_404(_: Request, exc: ConversationNotFoundError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content=_body(
                "conversation_not_found", exc.message or "conversation not found", exc.context
            ),
        )

    @app.exception_handler(RunNotFoundError)
    async def _run_404(_: Request, exc: RunNotFoundError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content=_body("run_not_found", exc.message or "run not found", exc.context),
        )

    @app.exception_handler(MemoryNodeNotFoundError)
    async def _memory_node_404(_: Request, exc: MemoryNodeNotFoundError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content=_body("memory_node_not_found", exc.message or "memory not found", exc.context),
        )

    @app.exception_handler(TurnNotActiveError)
    async def _turn_not_active_404(_: Request, exc: TurnNotActiveError) -> JSONResponse:
        # Spec P1 reattach: no live turn to reattach to → the client reconciles
        # via the conversation history instead of tailing.
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content=_body("turn_not_active", exc.message or "no active turn", exc.context),
        )

    @app.exception_handler(TurnAlreadyActiveError)
    async def _turn_conflict_409(_: Request, exc: TurnAlreadyActiveError) -> JSONResponse:
        # Spec P1 D-P1-one-active-turn: a turn is already running for this
        # conversation — block (don't queue) so the client disables the composer
        # while the persona answers.
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=_body(
                "turn_already_active", exc.message or "a turn is already running", exc.context
            ),
        )

    @app.exception_handler(MCPServerNotFoundError)
    async def _mcp_server_404(_: Request, exc: MCPServerNotFoundError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content=_body(
                "mcp_server_not_found", exc.message or "mcp server not found", exc.context
            ),
        )

    @app.exception_handler(MCPServerValidationError)
    async def _mcp_server_422(_: Request, exc: MCPServerValidationError) -> JSONResponse:
        # SSRF / URL-policy rejection (spec 30, D-30-4). The detail names the
        # category only (e.g. "private/loopback target"), never the resolved IP.
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=_body("mcp_server_invalid", exc.message or "mcp server rejected", exc.context),
        )

    @app.exception_handler(MCPCredentialError)
    async def _mcp_credential_503(_: Request, exc: MCPCredentialError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=_body(
                "mcp_credential_unavailable",
                exc.message or "credential encryption is not configured",
                exc.context,
            ),
        )

    @app.exception_handler(MCPAppNotAdoptableError)
    async def _mcp_app_not_adoptable_403(_: Request, exc: MCPAppNotAdoptableError) -> JSONResponse:
        # N4-D-6 vetted gate refused the app (fail-closed; nothing written).
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content=_body("mcp_app_not_adoptable", exc.message or "app not adoptable", exc.context),
        )

    @app.exception_handler(MCPAppAlreadyAdoptedError)
    async def _mcp_app_conflict_409(_: Request, exc: MCPAppAlreadyAdoptedError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=_body(
                "mcp_app_already_adopted", exc.message or "app already adopted", exc.context
            ),
        )

    @app.exception_handler(MCPOAuthError)
    async def _mcp_oauth_400(_: Request, exc: MCPOAuthError) -> JSONResponse:
        # Spec R8 (R8-D-6): every OAuth failure is fail-closed → 400, GENERIC body.
        # FastAPI dispatches by MRO so this base handler catches every subclass
        # (provider/state/exchange). The context is dropped to ``{}`` deliberately —
        # no sub-reason oracle on the CSRF/state path, and never a token/code/secret.
        # The type is logged server-side for diagnosis; the message is not (it is
        # author-controlled but we keep the surface minimal).
        _log.warning("mcp oauth failed ({kind})", kind=type(exc).__name__)
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=_body("mcp_oauth_failed", "the authorization could not be completed", {}),
        )

    @app.exception_handler(RateLimitExceededError)
    async def _rate(_: Request, exc: RateLimitExceededError) -> JSONResponse:
        headers = {}
        if "limit" in exc.context:
            headers["X-RateLimit-Limit"] = exc.context["limit"]
            headers["X-RateLimit-Remaining"] = exc.context.get("remaining", "0")
        if "reset" in exc.context:
            headers["X-RateLimit-Reset"] = exc.context["reset"]
            headers["Retry-After"] = exc.context.get("retry_after", exc.context["reset"])
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content=_body("rate_limit_exceeded", exc.message or "rate limit exceeded", exc.context),
            headers=headers,
        )

    @app.exception_handler(ConcurrencyCappedError)
    async def _imagegen_concurrency_429(_: Request, exc: ConcurrencyCappedError) -> JSONResponse:
        """Per-user image-generation in-flight cap exhausted (spec 15 T14, D-15-X-concurrency-cap).

        Distinct from :class:`RateLimitExceededError` (per-minute window) and
        :class:`SandboxQuotaExceededError` (per-tenant sandbox slots): the
        concurrency cap is the *in-flight* bound on a single capability for a
        single user. Returning 429 + ``Retry-After`` lets the client back off
        until the user's first in-flight generation completes (typical p95
        latency for `gpt-image-1` / Flux 1.1 [pro] is single-digit seconds —
        the 5s hint matches the lower bound of the expected wait).
        """
        retry_after = exc.context.get("retry_after_s", "5")
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content=_body(
                "concurrency_capped",
                exc.message or "image generation already in flight for this user",
                exc.context,
            ),
            headers={"Retry-After": retry_after},
        )

    @app.exception_handler(SandboxQuotaExceededError)
    async def _sandbox_quota_429(_: Request, exc: SandboxQuotaExceededError) -> JSONResponse:
        """Per-user sandbox cap exhausted (spec 12 T09c, D-12-17 quota path).

        Distinct from :class:`RateLimitExceededError` (general per-endpoint
        rate limit): the sandbox cap is a per-tenant concurrent-session policy
        (D-12-17; bounds SCP-12-1 multi-tenant attack surface). A 429 with
        ``Retry-After`` lets the client back off; the structured ``context``
        body (``user_id``, ``current_count``, ``cap``) surfaces the cap state
        for client-side messaging.

        ``Retry-After: 60`` is a coarse hint — quota frees whenever one of the
        user's existing sessions is released or reaped (D-12-17 idle_timeout
        default 300s; reap cadence 60s). The 60s value matches the reaper
        cadence so the next reap-window is the worst-case wait for a slot
        freed by idle reap; sessions released sooner via ``pool.release()``
        free the slot immediately.
        """
        _log.info(
            "sandbox quota rejection user={user} count={count} cap={cap}",
            user=exc.context.get("user_id", "?"),
            count=exc.context.get("current_count", "?"),
            cap=exc.context.get("cap", "?"),
        )
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content=_body(
                "sandbox_quota_exceeded",
                exc.message or "sandbox quota exceeded for this user",
                exc.context,
            ),
            headers={"Retry-After": "60"},
        )

    @app.exception_handler(ImageGenUnavailableError)
    async def _imagegen_unavailable_503(_: Request, exc: ImageGenUnavailableError) -> JSONResponse:
        """Image-generation backend unreachable (spec 15 T16, D-15-X-construction-time-fail-fast).

        Fires when the deployment did not set ``PERSONA_IMAGEGEN_API_KEY``
        OR when the provider rejected our credentials at call time (401/403
        from the SDK). Distinct from :class:`ImageProviderError` (502 — the
        provider is reachable but rejected us for non-credential reasons)
        and :class:`SandboxUnavailableError` (503 — the sandbox substrate
        is down).
        """
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=_body(
                "imagegen_unavailable",
                exc.message or "image generation backend is not configured",
                exc.context,
            ),
            headers={"Retry-After": "30"},
        )

    @app.exception_handler(ModelBackendUnavailableError)
    async def _model_unavailable_503(_: Request, exc: ModelBackendUnavailableError) -> JSONResponse:
        """Model-required write path with no usable model backend (R1-D-2).

        Fires on a keyless/unconfigured boot (authoring/chat/run) — the runtime
        was never wired, or a configured tier has no API key. A first-class 503
        (mirrors ``ImageGenUnavailableError``), not a leaked 500.
        """
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=_body(
                "model_unavailable",
                exc.message or "model backend is not configured",
                exc.context,
            ),
            headers={"Retry-After": "30"},
        )

    @app.exception_handler(ConnectorServiceUnavailableError)
    async def _connector_unavailable_503(
        _: Request, exc: ConnectorServiceUnavailableError
    ) -> JSONResponse:
        """Connector service unreachable for link initiation (Spec C6, C6-D-0 fail-soft).

        The surface maps ``connector_unavailable`` to a "temporarily unavailable" chip +
        retry — never a dead spinner. ``Retry-After: 15`` is a brief back-off hint.
        """
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=_body(
                "connector_unavailable",
                exc.message or "the connector service is temporarily unavailable",
                exc.context,
            ),
            headers={"Retry-After": "15"},
        )

    @app.exception_handler(SandboxUnavailableError)
    async def _sandbox_unavailable_503(_: Request, exc: SandboxUnavailableError) -> JSONResponse:
        """Substrate unreachable (spec 12 T09c, D-12-5 no-degraded-fallback path).

        Distinct from quota-exceeded (above): substrate-unavailability is an
        infrastructure outage (E2B API down; pool closed; SDK missing), not
        a per-tenant policy decision. Per D-12-5 there is no degraded fallback
        — the client retries later. ``Retry-After: 30`` is a brief back-off
        hint; the substrate-status dashboard is the authoritative recovery
        signal.
        """
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=_body(
                "sandbox_unavailable",
                exc.message or "sandbox substrate is unavailable",
                exc.context,
            ),
            headers={"Retry-After": "30"},
        )

    @app.exception_handler(RefinementLimitError)
    async def _refine_limit_422(_: Request, exc: RefinementLimitError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=_body(
                "refinement_limit_exceeded", exc.message or "refinement limit reached", exc.context
            ),
        )

    @app.exception_handler(SchemaVersionMismatchError)
    async def _schema_422(_: Request, exc: SchemaVersionMismatchError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=_body(
                "schema_version_mismatch", exc.message or "unsupported schema_version", exc.context
            ),
        )

    @app.exception_handler(RuntimeWriteForbiddenError)
    async def _write_forbidden(_: Request, exc: RuntimeWriteForbiddenError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content=_body("write_forbidden", exc.message or "write not permitted", exc.context),
        )

    @app.exception_handler(ValidationError)
    async def _pydantic_422(_: Request, exc: ValidationError) -> JSONResponse:
        # Invalid persona YAML / request body → structured 422 (acceptance #2).
        # jsonable_encoder so a non-JSON-native value in errors() (e.g. a datetime
        # in the offending `input`) can't turn the intended 422 into a 500.
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=jsonable_encoder(
                {"error": "validation_error", "detail": exc.errors(include_url=False)}
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def _request_422(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=jsonable_encoder({"error": "validation_error", "detail": exc.errors()}),
        )

    @app.exception_handler(PersonaError)
    async def _domain_500(_: Request, exc: PersonaError) -> JSONResponse:
        # Any other domain error we didn't specifically map: 500, logged, body
        # generic (don't leak internal detail to the client).
        _log.error("unhandled domain error: {err}", err=str(exc))
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"error": "internal_error", "detail": "an internal error occurred"},
        )

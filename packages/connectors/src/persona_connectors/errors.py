"""Domain exceptions for the connector framework (Spec C1).

Per ENGINEERING_STANDARDS: domain logic raises domain exceptions, never bare
``ValueError``/``RuntimeError``; every exception carries a ``context: dict[str,
str]`` keyword so log records stay structured. ``ConnectorError`` is the C1 base
and extends persona-core's :class:`~persona.errors.PersonaError` so the whole
stack catches one hierarchy.

More specific exceptions (e.g. an unlinked-identity error, a persona-not-
addressable error) land in later tasks where they are raised — this module ships
the base so T1's surface is real. Import-decoupled from persona_api.
"""

from __future__ import annotations

from persona.errors import PersonaError

__all__ = [
    "ConnectorError",
    "DiscordApiError",
    "DiscordCannotDeliverError",
    "DiscordRateLimitError",
    "IdentityNotLinkedError",
    "LinkTokenInvalidError",
    "PostmarkApiError",
    "PostmarkRateLimitError",
    "SlackApiError",
    "SlackRateLimitError",
    "TelegramApiError",
    "TelegramRateLimitError",
    "TwilioApiError",
    "TwilioRateLimitError",
]


class ConnectorError(PersonaError):
    """Base for every connector-framework domain error (Spec C1).

    Subclasses are introduced where they are raised (identity resolution,
    persona addressing, account linking, delivery). Carries the inherited
    ``context: dict[str, str]`` for structured logging.
    """


class IdentityNotLinkedError(ConnectorError):
    """An inbound platform identity has no live (active) Persona-user binding.

    The load-bearing security invariant (C1-D-5, criteria 6/7): an unlinked (or
    revoked) identity gets a link-instruction and ZERO access — never another
    user's personas. The flow catches this and replies with the linking prompt.
    """


class LinkTokenInvalidError(ConnectorError):
    """A presented link token is unknown, expired, already consumed, or mismatched.

    Single-use + short-TTL + platform-bound (C1-D-5); any violation fails loud so
    a replayed/forged/stale token never binds an identity.
    """


class TelegramApiError(ConnectorError):
    """A Telegram Bot API call failed (Spec C2 — the adapter-boundary domain error).

    Telegram's transport faults (HTTP errors, network failures) and logical
    rejections (``{"ok": false, ...}``) are caught at the Bot API client boundary
    and re-raised as this domain error, so callers depend on our hierarchy, not on
    ``httpx`` (the ENG-STD catch-at-the-boundary rule).

    The bot token rides in the request URL (``/bot<token>/<method>``), so the
    underlying ``httpx`` exception (which carries that URL) is **never chained or
    quoted** (D-C2-X-credential): the ``context`` carries only the method name and
    the Telegram ``error_code``/status — never the URL or the token.
    """


class TelegramRateLimitError(TelegramApiError):
    """Telegram throttled the bot (HTTP 429) — back off for :attr:`retry_after` seconds.

    Telegram returns ``429`` with ``parameters.retry_after`` when a send exceeds
    the rate limits (~30/s global, 1/s per chat — C2-R-1). The send path honours
    :attr:`retry_after` (D-C2-3); a non-retryable rejection maps to a
    :class:`~persona.delivery.DeliveryResult` ``failed`` (D-C1-X-platform-rejection)
    rather than a silent drop.

    Attributes:
        retry_after: Seconds Telegram asks the caller to wait before retrying.
    """

    def __init__(
        self,
        message: str = "",
        *,
        retry_after: int,
        context: dict[str, str] | None = None,
    ) -> None:
        merged = {**(context or {}), "retry_after": str(retry_after)}
        super().__init__(message, context=merged)
        self.retry_after = retry_after


class TwilioApiError(ConnectorError):
    """A Twilio Messages API call failed (Spec C4 — the adapter-boundary domain error).

    Twilio's transport faults (HTTP errors, network failures) and logical rejections
    (a non-2xx with a JSON ``{"code", "message"}`` body) are caught at the client
    boundary and re-raised as this domain error (the ENG-STD catch-at-the-boundary
    rule). The auth token rides in the HTTP-**Basic** auth header (never the URL), and
    the underlying ``httpx`` exception is **never chained or quoted** (D-C4-1 /
    D-C2-X-credential): the ``context`` carries only the method + status + the Twilio
    ``error_code`` — never the token or the URL.

    **The 24h-window signal (63016) is NOT interpreted here.** An out-of-window
    WhatsApp send surfaces as ``error_code`` on the create response (``status=failed``)
    or later via a status callback; the client only EXPOSES ``error_code`` (on
    :class:`~persona_connectors._twilio.client.TwilioMessageResult` / in this error's
    ``context``) so the delivery mapping (T9/T12) can act on it — the boundary stays thin.
    """


class TwilioRateLimitError(TwilioApiError):
    """Twilio throttled the account (HTTP 429) — back off and retry (Spec C4).

    Twilio returns ``429`` (with code ``20429``) when sends exceed the account's
    per-second message limits. The send path maps this to a
    :class:`~persona.delivery.DeliveryResult` ``pending`` (retryable —
    D-C1-X-platform-rejection), never a silent drop. A subclass of
    :class:`TwilioApiError` so a generic catch still maps a rejection to a result.
    """


class PostmarkApiError(ConnectorError):
    """A Postmark API call failed (Spec C5 — the email adapter-boundary domain error).

    Postmark's transport faults (HTTP errors, network failures) and logical rejections
    (a non-2xx, or a 200 with a non-zero ``ErrorCode``) are caught at the client boundary
    and re-raised as this domain error (catch-at-the-boundary). The **server token** rides
    in the ``X-Postmark-Server-Token`` header; the underlying ``httpx`` exception is never
    chained or quoted (``raise … from None``) so the token never reaches a traceback — the
    ``context`` carries only the method + status + Postmark ``ErrorCode``. Postmark's own
    ``Message`` text is safe to surface (no secret). The connector maps this to a
    :class:`~persona.delivery.DeliveryResult` ``failed`` (never a silent drop).
    """


class PostmarkRateLimitError(PostmarkApiError):
    """Postmark throttled the account (HTTP 429) — back off and retry (Spec C5).

    The send path maps this to a :class:`~persona.delivery.DeliveryResult` ``pending``
    (retryable — D-C1-X-platform-rejection), never a silent drop. A subclass of
    :class:`PostmarkApiError` so a generic catch still maps a rejection to a result.
    """


class DiscordApiError(ConnectorError):
    """A Discord REST API call failed (Spec C3 — the adapter-boundary domain error).

    Discord's transport faults (HTTP errors, network failures) and logical
    rejections (a non-2xx with a JSON ``{"code", "message"}`` body) are caught at the
    client boundary and re-raised as this domain error (the ENG-STD
    catch-at-the-boundary rule). The bot token rides in the ``Authorization: Bot``
    **header** (not the URL — safer than Telegram), and the underlying ``httpx``
    exception is **never chained or quoted** (D-C3-3 / D-C2-X-credential): the
    ``context`` carries only the method/path + the Discord ``code``/status, never the
    token.
    """


class DiscordCannotDeliverError(DiscordApiError):
    """Discord refused a DM to a user (codes ``50007`` / ``50278``) — the DM-ability gate.

    A bot can DM a user only with a mutual guild, a user-install, or an established
    DM channel (C3-R-1/R-4). When none holds, the message POST is rejected:
    ``50278`` (no mutual guilds) / ``50007`` (cannot send — blocked / DMs disabled).
    The connector maps this to a :class:`~persona.delivery.DeliveryResult` ``failed``
    with a friendly "share a server / message me first" detail — the message stays
    durably persisted (D-C1-X-platform-rejection / D-C3-4), never a silent drop and
    never a crash. A subclass of :class:`DiscordApiError` so a generic catch still
    maps to ``failed``; the dedicated type lets the connector phrase it warmly.
    """


class DiscordRateLimitError(DiscordApiError):
    """Discord throttled the bot (HTTP ``429`` or code ``40003``) — back off ``retry_after``.

    Discord returns ``429`` with a ``retry_after`` (float **seconds**) on a per-route
    or global limit; code ``40003`` ("opening DMs too fast") is the DM-open abuse
    guard. The send path honours :attr:`retry_after` and maps to a
    :class:`~persona.delivery.DeliveryResult` ``pending`` (retryable —
    D-C1-X-platform-rejection), never a silent drop.

    Attributes:
        retry_after: Seconds Discord asks the caller to wait before retrying (float —
            Discord reports sub-second backoffs).
    """

    def __init__(
        self,
        message: str = "",
        *,
        retry_after: float,
        context: dict[str, str] | None = None,
    ) -> None:
        merged = {**(context or {}), "retry_after": str(retry_after)}
        super().__init__(message, context=merged)
        self.retry_after = retry_after


class SlackApiError(ConnectorError):
    """A Slack Web API call failed (Spec C3 — the adapter-boundary domain error).

    Slack's Web API always returns HTTP 200 with a JSON ``{"ok": bool, ...}``; ``ok:
    false`` carries an ``error`` string. Transport faults and ``ok: false`` rejections
    are caught at the client boundary and re-raised as this domain error (the ENG-STD
    catch-at-the-boundary rule). The bot token rides in the ``Authorization: Bearer``
    header and the underlying ``httpx`` exception is **never chained or quoted**
    (D-C3-3 / D-C2-X-credential): the ``context`` carries only the method + the Slack
    ``error`` code, never the token. **Slack DMs are unconditional** (the app is
    installed in the workspace), so — unlike Discord — there is no cannot-deliver gate.
    """


class SlackRateLimitError(SlackApiError):
    """Slack throttled the app (HTTP 429) — back off :attr:`retry_after` seconds.

    Slack returns ``429`` with a ``Retry-After`` header (integer seconds), notably on
    ``chat.postMessage`` (~1 msg/s per channel). The send path honours
    :attr:`retry_after` and maps to a :class:`~persona.delivery.DeliveryResult`
    ``pending`` (retryable — D-C1-X-platform-rejection), never a silent drop.

    Attributes:
        retry_after: Seconds Slack asks the caller to wait before retrying.
    """

    def __init__(
        self,
        message: str = "",
        *,
        retry_after: int,
        context: dict[str, str] | None = None,
    ) -> None:
        merged = {**(context or {}), "retry_after": str(retry_after)}
        super().__init__(message, context=merged)
        self.retry_after = retry_after

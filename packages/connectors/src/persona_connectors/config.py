"""Settings for the persona-connectors service (Spec C1 T1).

Every knob lands here via environment variables — twelve-factor discipline (the
Spec 08 ``APIConfig`` / V1 ``VoiceConfig`` precedent). Connector-specific knobs
are prefixed ``PERSONA_CONNECTORS_``; the open-core edition reads the shared,
prefix-less ``PERSONA_EDITION`` var (Spec 33), exactly as api/web/voice do.

This module is part of the import-decoupled surface — it does NOT import
``persona_api`` (the api-coupling lives only in
:mod:`persona_connectors.composition`).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["ConnectorConfig", "SlackTransport", "TelegramTransport"]

# The two inbound transports (C2 D-C2-1): ``webhook`` (Telegram pushes to a
# public HTTPS endpoint — prod latency) vs ``longpoll`` (we pull via getUpdates —
# dev, no public endpoint). Mutually exclusive per bot.
TelegramTransport = Literal["webhook", "longpoll"]

# The two Slack inbound transports (D-C3-2): ``socket`` (an outbound WS via
# ``apps.connections.open`` — no public endpoint, the WS is app-token-authenticated)
# vs ``http`` (a public Request URL with per-request signature verification). Default
# ``socket`` — zero-infra, the connector-service posture.
SlackTransport = Literal["socket", "http"]


class ConnectorConfig(BaseSettings):
    """Environment-driven settings for the persona-connectors service."""

    model_config = SettingsConfigDict(
        env_prefix="PERSONA_CONNECTORS_",
        env_file=None,
        case_sensitive=False,
        extra="ignore",
        # Honor an explicit ``ConnectorConfig(edition=...)`` kwarg (the field
        # name) alongside the ``PERSONA_EDITION`` validation_alias (Spec 33).
        populate_by_name=True,
    )

    # --- Open-core edition (Spec 33) ---
    # Reads the SAME ``PERSONA_EDITION`` var as api/web/voice (no prefix).
    # ``community`` (default): single local owner, no auth, no credit metering.
    # ``cloud``: Clerk JWT + persona ownership + credits + multi-tenant RLS.
    edition: str = Field(default="community", validation_alias="PERSONA_EDITION")
    community_owner_id: str = Field(default="local-owner")
    community_owner_email: str = Field(default="local@localhost")

    @property
    def is_cloud(self) -> bool:
        """Whether this process runs the commercial cloud edition."""
        return self.edition.strip().lower() == "cloud"

    # --- Conversation boundaries (C1-D-3) ---
    # The per-(owner, platform, channel, persona) idle gap that ends a persona's
    # conversation. Tens of minutes, tunable; low-stakes (memory persists). Lazy
    # expiry on the next inbound — no background sweeper.
    idle_timeout_minutes: int = Field(default=30, gt=0)

    # --- Database (RLS-scoped persona-core direct access; cloud) ---
    # Same persona_app non-superuser role as persona-api (D-07-5); RLS scopes
    # every connection via the ``current_user_id`` contextvar the composition
    # root sets per inbound message (D-C1-X-rls-spine).
    database_url: str = Field(default="")
    db_pool_size: int = Field(default=5, gt=0)

    # --- Community-edition local persistence (Spec 33) ---
    community_db_path: str = Field(default="./persona_community.db")
    community_memory_path: str = Field(default="./persona_community_memory")

    # --- JWT verification (matches the JwtVerifierConfig Protocol shape) ---
    # Identical surface to ``APIConfig`` / ``VoiceConfig`` so the same
    # ``persona.auth.jwt_verifier.make_jwt_verifier`` consumes this via
    # structural typing — for resolving the authenticated identity at linking.
    jwt_secret: SecretStr | None = Field(default=None)
    jwt_public_key: SecretStr | None = Field(default=None)
    jwt_algorithms: str = Field(default="HS256")
    jwt_audience: str | None = Field(default=None)
    # R9-062: opt-in JWKS-based verification (persona.auth.jwt_verifier.make_jwks_verifier).
    # When set, the connector verifies Clerk tokens against this instance's published JWKS
    # (kid-matched, refetch-on-rotation) instead of the single static ``jwt_public_key`` —
    # so it works against ANY Clerk instance (dev + prod) and survives key rotation without
    # a redeploy. Not a credential (a JWKS endpoint is public); a plain URL, unset by default
    # so existing static-key deployments are byte-identical.
    jwt_jwks_url: str | None = Field(default=None)

    @field_validator("jwt_algorithms", mode="before")
    @classmethod
    def _normalise_algorithms(cls, v: object) -> str:
        """Allow the ``PERSONA_CONNECTORS_JWT_ALGORITHMS=HS256,RS256`` env form."""
        if v is None:
            return "HS256"
        return str(v)

    @property
    def jwt_algorithms_list(self) -> list[str]:
        """The configured algorithms as a list (consumed by ``make_jwt_verifier``)."""
        return [a.strip() for a in self.jwt_algorithms.split(",") if a.strip()]

    # --- Telegram adapter (Spec C2) ---
    # The bot token + webhook secret are CREDENTIALS (D-C2-X-credential): both
    # ``SecretStr`` (the Spec 08 posture, the ``jwt_secret`` precedent), never
    # logged, never in an error message — unwrapped only at the Bot API call
    # site. ``telegram_bot_token`` is ``None`` until the adapter is configured
    # (the service fails fast at startup if a Telegram transport is requested
    # without it — a later task).
    telegram_bot_token: SecretStr | None = Field(default=None)
    # The secret echoed back in the ``X-Telegram-Bot-Api-Secret-Token`` header on
    # every webhook request (D-C2-2 — constant-time-compared before parsing).
    telegram_webhook_secret: SecretStr | None = Field(default=None)
    # The public HTTPS URL Telegram pushes updates to (webhook transport).
    telegram_webhook_url: str = Field(default="")
    # The bot's @username for the ``t.me/<username>?start=<token>`` deep link
    # (C2-R-2). Empty → resolved via ``getMe`` at startup (a later task).
    telegram_bot_username: str = Field(default="")
    # The Bot API base (overridable for a local Bot API server / tests).
    telegram_api_base_url: str = Field(default="https://api.telegram.org")
    # D-C2-1: ``webhook`` (prod) vs ``longpoll`` (dev). Default ``longpoll`` —
    # the zero-infra dev posture (no public endpoint needed).
    telegram_transport: TelegramTransport = Field(default="longpoll")
    # The long-poll ``getUpdates`` timeout (seconds) — only used in longpoll mode.
    telegram_longpoll_timeout_seconds: int = Field(default=30, ge=0)
    # The deep-link token TTL (C2-R-2 / T6). The token rides VISIBLY in
    # ``t.me/<bot>?start=<token>`` (Telegram logs/caches it), so its safety rests on
    # being short-lived + single-use (C1-D-5). A tight default — the user clicks the
    # fresh link immediately; a stale link simply fails closed (regenerate).
    telegram_link_token_ttl_minutes: int = Field(default=15, gt=0)

    # --- Discord adapter (Spec C3) ---
    # The bot token is a CREDENTIAL (D-C3-3 / D-C2-X-credential carried forward):
    # ``SecretStr``, never logged, unwrapped only at the Bot API call site (it rides
    # in the ``Authorization: Bot <token>`` HEADER — not the URL — safer than
    # Telegram). ``None`` until the adapter is configured (the service fails fast at
    # startup if Discord is requested without it). v1 is a single bot per platform
    # (D-C3-X-v1-reach).
    discord_bot_token: SecretStr | None = Field(default=None)
    # OAuth2 app credentials for IDENTITY linking (D-C3-4): the public client id +
    # the secret client credential. The authorize step carries the C1 ``LinkToken``
    # as ``state`` (free CSRF); the callback exchanges ``code`` → ``/users/@me`` →
    # ``redeem_and_bind``. ``scope=identify+bot`` so one consent links identity AND
    # adds the bot to a shared guild (establishing DM-ability).
    discord_oauth_client_id: str = Field(default="")
    discord_oauth_client_secret: SecretStr | None = Field(default=None)
    # The registered OAuth redirect/callback URL (the connector's callback route).
    discord_oauth_redirect_uri: str = Field(default="")
    # The REST base (v10) + the Gateway WS URL — overridable for a faithful stub in
    # tests / a local proxy. The gateway is Discord's only inbound DM transport
    # (D-C3-1): one persistent WS in the connector process.
    discord_api_base_url: str = Field(default="https://discord.com/api/v10")
    discord_gateway_url: str = Field(default="wss://gateway.discord.gg/?v=10&encoding=json")
    # The OAuth ``state`` (= C1 LinkToken) TTL. Short-lived + single-use (C1-D-5);
    # a stale/replayed state fails closed (the CSRF-class boundary of OAuth linking).
    discord_link_token_ttl_minutes: int = Field(default=15, gt=0)

    # --- Slack adapter (Spec C3) ---
    # The bot token (``xoxb-…``) is a CREDENTIAL (D-C3-3): ``SecretStr``, in the
    # ``Authorization: Bearer`` header, never logged. v1 = a single workspace install
    # (D-C3-X-v1-reach). ``None`` until configured (fail-fast at startup).
    slack_bot_token: SecretStr | None = Field(default=None)
    # The app-level token (``xapp-…``, ``connections:write``) for socket mode — opens
    # the event WebSocket via ``apps.connections.open`` (D-C3-2). ``None`` for HTTP mode.
    slack_app_token: SecretStr | None = Field(default=None)
    # The signing secret for the HTTP-events transport (D-C3-3): every request carries
    # ``X-Slack-Signature`` = ``v0=``HMAC-SHA256(secret, ``v0:{ts}:{body}``), verified
    # constant-time before parsing, with a 5-minute replay window. Unused in socket mode.
    slack_signing_secret: SecretStr | None = Field(default=None)
    # OAuth v2 app credentials for IDENTITY linking (D-C3-4): the authorize step carries
    # the C1 ``LinkToken`` as ``state``; the callback exchanges ``code`` via
    # ``oauth.v2.access`` → ``authed_user.id`` → ``redeem_and_bind``.
    slack_oauth_client_id: str = Field(default="")
    slack_oauth_client_secret: SecretStr | None = Field(default=None)
    slack_oauth_redirect_uri: str = Field(default="")
    # The Web API base — overridable for a faithful stub in tests.
    slack_api_base_url: str = Field(default="https://slack.com/api")
    # D-C3-2: ``socket`` (default, no public endpoint) vs ``http`` (public signed endpoint).
    slack_transport: SlackTransport = Field(default="socket")
    # The OAuth ``state`` (= C1 LinkToken) TTL — short-lived + single-use (C1-D-5).
    slack_link_token_ttl_minutes: int = Field(default=15, gt=0)

    # --- Twilio WhatsApp + SMS adapters (Spec C4, D-C4-1) ---
    # ONE Twilio account drives BOTH channels (D-C4-1); the channel is chosen by the
    # from-address prefix (``whatsapp:+E164`` vs a bare E.164). The Account SID is a
    # PUBLIC identifier (it rides in the request URL path and Twilio prints it openly)
    # — a plain ``str``, not a credential. The auth token is the CREDENTIAL
    # (D-C2-X-credential carried forward): ``SecretStr``, never logged, unwrapped only
    # at the HTTP-Basic-auth call site. Both ``None``/empty until configured (the
    # service fails fast at startup if a Twilio channel is requested without them).
    twilio_account_sid: str = Field(default="")
    twilio_auth_token: SecretStr | None = Field(default=None)
    # The WhatsApp + SMS sender addresses (the Twilio ``From``): ``whatsapp:+E164`` for
    # WhatsApp, a bare ``+E164`` for SMS. Empty until the respective channel is configured.
    twilio_whatsapp_from: str = Field(default="")
    twilio_sms_from: str = Field(default="")
    # The token Twilio signs inbound + status webhooks with (T3 signature validation —
    # usually the same value as ``twilio_auth_token``, but a SEPARATE field so signing
    # vs API auth is explicit). ``SecretStr``; fail-closed when unset (D-C2-2 posture).
    twilio_webhook_auth_token: SecretStr | None = Field(default=None)
    # The approved WhatsApp re-engagement template content SID (``HX…``) used to reopen
    # a closed 24h customer-care window (T9/T13). Empty until a template is approved.
    whatsapp_reengagement_template_sid: str = Field(default="")
    # The Twilio API base (overridable for a faithful stub in tests / a local proxy).
    twilio_api_base_url: str = Field(default="https://api.twilio.com")
    # The phone-link token TTL (the SMS/WhatsApp account-linking flow, a later task).
    # Short-lived + single-use (C1-D-5); a stale link fails closed (regenerate).
    phone_link_token_ttl_minutes: int = Field(default=10, gt=0)
    # The SMS multi-segment concatenation budget (T12 splitter): how many 153-char
    # GSM-7 / 67-char UCS-2 parts a single reply may span before it is split into
    # separate messages. A tight default keeps replies cheap + readable.
    sms_max_segments: int = Field(default=3, gt=0)

    # --- Email (Spec C5) ---------------------------------------------------------------
    # The Postmark SERVER token — the send credential (``SecretStr``, never logged;
    # unwrapped only at the request header). The email channel is enabled iff this AND
    # ``email_inbound_address`` are set.
    postmark_server_token: SecretStr | None = Field(default=None)
    # The shared inbound address personas are reached at + sent from (the ``From``; the
    # persona rides in its display-name). Plus-addressing (``inbound+astrid@``) selects the
    # persona (D-C5-3). Empty until the email channel is configured.
    email_inbound_address: str = Field(default="")
    # The HTTP Basic-Auth credential the Postmark inbound webhook is configured with (B1,
    # D-C5-5): validate-before-parse, fail-closed when unset. ``SecretStr`` for the password.
    postmark_webhook_username: str = Field(default="")
    postmark_webhook_password: SecretStr | None = Field(default=None)
    # The Postmark API base (overridable for a faithful stub in tests / a local proxy).
    postmark_api_base_url: str = Field(default="https://api.postmarkapp.com")
    # The email-link token TTL (the email-address verification flow, D-C5-X-verification-
    # carrier). Short-lived + single-use (C1-D-5); a stale code fails closed (regenerate).
    email_link_token_ttl_minutes: int = Field(default=15, gt=0)

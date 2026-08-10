"""The connector service entry point (Spec C2 T9 / C3) — ``python -m persona_connectors``.

Brings the configured connector adapters to life: assembles the engines + the reused api
runtime (C1-D-1, the ``run_worker.py`` pattern — this module + ``composition`` + ``infra``
are the ONLY ``persona_api`` importers; the flow/domain/<adapter> surface stays api-free, the
reversibility ideal), registers each configured connector as a C0 ``MessageDeliverer``, and
runs every configured inbound transport concurrently — Telegram (long-poll / webhook),
Discord (the gateway WebSocket), Slack (socket mode / HTTP events) — plus the periodic idle
sweep. A platform is wired iff its bot token is configured (C3 multi-connector; v1 single bot
per platform — D-C3-X-v1-reach).

Deploy seam: the heavy :class:`RuntimeFactory` (embedder / tier-registry / model backends) and
the live transport loops are built/run here from the live environment and are exercised by the
operator pass, not CI (the same posture as api's own ``@external`` turn tests). The testable
wiring (flows, routing, render, linking, connectors, the persona-name lister, the multi-
connector delivery router) is unit + integration covered.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import uvicorn
from persona.auth.jwt_verifier import make_jwks_verifier, make_jwt_verifier
from persona.events import EventTriggerSettings
from persona.logging import get_logger
from persona.stores.chroma import ChromaBackend
from persona.stores.postgres import PostgresBackend
from persona_api.approvals.kill_switch import KillSwitchStore
from persona_api.config import APIConfig, Edition
from persona_api.editions.factory import build_credits_policy, build_stripe_gateway
from persona_api.events import (
    build_event_dispatcher,
    make_connector_linked_emit,
    make_message_received_emit,
)
from persona_api.jobs import JobQueue
from persona_api.services import persona_service
from persona_api.services.model_tiers import (
    build_free_tier_registry,
    resolve_openrouter_subscription_mode,
)
from persona_api.services.runtime_factory import RuntimeFactory
from persona_api.services.task_steering_service import TaskSteeringService
from persona_api.services.turn_log_writer import PostgresTurnLogWriter
from persona_api.services.verb_service_composition import build_conversational_verb_services
from persona_api.tasks.store import TaskStore
from persona_runtime.tier import tier_registry_from_env
from websockets.asyncio.client import connect as ws_connect

from persona_connectors import discord as discord_adapter
from persona_connectors import email as email_adapter
from persona_connectors import slack as slack_adapter
from persona_connectors import sms as sms_adapter
from persona_connectors import whatsapp as whatsapp_adapter
from persona_connectors._phone.flow import PhoneInboundFlow
from persona_connectors._phone.linking import PhoneLinkingService
from persona_connectors._postmark.client import PostmarkClient
from persona_connectors._postmark.webhook import PostmarkWebhookAuth
from persona_connectors._twilio.app import build_twilio_app
from persona_connectors._twilio.client import TwilioClient
from persona_connectors._twilio.status import map_delivery, parse_status_callback
from persona_connectors.composition import (
    ConnectorComposition,
    build_delivery_router,
    build_email_recipient_resolver,
    build_persona_name_lister,
    build_reply_runner,
)
from persona_connectors.config import ConnectorConfig
from persona_connectors.domain.flow import SharedInboundFlow
from persona_connectors.domain.linking import LinkingService
from persona_connectors.domain.resolution import InboundIdentityResolver
from persona_connectors.email.app import build_email_app
from persona_connectors.email.connector import EmailConnector
from persona_connectors.email.flow import EmailInboundFlow
from persona_connectors.email.linking import EmailLinkingService
from persona_connectors.errors import ConnectorError
from persona_connectors.infra import PostgresConversationStateStore, PostgresLinkStore
from persona_connectors.sms.cost import record_sms_cost
from persona_connectors.telegram import (
    InboundFlow as TelegramInboundFlow,
)
from persona_connectors.telegram import (
    TelegramClient,
    TelegramConnector,
    TelegramLinkingService,
    build_telegram_app,
    run_long_poll,
)

if TYPE_CHECKING:
    from collections.abc import (
        Awaitable,
        Callable,
        Collection,
        Coroutine,
        Mapping,
        Sequence,
    )
    from typing import Any

    from fastapi import FastAPI
    from persona.auth.jwt_verifier import AuthenticatedUser
    from persona.delivery import MessageDeliverer
    from persona_api.editions.credits_policy import CreditsPolicy
    from pydantic import SecretStr
    from sqlalchemy.engine import Engine

    from persona_connectors.domain.conversation_model import ConversationStateStore
    from persona_connectors.domain.flow import TurnRequest

_log = get_logger("connectors.service")
_IDLE_SWEEP_INTERVAL_SECONDS = 300  # run the lazy-expiry backstop every 5 minutes
_HTTP_PORT = 8080  # the single HTTP-serving transport's port (webhook / Slack events)


def _build_runtime_factory(
    api_config: APIConfig, rls_engine: Engine, *, credits_policy: CreditsPolicy
) -> RuntimeFactory:
    """Build the reused api runtime (mirrors app.py's lifespan, community + cloud).

    The deploy seam — torch (embedder) + model backends load here from the live env.
    Code-execution + image backends are off for the connector v1 (text-to-text).

    R9-074 — model-selection parity: a connector turn must resolve the SAME tier
    registry a web-chat turn does. Two things were missing and both defaulted the
    dangerous way. The OpenRouter subscription mode was never resolved, so the
    ``:free``-suffix filter never applied to the paid tiers; and ``free_tier_registry``
    was never passed at all, which ``RuntimeFactory._plan_tier_selection`` reads as
    *"plan gating is OFF"* — so every free-plan user was served the PAID tiers on
    every connector. Both now come from the shared
    :mod:`persona_api.services.model_tiers` helpers the api's own lifespan calls, so
    the two composition roots cannot drift apart again. Community is unaffected:
    :func:`~persona_api.services.model_tiers.build_free_tier_registry` returns ``None``
    outside the cloud edition, which is the byte-identical ungated path.

    Args:
        api_config: The resolved api config for this boot.
        rls_engine: The RLS-scoped engine every owner-scoped read/write runs on.
        credits_policy: The edition's credits policy, shared with the chat-turn
            registry so both halves of a turn meter through one object (as in api).

    Returns:
        The composed :class:`~persona_api.services.runtime_factory.RuntimeFactory`.
    """
    embedder = persona_service.default_embedder(api_config.embedder_model)
    if api_config.edition is Edition.community:
        memory_backend: ChromaBackend | PostgresBackend = ChromaBackend(
            persist_path=Path(api_config.community_memory_path), embedder=embedder
        )
    else:
        memory_backend = PostgresBackend(engine=rls_engine, embedder=embedder)
    openrouter_mode = resolve_openrouter_subscription_mode()
    return RuntimeFactory(
        rls_engine=rls_engine,
        embedder=embedder,
        tier_registry=tier_registry_from_env(openrouter_subscription_mode=openrouter_mode),
        free_tier_registry=build_free_tier_registry(
            api_config, openrouter_subscription_mode=openrouter_mode
        ),
        turn_log_writer=PostgresTurnLogWriter(rls_engine),
        audit_root=Path(api_config.audit_root),
        workspace_root=Path(api_config.workspace_root),
        api_config=api_config,
        credits_policy=credits_policy,
        memory_backend=memory_backend,
    )


async def _run_idle_sweep(
    store: PostgresConversationStateStore,
    idle_after: timedelta,
    exclude_platforms: Collection[str] = (),
) -> None:
    """Periodically end genuinely-idle conversations (the lazy-expiry backstop, §3).

    ``exclude_platforms`` (Spec C5, D-C5-2) is the set of native-boundary platforms
    (email) that opt out of the sweep — their conversation boundary is the thread,
    not idleness. Composition-supplied; empty = today's behavior (all swept).
    """
    while True:
        await asyncio.sleep(_IDLE_SWEEP_INTERVAL_SECONDS)
        try:
            ended = store.sweep_idle_conversations(
                now=datetime.now(UTC), idle_after=idle_after, exclude_platforms=exclude_platforms
            )
            if ended:
                _log.info("idle sweep ended {count} conversation(s)", count=ended)
        except Exception as exc:  # noqa: BLE001 — a sweep fault must not kill the service
            _log.warning("idle sweep failed: {error}", error=str(exc))


async def _serve_app(app: FastAPI, *, port: int) -> None:
    """Serve an ASGI app on ``port`` (the HTTP transport runner — webhook / Slack events).

    ``proxy_headers`` + ``forwarded_allow_ips`` are LOAD-BEARING, not hygiene (mirrors
    the api image's ``--proxy-headers --forwarded-allow-ips=*``): every deployment
    terminates TLS at a proxy (Fly) and forwards plain HTTP here. Twilio signs the
    REQUEST URL (``verify_twilio_signature(auth_token, str(request.url), …)`` —
    ``_twilio/app.py``), so without these uvicorn reconstructs ``http://…`` while
    Twilio signed ``https://…``, the signatures never match, and EVERY inbound
    WhatsApp/SMS webhook is rejected 403 — observed in production 2026-07-29.
    """
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="0.0.0.0",  # noqa: S104 — container-bound
            port=port,
            log_level="info",
            proxy_headers=True,
            forwarded_allow_ips="*",
        )
    )
    await server.serve()


# R9-073c: a crashed runner is RESTARTED, not left dead — exponential backoff from 1s
# (fast recovery from a one-off blip: a transient network drop, a momentary provider
# hiccup) doubling up to a 60s cap (so a persistently-failing runner doesn't hot-spin the
# reconnect/retry — the same order of magnitude as the gateway/socket-mode reconnect
# backoffs already in this codebase, kept small enough that a genuinely-transient outage
# self-heals inside a couple of minutes).
_RESTART_BACKOFF_INITIAL_SECONDS = 1.0
_RESTART_BACKOFF_MAX_SECONDS = 60.0
_RESTART_BACKOFF_MULTIPLIER = 2.0
# A runner that stayed up at least this long before crashing was genuinely healthy — that
# fault resets the failure count/backoff to a fresh start rather than counting toward the
# ceiling below. Without this, a platform that has run flawlessly for months would
# eventually trip the ceiling on nothing but ordinary, widely-spaced transient faults;
# WITH it, only a runner that keeps crashing IN QUICK SUCCESSION (i.e. is genuinely,
# persistently broken — a revoked token, a permanently-rejecting API) can ever reach it.
_RESTART_HEALTHY_UPTIME_SECONDS = 120.0
# After this many crashes IN A ROW (each arriving before the runner proved itself healthy),
# stop restarting and leave the platform down for the rest of the process's lifetime. A
# broken credential or a permanently-rejecting API must not retry forever, spamming the
# log and burning reconnect attempts — but it IS given a real chance first: the backoff
# schedule from attempt 1 through this ceiling sums to ~1+2+4+8+16+32+60+60+60+60s, close
# to 5 minutes of retrying before giving up.
_RESTART_MAX_CONSECUTIVE_FAILURES = 10


async def _supervised(
    name: str,
    make_runner: Callable[[], Coroutine[object, object, None]],
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """Run one platform's runner; RESTART a crash with backoff (R9-071 contain, R9-073c heal).

    ``asyncio.gather(*runners)`` propagates the FIRST exception raised by ANY awaitable it
    is given — without containment, a bug in one platform's runner (observed: a Discord
    gateway heartbeat send racing a normal 1000-close; separately, a provider rejecting an
    unavailable model) would end every OTHER runner too, including the shared HTTP server
    (link routes, OAuth callbacks, webhooks), so every platform stops responding along with
    the crashed one. That containment alone left the crashed platform dead for the rest of
    the process's life (observed in production, 2026-07-29: Telegram never came back after
    one bad turn) — every later message on that platform was silently dropped. This SELF-
    HEALS instead: on a crash, ``make_runner`` is called again (a fresh coroutine — a bare
    ``Coroutine`` can only be awaited once, so the caller supplies a zero-argument FACTORY,
    not an already-created one) after an exponential backoff, up to
    :data:`_RESTART_MAX_CONSECUTIVE_FAILURES` consecutive failures, after which this
    platform stays down (logged loudly) while every other platform keeps serving.
    ``asyncio.CancelledError`` (the deploy SIGINT shutdown path) is always re-raised, never
    swallowed — a clean shutdown must still cancel everything together, exactly as before.
    """
    consecutive_failures = 0
    backoff = _RESTART_BACKOFF_INITIAL_SECONDS
    while True:
        started_at = monotonic()
        try:
            await make_runner()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — contain the crash, restart instead of dying
            if monotonic() - started_at >= _RESTART_HEALTHY_UPTIME_SECONDS:
                consecutive_failures = 0
                backoff = _RESTART_BACKOFF_INITIAL_SECONDS
            consecutive_failures += 1
            if consecutive_failures > _RESTART_MAX_CONSECUTIVE_FAILURES:
                _log.error(
                    "connector runner '{name}' crashed {count} times in a row ({error}) — "
                    "giving up; this platform stays DOWN for the rest of the process's "
                    "lifetime, other platforms keep serving",
                    name=name,
                    count=consecutive_failures,
                    error=str(exc),
                )
                return
            _log.error(
                "connector runner '{name}' exited unexpectedly ({error}) — restarting in "
                "{backoff:.1f}s (attempt {count}/{ceiling}); other platforms keep serving",
                name=name,
                error=str(exc),
                backoff=backoff,
                count=consecutive_failures,
                ceiling=_RESTART_MAX_CONSECUTIVE_FAILURES,
            )
            await sleep(backoff)
            backoff = min(backoff * _RESTART_BACKOFF_MULTIPLIER, _RESTART_BACKOFF_MAX_SECONDS)
        else:
            # Every real runner (long-poll / gateway / socket-mode / the HTTP server) loops
            # forever until `should_continue()` goes false or it's cancelled, so a normal
            # RETURN (no exception) means "genuinely done" (e.g. a test double), not a
            # crash — no restart, no error logged.
            return


def _now() -> datetime:
    return datetime.now(UTC)


_JWKS_FETCH_TIMEOUT_SECONDS = 10.0  # short — the JWKS fetch sits on the request's auth path


def _build_connector_verifier(
    config: ConnectorConfig, http: httpx.AsyncClient
) -> Callable[[str], Awaitable[AuthenticatedUser]]:
    """Select the JWT verifier: JWKS (R9-062) when configured, else the static-key default.

    ``PERSONA_CONNECTORS_JWT_JWKS_URL`` set → build a
    :func:`~persona.auth.jwt_verifier.make_jwks_verifier` that fetches (and caches) the
    JWKS from that PINNED url via the shared ``httpx.AsyncClient`` — this lets the connector
    verify Clerk tokens from ANY Clerk instance (dev + prod) and survive key rotation, instead
    of pinning to one static public key. Unset (the default) → the existing
    :func:`~persona.auth.jwt_verifier.make_jwt_verifier` static verifier, byte-identical to
    every deployment predating this option.
    """
    if config.jwt_jwks_url is None:
        return make_jwt_verifier(config)
    jwks_url = config.jwt_jwks_url

    async def fetch_jwks() -> dict[str, Any]:
        response = await http.get(jwks_url, timeout=_JWKS_FETCH_TIMEOUT_SECONDS)
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        return payload

    return make_jwks_verifier(
        jwks_url=jwks_url,
        audience=config.jwt_audience,
        algorithms=config.jwt_algorithms_list,
        fetch_jwks=fetch_jwks,
    )


async def _setup_telegram(
    *,
    config: ConnectorConfig,
    token: SecretStr,
    http: httpx.AsyncClient,
    linking_service: LinkingService,
    resolver: InboundIdentityResolver,
    conversation_store: ConversationStateStore,
    list_persona_names: Callable[[str], Mapping[str, Sequence[str]]],
    run_turn: Callable[[TurnRequest], Awaitable[str]],
    owner_scope: Callable[[str], contextlib.AbstractContextManager[None]],
) -> tuple[MessageDeliverer, FastAPI, Callable[[], Coroutine[object, object, None]] | None]:
    """Assemble the Telegram adapter → (deliverer, HTTP app, inbound-transport runner factory).

    The HTTP app — the webhook receiver AND the authenticated
    ``/v1/connectors/telegram/link`` issue route — is built UNCONDITIONALLY (R9-061):
    the web front-door calls the link route regardless of which transport receives
    Telegram's own messages. The runner is the long-poll loop in ``longpoll`` mode;
    in ``webhook`` mode inbound delivery IS the HTTP app being served (via
    ``http_apps`` in ``_amain``), so there is no separate runner (``None``). The runner is
    a zero-argument FACTORY (R9-073c), not an already-created coroutine — ``_supervised``
    restarts a crashed platform by calling it again, and a bare ``Coroutine`` can only ever
    be awaited once.
    """
    client = TelegramClient(bot_token=token, http=http, api_base_url=config.telegram_api_base_url)
    bot_username = config.telegram_bot_username or await _telegram_username(client)
    telegram_linking = TelegramLinkingService(linking=linking_service, bot_username=bot_username)
    connector = TelegramConnector(
        client=client, conversation_store=conversation_store, owner_scope=owner_scope
    )
    flow = TelegramInboundFlow(
        resolver=resolver,
        linking=telegram_linking,
        conversation_store=conversation_store,
        connector=connector,
        client=client,
        list_persona_names=list_persona_names,
        run_turn=run_turn,
        now=_now,
    )
    ttl = timedelta(minutes=config.telegram_link_token_ttl_minutes)

    async def issue_deep_link(owner_id: str) -> str:
        return telegram_linking.issue_deep_link(owner_id=owner_id, now=_now(), ttl=ttl)

    secret = config.telegram_webhook_secret
    app = build_telegram_app(
        webhook_secret=secret,
        on_update=flow.handle,
        issue_deep_link=issue_deep_link,
        verify_jwt=_build_connector_verifier(config, http),
        link_ttl=ttl,  # C6-D-8: the issue response's server-authoritative expires_at
        now=_now,
    )
    if config.telegram_transport == "webhook":
        await client.set_webhook(
            url=config.telegram_webhook_url,
            secret_token=secret.get_secret_value() if secret is not None else None,
            allowed_updates=["message"],
        )
        return connector, app, None
    await client.delete_webhook()  # ensure no webhook competes with long-poll
    runner = functools.partial(
        run_long_poll,
        client=client,
        on_update=flow.handle,
        timeout=config.telegram_longpoll_timeout_seconds,
    )
    return connector, app, runner


async def _telegram_username(client: TelegramClient) -> str:
    me = await client.get_me()
    username = me.get("username")
    if not isinstance(username, str) or not username:
        raise ConnectorError("could not resolve the Telegram bot username via getMe")
    return username


async def _setup_discord(
    *,
    config: ConnectorConfig,
    token: SecretStr,
    http: httpx.AsyncClient,
    linking_service: LinkingService,
    resolver: InboundIdentityResolver,
    conversation_store: ConversationStateStore,
    list_persona_names: Callable[[str], Mapping[str, Sequence[str]]],
    run_turn: Callable[[TurnRequest], Awaitable[str]],
    owner_scope: Callable[[str], contextlib.AbstractContextManager[None]],
) -> tuple[MessageDeliverer, FastAPI, Callable[[], Coroutine[object, object, None]]]:
    """Assemble the Discord adapter → (deliverer, OAuth-link HTTP app, gateway runner factory).

    Discord's inbound message transport is ALWAYS the gateway WebSocket (there is no
    HTTP alternative), but the OAuth ``/v1/connectors/discord/link`` issue route + the
    ``/discord/oauth/callback`` route are the web front-door's account-linking carrier
    and are built UNCONDITIONALLY here (R9-061), independent of the gateway runner. The
    runner is the ``gateway.run`` BOUND METHOD, not a called coroutine (R9-073c) —
    ``_supervised`` restarts a crashed gateway by calling it again; the same
    ``DiscordGateway`` instance is reused across restarts, so its session state (for a
    RESUME rather than a cold IDENTIFY) survives the restart too.
    """
    client = discord_adapter.DiscordClient(
        bot_token=token, http=http, api_base_url=config.discord_api_base_url
    )
    me = await client.get_current_user()
    bot_user_id = me.get("id")
    if not isinstance(bot_user_id, str) or not bot_user_id:
        raise ConnectorError("could not resolve the Discord bot user id via /users/@me")
    connector = discord_adapter.DiscordConnector(
        client=client, conversation_store=conversation_store, owner_scope=owner_scope
    )
    flow = discord_adapter.InboundFlow(
        resolver=resolver,
        conversation_store=conversation_store,
        connector=connector,
        client=client,
        list_persona_names=list_persona_names,
        run_turn=run_turn,
        now=_now,
        bot_user_id=bot_user_id,
    )
    gateway = discord_adapter.DiscordGateway(
        token=token,
        on_event=flow.handle,
        connect=_gateway_connect,
        gateway_url=config.discord_gateway_url,
    )
    client_secret = config.discord_oauth_client_secret
    if (
        client_secret is None
        or not config.discord_oauth_client_id
        or not config.discord_oauth_redirect_uri
    ):
        raise ConnectorError(
            "Discord account linking requires PERSONA_CONNECTORS_DISCORD_OAUTH_"
            "{CLIENT_ID,CLIENT_SECRET,REDIRECT_URI} to be set"
        )
    oauth_client = discord_adapter.DiscordOAuthClient(
        client_id=config.discord_oauth_client_id,
        client_secret=client_secret,
        redirect_uri=config.discord_oauth_redirect_uri,
        http=http,
        api_base_url=config.discord_api_base_url,
    )
    discord_linking = discord_adapter.DiscordLinkingService(
        linking=linking_service,
        oauth=oauth_client,
        client_id=config.discord_oauth_client_id,
        redirect_uri=config.discord_oauth_redirect_uri,
    )
    ttl = timedelta(minutes=config.discord_link_token_ttl_minutes)

    async def issue_authorize_url(owner_id: str) -> str:
        return discord_linking.issue_authorize_url(owner_id=owner_id, now=_now(), ttl=ttl)

    async def complete_oauth(code: str, state: str) -> str:
        return await discord_linking.complete_oauth(code=code, state=state, now=_now())

    app = discord_adapter.build_discord_app(
        issue_authorize_url=issue_authorize_url,
        complete_oauth=complete_oauth,
        verify_jwt=_build_connector_verifier(config, http),
        link_ttl=ttl,  # C6-D-8: the issue response's server-authoritative expires_at
        now=_now,
    )
    return connector, app, gateway.run


async def _setup_slack(
    *,
    config: ConnectorConfig,
    token: SecretStr,
    http: httpx.AsyncClient,
    linking_service: LinkingService,
    resolver: InboundIdentityResolver,
    conversation_store: ConversationStateStore,
    list_persona_names: Callable[[str], Mapping[str, Sequence[str]]],
    run_turn: Callable[[TurnRequest], Awaitable[str]],
    owner_scope: Callable[[str], contextlib.AbstractContextManager[None]],
) -> tuple[MessageDeliverer, FastAPI, Callable[[], Coroutine[object, object, None]] | None]:
    """Assemble the Slack adapter → (deliverer, HTTP app, socket-mode runner factory).

    The OAuth ``/v1/connectors/slack/link`` issue route + the ``/slack/oauth/callback``
    route are the web front-door's account-linking carrier and are built
    UNCONDITIONALLY (R9-061), independent of the event transport. In ``http`` transport
    mode the signed ``/slack/events`` route is mounted onto the SAME app (served via
    ``http_apps`` in ``_amain``, no separate runner — ``None``); in ``socket`` mode the
    events route is never mounted (D-C3-2 — it stays HTTP-transport-only) and the
    runner is the ``socket.run`` BOUND METHOD, not a called coroutine (R9-073c) —
    ``_supervised`` restarts a crashed socket by calling it again.

    R9-067 fail-fast: a Slack bot token configured with an empty
    ``PERSONA_CONNECTORS_SLACK_SCOPE`` can only ever produce Slack's own "No
    scopes requested" rejection on the authorize page — never a working install
    link — so that misconfiguration is refused HERE, at startup, rather than
    reaching a user as a broken link.
    """
    if not config.slack_scope:
        raise ConnectorError(
            "Slack is configured (a bot token is set) but "
            "PERSONA_CONNECTORS_SLACK_SCOPE is empty — Slack's authorize page "
            "rejects an install with no scopes requested, so this must never "
            "reach a user as a broken install link"
        )
    client = slack_adapter.SlackClient(
        bot_token=token, http=http, api_base_url=config.slack_api_base_url
    )
    auth = await client.auth_test()
    bot_user_id = auth.get("user_id")
    if not isinstance(bot_user_id, str) or not bot_user_id:
        raise ConnectorError("could not resolve the Slack bot user id via auth.test")
    connector = slack_adapter.SlackConnector(
        client=client, conversation_store=conversation_store, owner_scope=owner_scope
    )
    flow = slack_adapter.InboundFlow(
        resolver=resolver,
        conversation_store=conversation_store,
        connector=connector,
        client=client,
        list_persona_names=list_persona_names,
        run_turn=run_turn,
        now=_now,
        bot_user_id=bot_user_id,
    )
    client_secret = config.slack_oauth_client_secret
    if (
        client_secret is None
        or not config.slack_oauth_client_id
        or not config.slack_oauth_redirect_uri
    ):
        raise ConnectorError(
            "Slack account linking requires PERSONA_CONNECTORS_SLACK_OAUTH_"
            "{CLIENT_ID,CLIENT_SECRET,REDIRECT_URI} to be set"
        )
    oauth_client = slack_adapter.SlackOAuthClient(
        client_id=config.slack_oauth_client_id,
        client_secret=client_secret,
        redirect_uri=config.slack_oauth_redirect_uri,
        http=http,
        api_base_url=config.slack_api_base_url,
    )
    slack_linking = slack_adapter.SlackLinkingService(
        linking=linking_service,
        oauth=oauth_client,
        client_id=config.slack_oauth_client_id,
        redirect_uri=config.slack_oauth_redirect_uri,
        scope=config.slack_scope,
        user_scope=config.slack_user_scope,
    )
    ttl = timedelta(minutes=config.slack_link_token_ttl_minutes)

    async def issue_authorize_url(owner_id: str) -> str:
        return slack_linking.issue_authorize_url(owner_id=owner_id, now=_now(), ttl=ttl)

    async def complete_oauth(code: str, state: str) -> str:
        return await slack_linking.complete_oauth(code=code, state=state, now=_now())

    app = slack_adapter.build_slack_app(
        issue_authorize_url=issue_authorize_url,
        complete_oauth=complete_oauth,
        verify_jwt=_build_connector_verifier(config, http),
        link_ttl=ttl,  # C6-D-8: the issue response's server-authoritative expires_at
        now=_now,
    )
    if config.slack_transport == "socket":
        app_token = config.slack_app_token
        if app_token is None:
            raise ConnectorError("PERSONA_CONNECTORS_SLACK_APP_TOKEN is required for socket mode")
        socket = slack_adapter.SlackSocketClient(
            app_token=app_token,
            http=http,
            on_event=flow.handle,
            connect=_socket_connect,
            api_base_url=config.slack_api_base_url,
        )
        return connector, app, socket.run
    events_app = slack_adapter.build_events_app(
        signing_secret=config.slack_signing_secret, on_event=flow.handle, now=_now
    )
    # Mount the HTTP-events route onto the SAME app as the OAuth link/callback routes
    # (one platform → one http_apps entry) — no path collision (/slack/events vs
    # /v1/connectors/slack/link + /slack/oauth/callback).
    app.router.routes.extend(events_app.router.routes)
    return connector, app, None


def _build_twilio_client(config: ConnectorConfig, http: httpx.AsyncClient) -> TwilioClient:
    """Build the shared Twilio client (one account drives BOTH channels — D-C4-1)."""
    if config.twilio_auth_token is None or not config.twilio_account_sid:
        raise ConnectorError(
            "a Twilio channel is configured but "
            "PERSONA_CONNECTORS_TWILIO_ACCOUNT_SID / _AUTH_TOKEN are not set"
        )
    return TwilioClient(
        account_sid=config.twilio_account_sid,
        auth_token=config.twilio_auth_token,
        http=http,
        api_base_url=config.twilio_api_base_url,
    )


async def _setup_whatsapp(
    *,
    config: ConnectorConfig,
    twilio_client: TwilioClient,
    http: httpx.AsyncClient,
    linking_service: LinkingService,
    resolver: InboundIdentityResolver,
    conversation_store: ConversationStateStore,
    list_persona_names: Callable[[str], Mapping[str, Sequence[str]]],
    run_turn: Callable[[TurnRequest], Awaitable[str]],
    owner_scope: Callable[[str], contextlib.AbstractContextManager[None]],
    emit_message_received: Callable[..., None] | None = None,
) -> tuple[MessageDeliverer, FastAPI]:
    """Assemble the WhatsApp adapter → (deliverer, the Twilio webhook/status/issue app)."""
    connector = whatsapp_adapter.WhatsAppConnector(
        client=twilio_client,
        from_address=config.twilio_whatsapp_from,
        conversation_store=conversation_store,
        owner_scope=owner_scope,
        reengagement_template_sid=config.whatsapp_reengagement_template_sid,
    )
    transport = whatsapp_adapter.WhatsAppFlowTransport(
        client=twilio_client, connector=connector, from_address=config.twilio_whatsapp_from
    )
    phone_linking = PhoneLinkingService(linking=linking_service, platform=whatsapp_adapter.PLATFORM)
    shared = SharedInboundFlow(
        resolver=resolver,
        conversation_store=conversation_store,
        list_persona_names=list_persona_names,
        run_turn=run_turn,
        emit_message_received=emit_message_received,
    )
    flow = PhoneInboundFlow(
        platform=whatsapp_adapter.PLATFORM,
        classify_inbound=whatsapp_adapter.classify_inbound,
        inbound_text_type=whatsapp_adapter.InboundText,
        inbound_non_text_type=whatsapp_adapter.InboundNonText,
        decline_message=whatsapp_adapter.decline_message,
        linking=phone_linking,
        shared=shared,
        transport=transport,
        now=_now,
    )

    async def on_status(params: Mapping[str, str]) -> None:
        # WhatsApp carries no per-segment cost; map the delivery signal for observability.
        callback = parse_status_callback(params)
        result = map_delivery(
            channel=whatsapp_adapter.PLATFORM,
            status=callback.status,
            error_code=callback.error_code,
        )
        _log.info(
            "whatsapp delivery (sid={sid} outcome={outcome} detail={detail})",
            sid=callback.message_sid,
            outcome=result.outcome,
            detail=result.detail,
        )

    return connector, _build_phone_app(
        config=config,
        http=http,
        platform=whatsapp_adapter.PLATFORM,
        destination=config.twilio_whatsapp_from,
        phone_linking=phone_linking,
        on_inbound=flow.handle,
        on_status=on_status,
    )


async def _setup_sms(
    *,
    config: ConnectorConfig,
    twilio_client: TwilioClient,
    http: httpx.AsyncClient,
    linking_service: LinkingService,
    resolver: InboundIdentityResolver,
    conversation_store: ConversationStateStore,
    list_persona_names: Callable[[str], Mapping[str, Sequence[str]]],
    run_turn: Callable[[TurnRequest], Awaitable[str]],
    owner_scope: Callable[[str], contextlib.AbstractContextManager[None]],
    emit_message_received: Callable[..., None] | None = None,
) -> tuple[MessageDeliverer, FastAPI]:
    """Assemble the SMS adapter → (deliverer, the Twilio webhook/status/issue app)."""
    connector = sms_adapter.SmsConnector(
        client=twilio_client,
        from_address=config.twilio_sms_from,
        conversation_store=conversation_store,
        owner_scope=owner_scope,
        max_segments=config.sms_max_segments,
    )
    transport = sms_adapter.SmsFlowTransport(
        client=twilio_client, connector=connector, from_address=config.twilio_sms_from
    )
    phone_linking = PhoneLinkingService(linking=linking_service, platform=sms_adapter.PLATFORM)
    shared = SharedInboundFlow(
        resolver=resolver,
        conversation_store=conversation_store,
        list_persona_names=list_persona_names,
        run_turn=run_turn,
        emit_message_received=emit_message_received,
    )
    flow = PhoneInboundFlow(
        platform=sms_adapter.PLATFORM,
        classify_inbound=sms_adapter.classify_inbound,
        inbound_text_type=sms_adapter.InboundText,
        inbound_non_text_type=sms_adapter.InboundNonText,
        decline_message=sms_adapter.decline_message,
        linking=phone_linking,
        shared=shared,
        transport=transport,
        now=_now,
    )

    async def on_status(params: Mapping[str, str]) -> None:
        # SMS is the one channel where verbosity costs money — record the per-segment cost
        # from the same status callback (the T12 single-ingestion seam).
        record_sms_cost(params)

    return connector, _build_phone_app(
        config=config,
        http=http,
        platform=sms_adapter.PLATFORM,
        destination=config.twilio_sms_from,
        phone_linking=phone_linking,
        on_inbound=flow.handle,
        on_status=on_status,
    )


async def _setup_email(
    *,
    config: ConnectorConfig,
    token: SecretStr,
    http: httpx.AsyncClient,
    linking_service: LinkingService,
    resolver: InboundIdentityResolver,
    conversation_store: ConversationStateStore,
    list_persona_names: Callable[[str], Mapping[str, Sequence[str]]],
    run_turn: Callable[[TurnRequest], Awaitable[str]],
    rls_engine: Engine,
    owner_scope: Callable[[str], contextlib.AbstractContextManager[None]],
    emit_message_received: Callable[..., None] | None = None,
) -> tuple[MessageDeliverer, FastAPI]:
    """Assemble the email adapter → (deliverer, the Postmark webhook + issue app, Spec C5)."""
    client = PostmarkClient(
        server_token=token, http=http, api_base_url=config.postmark_api_base_url
    )
    connector = EmailConnector(
        client=client,
        from_address=config.email_inbound_address,
        recipient_for=build_email_recipient_resolver(
            rls_engine=rls_engine, owner_scope=owner_scope
        ),
    )
    email_linking = EmailLinkingService(linking=linking_service)
    shared = SharedInboundFlow(
        resolver=resolver,
        conversation_store=conversation_store,
        list_persona_names=list_persona_names,
        run_turn=run_turn,
        emit_message_received=emit_message_received,
    )
    flow = EmailInboundFlow(connector=connector, linking=email_linking, shared=shared, now=_now)
    ttl = timedelta(minutes=config.email_link_token_ttl_minutes)

    async def issue_code(owner_id: str) -> str:
        return email_linking.issue_code(owner_id=owner_id, now=_now(), ttl=ttl)

    # B1 fail-closed: no configured Basic-Auth credential → every inbound is rejected.
    webhook_auth = (
        PostmarkWebhookAuth(
            username=config.postmark_webhook_username, password=config.postmark_webhook_password
        )
        if config.postmark_webhook_password is not None
        else None
    )
    app = build_email_app(
        webhook_auth=webhook_auth,
        on_inbound=flow.handle,
        issue_code=issue_code,
        verify_jwt=_build_connector_verifier(config, http),
        # C6-D-7: the PUBLIC inbound address the reversed flow shows ("email the code to …");
        # C6-D-8: expires_at = issue_time + ttl (single-source, from config).
        destination=config.email_inbound_address,
        link_ttl=ttl,
        now=_now,
    )
    return connector, app


def _build_phone_app(
    *,
    config: ConnectorConfig,
    http: httpx.AsyncClient,
    platform: str,
    destination: str,
    phone_linking: PhoneLinkingService,
    on_inbound: Callable[[Mapping[str, str]], Awaitable[None]],
    on_status: Callable[[Mapping[str, str]], Awaitable[None]],
) -> FastAPI:
    """Build a phone channel's Twilio app: bind ``issue_code`` + the JWT verifier (api-free).

    ``destination`` is the channel's own PUBLIC ``From`` number (``twilio_{whatsapp,sms}_from``)
    the reversed C4 flow shows the user (C6-D-7); ``link_ttl`` sets the issue response's
    server-authoritative ``expires_at`` (C6-D-8).
    """
    ttl = timedelta(minutes=config.phone_link_token_ttl_minutes)

    async def issue_code(owner_id: str) -> str:
        return phone_linking.issue_code(owner_id=owner_id, now=_now(), ttl=ttl)

    return build_twilio_app(
        platform=platform,
        auth_token=config.twilio_webhook_auth_token,
        on_inbound=on_inbound,
        on_status=on_status,
        issue_code=issue_code,
        verify_jwt=_build_connector_verifier(config, http),
        destination=destination,
        link_ttl=ttl,
        now=_now,
    )


async def _gateway_connect(url: str) -> discord_adapter.GatewayConnection:
    """Open a Discord gateway WebSocket (the injected connect factory).

    The ``websockets`` ``ClientConnection`` satisfies the ``GatewayConnection`` protocol
    structurally (async ``send``/``recv``/``close``).
    """
    return await ws_connect(url)


async def _socket_connect(url: str) -> slack_adapter.SlackSocketConnection:
    """Open a Slack socket-mode WebSocket (the injected connect factory).

    The ``websockets`` ``ClientConnection`` satisfies ``SlackSocketConnection`` structurally.
    """
    return await ws_connect(url)


async def _amain() -> None:
    config = ConnectorConfig()
    api_config = APIConfig()
    composition = ConnectorComposition(config)
    rls_engine = composition.make_engine()
    dispatch_engine = composition.make_dispatch_engine()

    # The reused api runtime + the injected flow callables (owner-scoped) — built once,
    # shared by every adapter.
    #
    # R9-079 billing parity: the edition's credits policy + Stripe gateway are built from
    # the SAME api factories app.py's create_app uses (``app.state.credits_policy`` /
    # ``app.state.stripe_gateway``), and the ONE policy object is shared by the runtime
    # factory and the chat-turn registry exactly as it is in the api. The job queue rides
    # the cross-tenant dispatch engine (the connector's counterpart to api's admin engine),
    # so a connector turn also enqueues turn-boundary synthesis like a web turn.
    credits_policy = build_credits_policy(api_config)
    runtime_factory = _build_runtime_factory(api_config, rls_engine, credits_policy=credits_policy)
    # R9-081: the worker side of the conversational verbs. The runtime this process
    # builds emits them unconditionally, so without these a persona confirmed a task
    # over Telegram, said "Done, I've set that up", and nothing was created. Reschedule
    # and initiative come from the SAME builder app.py uses, so the two roots cannot
    # drift apart the way the billing set once did (R9-074 / R9-079).
    _verb_services = build_conversational_verb_services(rls_engine=rls_engine, config=api_config)
    # Steering is built here rather than in the shared builder because the api gives it
    # a failure notifier and this process has none: pause / resume / cancel all take
    # effect, and only the narration of a cancel that did NOT take is unavailable (it
    # still logs). That is a real difference, so it is stated rather than hidden.
    task_steering_service = TaskSteeringService(tasks=TaskStore(rls_engine))
    run_turn = build_reply_runner(
        runtime_factory=runtime_factory,
        rls_engine=rls_engine,
        owner_scope=composition.owner_scope,
        api_config=api_config,
        credits_policy=credits_policy,
        gateway=build_stripe_gateway(api_config),
        job_queue=JobQueue(dispatch_engine),
        task_steering_service=task_steering_service,
        task_reschedule_service=_verb_services.reschedule,
        initiative_verb_service=_verb_services.initiative,
    )
    list_persona_names = build_persona_name_lister(
        rls_engine=rls_engine, owner_scope=composition.owner_scope
    )
    link_store = PostgresLinkStore(rls_engine=rls_engine, dispatch_engine=dispatch_engine)
    # A7 (T6/T8): the event-trigger emission seam — built ONCE here (this is the api-coupled
    # composition layer), gated on PERSONA_EVENT_TRIGGERS_ENABLED. A delivered inbound emits
    # connector.message_received; a successful link emits connector.linked; both dispatch through
    # the same real dispatcher. OFF ⇒ the callbacks are None and every flow is byte-identical.
    emit_message_received = None
    emit_linked = None
    if EventTriggerSettings().enabled:
        # A6-D-8 completeness: the inbound event path consults the owner pause (a read-only
        # kill-switch store on the connector's RLS engine) so a paused owner's triggers never fire.
        _a7_dispatcher = build_event_dispatcher(
            rls_engine=rls_engine,
            config=api_config,
            pause_check=KillSwitchStore(rls_engine).is_owner_autonomy_paused,
        )
        emit_message_received = make_message_received_emit(_a7_dispatcher)
        emit_linked = make_connector_linked_emit(_a7_dispatcher)
    linking_service = LinkingService(link_store, emit_linked=emit_linked)
    resolver = InboundIdentityResolver(linking_service)
    conversation_store = PostgresConversationStateStore(
        rls_engine=rls_engine, dispatch_engine=dispatch_engine
    )
    http = httpx.AsyncClient(timeout=httpx.Timeout(60.0))

    deliverers: dict[str, MessageDeliverer] = {}
    runners: list[Coroutine[object, object, None]] = []
    http_apps: dict[str, FastAPI] = {}  # platform → ASGI app, mounted + served once below

    if config.telegram_bot_token is not None:
        connector, app, runner = await _setup_telegram(
            config=config,
            token=config.telegram_bot_token,
            http=http,
            linking_service=linking_service,
            resolver=resolver,
            conversation_store=conversation_store,
            list_persona_names=list_persona_names,
            run_turn=run_turn,
            owner_scope=composition.owner_scope,
        )
        deliverers["telegram"] = connector
        http_apps["telegram"] = app
        if runner is not None:
            runners.append(_supervised("telegram", runner))
    if config.discord_bot_token is not None:
        connector, app, runner = await _setup_discord(
            config=config,
            token=config.discord_bot_token,
            http=http,
            linking_service=linking_service,
            resolver=resolver,
            conversation_store=conversation_store,
            list_persona_names=list_persona_names,
            run_turn=run_turn,
            owner_scope=composition.owner_scope,
        )
        deliverers["discord"] = connector
        http_apps["discord"] = app
        runners.append(_supervised("discord", runner))
    if config.slack_bot_token is not None:
        connector, app, runner = await _setup_slack(
            config=config,
            token=config.slack_bot_token,
            http=http,
            linking_service=linking_service,
            resolver=resolver,
            conversation_store=conversation_store,
            list_persona_names=list_persona_names,
            run_turn=run_turn,
            owner_scope=composition.owner_scope,
        )
        deliverers["slack"] = connector
        http_apps["slack"] = app
        if runner is not None:
            runners.append(_supervised("slack", runner))

    # The two Twilio phone channels share ONE client (one account, channel by the From
    # prefix — D-C4-1); built once, only when at least one phone channel is configured.
    if config.twilio_whatsapp_from or config.twilio_sms_from:
        twilio_client = _build_twilio_client(config, http)
        if config.twilio_whatsapp_from:
            connector, app = await _setup_whatsapp(
                config=config,
                twilio_client=twilio_client,
                http=http,
                linking_service=linking_service,
                resolver=resolver,
                conversation_store=conversation_store,
                list_persona_names=list_persona_names,
                run_turn=run_turn,
                owner_scope=composition.owner_scope,
                emit_message_received=emit_message_received,
            )
            deliverers["whatsapp"] = connector
            http_apps["whatsapp"] = app
        if config.twilio_sms_from:
            connector, app = await _setup_sms(
                config=config,
                twilio_client=twilio_client,
                http=http,
                linking_service=linking_service,
                resolver=resolver,
                conversation_store=conversation_store,
                list_persona_names=list_persona_names,
                run_turn=run_turn,
                owner_scope=composition.owner_scope,
                emit_message_received=emit_message_received,
            )
            deliverers["sms"] = connector
            http_apps["sms"] = app

    # Email (Spec C5) — enabled when the Postmark token + inbound address are set. The
    # webhook app namespaces its routes by ``/email/…`` (no collision with the phone apps).
    if config.postmark_server_token is not None and config.email_inbound_address:
        connector, app = await _setup_email(
            config=config,
            token=config.postmark_server_token,
            http=http,
            linking_service=linking_service,
            resolver=resolver,
            conversation_store=conversation_store,
            list_persona_names=list_persona_names,
            run_turn=run_turn,
            rls_engine=rls_engine,
            owner_scope=composition.owner_scope,
            emit_message_received=emit_message_received,
        )
        deliverers["email"] = connector
        http_apps["email"] = app

    if not deliverers:
        raise ConnectorError(
            "no connector configured — set at least one platform's bot token "
            "(PERSONA_CONNECTORS_{TELEGRAM,DISCORD,SLACK}_BOT_TOKEN) or a Twilio "
            "channel (PERSONA_CONNECTORS_TWILIO_{WHATSAPP,SMS}_FROM)"
        )

    # Every configured platform contributes an HTTP app — Telegram (webhook + link),
    # Discord/Slack (OAuth link + callback, + Slack's signed events route in ``http``
    # transport), the phone channels (webhook/status/issue), email (webhook/issue) —
    # regardless of that platform's own message-receiving transport (R9-061: the web
    # front-door's link route and OAuth's callback route are HTTP endpoints that must be
    # served no matter how messages themselves arrive). Each app already namespaces ALL
    # its routes by ``/{platform}/…`` / ``/v1/connectors/{platform}/link`` (e.g.
    # ``/whatsapp/webhook`` vs ``/sms/webhook`` vs ``/discord/oauth/callback``), so they
    # never collide; collect them onto ONE parent app served once on the HTTP port.
    if http_apps:
        from fastapi import FastAPI as _FastAPI

        if len(http_apps) == 1:
            parent = next(iter(http_apps.values()))
        else:
            parent = _FastAPI(title="persona-connectors (twilio)")
            for app in http_apps.values():
                parent.router.routes.extend(app.router.routes)
        runners.append(_supervised("http", functools.partial(_serve_app, parent, port=_HTTP_PORT)))

    # Register every configured connector as a C0 MessageDeliverer (criterion 6 / 8).
    build_delivery_router(
        deliverers=deliverers, rls_engine=rls_engine, home_channel=next(iter(deliverers))
    )

    idle_after = timedelta(minutes=config.idle_timeout_minutes)
    # Native-boundary platforms (email — the thread IS the conversation) opt out of the idle
    # sweep (Spec C5, D-C5-2): composition supplies the set from the registered
    # native-boundary connectors (email), so the framework store never hardcodes a platform
    # literal (D-08-3). Empty when email isn't configured ⇒ every platform swept (today).
    exclude_platforms = {email_adapter.PLATFORM} & set(deliverers)
    sweep = asyncio.create_task(_run_idle_sweep(conversation_store, idle_after, exclude_platforms))
    _log.info("connector service starting for: {platforms}", platforms=", ".join(deliverers))
    try:
        await asyncio.gather(*runners)
    finally:
        sweep.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sweep
        await http.aclose()


def main() -> None:
    """Run the connector service (the ``python -m persona_connectors`` entry)."""
    asyncio.run(_amain())


if __name__ == "__main__":
    main()

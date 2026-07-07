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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import uvicorn
from persona.auth.jwt_verifier import make_jwt_verifier
from persona.events import EventTriggerSettings
from persona.logging import get_logger
from persona.stores.chroma import ChromaBackend
from persona.stores.postgres import PostgresBackend
from persona_api.approvals.kill_switch import KillSwitchStore
from persona_api.config import APIConfig, Edition
from persona_api.editions.factory import build_credits_policy
from persona_api.events import (
    build_event_dispatcher,
    make_connector_linked_emit,
    make_message_received_emit,
)
from persona_api.services import persona_service
from persona_api.services.runtime_factory import RuntimeFactory
from persona_api.services.turn_log_writer import PostgresTurnLogWriter
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

    from fastapi import FastAPI
    from persona.delivery import MessageDeliverer
    from pydantic import SecretStr
    from sqlalchemy.engine import Engine

    from persona_connectors.domain.conversation_model import ConversationStateStore
    from persona_connectors.domain.flow import TurnRequest

_log = get_logger("connectors.service")
_IDLE_SWEEP_INTERVAL_SECONDS = 300  # run the lazy-expiry backstop every 5 minutes
_HTTP_PORT = 8080  # the single HTTP-serving transport's port (webhook / Slack events)


def _build_runtime_factory(api_config: APIConfig, rls_engine: Engine) -> RuntimeFactory:
    """Build the reused api runtime (mirrors app.py's lifespan, community + cloud).

    The deploy seam — torch (embedder) + model backends load here from the live env.
    Code-execution + image backends are off for the connector v1 (text-to-text).
    """
    embedder = persona_service.default_embedder(api_config.embedder_model)
    if api_config.edition is Edition.community:
        memory_backend: ChromaBackend | PostgresBackend = ChromaBackend(
            persist_path=Path(api_config.community_memory_path), embedder=embedder
        )
    else:
        memory_backend = PostgresBackend(engine=rls_engine, embedder=embedder)
    return RuntimeFactory(
        rls_engine=rls_engine,
        embedder=embedder,
        tier_registry=tier_registry_from_env(),
        turn_log_writer=PostgresTurnLogWriter(rls_engine),
        audit_root=Path(api_config.audit_root),
        workspace_root=Path(api_config.workspace_root),
        api_config=api_config,
        credits_policy=build_credits_policy(api_config),
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
    """Serve an ASGI app on ``port`` (the HTTP transport runner — webhook / Slack events)."""
    server = uvicorn.Server(
        uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")  # noqa: S104 — container-bound
    )
    await server.serve()


def _now() -> datetime:
    return datetime.now(UTC)


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
) -> tuple[MessageDeliverer, Coroutine[object, object, None]]:
    """Assemble the Telegram adapter → (deliverer, inbound-transport runner)."""
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
    if config.telegram_transport == "webhook":
        ttl = timedelta(minutes=config.telegram_link_token_ttl_minutes)

        async def issue_deep_link(owner_id: str) -> str:
            return telegram_linking.issue_deep_link(owner_id=owner_id, now=_now(), ttl=ttl)

        secret = config.telegram_webhook_secret
        await client.set_webhook(
            url=config.telegram_webhook_url,
            secret_token=secret.get_secret_value() if secret is not None else None,
            allowed_updates=["message"],
        )
        app = build_telegram_app(
            webhook_secret=secret,
            on_update=flow.handle,
            issue_deep_link=issue_deep_link,
            verify_jwt=make_jwt_verifier(config),
            link_ttl=ttl,  # C6-D-8: the issue response's server-authoritative expires_at
            now=_now,
        )
        return connector, _serve_app(app, port=_HTTP_PORT)
    await client.delete_webhook()  # ensure no webhook competes with long-poll
    return connector, run_long_poll(
        client=client, on_update=flow.handle, timeout=config.telegram_longpoll_timeout_seconds
    )


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
    resolver: InboundIdentityResolver,
    conversation_store: ConversationStateStore,
    list_persona_names: Callable[[str], Mapping[str, Sequence[str]]],
    run_turn: Callable[[TurnRequest], Awaitable[str]],
    owner_scope: Callable[[str], contextlib.AbstractContextManager[None]],
) -> tuple[MessageDeliverer, Coroutine[object, object, None]]:
    """Assemble the Discord adapter → (deliverer, gateway runner)."""
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
    return connector, gateway.run()


async def _setup_slack(
    *,
    config: ConnectorConfig,
    token: SecretStr,
    http: httpx.AsyncClient,
    resolver: InboundIdentityResolver,
    conversation_store: ConversationStateStore,
    list_persona_names: Callable[[str], Mapping[str, Sequence[str]]],
    run_turn: Callable[[TurnRequest], Awaitable[str]],
    owner_scope: Callable[[str], contextlib.AbstractContextManager[None]],
) -> tuple[MessageDeliverer, Coroutine[object, object, None]]:
    """Assemble the Slack adapter → (deliverer, socket-mode / HTTP-events runner)."""
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
        return connector, socket.run()
    events_app = slack_adapter.build_events_app(
        signing_secret=config.slack_signing_secret, on_event=flow.handle, now=_now
    )
    return connector, _serve_app(events_app, port=_HTTP_PORT)


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
        verify_jwt=make_jwt_verifier(config),
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
        verify_jwt=make_jwt_verifier(config),
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
    runtime_factory = _build_runtime_factory(api_config, rls_engine)
    run_turn = build_reply_runner(
        runtime_factory=runtime_factory, rls_engine=rls_engine, owner_scope=composition.owner_scope
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
        connector, runner = await _setup_telegram(
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
        runners.append(runner)
    if config.discord_bot_token is not None:
        connector, runner = await _setup_discord(
            config=config,
            token=config.discord_bot_token,
            http=http,
            resolver=resolver,
            conversation_store=conversation_store,
            list_persona_names=list_persona_names,
            run_turn=run_turn,
            owner_scope=composition.owner_scope,
        )
        deliverers["discord"] = connector
        runners.append(runner)
    if config.slack_bot_token is not None:
        connector, runner = await _setup_slack(
            config=config,
            token=config.slack_bot_token,
            http=http,
            resolver=resolver,
            conversation_store=conversation_store,
            list_persona_names=list_persona_names,
            run_turn=run_turn,
            owner_scope=composition.owner_scope,
        )
        deliverers["slack"] = connector
        runners.append(runner)

    # The two Twilio phone channels share ONE client (one account, channel by the From
    # prefix — D-C4-1); built once, only when at least one phone channel is configured.
    if config.twilio_whatsapp_from or config.twilio_sms_from:
        twilio_client = _build_twilio_client(config, http)
        if config.twilio_whatsapp_from:
            connector, app = await _setup_whatsapp(
                config=config,
                twilio_client=twilio_client,
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

    # The phone channels serve HTTP (webhook/status/issue routes). Each Twilio app already
    # namespaces ALL its routes by ``/{platform}/…`` (e.g. ``/whatsapp/webhook`` vs
    # ``/sms/webhook``), so they never collide; collect them onto ONE parent app served
    # once on the HTTP port (mirror Slack's events app being served on ``_HTTP_PORT``).
    if http_apps:
        from fastapi import FastAPI as _FastAPI

        if len(http_apps) == 1:
            parent = next(iter(http_apps.values()))
        else:
            parent = _FastAPI(title="persona-connectors (twilio)")
            for app in http_apps.values():
                parent.router.routes.extend(app.router.routes)
        runners.append(_serve_app(parent, port=_HTTP_PORT))

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

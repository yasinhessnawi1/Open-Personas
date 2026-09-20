"""The connector delivers its own originations, asserted against the REAL root (R9-081, R9-120).

The owner ruled on 2026-09-21 that the connector delivers its own, with one condition: the
same seam the api enters, not a second implementation that agrees today. Two halves have to
hold in production, and only one of them is a forwarding assertion:

1. **The channels reach the seam.** ``build_connectors`` used to call
   ``build_delivery_router`` and throw the result away, so every connector was registered
   nowhere reachable. Now it BINDS them into the registry the one origination router reads.
2. **The worker can act on a confirmed contract.** ``origination_service`` was ``None``, so a
   persona said "Done, I've set that up" on Telegram and nothing was created.

**Why this drives the real function and not a helper.** The R9-081 note says it plainly:
a harness that composes the services itself proves forwarding while remaining perfectly
compatible with the service entry never passing them, which is a fix unreachable in
production behind a green suite. R9-121 is the same lesson from the other end, where a
helper named ``_register_as_worker_root_does`` proved the mirror rather than the code. So
every assertion here runs against ``service.build_connectors`` itself, with only the heavy
collaborators injected.
"""

from __future__ import annotations

import pathlib
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import httpx
import pytest
from persona_api.config import APIConfig, Edition
from persona_api.services.origination_delivery import ChannelDeliverers
from persona_connectors import composition, service
from persona_connectors.config import ConnectorConfig

if TYPE_CHECKING:
    from pathlib import Path

    from persona_connectors.service import ConnectorsBundle

_PLATFORM_API_RESPONSES = {
    "getMe": {"ok": True, "result": {"username": "opbot", "id": "1"}},
    "users/@me": {"id": "999", "username": "opbot"},
    "auth.test": {"ok": True, "user_id": "U1", "team_id": "T1"},
}


def _handler(request: httpx.Request) -> httpx.Response:
    """One transport answering every platform's identity probe at setup."""
    url = str(request.url)
    for marker, payload in _PLATFORM_API_RESPONSES.items():
        if marker in url:
            return httpx.Response(200, json=payload)
    return httpx.Response(200, json={"ok": True, "result": {"username": "opbot", "id": "1"}})


def _connector_config(**overrides: object) -> ConnectorConfig:
    defaults: dict[str, object] = {
        "edition": "cloud",
        "jwt_secret": "test-jwt-secret",  # noqa: S106 - test literal
        "discord_oauth_client_id": "discord-client-id",
        "discord_oauth_client_secret": "discord-client-secret",  # noqa: S106 - test literal
        "discord_oauth_redirect_uri": "https://example.test/discord/oauth/callback",
        "slack_oauth_client_id": "slack-client-id",
        "slack_oauth_client_secret": "slack-client-secret",  # noqa: S106 - test literal
        "slack_oauth_redirect_uri": "https://example.test/slack/oauth/callback",
    }
    defaults.update(overrides)
    return ConnectorConfig(**defaults)  # type: ignore[arg-type]


def _api_config(tmp_path: Path) -> APIConfig:
    """A real APIConfig: the origination composition reads audit_root and edition."""
    return APIConfig(
        edition=Edition.community,
        community_db_path=str(tmp_path / "community.db"),
        community_memory_path=str(tmp_path / "memory"),
        audit_root=str(tmp_path / "audit"),
        workspace_root=str(tmp_path / "workspace"),
    )


async def _build(
    connector_config: ConnectorConfig,
    api_config: APIConfig,
    *,
    channels: ChannelDeliverers | None = None,
) -> ConnectorsBundle:
    """Drive the REAL ``build_connectors`` with every heavy collaborator injected."""
    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as http:
        return await service.build_connectors(
            connector_config=connector_config,
            api_config=api_config,
            rls_engine=MagicMock(),
            dispatch_engine=MagicMock(),
            runtime_factory=MagicMock(),
            credits_policy=MagicMock(),
            job_queue=MagicMock(),
            stripe_gateway=None,
            http=http,
            channels=channels,
        )


@pytest.mark.asyncio
async def test_the_real_root_binds_every_configured_platform_as_a_channel(
    tmp_path: Path,
) -> None:
    """THE guard: the deliverers reach the origination seam, from the real composition.

    Asserting equality with the configured platform set, rather than "something was
    bound", is what makes a dropped platform fail. A persona that can be texted on
    Slack but can only start a conversation on Telegram is the shape this catches.
    """
    config = _connector_config(
        telegram_bot_token="tg-token",  # noqa: S106 - test literal
        slack_bot_token="xoxb-token",  # noqa: S106 - test literal
        slack_app_token="xapp-token",  # noqa: S106 - test literal (socket mode needs one)
    )

    bundle = await _build(config, _api_config(tmp_path))

    assert bundle.channels.bound is True
    assert sorted(bundle.channels.snapshot()) == sorted(bundle.deliverers)
    assert sorted(bundle.channels.snapshot()) == ["slack", "telegram"]


@pytest.mark.asyncio
async def test_a_host_with_no_platform_configured_still_leaves_a_bound_registry(
    tmp_path: Path,
) -> None:
    """Empty is a stated outcome; unbound is a defect. They must never look alike.

    The embedded api can boot with the flag on and no token set (D-I1-6), and in that
    state origination must route web only BECAUSE nothing was configured, not because
    a composition root forgot to bind. Only ``bound`` can tell those apart afterwards.
    """
    bundle = await _build(_connector_config(), _api_config(tmp_path))

    assert bundle.deliverers == {}
    assert bundle.channels.bound is True
    assert bundle.channels.snapshot() == {}


@pytest.mark.asyncio
async def test_a_host_supplied_registry_is_the_one_that_gets_bound(tmp_path: Path) -> None:
    """One process, ONE registry.

    The embedded api holds its own origination service over its own registry. If
    ``build_connectors`` made a second one, that process would have two sets of channels
    and whichever service ran would decide where a persona speaks, which is the
    duplicate path the ruling's condition exists to prevent.
    """
    theirs = ChannelDeliverers()
    config = _connector_config(telegram_bot_token="tg-token")  # noqa: S106 - test literal

    bundle = await _build(config, _api_config(tmp_path), channels=theirs)

    assert bundle.channels is theirs
    assert theirs.bound is True
    assert sorted(theirs.snapshot()) == ["telegram"]


@pytest.mark.asyncio
async def test_the_real_root_wires_a_worker_that_can_act_on_a_confirmed_contract(
    tmp_path: Path,
) -> None:
    """The other R9-081 half, read off the REAL ``ChatTurnRegistry`` construction.

    A persona reaches a task-contract confirmation over Telegram whatever the worker
    holds, because the reply text is produced before the worker call and independently
    of it. With ``origination_service`` at ``None`` the user read "Done, I've set that
    up" and nothing existed: a silent lie rather than an error.
    """
    captured: list[dict[str, object]] = []
    real = composition.build_chat_turn_registry

    def _recording(**kwargs: object) -> object:
        captured.append(kwargs)
        return real(**kwargs)  # type: ignore[arg-type]

    config = _connector_config(telegram_bot_token="tg-token")  # noqa: S106 - test literal
    monkeypatched = pytest.MonkeyPatch()
    try:
        monkeypatched.setattr(composition, "build_chat_turn_registry", _recording, raising=True)
        await _build(config, _api_config(tmp_path))
    finally:
        monkeypatched.undo()

    assert len(captured) == 1, captured
    assert captured[0]["origination_service"] is not None, (
        "a task confirmed over a connector is created by nobody"
    )
    assert captured[0]["task_steering_service"] is not None, "pause / resume / cancel go nowhere"


@pytest.mark.asyncio
async def test_the_operator_switch_stops_origination_without_stopping_replies(
    tmp_path: Path,
) -> None:
    """The kill switch, and the half of it that matters: replies survive.

    The only other off switch is ``PERSONA_API_EMBED_CONNECTORS``, which takes the whole
    channel down. This one has to be narrower than that or it is not worth having, so the
    test asserts BOTH halves: nothing is bound (a persona starts nothing on Telegram) AND
    the platform is still wired to receive and answer.
    """
    api_config = _api_config(tmp_path).model_copy(update={"connector_origination_enabled": False})
    config = _connector_config(telegram_bot_token="tg-token")  # noqa: S106 - test literal

    bundle = await _build(config, api_config)

    assert bundle.channels.bound is True, "off is a declaration, not a missing bind"
    assert bundle.channels.snapshot() == {}, "a persona must not start a Telegram chat"
    assert sorted(bundle.deliverers) == ["telegram"], (
        "the switch took the whole connector down; that is the coarse switch's job"
    )
    assert "telegram" in bundle.runners, "the inbound transport must keep running"


@pytest.mark.asyncio
async def test_the_switch_is_on_by_default(tmp_path: Path) -> None:
    """A capability that ships off is how sixteen of them once ran dark here."""
    assert _api_config(tmp_path).connector_origination_enabled is True


def test_the_module_no_longer_carries_a_router_builder_that_goes_nowhere() -> None:
    """``build_delivery_router`` is gone, not merely unused.

    Leaving a builder whose result nothing consumes is how R9-120 survived three specs:
    the call looked like wiring. Its absence from the module is the cheapest possible
    statement that the binding site is the only one.
    """
    assert not hasattr(composition, "build_delivery_router")
    assert "build_delivery_router" not in composition.__all__


def test_the_service_entry_binds_before_it_can_return() -> None:
    """The ordering that stands between an unbound registry and production, stated.

    This is a cycle, not a bad order: the deliverers need ``run_turn``, which needs the
    chat turn registry, which needs the origination service, whose notifier needs the
    deliverers. So the bind is late by necessity, and what makes it safe is that it sits
    on the single path to the single ``return`` rather than inside the ``if deliverers:``
    branch it used to live in. That is an ordering, and relying on an ordering silently
    is what this project keeps paying for, so it is asserted rather than trusted:
    exactly one ``bind`` call, unconditional, textually before the only return.
    """
    module_source = pathlib.Path(service.__file__).read_text(encoding="utf-8")
    after_signature = module_source.split("async def build_connectors")[1]
    body = after_signature.split("\nasync def ")[0].split("\ndef ")[0]

    assert body.count("channels.bind(") == 1, "the bind must happen exactly once"
    assert body.index("channels.bind(") < body.index("return ConnectorsBundle"), (
        "the registry must be bound before the bundle can be handed to a host"
    )
    bind_line = next(line for line in body.splitlines() if "channels.bind(" in line)
    assert bind_line.startswith("    channels.bind("), (
        "the bind is indented into a branch, so a host can be handed an unbound registry"
    )

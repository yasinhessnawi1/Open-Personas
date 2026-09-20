"""The one origination delivery seam: binding, routing, and the refactor's no-op promise.

R9-081 / R9-120 T1. Two things are proven here and they pull in opposite directions,
which is why both are needed:

1. **Nothing changes today.** Until a composition root binds connector channels, every
   originated message routes exactly where it did before, and the resolver does not even
   touch the database. A refactor of the live origination path has to be able to say that
   and be believed.
2. **The channels are data, not logic.** Bind Telegram and the same code routes a Telegram
   conversation to Telegram, because the conversation's own row says where its reader is.
   That is the condition attached to the 2026-09-21 ruling: one seam holding a different
   set, never a second implementation that agrees by coincidence.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from persona.delivery import DeliveryOutcome, DeliveryResult
from persona.schema.origination import OriginatedMessage, PersonaIdentityTag
from persona_api.db.community import create_community_schema, ensure_owner, make_community_engine
from persona_api.db.models import connector_conversations, conversations, personas
from persona_api.services.origination_delivery import (
    ChannelDeliverers,
    DeliveryChannelsAlreadyBoundError,
    NoLiveSessions,
    build_origination_router,
    make_origination_channel_resolver,
)
from sqlalchemy import insert

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy import Engine

_OWNER = "user_alice"
_WEB_CONVERSATION = "conv_web"
_TELEGRAM_CONVERSATION = "conv_telegram"


class _RecordingDeliverer:
    """A ``MessageDeliverer`` that only remembers it was the one chosen."""

    def __init__(self, channel: str) -> None:
        self.channel = channel
        self.delivered: list[OriginatedMessage] = []

    async def deliver(self, message: OriginatedMessage) -> DeliveryResult:
        self.delivered.append(message)
        return DeliveryResult(outcome=DeliveryOutcome.DELIVERED, channel=self.channel)


class _CountingEngine:
    """Wraps an engine and counts ``begin()`` calls, to prove a query did NOT happen.

    "The resolver short-circuits" is only worth asserting if the assertion can fail, and
    a test that checks the returned channel cannot tell a skipped query from a query that
    happened to return nothing.
    """

    def __init__(self, inner: Engine) -> None:
        self._inner = inner
        self.begins = 0

    def begin(self) -> Any:  # noqa: ANN401 - a passthrough of SQLAlchemy's own context manager
        self.begins += 1
        return self._inner.begin()


def _message(conversation_id: str | None) -> OriginatedMessage:
    return OriginatedMessage(
        persona=PersonaIdentityTag(persona_id="p1", display_name="Ada"),
        owner_user_id=_OWNER,
        content="I have something for you.",
        conversation_id=conversation_id,
        created_at=datetime.now(UTC),
    )


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    """A community-schema SQLite engine holding one web and one Telegram conversation."""
    eng = make_community_engine(tmp_path / "origination.db")
    create_community_schema(eng)
    ensure_owner(eng, owner_id=_OWNER, email="alice@example.com")
    with eng.begin() as conn:
        conn.execute(
            insert(personas).values(id="p1", owner_id=_OWNER, yaml="identity:\n  name: Ada\n")
        )
        for conversation_id in (_WEB_CONVERSATION, _TELEGRAM_CONVERSATION):
            conn.execute(
                insert(conversations).values(
                    id=conversation_id, owner_id=_OWNER, persona_id="p1", title="t"
                )
            )
        conn.execute(
            insert(connector_conversations).values(
                id="cc1",
                owner_id=_OWNER,
                platform="telegram",
                channel_key="55512345",
                persona_id="p1",
                conversation_id=_TELEGRAM_CONVERSATION,
                status="active",
            )
        )
    return eng


# ---------------------------------------------------------------------------
# ChannelDeliverers: bound once, and "unbound" distinguishable from "empty"
# ---------------------------------------------------------------------------


def test_a_fresh_registry_is_unbound_and_holds_nothing() -> None:
    """Unbound must be readable as unbound.

    If ``bound`` did not exist, a root that never bound anything would be indistinguishable
    from a host with no connector configured, and the startup log could not tell anyone
    which of the two happened. That is how a capability goes dark without a red test.
    """
    channels = ChannelDeliverers()

    assert channels.bound is False
    assert channels.snapshot() == {}


def test_binding_no_channels_is_a_stated_outcome_not_an_absent_one() -> None:
    """A host with nothing configured binds an empty set, and that still counts as bound."""
    channels = ChannelDeliverers()

    channels.bind({})

    assert channels.bound is True
    assert channels.snapshot() == {}


def test_a_second_bind_is_refused_loudly() -> None:
    """Two roots binding means two roots believe they own delivery: fail at startup.

    This is the guard against the duplicate path the ruling's condition exists to prevent,
    so it must raise rather than let whichever root ran last decide where a persona speaks.
    """
    channels = ChannelDeliverers()
    channels.bind({"telegram": _RecordingDeliverer("telegram")})

    with pytest.raises(DeliveryChannelsAlreadyBoundError) as raised:
        channels.bind({"slack": _RecordingDeliverer("slack")})

    assert raised.value.context["bound_channels"] == "telegram"
    assert raised.value.context["rejected_channels"] == "slack"


def test_the_snapshot_cannot_be_mutated_back_into_the_registry() -> None:
    """A caller holding the snapshot must not be able to add a channel nobody bound."""
    channels = ChannelDeliverers()
    channels.bind({"telegram": _RecordingDeliverer("telegram")})

    channels.snapshot()["slack"] = _RecordingDeliverer("slack")

    assert sorted(channels.snapshot()) == ["telegram"]


# ---------------------------------------------------------------------------
# The resolver
# ---------------------------------------------------------------------------


def test_with_only_the_web_home_registered_the_resolver_never_queries(engine: Engine) -> None:
    """THE no-op promise: today's deployment gains neither a decision nor a query.

    Counting ``begin()`` is what makes this falsifiable. Asserting only the returned
    channel would pass just as well if the lookup ran and found nothing, which is the
    behaviour-preserving claim this refactor actually has to make.
    """
    counting = _CountingEngine(engine)
    resolve = make_origination_channel_resolver(
        rls_engine=counting,  # type: ignore[arg-type] - a begin()-only passthrough
        registered=["web"],
    )

    assert resolve(_message(_TELEGRAM_CONVERSATION)) is None
    assert counting.begins == 0


def test_a_connector_conversation_resolves_to_its_own_platform(engine: Engine) -> None:
    """The conversation row is the truth about where its reader is."""
    resolve = make_origination_channel_resolver(rls_engine=engine, registered=["web", "telegram"])

    assert resolve(_message(_TELEGRAM_CONVERSATION)) == "telegram"


def test_a_web_conversation_resolves_home_even_with_connectors_bound(engine: Engine) -> None:
    """Binding Telegram must not drag web conversations onto Telegram."""
    resolve = make_origination_channel_resolver(rls_engine=engine, registered=["web", "telegram"])

    assert resolve(_message(_WEB_CONVERSATION)) is None


def test_a_platform_no_host_registered_resolves_home(engine: Engine) -> None:
    """A conversation on a channel this process does not run falls back to the home.

    The connector was configured once and is not configured now: the message is still
    durably written, and routing it to a deliverer that does not exist here is not an
    option the router has.
    """
    resolve = make_origination_channel_resolver(rls_engine=engine, registered=["web", "slack"])

    assert resolve(_message(_TELEGRAM_CONVERSATION)) is None


def test_a_message_with_no_conversation_resolves_home(engine: Engine) -> None:
    """Nothing to look up, so nothing is looked up."""
    resolve = make_origination_channel_resolver(rls_engine=engine, registered=["web", "telegram"])

    assert resolve(_message(None)) is None


def test_a_lookup_failure_routes_home_rather_than_failing_the_message() -> None:
    """Origination is additive: a database hiccup must not lose an already-written message."""

    class _BrokenEngine:
        def begin(self) -> Any:  # noqa: ANN401 - never reached past the raise
            msg = "connection pool exhausted"
            raise RuntimeError(msg)

    resolve = make_origination_channel_resolver(
        rls_engine=_BrokenEngine(),  # type: ignore[arg-type] - deliberately broken
        registered=["web", "telegram"],
    )

    assert resolve(_message(_TELEGRAM_CONVERSATION)) is None


# ---------------------------------------------------------------------------
# The router factory
# ---------------------------------------------------------------------------


def test_the_web_home_is_always_registered(engine: Engine) -> None:
    """Routing always has a target, so the router's fail-fast home guard cannot trip here."""
    router = build_origination_router(rls_engine=engine)

    assert "web" in router._deliverers  # noqa: SLF001 - the registry is the thing under test


def test_bound_channels_are_registered_alongside_the_home(engine: Engine) -> None:
    channels = ChannelDeliverers()
    channels.bind({"telegram": _RecordingDeliverer("telegram")})

    router = build_origination_router(rls_engine=engine, channels=channels)

    assert sorted(router._deliverers) == ["telegram", "web"]  # noqa: SLF001


@pytest.mark.asyncio
async def test_a_telegram_conversation_is_delivered_by_the_telegram_deliverer(
    engine: Engine,
) -> None:
    """End to end through the real router: bind a channel and the persona speaks there.

    This is the behaviour R9-120 was: the deliverers existed, were tested, and were
    registered nowhere reachable, so a persona could answer on Telegram and never start.
    """
    telegram = _RecordingDeliverer("telegram")
    channels = ChannelDeliverers()
    channels.bind({"telegram": telegram})
    router = build_origination_router(rls_engine=engine, channels=channels)

    result = await router.deliver(_message(_TELEGRAM_CONVERSATION))

    assert [m.conversation_id for m in telegram.delivered] == [_TELEGRAM_CONVERSATION]
    assert result.channel == "telegram"


@pytest.mark.asyncio
async def test_a_channel_that_did_not_take_hands_the_message_home_once(engine: Engine) -> None:
    """The R9-120 fallback hop: one hop, to a channel the person can come back to.

    ``pending`` means "durably saved" everywhere, and on the web that also means "present
    when they next open the conversation". On Telegram nobody opens a conversation to look
    for something they were never told about, so a connector that reports it did not
    deliver hands the message to the home channel, which is where the durable record lives.
    """

    class _Refusing:
        async def deliver(self, message: OriginatedMessage) -> DeliveryResult:  # noqa: ARG002
            return DeliveryResult(
                outcome=DeliveryOutcome.PENDING,
                channel="telegram",
                detail="no connector channel for conversation",
            )

    recorded: list[dict[str, Any]] = []
    channels = ChannelDeliverers()
    channels.bind({"telegram": _Refusing()})
    router = build_origination_router(rls_engine=engine, channels=channels)
    router._record = lambda **kwargs: recorded.append(kwargs)  # noqa: SLF001 - the audit sink

    result = await router.deliver(_message(_TELEGRAM_CONVERSATION))

    # The home channel answered, so the message is saved and reachable there.
    assert result.channel == "web"
    assert len(recorded) == 1, "one message is one routing decision, however many hops"
    metadata = recorded[0]["metadata"]
    assert metadata["channel"] == "web"
    assert metadata["attempted_channel"] == "telegram"
    assert metadata["attempted_outcome"] == "pending"


@pytest.mark.asyncio
async def test_a_channel_that_delivered_is_never_hopped(engine: Engine) -> None:
    """No double delivery. The hop exists only for a channel that said it did not take."""
    hops: list[str] = []

    class _Home:
        async def deliver(self, message: OriginatedMessage) -> DeliveryResult:  # noqa: ARG002
            hops.append("web")
            return DeliveryResult(outcome=DeliveryOutcome.PENDING, channel="web")

    telegram = _RecordingDeliverer("telegram")
    channels = ChannelDeliverers()
    channels.bind({"telegram": telegram})
    router = build_origination_router(rls_engine=engine, channels=channels)
    router._deliverers["web"] = _Home()  # noqa: SLF001 - observe the home, do not re-route
    router._record = lambda **kwargs: None  # noqa: SLF001, ARG005

    result = await router.deliver(_message(_TELEGRAM_CONVERSATION))

    assert result.outcome is DeliveryOutcome.DELIVERED
    assert hops == [], "a delivered message was handed to the home channel as well"


@pytest.mark.asyncio
async def test_the_home_channel_does_not_hop_to_itself(engine: Engine) -> None:
    """A web conversation that finds no open tab is the end of the road, not a loop."""
    calls: list[str] = []

    class _CountingHome:
        async def deliver(self, message: OriginatedMessage) -> DeliveryResult:  # noqa: ARG002
            calls.append("web")
            return DeliveryResult(outcome=DeliveryOutcome.PENDING, channel="web")

    router = build_origination_router(rls_engine=engine)
    router._deliverers["web"] = _CountingHome()  # noqa: SLF001
    router._record = lambda **kwargs: None  # noqa: SLF001, ARG005

    await router.deliver(_message(_WEB_CONVERSATION))

    assert calls == ["web"], "the home channel was asked twice for the same message"


@pytest.mark.asyncio
async def test_a_web_conversation_still_goes_to_the_web_deliverer(engine: Engine) -> None:
    """The other half of the same assertion: binding Telegram must not steal web traffic."""
    telegram = _RecordingDeliverer("telegram")
    channels = ChannelDeliverers()
    channels.bind({"telegram": telegram})
    router = build_origination_router(rls_engine=engine, channels=channels)

    result = await router.deliver(_message(_WEB_CONVERSATION))

    assert telegram.delivered == []
    # No open tab in this process, so the web deliverer reports pending: durably written,
    # present on next open. Pending is a claim about our records, never about attention.
    assert result.outcome is DeliveryOutcome.PENDING


@pytest.mark.asyncio
async def test_with_no_sessions_the_web_home_is_persist_only(engine: Engine) -> None:
    """The default registry is the explicit no-sessions one, not an accident of ``None``."""
    router = build_origination_router(rls_engine=engine, sessions=None)

    result = await router.deliver(_message(_WEB_CONVERSATION))

    assert result.outcome is DeliveryOutcome.PENDING


def test_no_live_sessions_never_finds_a_sink() -> None:
    assert NoLiveSessions().lookup(_message(_WEB_CONVERSATION)) is None

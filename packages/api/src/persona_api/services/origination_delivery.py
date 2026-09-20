"""The one construction site for origination delivery (R9-081 / R9-120, T1).

Every originated message, whoever raises it, reaches its reader the same way: record
durably, resolve exactly one channel, hand it to that channel's
:class:`~persona.delivery.MessageDeliverer`, audit the routing decision. That seam is
:class:`~persona_api.services.delivery_router.DeliveryRouter` and it has been right
since C0. What was wrong is that it was BUILT in three different places, each with its
own idea of which channels exist:

* ``origination_adapters._originate_on_conversation`` built ``{"web": web}``,
* ``within_runtime_origination`` built ``{"web": web}`` again,
* the connector service built a router over the real Telegram / Slack / Discord /
  WhatsApp / SMS / email deliverers and **threw the result away** (R9-120).

So a persona could answer on a connector and never speak first on one, and the
owner ruling of 2026-09-21 ("the connector delivers its own") carries an explicit
condition: one seam both callers enter, no second copy of the resolve-and-send logic.
Two paths that merely agree today are how one fact came to live in two places once
already (R9-213). This module is that one place.

**What is deliberately NOT here.** No retry, no outbox, no queue. A channel that
cannot be reached right now yields ``pending``, which is a statement about our records
(the message is durably in the conversation), never a promise that the person saw it.
Blurring those two is the copy failure this seam must not enable.

**Binding order (the reason for** :class:`ChannelDeliverers` **).** The channels are
not knowable when the origination service is built, and that is a cycle rather than a
bad order: the connector's deliverers need its reply runner, the reply runner's chat
turn registry needs the origination service, and the origination service's notifier
needs the deliverers. Reordering cannot break a cycle. So the registry is created
empty, injected, and bound exactly once by whichever composition root learns the
channels. A single-assignment object rather than a callable provider, because an
unbound provider silently returns nothing and a capability that silently reaches
nobody is the defect this repo keeps shipping (ENGINEERING_STANDARDS §6b).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.errors import PersonaError
from persona.logging import get_logger
from sqlalchemy import select

from persona_api.db.models import connector_conversations
from persona_api.services.delivery_router import HOME_CHANNEL, DeliveryRouter
from persona_api.services.web_deliverer import WebAppDeliverer

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Mapping

    from persona.delivery import MessageDeliverer
    from persona.schema.origination import OriginatedMessage
    from sqlalchemy import Engine

    from persona_api.services.web_deliverer import LiveSessionRegistry, LiveSessionSink

__all__ = [
    "ChannelDeliverers",
    "DeliveryChannelsAlreadyBoundError",
    "NoLiveSessions",
    "build_origination_router",
    "make_origination_channel_resolver",
]

_logger = get_logger("api.origination_delivery")


class DeliveryChannelsAlreadyBoundError(PersonaError):
    """A composition root tried to bind the delivery channels a second time.

    One process holds one set of channels. A second bind means two composition roots
    each believe they own delivery, which is the duplicate-path failure the 2026-09-21
    ruling's condition exists to prevent, so it fails loudly at startup rather than
    letting whichever root ran last decide where a persona speaks.
    """


class NoLiveSessions:
    """A live-session registry with no open sessions: delivery is durable-persist only.

    Outside a run there is no open stream bound to the conversation, so live delivery
    yields ``pending`` (D-C0-4, not a drop) while the recorder's conversation write is
    the durable, un-suppressible record the failure account relies on. Durable is the
    claim; seen is not one this layer can make.

    Public here rather than private to one adapter module, because it is the default
    every origination path needs and a second private copy of it is the same drift in
    miniature.
    """

    def lookup(self, message: OriginatedMessage) -> LiveSessionSink | None:  # noqa: ARG002
        """No session is ever open here."""
        return None


class ChannelDeliverers:
    """The process-wide set of channels an originated message can be routed to.

    Created empty by the composition root, injected into the origination services, and
    bound exactly once when the host knows its channels (see the module docstring for
    why that cannot simply be reordered). The web home is NOT held here: it is built
    per call from the caller's live-session registry, which differs between an open run
    stream and the persistent user channel.

    Holds no global state: an instance is created and injected, never imported.
    """

    def __init__(self) -> None:
        self._deliverers: dict[str, MessageDeliverer] = {}
        self._bound = False

    @property
    def bound(self) -> bool:
        """Whether a composition root has bound its channels yet.

        Read by the startup log and by the wiring tests: "nobody bound anything" and
        "a host with no connectors configured" must be distinguishable, or an unbound
        registry reads as a correctly empty one forever.
        """
        return self._bound

    def bind(self, deliverers: Mapping[str, MessageDeliverer]) -> None:
        """Register this process's channels. Callable exactly once.

        Args:
            deliverers: ``platform`` to its :class:`MessageDeliverer`. May be empty:
                a host with no connector configured binds nothing, which is a stated
                outcome rather than an absent one.

        Raises:
            DeliveryChannelsAlreadyBoundError: Channels were already bound.
        """
        if self._bound:
            raise DeliveryChannelsAlreadyBoundError(
                "the origination delivery channels are already bound",
                context={
                    "bound_channels": ",".join(sorted(self._deliverers)),
                    "rejected_channels": ",".join(sorted(deliverers)),
                },
            )
        self._deliverers = dict(deliverers)
        self._bound = True
        _logger.info(
            "origination delivery channels bound: {channels}",
            channels=", ".join(sorted(self._deliverers)) or "none",
        )

    def ensure_bound(self) -> None:
        """Declare "no connector channels" unless a nested composition already bound some.

        The composition root's final word, called on the ONE path every boot takes. A
        root that hosts connectors has them bound by the connector composition before
        this runs; a root that does not (the flag off, no platform configured, a
        connector that failed to start) binds nothing here and says so.

        It exists so that "this process has no channels" is a declaration rather than the
        absence of one. Without it, forgetting to bind and having nothing to bind look
        identical from here on, which is the defect this class is shaped to prevent.
        """
        if not self._bound:
            self.bind({})

    def snapshot(self) -> dict[str, MessageDeliverer]:
        """The bound channels, as a copy the caller cannot mutate back into here."""
        return dict(self._deliverers)


def _connector_platform_of(rls_engine: Engine, conversation_id: str) -> str | None:
    """The connector platform a conversation lives on, or ``None`` for a web conversation.

    Mirrors ``ConversationStateStore.resolve_channel`` EXACTLY, including the absence of
    a status filter: the router's decision and the deliverer's own lookup must never
    disagree about which conversation is on which platform, and the cheapest way to
    guarantee that is the same predicate over the same row. ``UNIQUE(conversation_id)``
    makes it a single-row read.

    Owner-scoped by RLS through the caller's already-bound owner scope, so another
    owner's conversation id matches nothing (fail-closed, no cross-tenant leak).
    Fail-soft: origination is additive, so a database hiccup here routes home rather
    than failing the message that was already durably written.
    """
    try:
        with rls_engine.begin() as conn:
            platform = conn.execute(
                select(connector_conversations.c.platform).where(
                    connector_conversations.c.conversation_id == conversation_id
                )
            ).scalar()
    except Exception as exc:  # noqa: BLE001 - routing must not break an originated message
        _logger.warning(
            "connector channel lookup failed; routing home conversation_id={cid}: {err}",
            cid=conversation_id,
            err=str(exc),
        )
        return None
    return platform if isinstance(platform, str) else None


def make_origination_channel_resolver(
    *, rls_engine: Engine, registered: Collection[str]
) -> Callable[[OriginatedMessage], str | None]:
    """Build the router's channel resolver: which channel is this reader actually on.

    A lookup, never a ``platform == …`` switch (D-08-3): the conversation's own row in
    ``connector_conversations`` names its platform, and that name is matched against the
    registered channel keys. ``None`` means the home channel.

    When nothing but the home channel is registered there is no decision to make, so the
    resolver short-circuits and never touches the database. That is the shape of every
    deployment until the connectors are bound, which is what makes this refactor
    behaviour-preserving rather than merely intended to be.

    **Why the contract's stated ``channel`` preference is not consulted here**, and this
    is a real decision rather than an omission. ``UpdatePreference.channel`` is a
    model-extracted line of the user's own words ("I'll check in at milestones, on
    web"). Every connector's ``deliver`` resolves strictly by ``conversation_id``, so a
    preference naming a channel the CONVERSATION is not bound to selects a deliverer
    that can only answer ``pending``: it would convert a live web delivery into a silent
    durable write. Today that preference is already inert, because with only ``web``
    registered every other value falls back to home anyway. Honouring it once the
    connectors are bound would therefore be a new way to lose a message, not a feature.
    A genuine cross-channel preference needs an owner-level channel lookup that does not
    exist yet, and it belongs in the close-out as the named gap it is.

    Args:
        rls_engine: The owner-scoped engine the conversation lookup runs on.
        registered: The channel keys this router will actually hold.

    Returns:
        The ``resolve_channel`` callable :class:`DeliveryRouter` takes.
    """
    reachable = frozenset(registered) - {HOME_CHANNEL}
    if not reachable:

        def home_only(message: OriginatedMessage) -> str | None:  # noqa: ARG001
            return None

        return home_only

    def resolve(message: OriginatedMessage) -> str | None:
        if message.conversation_id is None:
            return None
        platform = _connector_platform_of(rls_engine, message.conversation_id)
        return platform if platform is not None and platform in reachable else None

    return resolve


def build_origination_router(
    *,
    rls_engine: Engine,
    sessions: LiveSessionRegistry | None = None,
    channels: ChannelDeliverers | None = None,
) -> DeliveryRouter:
    """Build the delivery router for one originated message. The ONLY such site.

    The web home is always registered, so routing always has a target and the fail-fast
    home guard in :class:`DeliveryRouter` can never trip from here. Every other channel
    comes from the bound :class:`ChannelDeliverers`, which is data rather than logic:
    that is what makes the api's path and the connector's path the same path holding a
    different set, instead of two implementations that happen to agree.

    Args:
        rls_engine: The owner-scoped engine for the lookup, the audit write and the web
            deliverer.
        sessions: The A11 live-session registry the web deliverer consults. ``None``
            gives :class:`NoLiveSessions`, which is persist-only, present-on-next-open.
            A connector process passes ``None`` on purpose: its
            ``UserEventChannel`` is an in-process bus (A11-D-1) that no browser tab
            subscribes to, so a registry there would report open sessions that do not
            exist. Under ``PERSONA_API_EMBED_CONNECTORS`` both halves share one process
            and the api's real registry is live for both.
        channels: The bound connector channels, or ``None`` for web only.

    Returns:
        A router over the web home plus every bound channel.
    """
    deliverers: dict[str, MessageDeliverer] = {
        HOME_CHANNEL: WebAppDeliverer(rls_engine=rls_engine, sessions=sessions or NoLiveSessions())
    }
    if channels is not None:
        for key, deliverer in channels.snapshot().items():
            if key != HOME_CHANNEL:
                deliverers[key] = deliverer
    return DeliveryRouter(
        deliverers=deliverers,
        rls_engine=rls_engine,
        home_channel=HOME_CHANNEL,
        resolve_channel=make_origination_channel_resolver(
            rls_engine=rls_engine, registered=deliverers.keys()
        ),
    )

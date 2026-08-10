"""The connector-service composition root (Spec C1 T1, C1-D-1).

This is the **single api-coupled module** in persona-connectors. Per C1-D-1 the
connector reuses persona-api's reply-producing chat flow + C0's delivery router
in-process, following the ``run_worker.py`` pattern — a separate long-lived
process that imports api services and sets the ``current_user_id`` RLS contextvar
per unit of work, outside any FastAPI request scope. Concentrating the
``persona_api`` import here keeps the owned surface (:mod:`persona_connectors.domain`)
import-decoupled, so a future extract-to-core is a dependency swap, not a reshape
(the reversibility guarantee).

T1 wires the **shared foundations** every later task needs: the edition switch,
the RLS engine, and the owner-scope (D-C1-X-rls-spine). The delivery-router
(C0 ``DeliveryRouter`` reuse, T10) and the conversation-loop builder (api's
``RuntimeFactory``, T9) plug in here in their tasks — their seams are marked
below.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast

import yaml
from persona_api.config import Edition
from persona_api.db.community import make_community_engine
from persona_api.db.engine import create_db_engine
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from persona_api.services import persona_service
from persona_api.services.chat_service import start_chat_turn
from persona_api.services.chat_turn_composition import build_chat_turn_registry
from persona_api.services.chat_turn_sink import MessagesTurnSink
from persona_api.services.delivery_router import DeliveryRouter
from sqlalchemy import text as _sql

from persona_connectors.errors import ConnectorError, TurnFailedError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping

    from persona.backends import StreamChunk
    from persona.delivery import MessageDeliverer
    from persona_api.background.chat_turn_worker import ChatTurnHandle
    from persona_api.billing import StripeGateway
    from persona_api.config import APIConfig
    from persona_api.editions.credits_policy import CreditsPolicy
    from persona_api.initiative.verb_service import InitiativeVerbService
    from persona_api.jobs.queue import JobQueue
    from persona_api.services.runtime_factory import RuntimeFactory
    from persona_api.services.task_reschedule_service import TaskRescheduleService
    from persona_api.services.task_steering_service import TaskSteeringService
    from sqlalchemy.engine import Engine

    from persona_connectors.config import ConnectorConfig
    from persona_connectors.telegram.flow import TurnRequest

__all__ = [
    "ConnectorComposition",
    "build_delivery_router",
    "build_persona_name_lister",
    "build_reply_runner",
]

# A generous upper bound — a single owner's persona roster is small (the web UI
# lists them); -1/unbounded isn't supported by list_personas, so cap high.
_PERSONA_LIST_LIMIT = 1000


class ConnectorComposition:
    """Assembles the connector service's shared foundations from its config.

    Holds no DB connection at construction (the engine is built lazily via
    :meth:`make_engine`); holds no global state. Dependency injection via the
    constructor (no globals — ENG-STD).
    """

    def __init__(self, config: ConnectorConfig) -> None:
        self._config = config

    @property
    def config(self) -> ConnectorConfig:
        """The service configuration this root was built from."""
        return self._config

    @property
    def edition(self) -> Edition:
        """The open-core edition (Spec 33), as the persona-api ``Edition`` enum."""
        return Edition(self._config.edition.strip().lower())

    def make_engine(self) -> Engine:
        """Build the edition-appropriate RLS-scoped engine (lazy — no connection).

        Cloud: a Postgres RLS engine (the Spec 08 D-08-1 checkout/checkin listener
        scopes every connection by ``current_user_id``). Community: the single-
        owner local engine (Spec 33). Fails fast on a cloud edition with no
        ``database_url`` — a misconfiguration caught at the boundary, not three
        layers deep.

        Returns:
            The SQLAlchemy :class:`~sqlalchemy.engine.Engine`. Connection happens
            lazily on first use, RLS-scoped by the owner contextvar set in
            :meth:`owner_scope`.

        Raises:
            ConnectorError: Cloud edition with no ``database_url`` configured.
        """
        if self.edition is Edition.community:
            return make_community_engine(Path(self._config.community_db_path))
        if not self._config.database_url:
            raise ConnectorError(
                "cloud edition requires a database_url",
                context={"edition": self.edition.value},
            )
        return make_rls_engine(self._config.database_url, pool_size=self._config.db_pool_size)

    def make_dispatch_engine(self) -> Engine:
        """Build the cross-tenant dispatch engine for the pre-auth resolve/redeem reads.

        An inbound arrives from an *unauthenticated* platform identity, so resolving
        ``(platform, sender_id) → owner`` and redeeming a link token are reads that
        precede any owner scope — they run BYPASSRLS on this engine, keyed by the
        ``UNIQUE`` spine / the unguessable token hash (the A0-worker pre-auth
        pattern, D-C1-5). After resolution, downstream work runs owner-scoped via
        :meth:`owner_scope` on the RLS engine. Community (single owner, no RLS) can
        reuse the same engine for both roles.

        Raises:
            ConnectorError: Cloud edition with no ``database_url`` configured.
        """
        if self.edition is Edition.community:
            return make_community_engine(Path(self._config.community_db_path))
        if not self._config.database_url:
            raise ConnectorError(
                "cloud edition requires a database_url for the dispatch engine",
                context={"edition": self.edition.value},
            )
        return create_db_engine(self._config.database_url)

    @contextlib.contextmanager
    def owner_scope(self, owner_id: str) -> Iterator[None]:
        """Scope a unit of work to ``owner_id`` (the run_worker.py RLS spine).

        Sets the persona-api ``current_user_id`` contextvar the RLS engine's
        checkout listener reads, so every store read/write inside the scope is
        owner-scoped exactly as the web request path is (D-C1-X-rls-spine); resets
        it in a ``finally`` so an error never leaks the owner to the next message.
        The connector flow (T9) enters this scope after resolving the inbound
        platform identity to its linked Persona user.
        """
        token = current_user_id.set(owner_id)
        try:
            yield
        finally:
            current_user_id.reset(token)


def _parse_persona_display_name(yaml_text: str) -> str:
    """Extract a persona's display name (``identity.name``) from its YAML, else ``""``."""
    try:
        parsed = yaml.safe_load(yaml_text)
    except yaml.YAMLError:
        return ""
    if isinstance(parsed, dict):
        identity = parsed.get("identity")
        if isinstance(identity, dict):
            name = identity.get("name")
            if isinstance(name, str):
                return name
    return ""


def build_persona_name_lister(
    *, rls_engine: Engine, owner_scope: Callable[[str], contextlib.AbstractContextManager[None]]
) -> Callable[[str], Mapping[str, list[str]]]:
    """Build the ``list_persona_names`` callable the flow injects (owner-scoped).

    Maps ``owner_id`` → ``{persona_id: [display_name]}`` by reading the owner's
    personas (RLS-scoped via ``owner_scope``) and parsing each display name from its
    YAML. The flow consumes this for addressing + the list-and-instructions reply;
    aliases aren't in the v1 schema, so only the display name is exposed.
    """

    def list_persona_names(owner_id: str) -> Mapping[str, list[str]]:
        with owner_scope(owner_id):
            rows = persona_service.list_personas(
                rls_engine=rls_engine, limit=_PERSONA_LIST_LIMIT, offset=0
            )
        names: dict[str, list[str]] = {}
        for row in rows:
            persona_id = str(row["id"])
            display = _parse_persona_display_name(str(row.get("yaml", "")))
            if display:
                names[persona_id] = [display]
        return names

    return list_persona_names


def build_email_recipient_resolver(
    *, rls_engine: Engine, owner_scope: Callable[[str], contextlib.AbstractContextManager[None]]
) -> Callable[[str], str | None]:
    """Build the email connector's ``recipient_for`` (owner-scoped) — Spec C5, Group E.

    Maps ``owner_id`` → the owner's linked email address (their active
    ``connector_identities`` row for ``platform='email'``), RLS-scoped via ``owner_scope`` —
    the recipient for a C0-originated email. ``None`` when the owner has no linked email
    (→ the connector reports ``pending``, never a lost message).
    """

    def recipient_for(owner_id: str) -> str | None:
        with owner_scope(owner_id), rls_engine.begin() as conn:
            row = conn.execute(
                _sql(
                    "SELECT platform_identity FROM connector_identities "
                    "WHERE owner_id = :o AND platform = 'email' AND status = 'active' LIMIT 1"
                ),
                {"o": owner_id},
            ).scalar()
        return row if isinstance(row, str) else None

    return recipient_for


@dataclass
class _ConversationLock:
    """A per-conversation lock plus its live waiter count (so the map stays bounded)."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    holders: int = 0


class _ConversationLocks:
    """Serialises this process's turns per conversation (R9-076).

    A connector turn now PERSISTS (``open_turn`` → drive → ``finalize``), and the
    api's one-active-turn invariant (D-P1-one-active-turn) allows exactly one
    in-flight assistant row per conversation. Two messages arriving back-to-back on
    the same chat — the normal texting shape — would otherwise race: the second
    would either be refused (``TurnAlreadyActiveError``) or load a history the first
    turn had not written yet, which is precisely the amnesia R9-076 fixes. Queueing
    them here makes the second turn see the first, in order.

    Entries are dropped when their last holder leaves, so a long-lived service never
    accumulates one lock per conversation it has ever seen.
    """

    def __init__(self) -> None:
        self._locks: dict[str, _ConversationLock] = {}

    @contextlib.asynccontextmanager
    async def hold(self, conversation_id: str) -> AsyncIterator[None]:
        """Hold the conversation's turn lock for the duration of the body."""
        entry = self._locks.get(conversation_id)
        if entry is None:
            entry = _ConversationLock()
            self._locks[conversation_id] = entry
        entry.holders += 1
        try:
            async with entry.lock:
                yield
        finally:
            entry.holders -= 1
            if entry.holders <= 0:
                self._locks.pop(conversation_id, None)


async def _collect_reply(handle: ChatTurnHandle) -> str:
    """Drain a detached turn's live tail to its terminal frame and return the text.

    The connector's no-streaming counterpart to api's
    :func:`~persona_api.services.chat_service.stream_turn`: same queue, same
    terminal contract, no SSE. Persistence + finalize happen in the worker, so a
    fault here can never lose the turn.

    Raises:
        TurnFailedError: The turn finalized as ``error`` — the shared flow answers
            honestly rather than sending an empty persona reply.
    """
    parts: list[str] = []
    while True:
        item = await handle.events.get()
        if item is None:  # end-of-stream sentinel
            break
        kind, payload = item
        if kind == "chunk":
            delta = cast("StreamChunk", payload).delta
            if delta:
                parts.append(delta)
        elif kind == "done":
            break
        elif kind == "error":
            frame = cast("Mapping[str, object]", payload)
            detail = frame.get("message")
            # R9-097 (remainder): the worker marks whether its message is safe to
            # show a person. Only a marked one is carried through to the reply --
            # an unmarked ``detail`` is the raw stringified exception, which is
            # exactly the vendor mix and routing internals we stopped showing in
            # chat. Unmarked keeps the generic apology, as before.
            raise TurnFailedError(
                "the persona turn failed",
                context={
                    "conversation_id": handle.conversation_id,
                    "detail": str(detail) if detail is not None else "",
                    "user_facing": "true" if frame.get("user_facing") is True else "false",
                },
            )
    return "".join(parts)


def build_reply_runner(
    *,
    runtime_factory: RuntimeFactory,
    rls_engine: Engine,
    owner_scope: Callable[[str], contextlib.AbstractContextManager[None]],
    api_config: APIConfig,
    credits_policy: CreditsPolicy,
    gateway: StripeGateway | None,
    job_queue: JobQueue | None,
    task_steering_service: TaskSteeringService | None = None,
    task_reschedule_service: TaskRescheduleService | None = None,
    initiative_verb_service: InitiativeVerbService | None = None,
) -> Callable[[TurnRequest], Awaitable[str]]:
    """Build the ``run_turn`` callable: run a turn through api's OWN chat-turn path.

    The no-streaming collector (§3), but the turn itself is persisted by the exact
    seam the web chat uses — :func:`~persona_api.services.chat_service.start_chat_turn`
    over :class:`~persona_api.services.chat_turn_sink.MessagesTurnSink` and
    :class:`~persona_api.background.chat_turn_worker.ChatTurnRegistry` (C1-D-1, the
    ``run_worker.py`` pattern: import api's services in-process and bind the RLS
    contextvar per unit of work). So one write path, not two: ``open_turn`` persists
    the user message + the in-progress assistant row, the worker checkpoints and
    finalizes, and the conversation the web UI reads is byte-identically the
    conversation the connector wrote.

    R9-076 — why this changed: the previous collector only ever READ
    (``_load_conversation``) and drove the loop; nothing was ever written back. The
    conversation row existed (chats appeared in the web UI) but stayed empty, and —
    far worse — every inbound re-loaded that empty history, so a persona had **no
    memory at all** on any connector. Reusing the api seam fixes both at once.

    Every write is owner-scoped: ``owner_scope`` binds ``current_user_id`` for the
    start (``open_turn``/heal), and the detached worker re-binds the same owner for
    its checkpoint/finalize writes (its own ``run_worker.py`` discipline).

    R9-079 — billing parity: a turn sent on Telegram / Discord / Slack / WhatsApp /
    SMS / email is a normal chat turn and is charged as one. The registry is composed
    through the SAME :func:`~persona_api.services.chat_turn_composition.build_chat_turn_registry`
    the api's own lifespan uses, from the same :class:`~persona_api.config.APIConfig`,
    so the credits policy, the Stripe auto-top-up gateway, the per-turn floor, the
    proportional switch, the charge ceiling and the credit markup are identical on
    both surfaces. Previously this called ``ChatTurnRegistry(sink=…, rls_engine=…)``
    and every billing input fell to its constructor default — ``credits_policy=None``
    means "bill nothing", so every connector turn ran for free, silently. The
    collaborators are REQUIRED arguments here for exactly that reason: "unbilled"
    (community's ``UnlimitedCreditsPolicy``) must be a stated decision, never a
    forgotten keyword.

    R9-081 — the conversational-verb worker services. The connector's
    ``RuntimeFactory`` builds the loop-side interpreters unconditionally, so a persona
    CAN confirm a task contract, reschedule, or answer an initiative dial over
    Telegram; with the worker side at ``None`` the confirmation was silently dropped
    and the user still read "Done, I've set that up". Steering, reschedule and
    initiative are now injected by the service entry and this path applies them.

    ``origination_service`` remains unwired, and that is the honest boundary rather
    than an oversight. Its failure notifier narrates "I could not create that after
    all" through the C0 delivery seam to an open web tab, and a connector process has
    neither the A11 live-session registry nor (at this point in its startup) its own
    delivery router — so a failed origination would be persisted and never seen. That
    needs a decision about where a connector-raised failure account is delivered, not
    a wiring change. Steering is wired WITHOUT its optional notifier for the same
    reason: pause, resume and cancel all take effect, and only the narration of a
    cancel that failed to take is unavailable (it still logs).

    ``request.persona_id`` is not passed down: ``start_chat_turn`` derives the persona
    from the conversation ROW, and the connector's conversation store creates one
    conversation per (owner, platform, channel, persona) — so the two always agree,
    and the DB stays the single source of truth for which persona owns a turn.

    NOTE (deploy seam): the heavy ``RuntimeFactory`` (embedder/tier-registry/model
    backends) is built by the service entry from the live env; the model half of this
    path is exercised by the live operator pass, not CI (the same posture as api's own
    ``@external`` turn tests).

    Args:
        runtime_factory: The reused api runtime that builds each turn's loop.
        rls_engine: The RLS-scoped engine every owner-scoped write runs on.
        owner_scope: Binds ``current_user_id`` for the duration of a unit of work.
        api_config: The resolved api config — the single source of the per-turn
            credit floor, the proportional switch and the charge ceiling.
        credits_policy: The edition's credits policy
            (``persona_api.editions.build_credits_policy``). Community's unlimited
            policy is how a self-host install stays unbilled.
        gateway: The Stripe gateway for Pro auto-top-up
            (``persona_api.editions.build_stripe_gateway``); ``None`` when billing
            is not active.
        job_queue: The durable queue for the turn-boundary synthesis enqueue;
            ``None`` makes it a no-op.
        task_steering_service: Worker side of the A4 steering verbs (pause / resume /
            cancel). ``None`` keeps the pre-R9-081 drop-on-the-floor behaviour.
        task_reschedule_service: Worker side of the A8 reschedule verb.
        initiative_verb_service: Worker side of the A5 initiative verbs.

    Returns:
        The ``run_turn`` callable the connector flows inject.
    """
    sink = MessagesTurnSink(rls_engine)
    registry = build_chat_turn_registry(
        sink=sink,
        rls_engine=rls_engine,
        config=api_config,
        credits_policy=credits_policy,
        gateway=gateway,
        job_queue=job_queue,
        # R9-081: see the note above. Origination alone stays None, pending the
        # decision about where a connector-raised failure account is delivered.
        origination_service=None,
        task_steering_service=task_steering_service,
        task_reschedule_service=task_reschedule_service,
        initiative_verb_service=initiative_verb_service,
    )
    locks = _ConversationLocks()

    async def run_turn(request: TurnRequest) -> str:
        async with locks.hold(request.conversation_id):
            with owner_scope(request.owner_id):
                handle = await start_chat_turn(
                    rls_engine=rls_engine,
                    sink=sink,
                    registry=registry,
                    loop_builder=runtime_factory.build_conversation_loop,
                    owner_id=request.owner_id,
                    conversation_id=request.conversation_id,
                    user_message=request.text,
                )
            # The worker owns persistence + the owner scope from here; the collector
            # only drains the in-process queue (no DB touch), so it runs unscoped.
            return await _collect_reply(handle)

    return run_turn


def build_delivery_router(
    *, deliverers: Mapping[str, MessageDeliverer], rls_engine: Engine, home_channel: str
) -> DeliveryRouter:
    """Register the configured connectors as C0's ``MessageDeliverer``s (criterion 8).

    The connector service routes every originated message to the deliverer for its
    channel (each ``deliver`` resolves the chat via the GAP-A ``resolve_channel`` and
    sends). ``home_channel`` is the always-available default target for this process; a
    ``pending`` outcome (no resolvable channel) is the deliverer's, never a silent drop.
    Generalised from C2's single-Telegram form to register Telegram / Discord / Slack
    side by side (C3 multi-connector service wiring).

    Args:
        deliverers: ``platform`` → its ``MessageDeliverer`` (the connector). At least one.
        rls_engine: The RLS-scoped engine the router's owner-scoped writes run on.
        home_channel: The default target channel (a key present in ``deliverers``).
    """
    return DeliveryRouter(
        deliverers=dict(deliverers),
        rls_engine=rls_engine,
        home_channel=home_channel,
    )

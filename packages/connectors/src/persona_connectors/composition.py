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

import contextlib
from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from persona_api.config import Edition
from persona_api.db.community import make_community_engine
from persona_api.db.engine import create_db_engine
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from persona_api.services import persona_service
from persona_api.services.chat_service import _load_conversation
from persona_api.services.delivery_router import DeliveryRouter
from sqlalchemy import text as _sql

from persona_connectors.errors import ConnectorError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator, Mapping

    from persona.delivery import MessageDeliverer
    from persona_api.services.runtime_factory import RuntimeFactory
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


def build_reply_runner(
    *,
    runtime_factory: RuntimeFactory,
    rls_engine: Engine,
    owner_scope: Callable[[str], contextlib.AbstractContextManager[None]],
) -> Callable[[TurnRequest], Awaitable[str]]:
    """Build the ``run_turn`` callable: drive ``ConversationLoop.turn`` + collect the reply.

    The no-streaming collector (§3): under the owner scope, build the persona's loop
    (reusing api's ``RuntimeFactory`` in-process — C1-D-1, the ``run_worker.py``
    pattern), load the conversation, run the turn, and accumulate ``chunk.delta`` to
    completion. The contextvar set by ``owner_scope`` persists across the awaits
    (task-local), so every store read inside the turn is RLS-scoped to the owner.

    NOTE (deploy seam): the heavy ``RuntimeFactory`` (embedder/tier-registry/model
    backends) is built by the service entry from the live env; this collector is
    exercised by the live operator pass, not CI (the same posture as api's own
    ``@external`` turn tests).
    """

    async def run_turn(request: TurnRequest) -> str:
        with owner_scope(request.owner_id):
            loop = await runtime_factory.build_conversation_loop(request.persona_id)
            with rls_engine.begin() as conn:
                conversation = _load_conversation(conn, request.conversation_id)
            reply = ""
            async for chunk in loop.turn(conversation, request.text):
                if chunk.delta:
                    reply += chunk.delta
                if chunk.is_final:
                    break
            return reply

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

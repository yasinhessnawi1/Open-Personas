"""The per-conversation "fine to bring this up" control (D-OWNER-11-18 (16), D-ORCH-11-18-u).

A conversation in which the user disclosed something sensitive is kept out of the persona's
unprompted initiative (R9-253). The owner can say, from the web app and nowhere else, that the
persona may draw on it again, and can undo that. This module is the read and the change behind
``/v1/conversations/{id}/initiative-hold``:

- ``held``: a disclosure keeps the conversation out (the gate's strict ``disclosed``);
- ``released``: the user's release holds (:meth:`ConversationWellbeingGate.released`);
- ``not_held``: nothing keeps it out, so there is nothing to release.

``version`` is an opaque token of the disclosure count the user saw (PR #44 review LOW-1: the
raw count told how many disclosures the conversation held). A release is written only for a
held conversation and only for the count behind that token, so a disclosure recorded in between
is never released unseen. Only the conversation's owner can read or change it: an unknown
conversation, or another owner's, is not found.

The token is keyless: a SHA-256 over the owner, the conversation, the fact row's own
``created_at`` (to the microsecond), the count and the latest write of a tagged node citing
the conversation, every instant in UTC. A hash of the conversation and the count
alone could be inverted by trying small counts; ``created_at`` is never shown by any route, so
the count cannot be guessed from the token, and no new server secret has to be deployed and
kept in step across processes. The token changes with every recorded disclosure, which is all
the 409 guard needs.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC
from typing import TYPE_CHECKING, Literal

from sqlalchemy import select

from persona_api.db.engine import rls_connection
from persona_api.db.models import conversations as conversations_t
from persona_api.errors import ConversationNotFoundError
from persona_api.initiative.disclosures import PgDisclosureStore, ReleaseOutcome
from persona_api.initiative.wellbeing_gate import ConversationWellbeingGate

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import datetime

    from persona.graph.models import ConceptNode
    from persona.graph.protocol import GraphStore
    from sqlalchemy import Engine

    from persona_api.initiative.disclosures import DisclosureState

__all__ = [
    "ConversationHold",
    "ConversationHoldService",
    "HoldChangedError",
    "HoldState",
    "version_token",
]

HoldState = Literal["held", "released", "not_held"]

_TOKEN_PURPOSE = "persona:initiative-hold:v1"


class HoldChangedError(Exception):
    """A disclosure was recorded after the user read the control (answer 409, read again)."""


@dataclass(frozen=True)
class ConversationHold:
    """What the control shows: the state, when a release was given, the version seen."""

    state: HoldState
    released_at: datetime | None
    version: str


def _utc(at: datetime | None) -> str:
    """An instant as UTC text, so the token is the same whatever the session time zone."""
    return "" if at is None else at.astimezone(UTC).isoformat()


def version_token(
    owner_id: str,
    conversation_id: str,
    state: DisclosureState | None,
    *,
    latest_tagged: datetime | None = None,
) -> str:
    """The opaque version of what the user saw (see the module).

    ``latest_tagged`` is the latest ``written_at`` of a tagged node citing the conversation
    (PR #44 delta review N2): a tagging path that skips the fact (``merge.py`` keeping a
    target's tag) moves it, so a write landing between the read and the release is a 409
    rather than covered unseen by the re-stamp.
    """
    material: tuple[str, ...]
    if state is None:
        material = (_TOKEN_PURPOSE, owner_id, conversation_id, "no-fact")
    else:
        count = str(state.disclosure_seq)
        material = (_TOKEN_PURPOSE, owner_id, conversation_id, _utc(state.created_at), count)
    material = (*material, _utc(latest_tagged))
    digest = hashlib.sha256("\x1f".join(material).encode("utf-8")).hexdigest()
    return f"v1.{digest[:32]}"


def _latest_tagged(flagged: Sequence[ConceptNode], conversation_id: str) -> datetime | None:
    """The latest write of a tagged node citing the conversation, if any."""
    writes = [
        p.written_at
        for node in flagged
        for p in node.provenance
        if p.interaction_id == conversation_id
    ]
    return max(writes, default=None)


_NOT_HELD = ConversationHold(state="not_held", released_at=None, version="")


def _persona_lookup(engine: Engine) -> Callable[[str, str], str | None]:
    def persona_of(owner_id: str, conversation_id: str) -> str | None:
        stmt = select(conversations_t.c.persona_id).where(
            conversations_t.c.id == conversation_id, conversations_t.c.owner_id == owner_id
        )
        with rls_connection(engine, owner_id) as conn:
            row = conn.execute(stmt).first()
        return None if row is None else str(row[0])

    return persona_of


class ConversationHoldService:
    """Read and change one conversation's hold, as its owner."""

    def __init__(
        self,
        store: GraphStore,
        engine: Engine,
        *,
        facts: PgDisclosureStore | None = None,
        gate: ConversationWellbeingGate | None = None,
        persona_of: Callable[[str, str], str | None] | None = None,
    ) -> None:
        """Bind the graph store and the owner-scoped engine (the fakes are for tests)."""
        self._store = store
        self._facts = facts if facts is not None else PgDisclosureStore(engine)
        self._gate = (
            gate
            if gate is not None
            else ConversationWellbeingGate(
                store, engine, disclosures=self._facts, releases=self._facts
            )
        )
        self._persona_of = persona_of if persona_of is not None else _persona_lookup(engine)

    def read(self, owner_id: str, conversation_id: str) -> ConversationHold:
        """The control's state for the owner's conversation (not found: not theirs)."""
        self._persona(owner_id, conversation_id)
        return self._hold(owner_id, conversation_id)

    def change(
        self,
        owner_id: str,
        conversation_id: str,
        *,
        released: bool,
        version: str | None,
        at: datetime,
    ) -> ConversationHold:
        """Release (``released``) for the version seen, or hold it back again; the new state."""
        persona_id = self._persona(owner_id, conversation_id)
        if not released:
            self._facts.hold(owner_id, persona_id, conversation_id)
            return self._hold(owner_id, conversation_id)
        if version is None:
            msg = "a release needs the version the user saw"
            raise ValueError(msg)
        state = self._facts.states(owner_id, [conversation_id]).get(conversation_id)
        current = self._hold(owner_id, conversation_id, state)
        if current.state != "held":
            return current
        if not hmac.compare_digest(version, current.version):
            msg = "the conversation changed since it was read"
            raise HoldChangedError(msg)
        # The store compares the count again under the row lock (a record racing this).
        seen = 0 if state is None else state.disclosure_seq
        outcome = self._facts.release(owner_id, persona_id, conversation_id, version=seen, at=at)
        if outcome is ReleaseOutcome.CONFLICT:
            msg = "the conversation changed since it was read"
            raise HoldChangedError(msg)
        if outcome is ReleaseOutcome.GONE:
            raise ConversationNotFoundError(
                "conversation not found", context={"id": conversation_id}
            )
        return self._hold(owner_id, conversation_id)

    def _persona(self, owner_id: str, conversation_id: str) -> str:
        persona_id = self._persona_of(owner_id, conversation_id)
        if persona_id is None:
            raise ConversationNotFoundError(
                "conversation not found", context={"id": conversation_id}
            )
        return persona_id

    def _hold(
        self,
        owner_id: str,
        conversation_id: str,
        state: DisclosureState | None = None,
    ) -> ConversationHold:
        flagged = self._store.flagged_nodes(owner_id)
        refs = [conversation_id]
        if state is None:
            state = self._facts.states(owner_id, refs).get(conversation_id)
        version = version_token(
            owner_id,
            conversation_id,
            state,
            latest_tagged=_latest_tagged(flagged, conversation_id),
        )
        released = self._gate.released(owner_id, refs, flagged=flagged)
        if conversation_id in released:
            return ConversationHold(
                state="released", released_at=released[conversation_id], version=version
            )
        if conversation_id in self._gate.disclosed(owner_id, refs, flagged=flagged):
            return ConversationHold(state="held", released_at=None, version=version)
        return ConversationHold(state="not_held", released_at=None, version=version)


def not_held() -> ConversationHold:
    """The control where no graph is composed: nothing is ever tagged, nothing is held."""
    return _NOT_HELD

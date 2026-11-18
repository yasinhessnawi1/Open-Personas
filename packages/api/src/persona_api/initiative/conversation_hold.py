"""The per-conversation "fine to bring this up" control (D-OWNER-11-18 (16), D-ORCH-11-18-u).

A conversation in which the user disclosed something sensitive is kept out of the persona's
unprompted initiative (R9-253). The owner can say, from the web app and nowhere else, that the
persona may draw on it again, and can undo that. This module is the read and the change behind
``/v1/conversations/{id}/initiative-hold``:

- ``held``: a disclosure keeps the conversation out (the gate's strict ``disclosed``);
- ``released``: the user's release holds (:meth:`ConversationWellbeingGate.released`);
- ``not_held``: nothing keeps it out, so there is nothing to release.

``version`` is the disclosure count the user saw. A release is written only for a held
conversation and only for that count, so a disclosure recorded in between is never released
unseen. Only the conversation's owner can read or change it: an unknown conversation, or
another owner's, is not found.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from sqlalchemy import select

from persona_api.db.engine import rls_connection
from persona_api.db.models import conversations as conversations_t
from persona_api.errors import ConversationNotFoundError
from persona_api.initiative.disclosures import PgDisclosureStore, ReleaseOutcome
from persona_api.initiative.wellbeing_gate import ConversationWellbeingGate

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from persona.graph.protocol import GraphStore
    from sqlalchemy import Engine

__all__ = ["ConversationHold", "ConversationHoldService", "HoldChangedError", "HoldState"]

HoldState = Literal["held", "released", "not_held"]


class HoldChangedError(Exception):
    """A disclosure was recorded after the user read the control (answer 409, read again)."""


@dataclass(frozen=True)
class ConversationHold:
    """What the control shows: the state, when a release was given, the count seen."""

    state: HoldState
    released_at: datetime | None
    version: int


_NOT_HELD = ConversationHold(state="not_held", released_at=None, version=0)


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
        version: int | None,
        at: datetime,
    ) -> ConversationHold:
        """Release (``released``) for the count seen, or hold it back again; the new state."""
        persona_id = self._persona(owner_id, conversation_id)
        if not released:
            self._facts.hold(owner_id, persona_id, conversation_id)
            return self._hold(owner_id, conversation_id)
        if version is None:
            msg = "a release needs the version the user saw"
            raise ValueError(msg)
        current = self._hold(owner_id, conversation_id)
        if current.state != "held":
            return current
        outcome = self._facts.release(owner_id, persona_id, conversation_id, version=version, at=at)
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

    def _hold(self, owner_id: str, conversation_id: str) -> ConversationHold:
        flagged = self._store.flagged_nodes(owner_id)
        refs = [conversation_id]
        state = self._facts.states(owner_id, refs).get(conversation_id)
        version = 0 if state is None else state.disclosure_seq
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

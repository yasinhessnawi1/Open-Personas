"""Which interaction the current turn, run or task leg belongs to (R9-253).

A tool is built once per persona, but the interaction it runs in changes per turn. The api
binds the interaction here, around the work, with a token it resets in ``finally`` (the
pattern of the RLS owner and the sandbox request context): a web or connector chat turn and a
delegated voice turn as ``conversation``, an agentic run as ``agentic_run``, a task leg as
``task``. ``record_user_fact`` reads it at dispatch, so a direct write names the interaction it
was learned in and a disclosure there gates that interaction for initiative. Unbound means no
interaction is known; the write then carries none, as before.

The graph-window precedent (``persona_runtime.graph_window``) sets its ContextVar without a
reset; this one always resets, so an interaction never outlives the work it was bound to.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass

__all__ = ["InteractionRef", "bind_interaction", "current_interaction", "reset_interaction"]


@dataclass(frozen=True)
class InteractionRef:
    """The interaction a write happens in: its kind and its id."""

    kind: str
    id: str


_CURRENT: ContextVar[InteractionRef | None] = ContextVar(
    "persona_runtime_interaction", default=None
)


def bind_interaction(kind: str, interaction_id: str) -> Token[InteractionRef | None]:
    """Bind the current interaction; return the token to reset it with."""
    if not kind or not interaction_id:
        msg = "an interaction needs a kind and an id"
        raise ValueError(msg)
    return _CURRENT.set(InteractionRef(kind=kind, id=interaction_id))


def reset_interaction(token: Token[InteractionRef | None]) -> None:
    """Restore what was bound before ``token`` (call it in a ``finally``)."""
    _CURRENT.reset(token)


def current_interaction() -> InteractionRef | None:
    """The bound interaction, or ``None`` when none is."""
    return _CURRENT.get()

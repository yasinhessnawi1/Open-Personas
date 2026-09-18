"""The ambient leg spend reporter: how a tool inside a task leg reports what it cost.

A task leg's ledger tallies three kinds of spend (:class:`~persona.tasks.ledger.SpendKind`:
model / sandbox / external). The MODEL kind is priced by the leg handler from the loop's
per-step usage callback. The other two are spent INSIDE a tool at dispatch time, in the
hosted ``code_execution`` tool and in the MCP adapter, and until 2026-09-18 neither told
the enclosing leg anything. The per-task budget cap and the per-leg spend bound therefore
saw model spend only: a task that ran the sandbox hard could pass far beyond the cap the
user set, invisibly, because the sandbox and external columns were never written at all.

This module is the door those tools report through. It ACCOUNTS; it never bills. The
sandbox tool keeps charging the owner through the billing seam exactly as before, and the
figure it reports here is the same number it charged, so the ledger and the charge cannot
disagree. An external (MCP) call reports the cost M3 rules for it inside an enclosing
billed op, which is :data:`SUBSUMED_EXTERNAL_CALL_CENTS`.

**Ambient, not a constructor argument.** The tools are built by the runtime factory, deep
below the leg handler, through a toolbox build whose signature ten other callers share.
Threading a per-leg reporter through every one of them is the same widening the api's
sandbox request context was introduced to avoid, so this takes the same shape: a
:class:`~contextvars.ContextVar` the leg handler binds around its run and the tools read
at dispatch. ``contextvars`` propagate through ``await`` and into ``asyncio.to_thread``,
so a tool dispatched anywhere inside the leg's task sees the binding, and a second leg
running concurrently in its own task sees its own. Outside a leg (interactive chat, a
run, the CLI, the tests that build a bare tool) nothing is bound, :func:`report_leg_spend`
returns ``False``, and nothing changes there.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import TYPE_CHECKING, Final, Protocol, runtime_checkable

if TYPE_CHECKING:
    from persona.tasks.ledger import SpendKind

__all__ = [
    "SUBSUMED_EXTERNAL_CALL_CENTS",
    "LegSpendReporter",
    "bind_leg_spend_reporter",
    "report_leg_spend",
    "reset_leg_spend_reporter",
]

#: What one connector / MCP call costs the leg that made it: nothing, by ruling.
#:
#: Spec M3 (T7) rules that sub-cent infra inside an enclosing billed op (embeddings,
#: connectors, MCP) is subsumed by that op's credit floor rather than charged per call;
#: the per-call infra rate fires only for a genuinely standalone op, which a call made
#: from inside a leg is not. So the priced cost of such a call, as M3 accounts it, is
#: zero, and that is the value the adapter reports. It is a real writer recording the
#: ruled value, not a column nobody writes: the difference is that a future ruling that
#: prices these calls changes this one number and nothing else.
SUBSUMED_EXTERNAL_CALL_CENTS: Final[float] = 0.0


@runtime_checkable
class LegSpendReporter(Protocol):
    """Where a tool inside a leg reports a priced cost (the api leg handler satisfies it)."""

    def report(self, kind: SpendKind, cost_cents: float) -> None:
        """Account ``cost_cents`` (US cents, already priced through M3) under ``kind``."""
        ...


_REPORTER: ContextVar[LegSpendReporter | None] = ContextVar(
    "persona_leg_spend_reporter", default=None
)


def bind_leg_spend_reporter(reporter: LegSpendReporter) -> Token[LegSpendReporter | None]:
    """Bind ``reporter`` for the current async context; return the token to reset with."""
    return _REPORTER.set(reporter)


def reset_leg_spend_reporter(token: Token[LegSpendReporter | None]) -> None:
    """Restore the binding that ``token`` replaced (call from a ``finally`` block)."""
    _REPORTER.reset(token)


def report_leg_spend(kind: SpendKind, cost_cents: float) -> bool:
    """Report a priced cost to the enclosing leg, if there is one.

    Args:
        kind: The spend class the cost belongs to.
        cost_cents: The cost in US cents, priced the same way it was (or would be) charged.

    Returns:
        ``True`` when a leg was listening and took the report; ``False`` outside a leg,
        where the report is dropped by design (interactive surfaces keep no task ledger).
    """
    reporter = _REPORTER.get()
    if reporter is None:
        return False
    reporter.report(kind, cost_cents)
    return True

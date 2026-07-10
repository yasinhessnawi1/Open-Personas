"""Startup restart sweep — reconcile orphaned in-flight rows (spec P1, T3, D-P1-restart-sweep).

The honest cross-restart story under the single-worker model (D-08-5): a chat
turn (``ChatTurnRegistry``) and an agentic run (``RunRegistry``) both execute as
in-process ``asyncio`` tasks. A process restart loses every task, but their DB
rows stay in a non-terminal state — a chat turn's assistant row stays
``streaming_status='running'``; a run stays ``running`` / ``awaiting_user``. A
client reattaching then finds no live task (the SSE 404s) and the UI spins
forever.

On API startup, BEFORE serving any request, :func:`reconcile_in_flight_on_startup`
marks every such orphaned row terminal — ``interrupted`` for chat turns (the
checkpointed partial is preserved; it renders as a stopped, not errored, message)
and ``error`` for runs (S08-2: viewable, not resumable). It runs on the
RLS-bypassing engine (the admin engine on cloud / the community engine on
sqlite) so it reconciles every tenant's rows, and it is **idempotent**: once the
rows are terminal a second pass matches nothing.

R9-022: this is the deploy's SINGLE-WRITER assumption made load-bearing — cloud
runs exactly one API process (D-08-5) and community's embedded/self-hosted
Postgres is the same single-process shape, so "no live registry entry survives
a restart" is universally true for every ``running`` row this sweep finds; there
is no second writer whose in-flight turn this could ever mistakenly interrupt.
It is the BETWEEN-PROCESS half of the orphaned-turn recovery story — it only
runs once, at boot. The BETWEEN-RESTARTS half (a long-lived process sitting on
an orphan for hours before its next restart) is the lazy self-heal in
``chat_service.start_chat_turn`` (``MessagesTurnSink.heal_orphaned_running``),
which reconciles the identical row shape the moment the conversation is next
used, without waiting for a boot. A healed count is logged at WARNING (never
silent) — an orphan is evidence of an unclean prior shutdown, worth an
operator's attention even though the row itself is now safely terminal.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from persona.logging import get_logger
from sqlalchemy import update

from persona_api.db.models import messages as messages_t
from persona_api.db.models import runs as runs_t

if TYPE_CHECKING:
    from sqlalchemy import Engine

_log = get_logger("api.restart_sweep")

__all__ = ["reconcile_in_flight_on_startup"]

# Runs left non-terminal by a restart. ``awaiting_user`` is also orphaned — its
# in-process response queue died with the task, so it can never be answered.
_ORPHANED_RUN_STATES = ("running", "awaiting_user")


def reconcile_in_flight_on_startup(*, engine: Engine) -> dict[str, int]:
    """Mark orphaned in-flight chat turns + runs terminal. Returns the per-table counts.

    MUST run on an RLS-bypassing engine (admin on cloud, the community engine on
    sqlite) — at startup no user scope is bound, so an RLS-scoped engine would
    match 0 rows. Idempotent: terminal rows are not matched on a second pass.

    Args:
        engine: The RLS-bypassing engine (``admin_engine`` if present, else the
            community ``rls_engine``).

    Returns:
        ``{"messages": <n turns interrupted>, "runs": <n runs errored>}``.
    """
    now = datetime.now(UTC)
    with engine.begin() as conn:
        msg_result = conn.execute(
            update(messages_t)
            .where(messages_t.c.streaming_status == "running")
            .values(streaming_status="interrupted")
        )
        run_result = conn.execute(
            update(runs_t)
            .where(runs_t.c.status.in_(_ORPHANED_RUN_STATES))
            .values(status="error", error="interrupted by a server restart", finished_at=now)
        )
    counts = {"messages": msg_result.rowcount, "runs": run_result.rowcount}
    if counts["messages"] or counts["runs"]:
        # R9-022: WARNING, not INFO — never silent (this repo's standing rule). A
        # nonzero count means the PRIOR process died mid-turn/mid-run; the rows are
        # now safely terminal, but an operator should still see that it happened.
        _log.warning(
            "restart sweep reconciled orphaned in-flight rows "
            "turns_interrupted={turns} runs_errored={runs}",
            turns=counts["messages"],
            runs=counts["runs"],
        )
    return counts

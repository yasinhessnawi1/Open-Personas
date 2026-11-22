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

R9-318: the same pass frees the durable long-op slots (``inflight_ops``) the previous
process held when it died. A turn abandoned at the shutdown deadline never reaches the
``finally`` that releases its slot, and the TTL would keep that slot for an hour, so a user
at the concurrency cap would get 429 until it aged out. The sweep is scoped to this api
instance's earlier boots (see :func:`persona.concurrency.release_previous_boot_long_ops`),
so it stays correct if a second api process is ever added, as long as each has its own
``PERSONA_API_INSTANCE_ID``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from persona.concurrency import PROCESS_BOOT_ID, release_previous_boot_long_ops
from persona.logging import get_logger
from sqlalchemy import update

from persona_api.db.connection_bounds import lift_statement_timeout
from persona_api.db.models import messages as messages_t
from persona_api.db.models import runs as runs_t

if TYPE_CHECKING:
    from sqlalchemy import Engine

_log = get_logger("api.restart_sweep")

__all__ = ["reconcile_in_flight_on_startup"]

# Runs left non-terminal by a restart. ``awaiting_user`` was also orphaned on the
# premise that its in-process response queue died with the task, so it could never be
# answered. Spec W1 (D-W1-34) makes that premise conditional: a run belonging to a TASK
# parks on its question durably — the task sits ``waiting(on_user)`` and the answer comes
# back through the reply route, which no restart can lose — so reaping it would error a
# run the user is about to answer. A parked run with no task behind it is still the old
# in-process shape and is still an orphan.
_ORPHANED_RUN_STATES = ("running", "awaiting_user")


def reconcile_in_flight_on_startup(
    *,
    engine: Engine,
    long_op_instance: str,
    boot_id: str = PROCESS_BOOT_ID,
) -> dict[str, int]:
    """Mark orphaned in-flight chat turns + runs terminal and free their long-op slots.

    MUST run on an RLS-bypassing engine (admin on cloud, the community engine on
    sqlite) — at startup no user scope is bound, so an RLS-scoped engine would
    match 0 rows. Idempotent: terminal rows are not matched on a second pass.

    Args:
        engine: The RLS-bypassing engine (``admin_engine`` if present, else the
            community ``rls_engine``).
        long_op_instance: This api instance's name (``PERSONA_API_INSTANCE_ID``). Only
            its own earlier boots' long-op slots are freed.
        boot_id: The current process's boot. Defaults to the one this process stamps on
            the slots it takes.

    Returns:
        ``{"messages": <n turns interrupted>, "runs": <n runs errored>,
        "long_ops": <n long-op slots freed>}``.
    """
    now = datetime.now(UTC)
    with engine.begin() as conn:
        # R9-313: either UPDATE can scan its whole table. This runs in the lifespan, so a
        # statement timeout here would fail every boot and crash-loop the api.
        lift_statement_timeout(conn)
        msg_result = conn.execute(
            update(messages_t)
            .where(messages_t.c.streaming_status == "running")
            .values(streaming_status="interrupted")
        )
        run_result = conn.execute(
            update(runs_t)
            .where(
                runs_t.c.status.in_(_ORPHANED_RUN_STATES),
                # Spec W1 (D-W1-34): a parked run that belongs to a task is answerable
                # after any restart, so it is never an orphan. Only the old in-process
                # shape (no task behind it) is.
                (runs_t.c.status != "awaiting_user") | (runs_t.c.task_id.is_(None)),
            )
            .values(status="error", error="interrupted by a server restart", finished_at=now)
        )
        # R9-318: the slots those chat turns held die with them. Only a chat turn takes a
        # long-op slot (W1, D-W1-24: an agentic one-off runs on the worker instead).
        long_ops = release_previous_boot_long_ops(
            conn=conn, instance_id=long_op_instance, boot_id=boot_id
        )
    counts = {"messages": msg_result.rowcount, "runs": run_result.rowcount, "long_ops": long_ops}
    if any(counts.values()):
        # R9-022: WARNING, not INFO — never silent (this repo's standing rule). A
        # nonzero count means the PRIOR process died mid-turn/mid-run; the rows are
        # now safely terminal, but an operator should still see that it happened.
        _log.warning(
            "restart sweep reconciled orphaned in-flight rows "
            "turns_interrupted={turns} runs_errored={runs} long_ops_released={long_ops}",
            turns=counts["messages"],
            runs=counts["runs"],
            long_ops=counts["long_ops"],
        )
    return counts

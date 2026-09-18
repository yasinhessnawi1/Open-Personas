"""Postgres TurnLogWriter (spec 08, T12, D-08-7).

Implements the spec-05 ``TurnLogWriter`` Protocol against the ``turn_logs`` table
(spec 07). Injected into the ConversationLoop (the loop is agnostic to the sink —
hexagonal). ``turn_logs`` is RLS-scoped via ``conversations``, so the write runs
under the request/run's tenant scope (the loop calls ``write`` synchronously
mid-turn, inside the active contextvar scope).

R9-179 item 3: this row is also where the api learns which model ACTUALLY served a
turn (D-M2-2), so it is where the free-model daily request meter is read off, the
same attribution point, not a second one. The meter is an injected collaborator and
absent by default, so a writer built without one behaves exactly as before.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import insert

from persona_api.db.models import turn_logs as turn_logs_t

if TYPE_CHECKING:
    from persona_runtime.logging import TurnLog
    from sqlalchemy import Engine

    from persona_api.services.free_model_usage import FreeModelDailyCounter

__all__ = ["PostgresTurnLogWriter"]


class PostgresTurnLogWriter:
    """Persists each :class:`TurnLog` to the ``turn_logs`` table (CQS: write-only)."""

    def __init__(
        self,
        engine: Engine,
        *,
        free_model_counter: FreeModelDailyCounter | None = None,
    ) -> None:
        self._engine = engine
        self._free_model_counter = free_model_counter

    def write(self, log: TurnLog) -> None:
        """Insert one turn log row. No return (CQS). RLS-scoped via conversations."""
        with self._engine.begin() as conn:
            conn.execute(
                insert(turn_logs_t).values(
                    id=f"turnlog_{uuid.uuid4().hex}",
                    conversation_id=log.conversation_id,
                    turn_index=log.turn_index,
                    tier_used=log.tier_used,
                    model_name=log.model_name,
                    provider=log.provider,
                    prompt_tokens=log.prompt_tokens,
                    completion_tokens=log.completion_tokens,
                    latency_ms=log.latency_ms,
                    cost_cents=log.cost_cents,
                    # Spec M2 (D-M2-4): the pricing provenance rides the row.
                    cost_basis=log.cost_basis,
                    tool_calls=log.tool_calls,
                    skill_used=log.skill_used,
                    history_compacted=log.history_compacted,
                    created_at=log.timestamp,
                )
            )
        # R9-179 item 3, meter the account-wide free daily cap off the SERVED pair
        # this row already carries. After the insert, so the durable record is never
        # held hostage to the gauge; the counter is fail-soft in its own right.
        if self._free_model_counter is not None:
            self._free_model_counter.record_served(
                provider=log.provider, model=log.model_name, when=log.timestamp
            )

"""The free-model daily request meter (R9-179 item 3).

OpenRouter caps free-model calls at N requests per UTC DAY **account-wide**, 1,000
since credits were bought, 50 before, and that one ceiling is shared by every
free-plan user's turn and by every background job that runs on a free chain
(synthesis, consolidation, title refresh, initiative scans). Nothing measured it, so
the first sign of exhaustion would have been free users' turns failing in the
afternoon, with no way to tell that from a provider outage.

This counts them, and says so out loud on the way up: an INFO line as the day's use
crosses 25, 50 and 75 percent of the cap, and a WARNING at 90, each exactly once per
UTC day, so the log is a signal rather than a stream.

**Where it counts.** At the api's EXISTING served-model attribution points, never at
the backend layer in core:

* :class:`persona_api.services.turn_log_writer.PostgresTurnLogWriter`, the per-turn
  served pair (D-M2-2: ``model_name`` / ``provider`` name the model that ACTUALLY
  served the turn, not the wrapper's primary), so every chat turn is counted.
* :func:`persona_api.services.background_billing.bill_background_llm`, the same pair
  for the background surfaces that bill their owner post-hoc.

**What it counts is a floor, honestly.** One served attribution = one counted request.
A tool-use turn that ran N sub-loop rounds is N requests at OpenRouter but one
TurnLog row, and a failed fallback attempt is a request nobody attributes at all. So
the meter can under-read a heavy day; it can never over-read. It is an early-warning
gauge, not an invoice, the invoice is OpenRouter's own dashboard.

**Once-per-day, without a flag to forget.** The guard is not a remembered "already
warned" bit: the increment is atomic and returns the day's NEW count, so each integer
occurs exactly once per day, and a threshold announces only on the count that IS its
boundary. That survives a restart for free, the count lives in the row, so a process
that comes back mid-day continues where the day was rather than re-warning from zero.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from persona.backends.credentials import is_free_openrouter_slot
from persona.logging import get_logger
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from persona_api.config import APIConfig
from persona_api.db.models import free_model_daily_usage as usage_t

if TYPE_CHECKING:
    from datetime import date

    from sqlalchemy import Engine

__all__ = ["FREE_DAILY_THRESHOLD_PERCENTS", "FreeModelDailyCounter"]

_LOG = get_logger("api.free_model_usage")

#: The share-of-cap marks the meter announces, in order. The first three are INFO
#: (the day is progressing); 90 is the WARNING, the point at which moving background
#: jobs off the free chain has to be a decision rather than a discovery.
FREE_DAILY_THRESHOLD_PERCENTS: Final[tuple[int, ...]] = (25, 50, 75, 90)

_WARNING_AT_PERCENT: Final[int] = 90


class FreeModelDailyCounter:
    """Counts free-model requests per UTC day and announces the day's climb.

    Stateless in the process: the whole count is one persisted row per day, so two
    api workers and a background worker share one number and a restart continues it.

    Args:
        engine: The api's SQLAlchemy engine. The table is account-wide and carries no
            user column, so the write needs no tenant scope.
        daily_cap: The provider's per-UTC-day free-request ceiling. ``<= 0`` disables
            the meter entirely (no counting, no logging), the "we are not on a
            capped free plan" posture.
    """

    def __init__(self, engine: Engine, *, daily_cap: int) -> None:
        self._engine = engine
        self._daily_cap = daily_cap

    @classmethod
    def from_env(cls, engine: Engine) -> FreeModelDailyCounter:
        """Build a counter whose cap comes from ``PERSONA_OPENROUTER_FREE_DAILY_CAP``."""
        return cls(engine, daily_cap=APIConfig().openrouter_free_daily_cap)

    @property
    def enabled(self) -> bool:
        """Is the meter armed (a positive cap)?"""
        return self._daily_cap > 0

    def record_served(
        self,
        *,
        provider: str,
        model: str,
        when: datetime | None = None,
    ) -> None:
        """Count one request served by a free model. No return value (CQS).

        Fail-soft throughout: a meter that cannot write must never break the turn or
        the background op it is measuring. A paid model is not counted at all.

        Args:
            provider: The SERVED provider (not the chain's primary).
            model: The SERVED model id.
            when: The moment the request was served; defaults to now. Converted to
                UTC before the day is taken, so the day rolls at UTC midnight
                whatever the server's local zone is.
        """
        if not self.enabled:
            return
        if not is_free_openrouter_slot(provider, model):
            return
        moment = when or datetime.now(UTC)
        day = moment.astimezone(UTC).date()
        try:
            count = self._increment(day)
        except Exception as exc:  # noqa: BLE001, the meter never breaks what it measures
            _LOG.warning(
                "free-model daily meter failed (fail-soft) day={day} model={model}: {err}",
                day=day.isoformat(),
                model=model,
                err=str(exc),
            )
            return
        self._announce(day=day, count=count)

    def _increment(self, day: date) -> int:
        """Atomically add one to ``day``'s row and return the NEW count.

        One statement, so concurrent workers can never read-modify-write over each
        other: every caller gets a distinct integer back, which is what makes the
        threshold announcements exactly-once without a remembered flag.
        """
        with self._engine.begin() as conn:
            # The upsert is dialect-specific (cloud Postgres, community SQLite); the
            # ON CONFLICT + RETURNING shape is identical on both.
            insert = pg_insert if conn.dialect.name == "postgresql" else sqlite_insert
            stmt = (
                insert(usage_t)
                .values(day=day, request_count=1)
                .on_conflict_do_update(
                    index_elements=["day"],
                    set_={"request_count": usage_t.c.request_count + 1},
                )
                .returning(usage_t.c.request_count)
            )
            return int(conn.execute(stmt).scalar_one())

    def _announce(self, *, day: date, count: int) -> None:
        """Log the thresholds this exact count is the boundary of (once per day each)."""
        for percent in FREE_DAILY_THRESHOLD_PERCENTS:
            if count != self._boundary(percent):
                continue
            log = _LOG.warning if percent >= _WARNING_AT_PERCENT else _LOG.info
            log(
                "free-model daily requests at {percent}% of the account cap "
                "day={day} count={count} cap={cap}",
                percent=percent,
                day=day.isoformat(),
                count=count,
                cap=self._daily_cap,
            )

    def _boundary(self, percent: int) -> int:
        """The first count that is at least ``percent`` of the cap."""
        return math.ceil(self._daily_cap * percent / 100)

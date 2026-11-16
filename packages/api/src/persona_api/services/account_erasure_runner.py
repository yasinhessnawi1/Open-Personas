"""Where an account erasure runs, and how an unfinished one is resumed (R9-284 T9).

Not a job: ``jobs.owner_id`` cascades from ``users``, so a job owned by the account would delete
its own row at the erasure's ``row`` step. An erasure runs on one dedicated thread instead, and
the ledger is the queue: a request hands the claimed erasure to the thread, and a sweep in every
api process hands it every unfinished account erasure again (at start, then on an interval).
The erasure lock makes a second run of the same erasure a no-op, so two processes sweeping is
safe.
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from persona.logging import get_logger

from persona_api.billing.account_erasure import StripeAccountEraser
from persona_api.config import Edition
from persona_api.services.account_erasure import AccountEraseDeps, erase_account
from persona_api.services.clerk_users import ClerkUsers
from persona_api.services.erasure_ledger import ErasureLedger, SubjectKind
from persona_api.services.erasure_start import erase_deps_from_state

if TYPE_CHECKING:
    from persona_api.services.erasure_ledger import Erasure

__all__ = [
    "SWEEP_SECONDS",
    "AccountEraseRunner",
    "account_erase_deps_from_state",
    "start_account_erasures",
]

_log = get_logger("api.erasure")

#: How often each api process hands unfinished account erasures back to its runner.
SWEEP_SECONDS = 300.0

# A year here is 366 days, so the retention is never shorter than the configured years.
_DAYS_PER_YEAR = 366


def account_erase_deps_from_state(state: Any) -> AccountEraseDeps | None:  # noqa: ANN401 - app.state
    """The account erasure's deps for this process; ``None`` off cloud or without a platform."""
    config = state.config
    if config.edition is not Edition.cloud:
        return None
    persona = erase_deps_from_state(state)
    if persona is None:
        return None
    gateway = getattr(state, "stripe_gateway", None)
    secret = config.clerk_secret_key
    return AccountEraseDeps(
        persona=persona,
        stripe=None if gateway is None else StripeAccountEraser(gateway.client),
        clerk=None if secret is None else ClerkUsers(secret.get_secret_value()),
        retention=timedelta(days=_DAYS_PER_YEAR * config.billing_retention_years),
    )


class AccountEraseRunner:
    """Runs account erasures one at a time on a dedicated thread."""

    def __init__(self, deps: AccountEraseDeps) -> None:
        self._deps = deps
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="account-erase")
        self._closed = threading.Event()

    def submit(self, erasure: Erasure) -> None:
        """Hand the erasure to the thread; a no-op once closed (the sweep resumes it later)."""
        if self._closed.is_set():
            return
        self._executor.submit(self._run, erasure)

    def sweep(self) -> int:
        """Hand every unfinished account erasure to the thread; how many were handed over."""
        pending = [
            e
            for e in ErasureLedger(self._deps.engine).unfinished()
            if e.subject_kind is SubjectKind.ACCOUNT
        ]
        for erasure in pending:
            self.submit(erasure)
        return len(pending)

    def close(self) -> None:
        self._closed.set()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _run(self, erasure: Erasure) -> None:
        try:
            erase_account(self._deps, erasure)
        except Exception as exc:  # noqa: BLE001 - recorded failed in the ledger; resumed
            _log.error(
                "account erasure failed; the sweep resumes it erasure={eid} error={err}",
                eid=erasure.id,
                err=type(exc).__name__,
            )


def start_account_erasures(
    state: Any,  # noqa: ANN401 - app.state
    *,
    interval_seconds: float = SWEEP_SECONDS,
) -> asyncio.Task[None] | None:
    """Build the runner on ``state.account_erase_runner`` and sweep now and every interval.

    ``None`` (and no runner) off cloud or without a platform engine. A failed sweep is logged
    and never stops the process; the task is cancelled at shutdown.
    """
    deps = account_erase_deps_from_state(state)
    if deps is None:
        state.account_erase_runner = None
        return None
    runner = AccountEraseRunner(deps)
    state.account_erase_runner = runner

    def _once() -> None:
        try:
            count = runner.sweep()
        except Exception as exc:  # noqa: BLE001 - must not stop the process; logged loudly
            _log.error("could not sweep account erasures error={err}", err=type(exc).__name__)
            return
        if count:
            _log.warning("handed unfinished account erasures to the runner count={n}", n=count)

    async def _loop() -> None:
        while True:
            await asyncio.to_thread(_once)
            await asyncio.sleep(interval_seconds)

    return asyncio.get_running_loop().create_task(_loop())

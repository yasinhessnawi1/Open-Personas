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
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from persona.logging import get_logger

from persona_api.billing.account_erasure import StripeAccountEraser
from persona_api.config import Edition
from persona_api.services.account_erasure import (
    AccountEraseDeps,
    erase_account,
    unready_reason,
)
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

#: The longest a failing erasure waits between attempts (review L4); the stall alert covers it.
MAX_BACKOFF_SECONDS = 3600.0

# A year here is 366 days, so the retention is never shorter than the configured years.
_DAYS_PER_YEAR = 366


#: An env-template placeholder (``__OWNER_CLERK_PROD__`` and the like) that was never replaced.
_PLACEHOLDER = re.compile(r"__[A-Z0-9_]+__")


def secret_value(secret: Any) -> str | None:  # noqa: ANN401 - a SecretStr or None
    """The secret's value, or ``None`` when it is unset, empty (an env line left blank) or still
    a template placeholder (review round 3 LOW-5): none of those is a key."""
    value: str | None = None if secret is None else str(secret.get_secret_value()).strip()
    if not value or _PLACEHOLDER.fullmatch(value):
        return None
    return value


def warn_missing_secrets(config: Any) -> list[str]:  # noqa: ANN401 - APIConfig
    """Log, at boot on cloud, each account-deletion secret that is not set; the names."""
    missing = [
        name
        for name, value in (
            ("PERSONA_CLERK_SECRET_KEY", config.clerk_secret_key),
            ("PERSONA_CLERK_WEBHOOK_SECRET", config.clerk_webhook_secret),
        )
        if secret_value(value) is None
    ]
    for name in missing:
        _log.warning(
            "account deletion is not fully configured: {name} is unset; deletion requests "
            "answer 503 until it is",
            name=name,
        )
    return missing


def account_erase_deps_from_state(state: Any) -> AccountEraseDeps | None:  # noqa: ANN401 - app.state
    """The account erasure's deps for this process; ``None`` off cloud or without a platform."""
    config = state.config
    if config.edition is not Edition.cloud:
        return None
    persona = erase_deps_from_state(state)
    if persona is None:
        return None
    gateway = getattr(state, "stripe_gateway", None)
    secret = secret_value(config.clerk_secret_key)
    return AccountEraseDeps(
        persona=persona,
        stripe=None if gateway is None else StripeAccountEraser(gateway.client),
        clerk=None if secret is None else ClerkUsers(secret),
        retention=timedelta(days=_DAYS_PER_YEAR * config.billing_retention_years),
    )


class AccountEraseRunner:
    """Runs account erasures one at a time on a dedicated thread."""

    def __init__(self, deps: AccountEraseDeps, *, clock: Any = time.monotonic) -> None:  # noqa: ANN401
        self._deps = deps
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="account-erase")
        self._closed = threading.Event()
        self._clock = clock
        # A failing erasure is retried after SWEEP_SECONDS, then twice as long each time, up
        # to MAX_BACKOFF_SECONDS (review L4), so it does not hit Stripe or Clerk every sweep.
        self._backoff: dict[str, tuple[float, float]] = {}  # id -> (delay, not before)
        self._lock = threading.Lock()

    def backing_off(self, erasure_id: str) -> bool:
        """Whether the sweep is leaving this erasure alone for now."""
        with self._lock:
            entry = self._backoff.get(erasure_id)
        return entry is not None and self._clock() < entry[1]

    def _failed(self, erasure_id: str) -> None:
        with self._lock:
            previous = self._backoff.get(erasure_id, (0.0, 0.0))[0]
            delay = min(max(SWEEP_SECONDS, previous * 2), MAX_BACKOFF_SECONDS)
            self._backoff[erasure_id] = (delay, self._clock() + delay)

    def _succeeded(self, erasure_id: str) -> None:
        with self._lock:
            self._backoff.pop(erasure_id, None)

    def unready_reason(self, *, owner_id: str, origin: str) -> str | None:
        """Why this runner could not finish that account's erasure (``None``: it can)."""
        return unready_reason(self._deps, owner_id=owner_id, origin=origin)

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
            if e.subject_kind is SubjectKind.ACCOUNT and not self.backing_off(e.id)
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
            self._failed(erasure.id)
            _log.error(
                "account erasure failed; the sweep resumes it erasure={eid} error={err}",
                eid=erasure.id,
                err=type(exc).__name__,
            )
        else:
            self._succeeded(erasure.id)


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
    warn_missing_secrets(state.config)
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

"""Long-poll inbound transport (Spec C2 T7, D-C2-1) — the dev receive path.

The zero-infra alternative to the webhook: pull updates with ``getUpdates`` in a
loop (no public HTTPS endpoint needed). Mutually exclusive with the webhook per
bot — the composition root picks one by config. Each consumed update advances the
``offset`` (``last_update_id + 1``) so Telegram drops it from the next batch (the
ack), then is handed to the same injected ``on_update`` handler the webhook uses —
so the inbound flow is transport-agnostic.

api-free: a loop over the injected client + handler; ``should_continue`` is
injected so a test (or a shutdown signal) can stop it deterministically.

**Observability + fault-tolerance (R9-065):** the loop logs once at INFO on start
(so "is the poller alive?" is answerable) and at DEBUG per non-empty batch (never
per empty poll — long-poll returns constantly and that would flood). A failed
``get_updates`` (network blip, Telegram 5xx) is caught, logged at WARNING, and
retried after a short backoff — one bad poll must never kill the loop or take the
whole service down with it (previously an uncaught exception here propagated into
``asyncio.gather`` in ``__main__._amain`` unlogged). A normal shutdown
(``asyncio.CancelledError``) is re-raised untouched, never swallowed.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from persona.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from persona_connectors.telegram.client import TelegramClient

__all__ = ["run_long_poll"]

_log = get_logger("connectors.telegram_longpoll")

_DEFAULT_ALLOWED = ("message",)
_DEFAULT_ERROR_BACKOFF_SECONDS = 2.0


def _always() -> bool:
    return True


async def run_long_poll(
    *,
    client: TelegramClient,
    on_update: Callable[[dict[str, object]], Awaitable[None]],
    timeout: int = 30,
    allowed_updates: list[str] | None = None,
    should_continue: Callable[[], bool] = _always,
    error_backoff_seconds: float = _DEFAULT_ERROR_BACKOFF_SECONDS,
) -> None:
    """Poll Telegram for updates and dispatch each to ``on_update`` (D-C2-1, dev).

    Args:
        client: The Bot API client.
        on_update: The inbound-update handler (the flow) — the SAME one the webhook
            calls, so the flow never knows which transport delivered the update.
        timeout: The long-poll wait (seconds) passed to ``getUpdates``.
        allowed_updates: Update types to receive (defaults to ``["message"]`` —
            text-message-only v1).
        should_continue: Polled each iteration; return ``False`` to stop the loop
            (a shutdown hook / a test bound).
        error_backoff_seconds: Delay before retrying after a failed ``get_updates``
            call (a couple of seconds by default) — stops a persistently failing
            API from hot-spinning the loop; injectable so tests run fast.
    """
    allowed = list(allowed_updates) if allowed_updates is not None else list(_DEFAULT_ALLOWED)
    offset: int | None = None
    _log.info("telegram long-poll loop starting")
    while should_continue():
        try:
            updates = await client.get_updates(
                offset=offset, timeout=timeout, allowed_updates=allowed
            )
        except asyncio.CancelledError:
            raise  # normal shutdown — never swallow cancellation
        except Exception as exc:  # noqa: BLE001 — one bad poll must never kill the loop/service
            _log.warning("telegram get_updates failed: {error}", error=str(exc))
            await asyncio.sleep(error_backoff_seconds)
            continue
        if updates:
            _log.debug("telegram long-poll received {count} update(s)", count=len(updates))
        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int) and not isinstance(update_id, bool):
                offset = update_id + 1  # ack: this update won't be re-delivered
            await on_update(update)

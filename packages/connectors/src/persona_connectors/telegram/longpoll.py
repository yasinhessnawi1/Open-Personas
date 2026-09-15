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

**The one fault this loop cannot recover from, and says so (2026-09-15):** Telegram allows a
single ``getUpdates`` consumer per bot token and terminates the previous caller on every new
one. Two deployments polling the same bot therefore evict each other indefinitely, and the
user sees a persona that answers, apologises, and answers again at random. Retrying cannot
help, because the other consumer is not going away. A SUSTAINED run of 409s is escalated to
``ERROR`` naming the cause and the remedy; a one-off stays a warning, since a deploy overlaps
consumers briefly by design. No lock and no self-shutdown: D-I1-7 ruled that a stale advisory
lock silently keeping a platform DOWN is worse than a loud, bounded hazard. This is the part
that makes it loud, which it was not when it happened.
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

#: Telegram's "only one bot instance" error. One consumer per bot token, full stop: each new
#: ``getUpdates`` terminates the one before it, so two consumers evict each other forever and
#: an update lands on whichever won the race at that instant. To a user that is a persona
#: that answers, then apologises, then answers, at random.
_CONFLICT_MARKER = "error_code=409"

#: How many CONSECUTIVE conflicts mean a second consumer is really running, rather than the
#: brief overlap of a deploy where the old machine has not stopped yet. At a couple of seconds
#: of backoff each, five is roughly ten seconds: far longer than a handover, far shorter than
#: a person noticing their bot is "glitchy".
_CONFLICT_ESCALATE_AFTER = 5

#: Re-state the ERROR every N conflicts after that, so it stays visible in a busy log without
#: becoming the log.
_CONFLICT_REPEAT_EVERY = 100


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
    conflicts = 0
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
            conflicts = conflicts + 1 if _CONFLICT_MARKER in str(exc) else 0
            if conflicts == _CONFLICT_ESCALATE_AFTER or (
                conflicts > _CONFLICT_ESCALATE_AFTER and conflicts % _CONFLICT_REPEAT_EVERY == 0
            ):
                # Not another warning. A sustained 409 is not a blip, it is a second
                # deployment polling this same bot token, and it makes every conversation
                # unreliable for as long as it lasts. It ran for forty hours once, logged
                # only as a repeating WARNING among thousands of lines, and was found by a
                # person saying the bot felt "glitchy" (I1 cutover, step 2 skipped).
                _log.error(
                    "telegram has TWO consumers on this bot token ({count} conflicts in a "
                    "row). Only one process may long-poll a bot: every getUpdates "
                    "terminates the previous one, so messages are delivered to whichever "
                    "wins the race and the rest fail. Stop the other one. If the api runs "
                    "embedded connectors (PERSONA_API_EMBED_CONNECTORS), the standalone "
                    "connectors app must be scaled to zero, and it must run ONE machine "
                    "even alone. See docs/ops/connectors_fold_cutover.md.",
                    count=conflicts,
                )
            await asyncio.sleep(error_backoff_seconds)
            continue
        conflicts = 0
        if updates:
            _log.debug("telegram long-poll received {count} update(s)", count=len(updates))
        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int) and not isinstance(update_id, bool):
                offset = update_id + 1  # ack: this update won't be re-delivered
            await on_update(update)

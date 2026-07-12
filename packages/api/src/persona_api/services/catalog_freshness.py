"""OpenRouter catalog warm + TTL refresh — the api lifespan's freshness task (Spec M2, D-M2-6).

Two jobs, both OFF the event loop and NEVER on the turn path:

* **Warm at boot** (the M2 F5 closure): the cost estimator resolves with
  ``allow_fetch=False``, so until the catalog index is built, OpenRouter-only
  models estimate ``unpriced``. The first :func:`refresh_catalog_once` run
  performs the fetch + index build in a worker thread so no turn — first or
  otherwise — ever pays it.
* **Periodic TTL refresh**: with a positive client TTL, re-check every
  ``poll_s``; ``refresh_if_stale`` fetches only when the window expired and
  keeps serving the stale copy on failure (fail-open, stale-forever beats
  empty). A refreshed catalog is followed by a derived-index ``reindex`` —
  never a second fetch.

The loop is fail-soft forever (a refresh error never crashes the app) and is
cancelled at shutdown by the lifespan.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from persona.logging import get_logger

if TYPE_CHECKING:
    from persona.backends.metadata import OpenRouterModelMetadataResolver
    from persona.backends.openrouter_catalog import OpenRouterCatalogClient

__all__ = ["refresh_catalog_once", "run_catalog_refresh_loop"]

_LOG = get_logger("api.services.catalog_freshness")

#: Ceiling between staleness checks — a check is a no-op while fresh, so
#: polling more often than the TTL only bounds how LATE past expiry a refresh
#: can happen. Floor guards against a pathologically small TTL busy-looping.
_MAX_POLL_S = 900.0
_MIN_POLL_S = 60.0


def refresh_catalog_once(
    client: OpenRouterCatalogClient,
    resolver: OpenRouterModelMetadataResolver | None,
) -> bool:
    """One warm-or-refresh step (sync — callers run it via ``asyncio.to_thread``).

    ``refresh_if_stale`` covers BOTH arms: a never-fetched client is stale by
    definition (the boot warm), an expired one re-fetches, a fresh one is a
    fast no-op, and a failed fetch stale-serves (returns ``False``). When a
    fresh catalog landed, the derived metadata index follows via ``reindex``
    (a rebuild from the client's cache — no second fetch).

    Returns:
        ``True`` iff a fresh catalog was fetched this step.
    """
    refreshed = client.refresh_if_stale()
    if resolver is not None and (refreshed or not resolver.warm):
        # Rebuild on a fresh catalog — or build the FIRST index at boot even
        # when the client cache was already warm (e.g. another consumer
        # fetched first): the whole point of the warm is a queryable index.
        resolver.reindex()
    return refreshed


async def run_catalog_refresh_loop(
    client: OpenRouterCatalogClient,
    resolver: OpenRouterModelMetadataResolver | None,
) -> None:
    """The lifespan task: immediate warm, then TTL-paced refresh checks.

    Every step runs in a worker thread (``asyncio.to_thread``) — the sync
    httpx fetch never touches the event loop (the M2 §2 invariant + the
    event-loop-starvation lesson). TTL off (``client.ttl_s <= 0``) → warm
    once and exit (the cache is process-lifetime by contract). Fail-soft
    forever: any unexpected error logs and the loop keeps its cadence.
    """
    try:
        await asyncio.to_thread(refresh_catalog_once, client, resolver)
    except Exception:  # noqa: BLE001 — the warm is best-effort by design
        _LOG.opt(exception=True).warning("catalog warm failed; estimates degrade until refresh")
    if client.ttl_s <= 0:
        return
    poll_s = max(_MIN_POLL_S, min(client.ttl_s, _MAX_POLL_S))
    while True:
        await asyncio.sleep(poll_s)
        try:
            await asyncio.to_thread(refresh_catalog_once, client, resolver)
        except Exception:  # noqa: BLE001 — refresh must never crash the app
            _LOG.opt(exception=True).warning("catalog refresh step failed; will retry")

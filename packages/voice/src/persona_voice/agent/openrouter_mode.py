"""The OpenRouter subscription mode voice builds its tier registries with (R9-224).

Voice composes its own tier registries (D-V5-6) and, until R9-224, built them with no
subscription mode at all, so a chain the api filtered to its ``:free`` models ran
unfiltered on a call. The mode now comes from the one resolver every process shares,
:func:`persona_runtime.openrouter_subscription.resolve_openrouter_subscription_mode`.

Two things differ from the api, both deliberate:

* **Off the event loop.** The resolver may probe OpenRouter synchronously (up to the
  catalog client's 30 s timeout). Its callers are the launcher's registry build, which
  runs on the event loop in the voice app's startup and again on a call's path when
  the startup build did not finish, and the runner's fallback build for a direct
  caller. So the resolution runs on a worker thread and never stalls audio.
* **No resolution failure stops voice.** The api and the connector service refuse to
  boot on an invalid ``PERSONA_OPENROUTER_SUBSCRIPTION_MODE``. Voice ignored the
  setting until R9-224 and has never probed OpenRouter at boot, so neither a latent bad
  value nor a probe failing in a way nobody has seen yet may turn into an outage. Voice
  logs ERROR naming the setting and the exception class, never the message (which can
  quote the value), and keeps its behaviour from before R9-224: no free-mode filter.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from persona.logging import get_logger
from persona_runtime.openrouter_subscription import (
    SUBSCRIPTION_MODE_ENV,
    resolve_openrouter_subscription_mode,
)

if TYPE_CHECKING:
    from persona.backends.openrouter_catalog import OpenRouterSubscriptionMode

__all__ = ["resolve_voice_openrouter_mode"]

_logger = get_logger("agent.openrouter_mode")


async def resolve_voice_openrouter_mode() -> OpenRouterSubscriptionMode | None:
    """Resolve the OpenRouter subscription mode for voice, on a worker thread.

    Returns:
        ``"free"`` / ``"paid"``, or ``None`` when OpenRouter is unconfigured, its key
        was rejected, or the resolution failed in any way (voice only; see the module
        docstring).
    """
    try:
        return await asyncio.to_thread(resolve_openrouter_subscription_mode)
    except Exception as exc:  # noqa: BLE001 - voice must boot whatever the resolution does
        _logger.error(
            "{setting} could not be resolved ({error_class}); voice applies no OpenRouter "
            "free-mode filter, as before it read the setting",
            setting=SUBSCRIPTION_MODE_ENV,
            error_class=type(exc).__name__,
        )
        return None

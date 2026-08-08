"""Turn an internal failure into something safe to show a person (R9-097).

A tier exhaustion used to reach the user as the raw stringified exception::

    every backend in MultiModelChatBackend exhausted [tier=frontier
    attempt_count=2 attempts_json=[{'provider': 'openrouter', 'model':
    'openai/gpt-oss-20b:free', 'last_error_class': 'EmptyCompletionError', ...}]
    final_error_class=RateLimitError]

That is three failures at once: it leaks our vendor mix, model ids and routing
strategy to anyone who hits a busy moment; it tells the reader nothing about
what to do; and it is indistinguishable from the product being broken, so a
transient capacity blip reads as a fault.

The exception itself is good telemetry and stays exactly as it is --
``AllModelsFailedError`` carries ``tier`` / ``attempt_count`` / ``attempts_json``
/ ``final_error_class``, which is precisely what diagnosis needs. Only the
*rendering at the user boundary* changes: log and store the full detail, show a
person the sentence below.

**Scope is deliberately narrow.** Only failures known to carry internal detail
are rewritten. Other exceptions pass through untouched, because some carry
genuinely useful, already-safe text (an empty-reply notice, a credits message),
and blanket-genericising them would trade one bad experience for another. Widen
this only per error class, with the same "is this safe AND useful" test.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.backends import AllModelsFailedError
from persona.logging import get_logger

if TYPE_CHECKING:
    from sqlalchemy import Engine

__all__ = [
    "CAPACITY_BUSY_FREE_MESSAGE",
    "CAPACITY_BUSY_MESSAGE",
    "owner_on_free_plan",
    "user_facing_error_message",
]

_log = get_logger("services.user_facing_errors")

#: Capacity exhaustion, paid plan. Names the condition, says what to do, and
#: does not imply the reader did anything wrong.
CAPACITY_BUSY_MESSAGE = (
    "The models are unusually busy right now, so this one didn't get through. "
    "Give it a minute and try again."
)

#: Capacity exhaustion, free plan. Same honesty, plus the one thing that
#: actually fixes it for them. Never shown to a paid user, for whom "the free
#: models are busy" would simply be false.
CAPACITY_BUSY_FREE_MESSAGE = (
    "The free models are in heavy demand right now, so this one didn't get through. "
    "Give it a minute and try again, or move to a paid plan for capacity that's yours."
)


def user_facing_error_message(exc: Exception, *, on_free_plan: bool = False) -> str | None:
    """The safe message for ``exc``, or ``None`` to keep the existing text.

    Args:
        exc: The failure that ended the turn.
        on_free_plan: Whether the affected owner is on the free plan. Selects
            between the two capacity messages: telling a paying customer that
            "the free models are busy" would be wrong, and telling a free user
            only "try again" hides the fix.

    Returns:
        A sentence safe to show a person, or ``None`` when the exception is not
        one this module rewrites (the caller then keeps whatever it had).
    """
    if isinstance(exc, AllModelsFailedError):
        return CAPACITY_BUSY_FREE_MESSAGE if on_free_plan else CAPACITY_BUSY_MESSAGE
    return None


def owner_on_free_plan(engine: Engine | None, owner_id: str) -> bool:
    """Whether ``owner_id`` is on the free plan, for message selection only.

    Lives here rather than on one worker because THREE surfaces now render the
    same failure (chat turns, agentic runs, task legs) and each needs the same
    choice between the two capacity messages. A second copy is how the messages
    would drift apart.

    Only ever reached on an error path, so the extra indexed read costs nothing
    in the normal case.

    **Fail-safe is ``False``.** If the plan cannot be determined we show the
    neutral "models are busy" line rather than the free-tier one, because telling
    a paying customer that "the free models are busy" is simply false, while
    showing a free user the neutral line merely omits the upgrade hint.

    Args:
        engine: The RLS engine, or ``None`` in a deployment without one.
        owner_id: The affected owner.

    Returns:
        ``True`` when the owner is known to be on the free plan.
    """
    if engine is None:
        return False
    try:
        from persona_api.services import subscription_service  # noqa: PLC0415

        row = subscription_service.get_subscription(engine, user_id=owner_id)
    except Exception as exc:  # noqa: BLE001 - message selection must never break the caller
        _log.warning("plan lookup for the failure message failed: {err}", err=str(exc))
        return False
    if row is None:
        return True  # no subscription row IS the free plan (the D-M4-9 default)
    return str(row.get("plan_code") or "free") == "free"

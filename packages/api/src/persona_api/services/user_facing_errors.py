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

from persona.backends import AllModelsFailedError

__all__ = [
    "CAPACITY_BUSY_FREE_MESSAGE",
    "CAPACITY_BUSY_MESSAGE",
    "user_facing_error_message",
]

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

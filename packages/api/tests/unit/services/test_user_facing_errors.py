"""A capacity failure must not leak our vendor mix to users (R9-097).

The bug was not "the message was ugly". It was that the raw exception -- which
names every provider, model id, tier and error class the router tried -- was
rendered straight into the user's chat. These tests assert the LEAK is closed,
not merely that some friendly string exists, because a friendly string sitting
next to the leaked detail would still be the bug.
"""

from __future__ import annotations

import pytest
from persona.backends import AllModelsFailedError
from persona_api.services.user_facing_errors import (
    CAPACITY_BUSY_FREE_MESSAGE,
    CAPACITY_BUSY_MESSAGE,
    user_facing_error_message,
)

#: The real production exception, verbatim in shape: this is what used to reach
#: the user (2026-08-02, owner report).
_REAL_EXHAUSTION = AllModelsFailedError(
    "every backend in MultiModelChatBackend exhausted",
    context={
        "tier": "frontier",
        "attempt_count": "2",
        "attempts_json": str(
            [
                {
                    "provider": "openrouter",
                    "model": "openai/gpt-oss-20b:free",
                    "last_error_class": "EmptyCompletionError",
                    "retried_same_model": True,
                },
                {
                    "provider": "openrouter",
                    "model": "google/gemma-4-31b-it:free",
                    "last_error_class": "RateLimitError",
                    "retried_same_model": True,
                },
            ]
        ),
        "final_error_class": "RateLimitError",
    },
)

#: Every token that must never reach a user: our providers, our exact model
#: ids, our internal tier names, and the exception-class vocabulary.
_MUST_NOT_LEAK = (
    "openrouter",
    "gpt-oss-20b",
    "gemma-4-31b",
    "MultiModelChatBackend",
    "RateLimitError",
    "EmptyCompletionError",
    "attempts_json",
    "frontier",
    "tier=",
)


@pytest.mark.parametrize("on_free_plan", [True, False])
def test_the_capacity_message_leaks_nothing(on_free_plan: bool) -> None:
    """THE regression: no provider, model id, tier or error class in user copy."""
    message = user_facing_error_message(_REAL_EXHAUSTION, on_free_plan=on_free_plan)
    assert message is not None
    lowered = message.lower()
    for secret in _MUST_NOT_LEAK:
        assert secret.lower() not in lowered, (
            f"user-facing copy leaks {secret!r} — this is our vendor mix and routing "
            f"strategy shown to anyone who hits a busy moment. Copy was: {message!r}"
        )


def test_the_raw_exception_really_does_contain_what_we_are_hiding() -> None:
    """Guards the test above from rotting into a tautology.

    If the exception ever stops carrying this detail, the leak assertions would
    pass trivially and silently stop protecting anything.
    """
    raw = str(_REAL_EXHAUSTION).lower()
    assert "openrouter" in raw, "the fixture no longer reproduces the real leak"
    assert "gpt-oss-20b" in raw


def test_a_paid_user_is_never_told_the_free_models_are_busy() -> None:
    """Telling a paying customer their free models are busy is simply false."""
    message = user_facing_error_message(_REAL_EXHAUSTION, on_free_plan=False)
    assert message == CAPACITY_BUSY_MESSAGE
    assert "free" not in message.lower()


def test_a_free_user_is_told_what_actually_fixes_it() -> None:
    """The free-tier line names the condition and the one thing that resolves it."""
    message = user_facing_error_message(_REAL_EXHAUSTION, on_free_plan=True)
    assert message == CAPACITY_BUSY_FREE_MESSAGE
    assert "free" in message.lower()


def test_both_messages_tell_the_reader_to_retry() -> None:
    """A transient failure that does not say 'try again' reads as permanent."""
    for message in (CAPACITY_BUSY_MESSAGE, CAPACITY_BUSY_FREE_MESSAGE):
        assert "try again" in message.lower()
        # The standing voice rule: no em dashes in user-visible copy.
        assert "—" not in message


def test_an_unmapped_exception_passes_through_untouched() -> None:
    """Scope is deliberately narrow: only known-unsafe failures are rewritten.

    Other exceptions may carry genuinely useful, already-safe text, and blanket
    genericising would trade one bad experience for another.
    """
    assert user_facing_error_message(ValueError("something specific and useful")) is None

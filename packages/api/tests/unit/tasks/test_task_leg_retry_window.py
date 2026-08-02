"""A task leg's retry window must outlast a provider rate-limit window (R9-092).

Production: a scheduled leg dead-lettered with "every backend in
MultiModelChatBackend exhausted", the two free frontier models having returned
`EmptyCompletionError` and `RateLimitError`. The leg's dominant failure mode is
provider CAPACITY, not a code fault -- but its retry policy was sized for a code
fault: 3 attempts on a 2s base backs off 2s then 4s and gives up in about six
seconds, against free-tier limits that reset on a per-minute window.

This is also why chat kept working while scheduled legs failed on the identical
model chain: `MultiModelChatBackend`'s own same-model retry is a 200ms sleep and
it discards any `Retry-After` hint above 2s (D-20-10). Failing fast is correct
for a user-facing turn and wrong for background work, so the tolerance has to
live at the job layer.

These tests pin the PROPERTY (the window outlasts a rate limit), not the exact
constants, so tuning stays free but a silent regression to fail-fast does not.
"""

from __future__ import annotations

from persona.jobs.models import RetryPolicy
from persona_api.tasks.handler import TASK_LEG_RETRY_POLICY

#: A free-tier rate limit typically resets on a per-minute window; the retry
#: window must comfortably clear one, or a transient limit is a permanent failure.
_RATE_LIMIT_WINDOW_SECONDS = 60.0


def _total_window(policy: RetryPolicy) -> float:
    """Worst-case seconds spanned by all backoffs before dead-lettering."""
    return sum(policy.backoff_for(attempt) for attempt in range(1, policy.max_attempts))


def test_the_leg_retry_window_outlasts_a_rate_limit_window() -> None:
    """THE regression: ~6 seconds of backoff cannot survive a per-minute limit."""
    window = _total_window(TASK_LEG_RETRY_POLICY)
    assert window > _RATE_LIMIT_WINDOW_SECONDS, (
        f"a leg gives up after {window:.0f}s of backoff, inside a "
        f"{_RATE_LIMIT_WINDOW_SECONDS:.0f}s rate-limit window — a transient "
        "provider limit becomes a permanent task failure"
    )


def test_the_first_retry_is_not_immediate() -> None:
    """Retrying within seconds just re-hits the same limit and burns an attempt."""
    assert TASK_LEG_RETRY_POLICY.backoff_for(1) >= 10.0, (
        "the first retry must give the provider real time to recover"
    )


def test_the_window_stays_bounded() -> None:
    """Tolerance, not unboundedness: a genuinely broken leg must still dead-letter."""
    assert TASK_LEG_RETRY_POLICY.max_attempts <= 8, "a broken leg must not retry indefinitely"
    assert _total_window(TASK_LEG_RETRY_POLICY) < 3600.0, "the retry window must stay under an hour"

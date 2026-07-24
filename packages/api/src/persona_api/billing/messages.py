"""User-facing billing copy (Spec M4, T5b; Tension-5).

The out-of-credits / upgrade prompts live in the API layer — NOT in persona-core (product
copy must not live in the provider-agnostic engine; core keeps raising the domain error).
The web renders the plan-appropriate CTA from the structured ``error`` code + ``action``.
"""

from __future__ import annotations

__all__ = [
    "CREDITS_EXHAUSTED_DETAIL",
    "FREE_CAPACITY_UNAVAILABLE_DETAIL",
    "UPGRADE_ACTION",
]

#: The structured client hint (the web maps it to the plan-appropriate CTA:
#: "Upgrade" for a Free user, "Add credits" for a paid user).
UPGRADE_ACTION = "upgrade"

#: Balance exhausted (rides M3's cutoff) — a real upgrade prompt, never "contact support".
CREDITS_EXHAUSTED_DETAIL = (
    "You're out of credits. Upgrade your plan or add a credit pack to keep going."
)

#: A free user's model capacity is unavailable (empty/unconfigured free tier → the
#: fail-closed T5a path raises ``TierNotConfiguredError``) — a graceful surface, never a
#: 500 stack trace. Generic enough to also cover a misconfigured paid tier gracefully.
FREE_CAPACITY_UNAVAILABLE_DETAIL = (
    "Model capacity is unavailable right now. Upgrade your plan for more capacity, "
    "or try again shortly."
)

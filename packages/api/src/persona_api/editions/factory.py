"""Edition selection (Spec 33, D-33-1): build the seam impls from config.

A single ``PERSONA_EDITION`` switch picks the ``OwnerResolver`` and
``CreditsPolicy`` implementations at the app factory. Call sites consume the
selected interface from ``app.state`` — no scattered ``if edition`` checks.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona_api.config import Edition
from persona_api.editions.credits_policy import (
    CreditsPolicy,
    MeteredCreditsPolicy,
    UnlimitedCreditsPolicy,
)
from persona_api.editions.owner_resolver import (
    CloudOwnerResolver,
    CommunityOwnerResolver,
    OwnerResolver,
)

if TYPE_CHECKING:
    from persona_api.config import APIConfig

__all__ = ["build_credits_policy", "build_owner_resolver"]


def build_owner_resolver(config: APIConfig) -> OwnerResolver:
    """The edition's request-ownership resolver (§2.1)."""
    if config.edition is Edition.cloud:
        return CloudOwnerResolver()
    return CommunityOwnerResolver(
        owner_id=config.community_owner_id, email=config.community_owner_email
    )


def build_credits_policy(config: APIConfig) -> CreditsPolicy:
    """The edition's credits policy (§2.2).

    Spec R7 (R7-D-6): cloud's metered policy carries the per-UTC-day spend cap
    (``CREDITS_MAX_PER_DAY``); community's :class:`UnlimitedCreditsPolicy` no-ops, so
    the cap never applies to a self-host install.
    """
    if config.edition is Edition.cloud:
        return MeteredCreditsPolicy(daily_cap=config.credits_max_per_day)
    return UnlimitedCreditsPolicy()

"""Open-core edition seams (Spec 33).

The commercial concerns — request ownership (auth), credits metering — sit
behind interfaces with a community default impl and the existing cloud impl,
selected once by ``PERSONA_EDITION`` at the app factory (D-33-1). The
persistence seam (SQLite vs Postgres) lives in ``persona_api.db``; the safety
guard lives in :mod:`persona_api.editions.guard`.
"""

from __future__ import annotations

from persona_api.editions.cloud_guard import check_cloud_config_guard
from persona_api.editions.credits_policy import (
    CreditsPolicy,
    MeteredCreditsPolicy,
    UnlimitedCreditsPolicy,
)
from persona_api.editions.factory import build_credits_policy, build_owner_resolver
from persona_api.editions.gateway_guard import check_gateway_edition_posture
from persona_api.editions.guard import check_public_noauth_guard
from persona_api.editions.owner_resolver import (
    CloudOwnerResolver,
    CommunityOwnerResolver,
    OwnerResolver,
)
from persona_api.editions.per_tenant_mcp_guard import check_per_tenant_mcp_posture

__all__ = [
    "CloudOwnerResolver",
    "CommunityOwnerResolver",
    "CreditsPolicy",
    "MeteredCreditsPolicy",
    "OwnerResolver",
    "UnlimitedCreditsPolicy",
    "build_credits_policy",
    "build_owner_resolver",
    "check_cloud_config_guard",
    "check_gateway_edition_posture",
    "check_per_tenant_mcp_posture",
    "check_public_noauth_guard",
]

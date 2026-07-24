"""Credits counter + deduction — back-compat re-export shim.

The implementation has been relocated to :mod:`persona.credits.service` at
Spec 19 L6c (D-19-X-credits-service-domain-relocation) so persona-voice can
consume the same surface without taking a persona-api dependency — the voice
surface is latency-critical (R-V1-1) and cannot afford an HTTP/RPC hop. This
module is now a thin re-export shim that preserves every prior import site
(``from persona_api.services import credits_service`` continues to work
byte-for-byte) and lets the existing api-side tests keep their import paths.

See :mod:`persona.credits` for the canonical surface. New call sites should
prefer the persona-core path.
"""

from __future__ import annotations

from persona.credits import (
    deduct,
    ensure_balance,
    get_balance,
    list_turn_usage,
    list_usage,
    refund,
    require_credits,
    wallet_snapshot,
)

__all__ = [
    "deduct",
    "ensure_balance",
    "get_balance",
    "list_turn_usage",
    "list_usage",
    "refund",
    "require_credits",
    "wallet_snapshot",
]

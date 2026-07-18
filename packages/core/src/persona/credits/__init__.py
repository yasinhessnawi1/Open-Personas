"""Credits counter + ledger surface shared by persona-api and persona-voice.

Relocated from ``persona_api.services.credits_service`` at Spec 19 L6c
(D-19-X-credits-service-domain-relocation; additive amendment per the
V1 T03 D-V1-X-jwt-verifier-extraction precedent). persona-api re-exports
for back-compat; persona-voice imports from here directly so it does not take
a persona-api dependency — the voice surface is latency-critical per R-V1-1
and cannot afford an HTTP/RPC hop to the API for credit checks.

Public surface mirrors the prior ``persona_api.services.credits_service``:

* :func:`require_credits` — pre-flight gate (raises :class:`CreditsExhaustedError`).
* :func:`ensure_balance` / :func:`get_balance` — read current balance.
* :func:`deduct` / :func:`refund` — atomic balance moves with ledger row.
* :func:`capture_up_to` — the opt-in partial-capture sibling of ``deduct``
  (Spec M2 review, C1); ONLY the chat-turn worker's post-success billing uses
  it — every other caller keeps using ``deduct`` unchanged.
* :func:`list_usage` / :func:`list_turn_usage` — paginated audit log views.
* :data:`LOW_BALANCE_THRESHOLD` — UI warning threshold.

The implementation is verbatim from ``persona_api.services.credits_service`` with
one structural change: the SQLAlchemy table objects are defined locally on a
private :class:`MetaData` (the same pattern :mod:`persona.stores.postgres` uses
for ``memory_chunks``) so persona-core does not import the persona-api db.models
module. A contract test (the api side's existing route integration tests)
guards that the two table views agree.
"""

from __future__ import annotations

from persona.credits.service import (
    LOW_BALANCE_THRESHOLD,
    capture_up_to,
    capture_up_to_idempotent,
    deduct,
    deduct_idempotent,
    ensure_balance,
    get_balance,
    list_turn_usage,
    list_usage,
    refund,
    require_credits,
)

__all__ = [
    "LOW_BALANCE_THRESHOLD",
    "capture_up_to",
    "capture_up_to_idempotent",
    "deduct",
    "deduct_idempotent",
    "ensure_balance",
    "get_balance",
    "list_turn_usage",
    "list_usage",
    "refund",
    "require_credits",
]

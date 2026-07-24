"""Protocol conformance for the M4 ``grant_idempotent`` surface (Spec M4, T1b).

``pytest -m contract`` — proves the grant primitive is declared across the whole
injected surface: the ``CreditsPolicy`` Protocol (cloud ``MeteredCreditsPolicy``
delegates; community ``UnlimitedCreditsPolicy`` no-ops) and the core ``LedgerPort``
(``CoreCreditsLedger``). Both Protocols are ``@runtime_checkable``, so the concrete
editions must structurally satisfy them. No DB — the community no-op is proven to
touch no engine by handing it a sentinel that would raise if used.
"""

from __future__ import annotations

import pytest
from persona.billing import CoreCreditsLedger, LedgerPort
from persona_api.editions.credits_policy import (
    _UNLIMITED_BALANCE,  # noqa: PLC2701 — the community sentinel, asserted by the no-op test
    CreditsPolicy,
    MeteredCreditsPolicy,
    UnlimitedCreditsPolicy,
)

pytestmark = pytest.mark.contract


def test_credits_policy_protocol_declares_grant_idempotent() -> None:
    assert hasattr(CreditsPolicy, "grant_idempotent")


def test_both_editions_conform_to_credits_policy_with_grant() -> None:
    """Runtime-checkable structural conformance — both editions carry the whole
    surface INCLUDING ``grant_idempotent``."""
    metered = MeteredCreditsPolicy()
    unlimited = UnlimitedCreditsPolicy()
    assert isinstance(metered, CreditsPolicy)
    assert isinstance(unlimited, CreditsPolicy)
    assert hasattr(metered, "grant_idempotent")
    assert hasattr(unlimited, "grant_idempotent")


def test_core_ledger_conforms_to_ledger_port_with_grant() -> None:
    ledger = CoreCreditsLedger()
    assert isinstance(ledger, LedgerPort)
    assert hasattr(ledger, "grant_idempotent")


def test_community_grant_is_a_noop_that_touches_no_db() -> None:
    """The community edition is unmetered — ``grant_idempotent`` returns the
    sentinel balance and NEVER touches the engine (M4 is a cloud-only no-op).

    The ``rls_engine`` is a sentinel object with no DB methods: if the no-op tried
    to open a transaction it would ``AttributeError`` — so a clean return proves no
    DB write."""
    sentinel_engine = object()
    result = UnlimitedCreditsPolicy().grant_idempotent(
        rls_engine=sentinel_engine,  # type: ignore[arg-type]
        user_id="anyone",
        amount=500,
        reason="subscription_renewal",
        billing_key="in_evt_x",
        cost_basis="grant_subscription",
    )
    assert result == _UNLIMITED_BALANCE

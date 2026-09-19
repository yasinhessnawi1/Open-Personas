"""A subscription entitles you while it is being paid for, not merely while the row says "pro".

``subscription`` carries a ``plan_code`` and a ``status``, and the status is written verbatim
from Stripe (``handlers.py`` passes ``sub.status`` straight through), so it is Stripe's whole
vocabulary: active, trialing, past_due, unpaid, incomplete, incomplete_expired, canceled,
paused. Entitlement read ONLY ``plan_code``, and ``mark_past_due`` leaves ``plan_code`` alone,
so a subscriber whose payment failed kept every paid model. The free monthly allowance is
guarded by the same shape, from the other side: its SQL skipped anyone with a non-free
``plan_code``, so that same user was also denied the free allowance they had fallen back to.
Entitled to what they were not paying for, denied the fallback they should have had.

**The ruling, made here because the repo did not have one.** The M4 documents say nothing
about status (``trialing`` appears nowhere in the tree), and the only status rule in the code
was ``autotopup``'s stricter ``== "active"``, which is a CHARGING decision and rightly
stricter. Entitlement is ``active`` or ``trialing``: Stripe's two paid-and-current states. A
trial is an entitled state by Stripe's own semantics, and nothing configures trials today, so
including it costs nothing now and is right the day one is configured.
"""

from __future__ import annotations

import pytest
from persona.billing.plans import ENTITLED_SUBSCRIPTION_STATUSES, entitled_plan_code


@pytest.mark.parametrize("status", ["active", "trialing"])
def test_a_paid_and_current_subscription_entitles_its_plan(status: str) -> None:
    assert entitled_plan_code("pro", status) == "pro"


@pytest.mark.parametrize(
    "status",
    ["past_due", "unpaid", "incomplete", "incomplete_expired", "canceled", "paused"],
)
def test_a_subscription_that_is_not_being_paid_for_falls_back_to_free(status: str) -> None:
    """The defect, per status: every one of these kept the paid models."""
    assert entitled_plan_code("pro", status) == "free"


def test_an_unknown_status_falls_back_to_free() -> None:
    """Stripe adds states; a status we do not recognise must not open the paid tiers.

    The same restrictive default the plan read already applies to an unknown plan_code.
    """
    assert entitled_plan_code("pro", "some_future_state") == "free"


@pytest.mark.parametrize("status", [None, ""])
def test_a_missing_status_falls_back_to_free(status: str | None) -> None:
    """``handlers.py`` writes ``str(sub.status or "")``, so an empty status is reachable."""
    assert entitled_plan_code("pro", status) == "free"


@pytest.mark.parametrize("status", ["active", "past_due", "canceled", None])
def test_a_free_plan_is_free_whatever_the_status_says(status: str | None) -> None:
    """A free row's status is noise: the subscription table defaults it to 'active' for
    everyone, including users who never subscribed."""
    assert entitled_plan_code("free", status) == "free"


def test_an_absent_plan_is_free() -> None:
    assert entitled_plan_code(None, "active") == "free"


def test_the_entitled_set_is_exactly_the_two_paid_and_current_states() -> None:
    """Pinned so widening it is a deliberate edit with a reason, not a quiet drift."""
    assert frozenset({"active", "trialing"}) == ENTITLED_SUBSCRIPTION_STATUSES

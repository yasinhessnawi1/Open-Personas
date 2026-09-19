"""Voice entitles a subscriber on the same rule as every other surface (M4, T5c).

``entitled_plan_code`` settled it for the api: a subscription entitles you while it is being
PAID FOR, not merely while the row still says "pro". ``mark_past_due`` leaves ``plan_code``
alone, so reading that column by itself hands a lapsed subscriber the paid tiers.

Voice kept its own copy of the read. It is a deliberate raw ``SELECT`` on the subscription
table, to keep persona-voice free of a persona-api dependency (the layering line), and that is
exactly why the api-side fix did not reach it: for one commit on main, a past-due subscriber
got free models in chat and background, got their free monthly allowance back, and still got
the FULL PAID registry on a voice call, which is the surface that burns fastest.

The rule crosses no layer: ``entitled_plan_code`` lives in ``persona.billing.plans``, which is
core and MIT, and persona-voice already depends on ``persona.billing`` for its ledger and its
billing config. One rule, three call sites.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from persona_voice.agent import runner as runner_module
from persona_voice.agent.runner import _select_voice_tier_registry
from persona_voice.config import VoiceConfig
from sqlalchemy import create_engine, text

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine

_PAID = "PAID-REGISTRY"
_FREE = "FREE-REGISTRY"
_USER = "u1"


@pytest.fixture
def engine() -> Iterator[Engine]:
    """The subscription table, created raw because voice reads it raw."""
    eng = create_engine("sqlite+pysqlite:///:memory:")
    with eng.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE subscription ("
                "user_id TEXT PRIMARY KEY, plan_code TEXT NOT NULL, "
                "status TEXT NOT NULL DEFAULT 'active')"
            )
        )
    yield eng
    eng.dispose()


def _subscribe(eng: Engine, plan_code: str, status: str) -> None:
    with eng.begin() as conn:
        conn.execute(
            text("INSERT INTO subscription (user_id, plan_code, status) VALUES (:u, :p, :s)"),
            {"u": _USER, "p": plan_code, "s": status},
        )


def _select(eng: Engine, *, cloud: bool = True) -> str:
    config = VoiceConfig(edition="cloud" if cloud else "community")
    return _select_voice_tier_registry(
        config=config,
        tier_registry=_PAID,  # type: ignore[arg-type]
        free_tier_registry=_FREE,  # type: ignore[arg-type]
        rls_engine=eng,
        user_id=_USER,
    )


@pytest.mark.parametrize("status", ["active", "trialing"])
def test_a_paid_and_current_subscriber_gets_the_paid_registry(engine: Engine, status: str) -> None:
    _subscribe(engine, "pro", status)

    assert _select(engine) is _PAID


@pytest.mark.parametrize(
    "status",
    ["past_due", "unpaid", "incomplete", "incomplete_expired", "canceled", "paused"],
)
def test_a_lapsed_subscriber_gets_the_free_registry(engine: Engine, status: str) -> None:
    """The defect, per status: every one of these reached the paid voice models."""
    _subscribe(engine, "pro", status)

    assert _select(engine) is _FREE


def test_an_unknown_status_falls_back_to_free(engine: Engine) -> None:
    """Stripe adds states; an unrecognised one must not open the paid tiers on a call."""
    _subscribe(engine, "pro", "some_future_state")

    assert _select(engine) is _FREE


@pytest.mark.parametrize("status", ["active", "past_due"])
def test_a_free_plan_is_free_whatever_the_status_says(engine: Engine, status: str) -> None:
    """The table defaults status to 'active' for every row, including never-subscribed users."""
    _subscribe(engine, "free", status)

    assert _select(engine) is _FREE


def test_no_subscription_row_is_free(engine: Engine) -> None:
    """The existing fail-safe: a lookup miss can never open the paid tiers."""
    assert _select(engine) is _FREE


def test_a_failed_read_is_free(engine: Engine) -> None:
    """The existing fail-safe, preserved: a broken read must never break a call NOR pay out."""
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE subscription"))

    assert _select(engine) is _FREE


def test_community_is_ungated_and_never_reads_a_plan(engine: Engine) -> None:
    """Self-host has no plans; the paid registry is the only one, byte-identical to before."""
    _subscribe(engine, "free", "canceled")

    assert _select(engine, cloud=False) is _PAID


def test_the_runner_never_selects_a_registry_from_plan_code_alone() -> None:
    """Guard for the shape this finding is made of, mirroring the audit-sink guard.

    Reading ``plan_code`` without ``status`` is silently wrong: it returns a real plan and
    entitles a subscriber who has stopped paying. A behavioural test of the predicate passes
    whether or not this module calls it, so the structural check is what holds the wiring.
    """
    tree = ast.parse(Path(runner_module.__file__).read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "entitled_plan_code"
    ]

    assert calls, (
        "voice resolves a tier registry without routing the subscription through "
        "entitled_plan_code, so a lapsed subscriber keeps the paid voice models"
    )

"""Voice enforces the per-UTC-day spend cap, like every other paid surface (R7).

``CoreCreditsLedger`` takes a ``daily_cap`` and forwards it to ``persona.credits.deduct``;
``0`` means uncapped, and ``0`` is the default. The api wires the configured value through
its edition factory (``MeteredCreditsPolicy(daily_cap=config.credits_max_per_day)``). Voice
built the ledger with no argument at all, so R7's denial-of-wallet guard was absent on the
surface that burns credits fastest: a live call meters STT, TTS, the model and LiveKit
transport every turn, with no ceiling but the wallet itself.

The edition split is the api's, kept deliberately: cloud enforces, community is uncapped,
because a self-host install is single-owner and has no denial-of-wallet surface to guard.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from persona.billing.metered import CoreCreditsLedger
from persona_voice.agent import runner as runner_module
from persona_voice.config import VoiceConfig

if TYPE_CHECKING:
    from sqlalchemy import Engine


class _FakeEngine:
    """The ledger only hands this through to ``deduct``."""


def _engine() -> Engine:
    from typing import cast

    return cast("Engine", _FakeEngine())


# ------------------------------------------------------------------ the configured value


def test_cloud_enforces_the_configured_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSONA_EDITION", "cloud")
    monkeypatch.setenv("CREDITS_MAX_PER_DAY", "7500")

    assert VoiceConfig().effective_daily_cap == 7500


def test_cloud_reads_the_same_variable_the_api_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """``CREDITS_MAX_PER_DAY`` is unprefixed on purpose: one knob, both services, one answer.

    A voice-only name would let the two halves of one product disagree about a user's cap.
    """
    monkeypatch.setenv("PERSONA_EDITION", "cloud")
    monkeypatch.delenv("CREDITS_MAX_PER_DAY", raising=False)

    from persona_api.config import APIConfig

    assert VoiceConfig().credits_max_per_day == APIConfig().credits_max_per_day


def test_community_is_uncapped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Self-host is single-owner: there is nobody to deny the wallet to, and a cap that
    stopped a user's own machine mid-call would be a regression, not a guard."""
    monkeypatch.setenv("PERSONA_EDITION", "community")
    monkeypatch.setenv("CREDITS_MAX_PER_DAY", "7500")

    assert VoiceConfig().effective_daily_cap == 0


# ------------------------------------------------------------------ it reaches the deduct


def test_the_cap_reaches_the_credit_deduct(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ledger is only a forwarder; this pins that the number arrives where it binds."""
    seen: dict[str, Any] = {}

    def _fake_deduct(**kwargs: Any) -> int:  # noqa: ANN401
        seen.update(kwargs)
        return 100

    monkeypatch.setattr("persona.credits.deduct", _fake_deduct)

    CoreCreditsLedger(daily_cap=7500).deduct(
        rls_engine=_engine(), user_id="u1", amount=10, reason="voice:turn"
    )

    assert seen["daily_cap"] == 7500


# ------------------------------------------------------------------ the wiring itself


def test_the_runner_never_builds_an_uncapped_ledger() -> None:
    """Guard for the shape this finding is made of (mirrors the audit-sink guard above it).

    ``CoreCreditsLedger()`` with no argument is silently uncapped, which is why this went
    unnoticed: nothing errors, nothing logs, and the cap simply never applies. A structural
    check is the only kind that survives someone reverting the call site later, because a
    behavioural test of the helper would still pass with the runner back on the default.
    """
    tree = ast.parse(Path(runner_module.__file__).read_text(encoding="utf-8"))

    builds = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "CoreCreditsLedger"
    ]

    assert builds, "the runner no longer builds a credits ledger; this guard needs rewriting"
    for call in builds:
        kwargs = {kw.arg for kw in call.keywords}
        assert "daily_cap" in kwargs, (
            f"CoreCreditsLedger built without daily_cap at runner.py:{call.lineno} "
            ", an unset cap is silently uncapped"
        )

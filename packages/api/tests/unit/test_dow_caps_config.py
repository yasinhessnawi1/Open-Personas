"""Unit tests for the R7 denial-of-wallet knobs + the edition-off no-op path (T10).

No DB, no network:

- Both concurrency knobs + the day-cap knob parse from env with the ratified
  defaults (10000 / 1 / 3).
- ``build_credits_policy`` carries the day cap into cloud's ``MeteredCreditsPolicy``
  and community stays the uncapped ``UnlimitedCreditsPolicy``.
- The edition-off (community / uncapped) path is a TRUE no-op — the concurrency
  helpers take NO advisory lock and write NO row when the cap is 0, so the SQLite
  community backend is never handed Postgres-only SQL.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from persona.concurrency import acquire_user_concurrency, admit_long_op, release_long_op
from persona_api.config import APIConfig, Edition
from persona_api.editions import (
    MeteredCreditsPolicy,
    UnlimitedCreditsPolicy,
    build_credits_policy,
)

# --- config parse --------------------------------------------------------------


def test_dow_knobs_default_to_ratified_values(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "CREDITS_MAX_PER_DAY",
        "MAX_CONCURRENT_BOUNDED_OPS_PER_USER",
        "MAX_CONCURRENT_LONG_OPS_PER_USER",
    ):
        monkeypatch.delenv(var, raising=False)
    config = APIConfig()
    assert config.credits_max_per_day == 10_000
    assert config.max_concurrent_bounded_ops_per_user == 1
    assert config.max_concurrent_long_ops_per_user == 3


def test_dow_knobs_parse_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CREDITS_MAX_PER_DAY", "25000")
    monkeypatch.setenv("MAX_CONCURRENT_BOUNDED_OPS_PER_USER", "2")
    monkeypatch.setenv("MAX_CONCURRENT_LONG_OPS_PER_USER", "5")
    config = APIConfig()
    assert config.credits_max_per_day == 25_000
    assert config.max_concurrent_bounded_ops_per_user == 2
    assert config.max_concurrent_long_ops_per_user == 5


def test_zero_disables_a_knob(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CREDITS_MAX_PER_DAY", "0")
    monkeypatch.setenv("MAX_CONCURRENT_LONG_OPS_PER_USER", "0")
    config = APIConfig()
    assert config.credits_max_per_day == 0
    assert config.max_concurrent_long_ops_per_user == 0


# --- day cap rides the CreditsPolicy edition seam ------------------------------


def test_build_credits_policy_carries_day_cap_in_cloud(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CREDITS_MAX_PER_DAY", "7500")
    policy = build_credits_policy(APIConfig(edition=Edition.cloud))
    assert isinstance(policy, MeteredCreditsPolicy)
    assert policy._daily_cap == 7500  # noqa: SLF001 — wiring proof


def test_build_credits_policy_community_is_uncapped() -> None:
    policy = build_credits_policy(APIConfig(edition=Edition.community))
    assert isinstance(policy, UnlimitedCreditsPolicy)


# --- edition-off no-op: community / uncapped is SQLite-safe ---------------------


def test_bounded_cap_zero_takes_no_lock() -> None:
    """slots<=0 (community/uncapped): admits WITHOUT executing any advisory-lock SQL
    — so the community SQLite backend is never handed a Postgres-only lock query."""
    conn = MagicMock()
    with acquire_user_concurrency(conn=conn, user_id="u", slots=0) as acquired:
        assert acquired is True
    conn.execute.assert_not_called()


def test_long_cap_zero_writes_no_row_and_never_touches_db() -> None:
    """max_concurrent<=0 (community/uncapped): admits a sentinel token WITHOUT opening
    a transaction or writing an inflight_ops row — SQLite-safe."""
    engine = MagicMock()
    token = admit_long_op(rls_engine=engine, user_id="u", op_class="chat", max_concurrent=0)
    assert token is not None  # admitted
    engine.begin.assert_not_called()
    # Releasing the unlimited sentinel (or None) is a no-op that never touches the DB.
    release_long_op(rls_engine=engine, user_id="u", op_id=token)
    release_long_op(rls_engine=engine, user_id="u", op_id=None)
    engine.begin.assert_not_called()

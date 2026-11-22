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


# --- R9-318: a slot names the instance and the boot that took it ----------------


def test_admit_long_op_stamps_the_instance_and_this_process_boot() -> None:
    """The boot sweep can only tell an earlier process's slot from a live one if every slot
    records who took it: the api instance and this process's boot id."""
    from persona.concurrency import PROCESS_BOOT_ID  # noqa: PLC0415

    engine = MagicMock()
    conn = engine.begin.return_value.__enter__.return_value
    conn.execute.return_value.scalar_one.return_value = 0
    token = admit_long_op(
        rls_engine=engine, user_id="u", op_class="chat", max_concurrent=3, instance_id="api-2"
    )
    assert token is not None
    inserts = [c for c in conn.execute.call_args_list if "INSERT INTO inflight_ops" in str(c[0][0])]
    assert len(inserts) == 1
    params = inserts[0][0][1]
    assert params["instance"] == "api-2"
    assert params["boot"] == PROCESS_BOOT_ID
    assert params["id"] == token


def test_instance_id_defaults_to_api(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PERSONA_API_INSTANCE_ID", raising=False)
    assert APIConfig().instance_id == "api"


def test_instance_id_parses_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSONA_API_INSTANCE_ID", "api-2")
    assert APIConfig().instance_id == "api-2"


@pytest.mark.parametrize("bad", ["", "has space", "x" * 65, "semi;colon"])
def test_instance_id_rejects_a_malformed_name(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    from pydantic import ValidationError  # noqa: PLC0415

    monkeypatch.setenv("PERSONA_API_INSTANCE_ID", bad)
    with pytest.raises(ValidationError):
        APIConfig()


@pytest.mark.asyncio
async def test_a_chat_turn_takes_its_slot_under_the_configured_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The route hands the app's instance id to the turn, and the turn to the admission."""
    import persona.concurrency as concurrency  # noqa: PLC0415
    from persona_api.errors import ConcurrencyCappedError  # noqa: PLC0415
    from persona_api.services import chat_service  # noqa: PLC0415

    seen: dict[str, object] = {}

    def _admit(**kwargs: object) -> None:
        seen.update(kwargs)

    monkeypatch.setattr(concurrency, "admit_long_op", _admit)
    registry = MagicMock()
    registry.get.return_value = None
    sink = MagicMock()
    sink.heal_orphaned_running.return_value = None
    with pytest.raises(ConcurrencyCappedError):
        await chat_service.start_chat_turn(
            rls_engine=MagicMock(),
            sink=sink,
            registry=registry,
            loop_builder=MagicMock(),
            owner_id="u",
            conversation_id="c",
            user_message="hi",
            max_concurrent_long_ops=3,
            long_op_instance="api-2",
        )
    assert seen["instance_id"] == "api-2"
    assert seen["op_class"] == "chat"

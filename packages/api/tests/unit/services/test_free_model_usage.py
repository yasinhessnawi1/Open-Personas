"""The free-model daily request meter (R9-179 item 3).

OpenRouter's free-model ceiling is per UTC day and ACCOUNT-WIDE, shared by every
free-plan user and every background job on a free chain. These tests pin the four
properties that make the meter worth having: it counts the right requests, it says so
once per threshold per day, it survives a restart mid-day, and the day rolls at UTC
midnight rather than at the server's local one.

The engine is a real (SQLite) database rather than a double, because "a restart
continues the count" is a claim about persistence, and a fake that holds the number in
memory would pass it while production reset to zero on every deploy.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import pytest
from loguru import logger
from persona_api.db.community import create_community_schema, make_community_engine
from persona_api.db.models import free_model_daily_usage as usage_t
from persona_api.services.free_model_usage import FreeModelDailyCounter
from sqlalchemy import select

if TYPE_CHECKING:
    from collections.abc import Iterator
    from datetime import date
    from pathlib import Path

    from sqlalchemy import Engine

_FREE_MODEL = "meta-llama/llama-3.3-70b-instruct:free"
_AUTO_ROUTER = "free"  # the ``openrouter/free`` slug, provider token split off
_PAID_MODEL = "anthropic/claude-3.5-sonnet"
_NOON = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "meter.db"
    engine = make_community_engine(path)
    create_community_schema(engine)
    engine.dispose()
    return path


@pytest.fixture
def engine(db_path: Path) -> Iterator[Engine]:
    eng = make_community_engine(db_path)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def captured() -> Iterator[list[str]]:
    """Every INFO-or-worse line the meter emits, as plain text."""
    messages: list[str] = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level="INFO")
    try:
        yield messages
    finally:
        logger.remove(sink_id)


def _counts(engine: Engine) -> dict[date, int]:
    with engine.begin() as conn:
        return {
            row.day: row.request_count
            for row in conn.execute(select(usage_t.c.day, usage_t.c.request_count))
        }


def _threshold_lines(captured: list[str], percent: int) -> list[str]:
    return [m for m in captured if f"at {percent}% of the account cap" in m]


def test_a_free_model_request_is_counted(engine: Engine) -> None:
    counter = FreeModelDailyCounter(engine, daily_cap=1000)
    counter.record_served(provider="openrouter", model=_FREE_MODEL, when=_NOON)
    assert _counts(engine) == {_NOON.date(): 1}


def test_the_free_only_auto_router_is_counted(engine: Engine) -> None:
    # ``openrouter/free`` has no ``:free`` suffix and IS free, the same slug the
    # free-mode tier filter now keeps (R9-179 item 1). Filter and meter agree.
    counter = FreeModelDailyCounter(engine, daily_cap=1000)
    counter.record_served(provider="openrouter", model=_AUTO_ROUTER, when=_NOON)
    assert _counts(engine) == {_NOON.date(): 1}


def test_a_paid_model_is_not_counted(engine: Engine) -> None:
    counter = FreeModelDailyCounter(engine, daily_cap=1000)
    counter.record_served(provider="openrouter", model=_PAID_MODEL, when=_NOON)
    counter.record_served(provider="nvidia", model="nvidia/nemotron", when=_NOON)
    assert _counts(engine) == {}


def test_each_threshold_announces_once_and_only_once(engine: Engine, captured: list[str]) -> None:
    # Cap 100 ⇒ boundaries at 25 / 50 / 75 / 90. Drive the whole day past all four.
    counter = FreeModelDailyCounter(engine, daily_cap=100)
    for _ in range(100):
        counter.record_served(provider="openrouter", model=_FREE_MODEL, when=_NOON)
    assert _counts(engine) == {_NOON.date(): 100}
    for percent in (25, 50, 75, 90):
        lines = _threshold_lines(captured, percent)
        assert len(lines) == 1, f"{percent}% announced {len(lines)} times: {lines}"
    assert "WARNING" in _threshold_lines(captured, 90)[0]
    assert "WARNING" not in _threshold_lines(captured, 75)[0]
    assert "count=90" in _threshold_lines(captured, 90)[0]
    assert "day=2026-09-18" in _threshold_lines(captured, 90)[0]


def test_a_restart_mid_day_continues_the_count(db_path: Path, captured: list[str]) -> None:
    first = make_community_engine(db_path)
    counter = FreeModelDailyCounter(first, daily_cap=100)
    for _ in range(25):
        counter.record_served(provider="openrouter", model=_FREE_MODEL, when=_NOON)
    assert len(_threshold_lines(captured, 25)) == 1
    first.dispose()  # the deploy

    second = make_community_engine(db_path)  # the process that comes back
    resumed = FreeModelDailyCounter(second, daily_cap=100)
    resumed.record_served(provider="openrouter", model=_FREE_MODEL, when=_NOON)
    assert _counts(second) == {_NOON.date(): 26}
    # Continuing, not restarting: the day's 25% mark is not announced a second time.
    assert len(_threshold_lines(captured, 25)) == 1
    second.dispose()


def test_the_day_rolls_at_utc_midnight(engine: Engine) -> None:
    counter = FreeModelDailyCounter(engine, daily_cap=1000)
    before = datetime(2026, 9, 18, 23, 59, tzinfo=UTC)
    after = before + timedelta(minutes=2)
    counter.record_served(provider="openrouter", model=_FREE_MODEL, when=before)
    counter.record_served(provider="openrouter", model=_FREE_MODEL, when=after)
    assert _counts(engine) == {before.date(): 1, after.date(): 1}


def test_a_local_zone_moment_lands_on_its_utc_day(engine: Engine) -> None:
    # 01:30 on the 19th in Oslo (UTC+2) is still the 18th account-wide.
    counter = FreeModelDailyCounter(engine, daily_cap=1000)
    oslo = datetime(2026, 9, 19, 1, 30, tzinfo=timezone(timedelta(hours=2)))
    counter.record_served(provider="openrouter", model=_FREE_MODEL, when=oslo)
    assert _counts(engine) == {datetime(2026, 9, 18, tzinfo=UTC).date(): 1}


def test_a_cap_of_zero_disables_the_meter(engine: Engine, captured: list[str]) -> None:
    counter = FreeModelDailyCounter(engine, daily_cap=0)
    assert not counter.enabled
    counter.record_served(provider="openrouter", model=_FREE_MODEL, when=_NOON)
    assert _counts(engine) == {}
    assert not captured


def test_the_meter_never_breaks_what_it_measures(captured: list[str]) -> None:
    broken: Any = object()  # no ``begin``; any DB failure has the same shape
    FreeModelDailyCounter(broken, daily_cap=1000).record_served(
        provider="openrouter", model=_FREE_MODEL, when=_NOON
    )
    assert any("free-model daily meter failed" in m for m in captured)


class TestTheMeterIsReachedInProduction:
    """The two api call sites that actually feed the meter (no-dark-code)."""

    def test_a_chat_turn_meters_its_served_model(self, engine: Engine) -> None:
        # The production path: ConversationLoop → TurnLogWriter.write. The row's
        # ``provider`` / ``model_name`` ARE the served pair (D-M2-2), so writing the
        # turn is what counts it, no second attribution, no separate call.
        from persona_api.db.community import ensure_owner
        from persona_api.db.models import conversations as conversations_t
        from persona_api.db.models import personas as personas_t
        from persona_api.services.turn_log_writer import PostgresTurnLogWriter
        from persona_runtime.logging import TurnLog
        from sqlalchemy import insert

        ensure_owner(engine, owner_id="user_alice", email="a@example.com")
        with engine.begin() as conn:
            conn.execute(
                insert(personas_t).values(id="astrid", owner_id="user_alice", yaml="name: A")
            )
            conn.execute(
                insert(conversations_t).values(
                    id="conv_1", owner_id="user_alice", persona_id="astrid"
                )
            )

        writer = PostgresTurnLogWriter(
            engine, free_model_counter=FreeModelDailyCounter(engine, daily_cap=1000)
        )
        writer.write(
            TurnLog(
                conversation_id="conv_1",
                turn_index=0,
                tier_used="frontier",
                model_name=_FREE_MODEL,
                provider="openrouter",
                prompt_tokens=10,
                completion_tokens=20,
                latency_ms=1.0,
                cost_cents=0.0,
                timestamp=_NOON,
            )
        )
        assert _counts(engine) == {_NOON.date(): 1}

    def test_a_background_op_meters_its_served_model(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The production path for background surfaces: bill_background_llm holds the
        # served pair for consolidation / voice auto-pick / the initiative scan.
        from persona_api.services import background_billing

        monkeypatch.setenv("PERSONA_OPENROUTER_FREE_DAILY_CAP", "1000")

        class _Policy:
            def capture_up_to_idempotent(self, **_kw: object) -> tuple[int, int]:
                return 1, 0

        background_billing.bill_background_llm(
            credits_policy=_Policy(),  # type: ignore[arg-type]
            rls_engine=engine,
            owner_id="user_alice",
            provider="openrouter",
            model=_FREE_MODEL,
            prompt_tokens=100,
            completion_tokens=100,
            cost_usd=0.0,
            surface="graph_consolidation",
            billing_key="graph_consolidation:1",
        )
        assert sum(_counts(engine).values()) == 1

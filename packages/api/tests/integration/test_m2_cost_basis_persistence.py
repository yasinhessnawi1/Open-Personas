"""M2-T4 — ``turn_logs.cost_basis`` persistence parity (D-M2-4).

Against the real Postgres schema (:5436, the shared integration DB):

* :class:`PostgresTurnLogWriter` persists the runtime ``TurnLog.cost_basis``
  and it reads back through ``credits.list_turn_usage`` (the ``/v1/me/usage``
  service path — a whole-table select, so the column flows with no service
  change),
* a legacy row inserted WITHOUT the column (pre-M2 shape) reads back ``NULL``
  and the :class:`UsageEntry` projection renders it ``cost_basis=None`` +
  ``pricing_source="unified"`` (the additive route shape).

Harness mirrors ``test_day_spent_provider.py`` (raw-SQL parent seeding on the
shared ``pg_engine`` fixture; ON CONFLICT DO NOTHING).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import pytest
from persona.credits import list_turn_usage
from persona_api.schemas.responses import UsageEntry
from persona_api.services.turn_log_writer import PostgresTurnLogWriter
from persona_runtime.logging import TurnLog
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_OWNER = "u_m2_basis"
_PERSONA = "p_m2_basis"
_CONV = "c_m2_basis"


def _seed_parents(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
            {"i": _OWNER, "e": f"{_OWNER}@x.test"},
        )
        conn.execute(
            text(
                "INSERT INTO personas (id, owner_id, yaml) VALUES (:i, :o, 'y') "
                "ON CONFLICT DO NOTHING"
            ),
            {"i": _PERSONA, "o": _OWNER},
        )
        conn.execute(
            text(
                "INSERT INTO conversations (id, owner_id, persona_id) "
                "VALUES (:c, :o, :p) ON CONFLICT DO NOTHING"
            ),
            {"c": _CONV, "o": _OWNER, "p": _PERSONA},
        )


def _turn_log(*, turn_index: int, cost_basis: str, cost_cents: float) -> TurnLog:
    return TurnLog(
        conversation_id=_CONV,
        turn_index=turn_index,
        tier_used="frontier",
        model_name="z-ai/glm-4.6",
        provider="openrouter",
        prompt_tokens=10,
        completion_tokens=5,
        latency_ms=12.0,
        cost_cents=cost_cents,
        cost_basis=cost_basis,
        timestamp=datetime.now(UTC),
    )


def _rows_for_conv(engine: Engine) -> list[dict[str, object]]:
    return [
        r
        for r in list_turn_usage(rls_engine=engine, limit=200, offset=0)
        if r.get("conversation_id") == _CONV
    ]


@pytest.fixture
def seeded(pg_engine: Engine) -> Engine:
    _seed_parents(pg_engine)
    with pg_engine.begin() as conn:  # test-scoped rows only; conv is ours
        conn.execute(text("DELETE FROM turn_logs WHERE conversation_id = :c"), {"c": _CONV})
    return pg_engine


def test_writer_persists_basis_and_it_reads_back(seeded: Engine) -> None:
    writer = PostgresTurnLogWriter(seeded)
    writer.write(_turn_log(turn_index=0, cost_basis="actual_openrouter", cost_cents=0.042))
    writer.write(_turn_log(turn_index=1, cost_basis="estimate_static", cost_cents=1.05))

    rows = sorted(_rows_for_conv(seeded), key=lambda r: cast("int", r["turn_index"]))
    assert [r["cost_basis"] for r in rows] == ["actual_openrouter", "estimate_static"]
    assert [r["cost_cents"] for r in rows] == [pytest.approx(0.042), pytest.approx(1.05)]


def test_legacy_row_without_basis_reads_null_and_renders_estimate(seeded: Engine) -> None:
    # A pre-M2 row: inserted WITHOUT cost_basis (the legacy column set).
    with seeded.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO turn_logs (conversation_id, turn_index, tier_used, model_name, "
                "provider, prompt_tokens, completion_tokens, latency_ms, cost_cents, "
                "tool_calls, history_compacted) VALUES "
                "(:c, 0, 'frontier', 'claude-sonnet-4-6', 'anthropic', 10, 5, 1.0, 1.05, "
                "0, FALSE)"
            ),
            {"c": _CONV},
        )

    rows = _rows_for_conv(seeded)
    assert len(rows) == 1
    assert rows[0]["cost_basis"] is None  # legacy NULL — never invented

    # The /v1/me/usage projection: additive fields, list shape unchanged.
    entry = UsageEntry(
        persona_id=cast("str | None", rows[0].get("persona_id")),
        tier_used=str(rows[0]["tier_used"]),
        model_name=str(rows[0]["model_name"]),
        prompt_tokens=int(cast("int", rows[0]["prompt_tokens"])),
        completion_tokens=int(cast("int", rows[0]["completion_tokens"])),
        cost_cents=float(cast("float", rows[0]["cost_cents"])),
        cost_basis=cast("str | None", rows[0].get("cost_basis")),
        created_at=cast("datetime", rows[0]["created_at"]),
    )
    assert entry.cost_basis is None  # web rule: NULL renders as an estimate
    assert entry.pricing_source == "unified"


def test_new_vocabulary_round_trips_through_the_usage_service(seeded: Engine) -> None:
    writer = PostgresTurnLogWriter(seeded)
    for i, basis in enumerate(
        ("actual_openrouter", "estimate_static", "estimate_catalog", "unpriced")
    ):
        writer.write(_turn_log(turn_index=i, cost_basis=basis, cost_cents=float(i)))

    rows = sorted(_rows_for_conv(seeded), key=lambda r: cast("int", r["turn_index"]))
    assert [r["cost_basis"] for r in rows] == [
        "actual_openrouter",
        "estimate_static",
        "estimate_catalog",
        "unpriced",
    ]

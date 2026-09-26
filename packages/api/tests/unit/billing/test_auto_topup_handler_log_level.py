"""The auto top-up handler logs each outcome at the level an operator needs (R9-215).

Voice enqueues one job per charged turn on every cloud call (D-M5-16), so the handler's
line runs once per turn. Owner ruling 2026-09-26: a single INFO line when the api actually
charges. That line is ``maybe_auto_topup``'s "auto-top-up invoiced", so the handler logs
``charged`` at DEBUG, and ``not_crossed`` (the per-turn no-op) at DEBUG too. Every other
outcome needs a person or explains a missing top-up, and stays INFO.

Driven through the REAL :class:`AutoTopupHandler` with ``maybe_auto_topup`` replaced by a
stand-in returning each outcome, and a loguru sink scoped to the handler's component.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from loguru import logger as _loguru_logger
from persona.jobs import AutoTopupPayload
from persona_api.billing.autotopup import AutoTopupOutcome
from persona_api.jobs.handlers import auto_topup as handler_module
from persona_api.jobs.handlers.auto_topup import AutoTopupHandler

if TYPE_CHECKING:
    from collections.abc import Iterator

_COMPONENT = "api.jobs.auto_topup"

#: Every outcome and the level its evaluated line is logged at. Asserted complete below.
_EXPECTED_LEVELS: dict[AutoTopupOutcome, str] = {
    AutoTopupOutcome.NOT_CROSSED: "DEBUG",
    AutoTopupOutcome.CHARGED: "DEBUG",
    AutoTopupOutcome.NOT_ELIGIBLE: "INFO",
    AutoTopupOutcome.DISABLED: "INFO",
    AutoTopupOutcome.NO_CUSTOMER: "INFO",
    AutoTopupOutcome.REQUIRES_ACTION: "INFO",
    AutoTopupOutcome.ERROR: "INFO",
}


class _Context:
    owner_id = "u1"
    job_id = "job-1"


@pytest.fixture
def handler_records() -> Iterator[list[tuple[str, str]]]:
    """Every record the handler logs at any level, as ``(level name, message)``."""
    captured: list[tuple[str, str]] = []
    sink_id = _loguru_logger.add(
        lambda message: captured.append((message.record["level"].name, message.record["message"])),
        level=0,
        filter=lambda record: record["extra"].get("component") == _COMPONENT,
    )
    yield captured
    _loguru_logger.remove(sink_id)


def test_the_level_table_covers_every_outcome() -> None:
    assert set(_EXPECTED_LEVELS) == set(AutoTopupOutcome)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "level"),
    [pytest.param(outcome, level, id=str(outcome)) for outcome, level in _EXPECTED_LEVELS.items()],
)
async def test_each_outcome_is_logged_once_at_its_level(
    monkeypatch: pytest.MonkeyPatch,
    handler_records: list[tuple[str, str]],
    outcome: AutoTopupOutcome,
    level: str,
) -> None:
    seen: list[dict[str, object]] = []

    def _decide(**kwargs: object) -> AutoTopupOutcome:
        seen.append(kwargs)
        return outcome

    monkeypatch.setattr(handler_module, "maybe_auto_topup", _decide)
    handler = AutoTopupHandler(rls_engine=object(), gateway=object())  # type: ignore[arg-type]
    payload = AutoTopupPayload(
        old_balance=450, new_balance=430, source="voice", call_id="call1", turn_seq=3
    )

    await handler.handle(payload, _Context())  # type: ignore[arg-type]

    assert len(seen) == 1
    assert seen[0]["user_id"] == "u1"
    assert handler_records == [
        (
            level,
            f"auto-top-up trigger from voice evaluated: {outcome} (call=call1 turn=3)",
        )
    ]

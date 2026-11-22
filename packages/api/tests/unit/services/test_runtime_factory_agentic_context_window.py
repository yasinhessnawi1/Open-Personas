"""The composed agentic loop gets the shared metadata chain and the budget knob (R9-314).

The runtime suite proves the loop compacts against whatever window its metadata source
reports. What it cannot see is that the api hands the loop the RIGHT source: the
factory's shared chain, the one the turn pricer reads (static tables plus the OpenRouter
catalog when a key is configured). Without that wiring a hosted run would fall back to
the static tables and miss every catalog-only model, so this builds the loop through
``RuntimeFactory.build_agentic_loop`` and checks what it holds.

The budget is the owner's cost knob, ``PERSONA_API_AGENTIC_COMPACTION_BUDGET_TOKENS``:
its default must reproduce today's 4,096 so a default deploy pays what it paid before.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from persona_api.config import APIConfig
from persona_api.services.runtime_factory import RuntimeFactory
from pydantic import ValidationError
from test_runtime_factory_context_cost_ceiling import _loop_from  # type: ignore[import-not-found]

if TYPE_CHECKING:
    from persona_runtime.agentic.loop import AgenticLoop


def _source(loop: AgenticLoop) -> object:
    return loop._metadata_source  # noqa: SLF001 (the wiring IS the assertion)


@pytest.mark.asyncio
async def test_the_agentic_loop_reads_windows_from_the_factory_metadata_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[RuntimeFactory] = []
    original = RuntimeFactory.build_agentic_loop

    async def _capture(self: RuntimeFactory, *args: object, **kwargs: object) -> AgenticLoop:
        seen.append(self)
        return await original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(RuntimeFactory, "build_agentic_loop", _capture)
    loop = await _loop_from(None, monkeypatch)

    (factory,) = seen
    assert _source(loop) is factory.metadata_resolver


def _budget(loop: AgenticLoop) -> int:
    return loop._compaction_budget_tokens  # noqa: SLF001 (the wiring IS the assertion)


@pytest.mark.asyncio
async def test_without_a_config_the_loop_gets_todays_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default is cost-neutral: the 4,096 every run compacted against before."""
    loop = await _loop_from(None, monkeypatch)

    assert _budget(loop) == 4096


@pytest.mark.asyncio
async def test_the_configured_budget_reaches_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSONA_API_AGENTIC_COMPACTION_BUDGET_TOKENS", "50000")
    loop = await _loop_from(APIConfig(), monkeypatch)

    assert _budget(loop) == 50_000


def test_the_default_setting_is_todays_budget() -> None:
    assert APIConfig().agentic_compaction_budget_tokens == 4096


@pytest.mark.parametrize("value", ["0", "-1"])
def test_a_budget_that_is_not_positive_is_refused_at_startup(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("PERSONA_API_AGENTIC_COMPACTION_BUDGET_TOKENS", value)
    with pytest.raises(ValidationError):
        APIConfig()

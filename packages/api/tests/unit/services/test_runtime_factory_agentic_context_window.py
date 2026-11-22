"""The composed agentic loop sizes compaction from the shared metadata chain (R9-314).

The runtime suite proves the loop compacts against whatever window its metadata source
reports. What it cannot see is that the api hands the loop the RIGHT source: the
factory's shared chain, the one the turn pricer reads (static tables plus the OpenRouter
catalog when a key is configured). Without that wiring a hosted run would fall back to
the static tables and miss every catalog-only model, so this builds the loop through
``RuntimeFactory.build_agentic_loop`` and checks what it holds.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from persona_api.services.runtime_factory import RuntimeFactory
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

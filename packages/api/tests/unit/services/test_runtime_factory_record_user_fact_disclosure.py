"""RuntimeFactory hands ``record_user_fact`` its interaction and the fact store (R9-253).

Built-but-inert is the failure class: the runtime tool reads the bound interaction and
records the disclosure fact only if the composition root gives it both. No database: the
factory builds the toolbox with a ``None`` engine and a stand-in graph store, and the tool
factory is wrapped to capture what it is handed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from persona.schema.persona import Persona, PersonaIdentity
from persona_api.initiative.disclosures import PgDisclosureStore
from persona_api.services.runtime_factory import RuntimeFactory
from persona_runtime.extraction import direct_write
from persona_runtime.interaction_scope import current_interaction


def _factory() -> RuntimeFactory:
    factory = RuntimeFactory(
        rls_engine=None,  # type: ignore[arg-type]
        embedder=None,  # type: ignore[arg-type]
        tier_registry=None,  # type: ignore[arg-type]
        turn_log_writer=None,  # type: ignore[arg-type]
        audit_root=Path("/tmp/persona-r9-253-audit"),
        sandbox_pool=None,
        workspace_root=None,
        image_backend=None,
    )
    factory._graph_store = object()  # type: ignore[assignment]  # noqa: SLF001
    factory._record_user_fact_enabled = True  # noqa: SLF001
    return factory


@pytest.mark.asyncio
async def test_the_tool_gets_the_bound_interaction_and_the_fact_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handed: list[dict[str, Any]] = []
    real = direct_write.make_record_user_fact_tool

    def capture(**kwargs: Any) -> Any:  # noqa: ANN401
        handed.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(direct_write, "make_record_user_fact_tool", capture)
    factory = _factory()
    persona = Persona(
        persona_id="persona_r9253_wire",
        identity=PersonaIdentity(name="Astrid", role="assistant", background="A test helper."),
        tools=["file_read"],
    )

    toolbox = await factory._build_toolbox(persona, scanned_skills=[])  # type: ignore[attr-defined]  # noqa: SLF001

    assert "record_user_fact" in toolbox.names()
    (kwargs,) = handed
    assert kwargs["interaction_provider"] is current_interaction
    recorder = kwargs["disclosures"]
    assert isinstance(recorder, PgDisclosureStore)
    assert recorder._engine is factory._engine  # noqa: SLF001 - the owner-scoped engine
